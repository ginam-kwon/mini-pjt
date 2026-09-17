# agent.py - 공식 진입점: 메인 에이전트 그래프 + FastAPI (POST /query, POST /approve/{id})
#
# 공식 API 계약(§4-2): POST /query 는 자연어 question 하나만 받는 유일한 진단 라우트이며, 지금도
# SQL/실행계획을 사용자가 직접 주는 입력 경로는 없다(db_tool로만 조회). 다만 seed v2.4.0부터는
# 별도 라우트 POST /validate로 사용자가 SELECT/WITH SQL 원문을 직접 입력해 검증받을 수 있다
# (실행계획 원문은 이 경로에서도 여전히 사용자 입력이 아니라 SQLcl MCP로 새로 조회한다).
# 보호된 흐름(SQL 생성/검증/후보 탐색, 피드백)과 승인 작업은 X-Approver-Token이 필요하다.
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from pydantic import BaseModel

from src.actions import CHECKPOINT_DB, build_action_graph
from src.auth import verify_approver_token, write_audit
from src.pipeline import (
    run_business_requirement,
    run_candidate_diagnose,
    run_candidate_search,
    run_feedback,
    run_query,
    run_sql_validation,
)
from src.validator import review_sql

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Oracle SQL 실행계획 성능진단 Agent")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    """데모 UI (static/index.html) — /query, /actions/apply, /approve 를 시각적으로 시연한다."""
    return FileResponse(STATIC_DIR / "index.html")


class QueryRequest(BaseModel):
    question: str


class GenerateRequest(BaseModel):
    requirement: str


class ValidateRequest(BaseModel):
    sql: str


class CandidateSearchRequest(BaseModel):
    question: str
    session_id: str = ""


class CandidateDiagnoseRequest(BaseModel):
    sql_id: str
    session_id: str = ""


class FeedbackRequest(BaseModel):
    feedback_type: str
    content: str
    session_id: str = ""
    sql: str = ""


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
async def query(req: QueryRequest):
    """공식 계약: {"question": str} -> {"answer": str, "contexts": [...], "trace": [...]}"""
    return await run_query(question=req.question)


@app.post("/generate")
async def generate(req: GenerateRequest, token=Depends(verify_approver_token)):
    """비즈니스 요구사항에서 SELECT SQL을 생성하고 검증한다 (승인책임자 전용)."""
    result = await run_business_requirement(requirement=req.requirement)
    write_audit(user_hash=token, action="generate", result=result.get("status", ""))
    return result


@app.post("/validate")
async def validate_sql(req: ValidateRequest, token=Depends(verify_approver_token)):
    """사용자가 입력한 SQL을 사전 검증하고 accept/revise/reject 결과를 반환한다 (승인책임자 전용)."""
    result = await run_sql_validation(sql=req.sql)
    write_audit(user_hash=token, action="validate", result=result.get("status", ""))
    return result


@app.post("/candidates")
async def search_candidates(req: CandidateSearchRequest, token=Depends(verify_approver_token)):
    """자연어 운영 성능 요청에서 마스킹된 SQL 후보 목록을 반환한다 (승인책임자 전용)."""
    result = await run_candidate_search(question=req.question, session_id=req.session_id or None)
    write_audit(user_hash=token, action="candidate_search", result=result.get("status", ""))
    return result


@app.post("/candidates/diagnose")
async def diagnose_candidate(req: CandidateDiagnoseRequest, token=Depends(verify_approver_token)):
    """사용자가 선택한 후보 SQL의 실행계획을 진단한다 (승인책임자 전용)."""
    result = await run_candidate_diagnose(sql_id=req.sql_id, session_id=req.session_id or None)
    write_audit(user_hash=token, action="candidate_diagnose", result=result.get("status", ""))
    return result


@app.post("/feedback")
async def feedback(req: FeedbackRequest, token=Depends(verify_approver_token)):
    """이전 검증/진단 결과에 대한 사용자 피드백을 memory.sqlite에 영속 저장한다 (승인책임자 전용)."""
    result = await run_feedback(
        feedback_type=req.feedback_type,
        content=req.content,
        session_id=req.session_id or None,
        sql=req.sql or None,
    )
    write_audit(user_hash=token, action="feedback", result=result.get("status", ""))
    return result


@app.post("/actions/apply")
def apply_action(req: ActionRequest, token: str = Depends(verify_approver_token)):
    """[보호됨] 진단 결과에 나온 개선안을 실제로 적용하고 싶을 때만 거치는 별도 경로 — 항상 HITL 승인 게이트를 지난다."""
    approval_id = str(uuid.uuid4())
    write_audit(user_hash=token, action="actions_apply", result="initiated", extra={"tool": req.tool, "approval_id": approval_id})
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
def approve(approval_id: str, req: ApproveRequest, token: str = Depends(verify_approver_token)):
    """[보호됨] 대기 중인 승인 작업을 처리한다."""
    with SqliteSaver.from_conn_string(CHECKPOINT_DB) as checkpointer:
        graph = build_action_graph(checkpointer)
        config = {"configurable": {"thread_id": approval_id}}
        resume_value = {"decision": req.decision, "args": req.args, "reason": req.reason}
        result = graph.invoke(Command(resume=resume_value), config=config)
    write_audit(user_hash=token, action="approve", result=req.decision, extra={"approval_id": approval_id})
    return {"status": "done", "result": result["result"]}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
