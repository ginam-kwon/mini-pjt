# actions.py - 위험 작업(HITL) 실행 그래프 (day5 hitl_flow.py 패턴)
#
# 진단(plan_execute.py)은 항상 read-only 다. 사용자가 진단 결과에 나온 개선안을 실제로
# "적용"하고 싶을 때만 이 그래프를 거친다 — write/destructive 위험도구는 반드시
# interrupt() 로 사람 승인을 받은 뒤에만 실행된다.
from __future__ import annotations

from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from src.guardrails import needs_approval
from src.tools import create_index, gather_stats

TOOL_MAP = {t.name: t for t in [gather_stats, create_index]}

# 일반 요청 세션 저장소(src/storage.py)의 checkpoints 테이블과 LangGraph의 체크포인터 테이블은
# 스키마가 다르다. 같은 파일을 공유하면 `thread_id` 컬럼 충돌로 HITL 요청이 500이 된다.
CHECKPOINT_DB = "hitl_checkpoints.sqlite"


class ActionState(TypedDict):
    tool_name: str
    args: dict
    result: str


def gate_node(state: ActionState) -> dict:
    need, reason = needs_approval(state["tool_name"], state["args"])
    if not need:
        return {}

    decision = interrupt({
        "tool": state["tool_name"],
        "args": state["args"],
        "reason": reason,
        "question": "승인(approve), 수정(modify), 거절(reject) 중 선택하세요.",
    })
    choice = decision.get("decision", "reject")

    if choice == "reject":
        return {"result": f"[거절됨] 사유: {decision.get('reason', '없음')}"}
    if choice == "modify":
        return {"args": {**state["args"], **decision.get("args", {})}}
    return {}


def run_node(state: ActionState) -> dict:
    if state.get("result"):
        return {}
    tool = TOOL_MAP[state["tool_name"]]
    output = tool.invoke(state["args"])
    return {"result": output}


def build_action_graph(checkpointer):
    builder = StateGraph(ActionState)
    builder.add_node("gate", gate_node)
    builder.add_node("run", run_node)
    builder.add_edge(START, "gate")
    builder.add_edge("gate", "run")
    builder.add_edge("run", END)
    return builder.compile(checkpointer=checkpointer)


if __name__ == "__main__":
    with SqliteSaver.from_conn_string(CHECKPOINT_DB) as checkpointer:
        graph = build_action_graph(checkpointer)
        config = {"configurable": {"thread_id": "demo-create-index"}}

        graph.invoke(
            {"tool_name": "create_index", "args": {"table_name": "ORDERS", "index_ddl": "CREATE INDEX idx_orders_date ON orders(order_date)"}},
            config=config,
        )
        state = graph.get_state(config)
        print("승인 대기 중:", state.tasks[0].interrupts[0].value)

        result = graph.invoke(Command(resume={"decision": "approve"}), config=config)
        print("결과:", result["result"])
