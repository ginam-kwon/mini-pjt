# agent.py - 공식 진입점: 메인 에이전트 그래프 + FastAPI (POST /query, POST /approve/{id})
#
# 공식 API 계약(§4-2): POST /query 는 자연어 question 하나만 받는 유일한 진단 라우트다.
# SQL/실행계획을 사용자가 직접 주는 입력 경로는 존재하지 않는다 (src/pipeline.py가 db_tool로 조회).
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from pydantic import BaseModel

from src.actions import CHECKPOINT_DB, build_action_graph
from src.pipeline import run_query

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Oracle SQL 실행계획 성능진단 Agent")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    """데모 UI (static/index.html) — /query, /actions/apply, /approve 를 시각적으로 시연한다."""
    return FileResponse(STATIC_DIR / "index.html")


class QueryRequest(BaseModel):
    question: str


class ActionRequest(BaseModel):
    tool: str
    args: dict = {}


class ApproveRequest(BaseModel):
    decision: str  # "approve" | "modify" | "reject"
    args: dict = {}
    reason: str = ""


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/health")
def health_alias():
    """일부 프록시/터널 환경이 '/health' 경로를 자체 헬스체크용으로 가로채는 경우를 위한 별칭.
    데모 UI(static/index.html)는 이 경로를 사용한다."""
    return {"status": "ok"}


@app.post("/query")
def query(req: QueryRequest):
    """공식 계약: {"question": str} -> {"answer": str, "contexts": [...], "trace": [...]}"""
    return run_query(question=req.question)


@app.post("/actions/apply")
def apply_action(req: ActionRequest):
    """진단 결과에 나온 개선안을 실제로 적용하고 싶을 때만 거치는 별도 경로 — 항상 HITL 승인 게이트를 지난다."""
    approval_id = str(uuid.uuid4())
    with SqliteSaver.from_conn_string(CHECKPOINT_DB) as checkpointer:
        graph = build_action_graph(checkpointer)
        config = {"configurable": {"thread_id": approval_id}}
        result = graph.invoke({"tool_name": req.tool, "args": req.args}, config=config)
        state = graph.get_state(config)
        if state.next:
            interrupt_value = state.tasks[0].interrupts[0].value
            return {"status": "awaiting_approval", "approval_id": approval_id, **interrupt_value}
        return {"status": "done", "result": result["result"]}


@app.post("/approve/{approval_id}")
def approve(approval_id: str, req: ApproveRequest):
    with SqliteSaver.from_conn_string(CHECKPOINT_DB) as checkpointer:
        graph = build_action_graph(checkpointer)
        config = {"configurable": {"thread_id": approval_id}}
        resume_value = {"decision": req.decision, "args": req.args, "reason": req.reason}
        result = graph.invoke(Command(resume=resume_value), config=config)
        return {"status": "done", "result": result["result"]}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
