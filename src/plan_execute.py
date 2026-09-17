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
from src.guardrails import mask_pii, mask_sql_for_external
from src.schemas import Plan, Replan, SqlPlanAnalysis

# SQL 텍스트는 리터럴+PII를 모두 마스킹(구조만 남김)하고, 실행계획 텍스트는 PII만 마스킹한다
# (Cost=/Rows= 같은 진단 수치는 리터럴 마스킹 대상이 아니라 보존해야 분석 품질이 유지된다).
_mask_sql = mask_sql_for_external
_mask_plan = mask_pii

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
        f"SQL:\n{_mask_sql(state['sql'])}\n\n"
        f"실행계획:\n{_mask_plan(state['execution_plan'])}\n\n"
        f"추가 질문: {state.get('question', '(없음)')}"
    )
    return {"plan": result.steps, "from_memory": False}


async def execute_node(state: DiagnosisState, *, store) -> dict:
    """explain_agent/knowledge_agent 등 supervisor 산하 에이전트는 SQLcl MCP 도구를 바인딩하고
    있는데, langchain-mcp-adapters로 만든 MCP 도구는 비동기(ainvoke)만 지원한다
    (StructuredTool.invoke() 호출 시 NotImplementedError). 그래서 supervisor는 반드시
    ainvoke()로 호출해야 하며, 이 노드도 async여야 한다 — 그래프 전체가 ainvoke()로
    실행되면 LangGraph가 동기 노드는 그대로, 이 노드처럼 async인 노드는 await해서 처리한다."""
    if state.get("from_memory"):
        fp = sql_fingerprint(state["sql"])
        cached = store.get(MEMORY_NAMESPACE, fp)
        cached_json = json.dumps(cached.value, ensure_ascii=False)
        return {"plan": [], "past_steps": [("장기 메모리에서 과거 진단 결과 재사용", cached_json)]}

    step = state["plan"][0]
    msg = (
        f"SQL:\n{_mask_sql(state['sql'])}\n\n실행계획:\n{_mask_plan(state['execution_plan'])}\n\n"
        f"지금까지 결과: {state['past_steps']}\n\n지금 수행할 단계: {step}"
    )
    result = await _supervisor().ainvoke({"messages": [HumanMessage(content=msg)]})
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
        f"SQL:\n{_mask_sql(state['sql'])}\n\n실행계획:\n{_mask_plan(state['execution_plan'])}\n\n중간 결과:\n{context}"
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


# ---------------------------------------------------------------------------
# 비즈니스 요구사항 → SELECT 초안 → 검증 → 실행계획 검토 5단계 그래프 (AC 7)
# ---------------------------------------------------------------------------
import src.tools as _tools  # SQLcl MCP 경계 — 모듈 레벨 import로 testability 유지
from src.prompts.query_planner import SYSTEM_PROMPT as QUERY_PLANNER_SYSTEM_PROMPT


class QueryPlannerState(TypedDict):
    """비즈니스 요구사항 처리 흐름 상태."""
    requirement: str
    steps_completed: Annotated[list[str], operator.add]
    requirement_plan: list[str]
    schema_info: str
    sql_draft: str
    validation: dict | None
    explain_plan: str
    risk_assessment: dict | None
    answer: str


# 5단계 순서 상수 — 테스트에서 순서 검증에 사용한다
QUERY_PLANNER_ORDERED_STEPS = [
    "plan_requirement",
    "lookup_schema",
    "draft_sql",
    "validate_sql",
    "review_plan",
]


def _plan_requirement_node(state: QueryPlannerState) -> dict:
    """1단계: 요구사항 계획 — 비즈니스 요구사항을 분석해 접근 계획을 수립한다.

    Plan-Execute 패턴의 Plan 단계다. query_planner 역할 프롬프트 모듈을 system prompt로 쓰고
    구조화 출력(Plan)으로 단계 목록을 받는다. LLM 호출이 실패해도 흐름이 끊기지 않도록
    고정 순서의 기본 계획으로 대체한다.
    """
    requirement = (state.get("requirement") or "").strip()
    fallback_plan = [
        "SQLcl MCP로 요구사항 관련 테이블·컬럼 스키마를 조회한다",
        "조회한 스키마로 SELECT 초안을 작성한다",
        "초안 SQL의 안전성과 요구사항 충족 여부를 검증한다",
        "검증을 통과한 SQL의 실행계획을 조회해 위험도를 검토한다",
    ]
    try:
        result = default_llm().with_structured_output(Plan).invoke(
            [
                ("system", QUERY_PLANNER_SYSTEM_PROMPT),
                (
                    "human",
                    "다음 비즈니스 요구사항을 SELECT 조회문으로 옮기기 위한 2~4단계 계획을 세워라. "
                    "각 단계는 스키마 조회, 초안 작성, 검증, 실행계획 검토 중 하나에 대응하는 "
                    "구체적인 작업이어야 한다.\n\n"
                    f"비즈니스 요구사항:\n{requirement}",
                ),
            ]
        )
        plan_steps = [str(step) for step in (result.steps or []) if str(step).strip()]
    except Exception:
        plan_steps = []
    return {
        "steps_completed": ["plan_requirement"],
        "requirement_plan": plan_steps or fallback_plan,
    }


def _lookup_schema_node(state: QueryPlannerState) -> dict:
    """2단계: SQLcl MCP 스키마 조회 — 관련 테이블·컬럼 정보를 SQLcl MCP로 조회한다."""
    schema_info = ""
    if _tools.sqlcl_available() and _tools._oracle_configured():
        try:
            async def _fetch_schema():
                from langchain_mcp_adapters.client import MultiServerMCPClient
                from langchain_mcp_adapters.tools import load_mcp_tools
                config = _tools._sqlcl_connected_config()
                server_name = next(iter(config))
                client = MultiServerMCPClient(config)
                async with client.session(server_name) as session:
                    tools_list = await load_mcp_tools(session)
                    if not tools_list:
                        return "[스키마 조회: 도구 없음]"
                    tools_map = {t.name: t for t in tools_list}
                    sql_tool = None
                    for name in ("run_statement", "sql", "execute_sql", "run_sql"):
                        if name in tools_map:
                            sql_tool = tools_map[name]
                            break
                    if sql_tool is None:
                        sql_tool = tools_list[0]
                    result = await sql_tool.ainvoke(
                        {"sql": "SELECT table_name FROM user_tables ORDER BY table_name"}
                    )
                    return str(result)
            schema_info = _tools._run_async_in_new_thread(_fetch_schema())
        except Exception as e:
            schema_info = f"[SQLcl MCP 스키마 조회 실패: {type(e).__name__}] mock 스키마로 진행"
    else:
        schema_info = "[SQLcl MCP 미연결] 스키마 조회 불가 — mock 스키마로 진행"
    return {"steps_completed": ["lookup_schema"], "schema_info": schema_info}


def _draft_sql_node(state: QueryPlannerState) -> dict:
    """3단계: SELECT 초안 생성 — 스키마 정보를 바탕으로 SELECT 조회문 초안을 작성한다."""
    prompt = (
        f"비즈니스 요구사항: {state['requirement']}\n\n"
        f"스키마 정보(SQLcl MCP 조회 결과): {state.get('schema_info', '불명')}\n\n"
        "위 정보를 바탕으로 Oracle SELECT 단일 조회문을 작성해라. "
        "WITH절도 허용된다. SELECT 조회문 텍스트만 반환하고 설명은 포함하지 않는다. "
        "UPDATE, DELETE, INSERT, DROP, TRUNCATE 키워드는 절대 포함하지 않는다."
    )
    result = default_llm().invoke(prompt)
    from src.common import get_text
    sql_draft = get_text(result)
    return {"steps_completed": ["draft_sql"], "sql_draft": sql_draft}


def _validate_sql_node(state: QueryPlannerState) -> dict:
    """4단계: SQL 검증 — SELECT 초안이 안전성·성능 기준을 통과하는지 검증한다."""
    from src.validator import review_sql
    review = review_sql(state.get("sql_draft", ""))
    return {
        "steps_completed": ["validate_sql"],
        "validation": review.model_dump(),
    }


def _review_plan_node(state: QueryPlannerState) -> dict:
    """5단계: 실행계획 검토 — 검증된 SQL의 예상 실행계획을 SQLcl MCP로 조회해 위험도를 평가한다."""
    from src.plan_risk import get_user_sql_explain_plan, assess_risk
    sql = state.get("sql_draft", "")
    validation = state.get("validation") or {}

    if validation.get("result") == "reject":
        return {
            "steps_completed": ["review_plan"],
            "explain_plan": "",
            "risk_assessment": {"risk_level": "HIGH", "reason": "SQL 검증 실패", "allow_execution": False},
            "answer": f"SQL 검증 실패 — 재작성 필요: {validation.get('reason', '')}",
        }

    explain_plan = get_user_sql_explain_plan(sql)
    risk = assess_risk(explain_plan)

    answer_parts = [
        f"[생성 SQL]\n{sql}",
        f"\n[검증 결과]\n{validation.get('result', '')} — {validation.get('reason', '')}",
        f"\n[실행계획 위험도]\n{risk.risk_level} — {risk.reason}",
    ]
    if risk.recommendation:
        answer_parts.append(f"\n[권고사항]\n{risk.recommendation}")

    return {
        "steps_completed": ["review_plan"],
        "explain_plan": explain_plan,
        "risk_assessment": risk.model_dump(),
        "answer": "\n".join(answer_parts),
    }


def build_query_planner_graph():
    """비즈니스 요구사항을 5단계로 처리하는 Plan-Execute 그래프.

    순서: 요구사항 계획 → SQLcl MCP 스키마 조회 → SELECT 초안 생성 → SQL 검증 → 실행계획 검토
    """
    builder = StateGraph(QueryPlannerState)
    builder.add_node("plan_requirement", _plan_requirement_node)
    builder.add_node("lookup_schema", _lookup_schema_node)
    builder.add_node("draft_sql", _draft_sql_node)
    builder.add_node("validate_sql", _validate_sql_node)
    builder.add_node("review_plan", _review_plan_node)

    builder.add_edge(START, "plan_requirement")
    builder.add_edge("plan_requirement", "lookup_schema")
    builder.add_edge("lookup_schema", "draft_sql")
    builder.add_edge("draft_sql", "validate_sql")
    builder.add_edge("validate_sql", "review_plan")
    builder.add_edge("review_plan", END)

    return builder.compile()


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
