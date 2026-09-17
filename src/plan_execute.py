# plan_execute.py - Plan-Execute 그래프 + 장기 메모리(day7 패턴)
#
# SQL 지문(fingerprint)별로 과거 진단 결과를 InMemoryStore에 저장해, 같은(또는 정규화상 동일한)
# 쿼리를 반복 진단할 때 전체 파이프라인을 다시 돌리지 않고 재사용한다 — "반복 업무 자동화"라는
# 미니프로젝트의 목적과 직접 맞닿아 있는 부분이다.
from __future__ import annotations

import hashlib
import json
import operator
import re
from functools import lru_cache
from typing import Annotated, TypedDict

from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.store.memory import InMemoryStore

from src.agents import build_supervisor
from src.common import default_llm, last_nonempty_text
from src.schemas import Plan, Replan, SqlPlanAnalysis

MEMORY_NAMESPACE = ("sql_diagnoses",)


def sql_fingerprint(sql: str) -> str:
    normalized = re.sub(r"\s+", " ", sql.strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


class DiagnosisState(TypedDict):
    sql: str
    execution_plan: str
    question: str
    plan: list[str]
    past_steps: Annotated[list[tuple[str, str]], operator.add]
    from_memory: bool
    analysis: dict | None


@lru_cache(maxsize=1)
def _supervisor():
    return build_supervisor()


def planner_node(state: DiagnosisState, *, store) -> dict:
    fp = sql_fingerprint(state["sql"])
    cached = store.get(MEMORY_NAMESPACE, fp)
    if cached:
        return {"plan": [], "from_memory": True}

    result = default_llm().with_structured_output(Plan).invoke(
        "다음 Oracle SQL 성능 진단 요청을 처리하기 위한 2~4단계 계획을 세워라. "
        "각 단계는 'explain_agent에게 실행계획 문제 짚어내게 하기', "
        "'knowledge_agent에게 관련 튜닝 패턴 찾게 하기'처럼 구체적인 위임 작업이어야 한다.\n\n"
        f"SQL:\n{state['sql']}\n\n실행계획:\n{state['execution_plan']}\n\n"
        f"추가 질문: {state.get('question', '(없음)')}"
    )
    return {"plan": result.steps, "from_memory": False}


def execute_node(state: DiagnosisState, *, store) -> dict:
    if state.get("from_memory"):
        fp = sql_fingerprint(state["sql"])
        cached = store.get(MEMORY_NAMESPACE, fp)
        cached_json = json.dumps(cached.value, ensure_ascii=False)
        return {"plan": [], "past_steps": [("장기 메모리에서 과거 진단 결과 재사용", cached_json)]}

    step = state["plan"][0]
    msg = (
        f"SQL:\n{state['sql']}\n\n실행계획:\n{state['execution_plan']}\n\n"
        f"지금까지 결과: {state['past_steps']}\n\n지금 수행할 단계: {step}"
    )
    result = _supervisor().invoke({"messages": [HumanMessage(content=msg)]})
    output = last_nonempty_text(result["messages"])
    return {"plan": state["plan"][1:], "past_steps": [(step, output)]}


def replan_node(state: DiagnosisState) -> dict:
    if not state["plan"]:
        return {"plan": []}
    result = default_llm().with_structured_output(Replan).invoke(
        "지금까지 수행한 단계와 결과를 보고, 남은 계획을 계속 수행할지 아니면 이미 원인과 "
        "개선안을 판단하기 충분한 정보가 모였으니 종료할지 정하라.\n\n"
        f"완료된 단계: {state['past_steps']}\n\n남은 계획: {state['plan']}"
    )
    return {"plan": result.steps}


def finalize_node(state: DiagnosisState, *, store) -> dict:
    if state.get("from_memory"):
        fp = sql_fingerprint(state["sql"])
        return {"analysis": store.get(MEMORY_NAMESPACE, fp).value}

    context = "\n\n".join(f"[{step}]\n{output}" for step, output in state["past_steps"])
    analysis: SqlPlanAnalysis = default_llm().with_structured_output(SqlPlanAnalysis).invoke(
        "아래는 Oracle SQL 실행계획 진단 과정에서 나온 중간 결과들이다. 이를 종합해 최종 진단을 "
        "구조화된 형식으로 작성하라. 근거 없는 원인/개선안은 포함하지 마라. summary와 evidence에는 "
        "실행계획에 나온 연산자명(예: HASH JOIN, TABLE ACCESS FULL, TempSpc 등)과 수치를 의역하지 "
        "말고 원문 그대로 인용해 근거를 명확히 하라.\n\n"
        f"SQL:\n{state['sql']}\n\n실행계획:\n{state['execution_plan']}\n\n중간 결과:\n{context}"
    )
    data = analysis.model_dump()
    fp = sql_fingerprint(state["sql"])
    store.put(MEMORY_NAMESPACE, fp, data)
    return {"analysis": data}


def route_after_replan(state: DiagnosisState) -> str:
    return "execute" if state["plan"] else "finalize"


def build_diagnosis_graph():
    builder = StateGraph(DiagnosisState)
    builder.add_node("planner", planner_node)
    builder.add_node("execute", execute_node)
    builder.add_node("replan", replan_node)
    builder.add_node("finalize", finalize_node)

    builder.add_edge(START, "planner")
    builder.add_edge("planner", "execute")
    builder.add_edge("execute", "replan")
    builder.add_conditional_edges("replan", route_after_replan, {"execute": "execute", "finalize": "finalize"})
    builder.add_edge("finalize", END)

    return builder.compile(store=InMemoryStore())


if __name__ == "__main__":
    graph = build_diagnosis_graph()
    sql = open("data/samples/sample_query.sql", encoding="utf-8").read()
    plan_text = open("data/samples/sample_plan.txt", encoding="utf-8").read()

    print("=== 1차 진단 (메모리 없음) ===")
    r1 = graph.invoke({"sql": sql, "execution_plan": plan_text, "question": "", "past_steps": []})
    print(json.dumps(r1["analysis"], ensure_ascii=False, indent=2))

    print("\n=== 2차 진단 (동일 SQL, 메모리 재사용 기대) ===")
    r2 = graph.invoke({"sql": sql, "execution_plan": plan_text, "question": "", "past_steps": []})
    print("from_memory 경로였는지:", r2.get("analysis") == r1.get("analysis"))
