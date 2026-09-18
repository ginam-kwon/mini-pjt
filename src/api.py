# api.py - FastAPI 진입점 (POST /query, POST /approve/{id})
#
# seed v2.6.0: /assist를 다시 /query로 흡수했다 — 두 개의 진입점을 유지할 이유가 없다는 지적을
# 받아, 자연어 question은 /query 하나로 처리한다. 기존 방식대로 {"question": str}만 보내고
# 토큰 없이도 자연어를 Supervisor로 보낸다. 보호 Agent가 실제 전용 Tool을 실행하는 순간
# X-Approver-Token을 검사하므로, 공개 지식 질문은 토큰 없이 처리되고 보호 흐름은 401/403으로
# 종료한다. src/pipeline.py의 run_via_supervisor 참고.
#
# seed v2.7.0: 후보 진단(sql_id)은 다시 /query에서 분리해 POST /candidates/diagnose 전용
# 라우트로 뺐다 — 후보 진단은 사용자가 목록에서 카드를 "클릭"해서 나온, 분류할 자연어가 전혀
# 없는 결정적 액션이라 Supervisor를 거칠 이유가 없다(오히려 불필요한 LLM 분류 호출과 오분류
# 위험만 생김). /query는 "자연어 question 입력" 전용, /candidates/diagnose는 "이미 확정된
# sql_id 선택" 전용으로 관심사를 분리한다.
from __future__ import annotations

import uuid
from pathlib import Path
from threading import Thread

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from pydantic import BaseModel, Field

from src.actions import CHECKPOINT_DB, build_action_graph
from src.auth import approver_token_scheme, verify_approver_token, write_audit
from src.pipeline import (
    MAX_QUESTION_LEN,
    MIN_QUESTION_LEN,
    resume_business_requirement,
    run_candidate_diagnose,
    run_feedback,
    run_query,
    run_via_supervisor,
)
from src.tools import is_safe_select, looks_like_sql

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(
    title="Oracle SQL 실행계획 성능진단 Agent",
    description=(
        "자연어 질문/SQL 원문을 받아 Oracle 실행계획을 진단하는 Multi-Agent API. "
        "보호된 작업은 `X-Approver-Token` 헤더가 필요하며, 아래 Authorize 버튼으로 설정할 수 있다."
    ),
    version="1.0.0",
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
def _warmup_on_startup() -> None:
    """대표 운영 쿼리를 실제로 실행해 V$SQL 공유 풀에 올려둔다(search_sql_candidates가 조회할
    대상). Oracle 미설정/SQLcl 없음/실패는 조용히 무시한다. MCP 연결이 지연되어도 API
    기동과 첫 요청을 막지 않도록 백그라운드에서 실행한다."""
    from src.tools import warmup_operational_queries

    Thread(target=warmup_operational_queries, name="oracle-sql-warmup", daemon=True).start()


@app.get("/")
def index():
    """데모 UI (static/index.html)를 제공한다."""
    return FileResponse(STATIC_DIR / "index.html")


class QueryRequest(BaseModel):
    """POST /query의 요청 바디.

    question: 자연어 요구사항/질문 또는 SELECT·WITH SQL 원문.
    session_id: 후보 탐색 세션 연결(체크포인트 저장용). 기존 {"question": str}만 보내는 호출은
    session_id가 빈 문자열이라 예전과 동일하게 동작한다 — 하위호환 확장.
    """

    question: str = Field(default="", description="자연어 요구사항/질문 또는 SELECT·WITH SQL 원문.", examples=["SELECT * FROM ORDERS WHERE STATUS = 'PENDING'"])
    session_id: str = Field(default="", description="후보 탐색 세션 연결(체크포인트 저장용). 생략 시 새 흐름으로 처리된다.")


class CandidateDiagnoseRequest(BaseModel):
    """POST /candidates/diagnose의 요청 바디 — 후보 탐색에서 선택한 sql_id를 진단한다."""

    sql_id: str = Field(description="후보 목록에서 선택한 V$SQL의 SQL_ID.", examples=["7xj0k3n9h2vqw"])
    session_id: str = Field(default="", description="후보 탐색 세션 연결(체크포인트 저장용).")


class PlanApprovalRequest(BaseModel):
    decision: str = Field(description="approve | modify | reject 중 하나.", examples=["approve"])
    requirement: str = Field(default="", description="decision=modify일 때 수정된 요구사항.")


class FeedbackRequest(BaseModel):
    feedback_type: str = Field(description="피드백 종류(예: validation, diagnosis).")
    content: str = Field(description="피드백 본문.")
    session_id: str = Field(default="", description="연결할 세션 ID.")
    sql: str = Field(default="", description="피드백 대상 SQL 원문(선택).")


class ActionRequest(BaseModel):
    tool: str = Field(description="실행할 위험 도구 이름(gather_stats | create_index).")
    args: dict = Field(default_factory=dict, description="도구 인자.")


class ApproveRequest(BaseModel):
    decision: str = Field(description="approve | modify | reject 중 하나.", examples=["approve"])
    args: dict = Field(default_factory=dict, description="decision=modify일 때 수정된 인자.")
    reason: str = Field(default="", description="decision=reject일 때 사유.")


@app.get("/health", tags=["system"], summary="헬스체크")
def health():
    return {"status": "ok"}


@app.get("/api/health", tags=["system"], summary="헬스체크(별칭)")
def health_alias():
    """일부 프록시/터널 환경이 '/health' 경로를 자체 헬스체크용으로 가로채는 경우를 위한 별칭.
    데모 UI(static/index.html)는 이 경로를 사용한다."""
    return {"status": "ok"}


@app.post("/query", tags=["query"], summary="자연어 질문 또는 SQL 원문 처리")
async def query(req: QueryRequest, x_approver_token: str | None = Depends(approver_token_scheme)):
    """단일 진입점.

    X-Approver-Token 헤더 자체가 없으면 기존 계약 그대로 legacy run_query로 처리한다(SQL
    원문이든 자연어든 동일 — 헤더가 없다는 것 자체가 "보호 흐름을 쓸 생각이 없다"는 신호이므로
    Supervisor/LLM 분류를 거치지 않는다). 헤더가 있으면(값이 맞든 틀리든) Supervisor 전체
    라우팅이 열리고, 보호 Agent가 자신의 Tool을 실행하려는 순간에만 401/403을 결정한다.

    UPDATE·DELETE·DROP 등 변경/DDL 성격의 SQL은 헤더 유무와 무관하게 항상 예외다 — 위험한
    명령을 막는 가드레일이 LLM의 확률적 분류 결과에 좌우되면 안 되므로, 코드로 즉시·결정적으로
    차단한다.
    """
    # 빈 입력은 분류할 의도가 없으므로 Supervisor/LLM을 호출하지 않는다. 기존 /query 계약의
    # 결정적 입력 검증 응답을 그대로 돌려주며, 브라우저가 로딩 상태에 머무르지 않게 한다.
    # 토큰 헤더가 아예 없는 요청도 기존 계약 그대로 legacy run_query로 처리한다.
    question = req.question.strip()
    if not question or x_approver_token is None:
        return await run_query(question=req.question)

    # 너무 짧거나(예: "x") 너무 긴 질문은 의도를 분류할 수 없다 — Supervisor/LLM 분류로 넘기면
    # 모델이 그럴싸한 답을 지어낼 위험이 있으므로, 토큰 유무와 무관하게 run_query의 결정적
    # no_answer 가드를 그대로 태운다(길이 상수는 src/pipeline.py가 SSOT).
    if len(question) < MIN_QUESTION_LEN or len(question) > MAX_QUESTION_LEN:
        return await run_query(question=req.question)

    # 변경/DDL SQL은 결정적 코드 가드레일로 즉시 차단한다(LLM 분류를 거치지 않는다).
    if looks_like_sql(req.question) and not is_safe_select(req.question):
        return await run_query(question=req.question)

    result = await run_via_supervisor(
        req.question,
        session_id=req.session_id or None,
        x_approver_token=x_approver_token,
    )
    if result.get("status") == "authorization_required":
        raise HTTPException(status_code=401, detail=result["reason"])
    if result.get("status") == "authorization_forbidden":
        raise HTTPException(status_code=403, detail=result["reason"])
    if result.get("protected"):
        write_audit(
            user_hash=result["user_hash"],
            action=result.get("mode") or "protected_agent",
            result=result.get("status", ""),
        )
    return result


@app.post("/candidates/diagnose", tags=["candidates"], summary="[보호됨] 선택한 후보 SQL 진단")
async def diagnose_candidate(req: CandidateDiagnoseRequest, token: str = Depends(verify_approver_token)):
    """[보호됨] 후보 탐색에서 사용자가 선택한 sql_id의 실행계획을 진단한다.

    분류할 자연어가 없는 결정적 액션이라 Supervisor를 거치지 않는다 — 이미 사용자가 목록에서
    정확히 이 후보를 클릭해 선택을 확정했으므로, LLM에게 다시 "무슨 의도냐"를 묻는 것은
    불필요한 지연·비용이자 잘못된 재해석의 위험만 만든다."""
    result = await run_candidate_diagnose(sql_id=req.sql_id, session_id=req.session_id or None)
    write_audit(user_hash=token, action="candidate_diagnose", result=result.get("status", ""))
    return result


@app.post("/plans/{session_id}/approve", tags=["plans"], summary="[보호됨] SQL 생성 계획 승인/수정/거절")
async def approve_query_plan(session_id: str, req: PlanApprovalRequest, token: str = Depends(verify_approver_token)):
    """보호된 SQL 생성 계획을 승인·수정·거절하고 다음 단계를 재개한다."""
    result = await resume_business_requirement(
        session_id=session_id,
        decision=req.decision,
        requirement=req.requirement or None,
    )
    write_audit(user_hash=token, action="query_plan_approval", result=result.get("status", ""))
    return result


@app.post("/feedback", tags=["feedback"], summary="[보호됨] 진단/검증 결과 피드백 저장")
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


@app.post("/actions/apply", tags=["actions"], summary="[보호됨] 위험 도구 실행 요청 (항상 HITL 승인 게이트)")
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


@app.post("/approve/{approval_id}", tags=["actions"], summary="[보호됨] 대기 중인 위험 도구 승인 처리")
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
