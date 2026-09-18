# agents.py - 서브 에이전트(explain/knowledge/query_planner/sql_validator/candidate_search/general) + Supervisor 조립
from __future__ import annotations

import contextvars
from functools import lru_cache

from langchain.agents import create_agent
from langchain_core.tools import tool
from langgraph_supervisor import create_supervisor

from src.common import default_llm
from src.middleware import LoggingMiddleware, MaskingMiddleware, OutputCheckMiddleware
from src.tools import get_oracle_mcp_tools
from src.prompts import (
    explain as _explain_mod,
    knowledge as _knowledge_mod,
    query_planner as _query_planner_mod,
    sql_validator as _sql_validator_mod,
    candidate_search as _candidate_search_mod,
    general as _general_mod,
    supervisor as _supervisor_mod,
)

EXPLAIN_SYSTEM_PROMPT = _explain_mod.SYSTEM_PROMPT
KNOWLEDGE_SYSTEM_PROMPT = _knowledge_mod.SYSTEM_PROMPT
QUERY_PLANNER_SYSTEM_PROMPT = _query_planner_mod.SYSTEM_PROMPT
SQL_VALIDATOR_SYSTEM_PROMPT = _sql_validator_mod.SYSTEM_PROMPT
CANDIDATE_SEARCH_SYSTEM_PROMPT = _candidate_search_mod.SYSTEM_PROMPT
GENERAL_SYSTEM_PROMPT = _general_mod.SYSTEM_PROMPT

# 에이전트 이름 상수 — 라우팅 검증에 사용
AGENT_NAMES = frozenset({
    "query_planner_agent",
    "sql_validator_agent",
    "candidate_search_agent",
    "explain_agent",
    "knowledge_agent",
    "general_agent",
})


@lru_cache(maxsize=1)
def _oracle_tools() -> tuple:
    """SQLcl MCP 서버가 있으면 그 도구들을 추가로 바인딩한다.

    explain/query_planner/sql_validator/candidate_search 4개 에이전트가 각자 이 함수를
    부르면 SQLcl(JVM) 서브프로세스가 매번 새로 뜬다 — 프로세스당 한 번만 조회해 공유한다.
    반환형이 list가 아니라 tuple인 것은 lru_cache 캐시 키/값을 안전하게 공유하기 위함이며,
    create_agent(tools=...)는 list든 tuple이든 그대로 받는다.

    build_supervisor()가 이제 run_query 같은 async 호출 체인 안(이미 실행 중인 이벤트 루프)에서
    지연 생성되므로, 여기서 asyncio.run()을 직접 쓰면 'cannot be called from a running event
    loop'로 깨진다(예외를 삼키던 이전 코드에서는 도구 목록이 조용히 빈 리스트가 되는 형태로
    드러났다). tools.py의 _run_async_in_new_thread와 같은 방식으로 별도 스레드에서 새 이벤트
    루프를 돌려 호출자의 이벤트 루프 유무와 무관하게 항상 동작하게 한다."""
    from src.tools import _run_async_in_new_thread

    try:
        mcp_tools = _run_async_in_new_thread(get_oracle_mcp_tools())
    except Exception:
        mcp_tools = []
    return tuple(mcp_tools or [])


# ------------------------------------------------------------------
# POST /query(토큰 있을 때 Supervisor 전체 라우팅) 지원 — Supervisor가 query_planner_agent/sql_validator_agent/
# candidate_search_agent 중 하나로 위임하면, 이 에이전트들은 범용 도구 호출 대신 전용 도구
# 하나만 호출해 pipeline.py의 기존 구현(Plan-Execute 그래프/검증기/후보검색)을 그대로 실행한다.
#
# 구조화된 결과(sql_draft/candidates/annotated_sql 등)를 호출부(pipeline.run_via_supervisor)로
# 돌려주는 방법을 두 번 실측으로 검증하며 골랐다:
#   1) ContextVar에 도구가 결과를 set() — 실패. LangGraph가 도구 호출을 별도 asyncio Task로
#      실행해서, 자식 Task가 쓴 값이 부모 Task로 역류하지 않는다(자식은 생성 시점 값을 읽을 수는
#      있어도 쓴 값을 부모에게 돌려줄 수 없다).
#   2) 도구 결과를 ToolMessage.content(JSON)에 담아 Supervisor 실행 후 메시지 목록에서 찾기 —
#      역시 실패. langgraph_supervisor는 하위 에이전트의 내부 도구 호출 기록을 상위 스레드에
#      노출하지 않고, 그 에이전트의 최종 요약 AIMessage 하나만 상위로 올려보낸다(직접
#      build_supervisor().ainvoke(...)로 메시지 목록을 덤프해 확인함).
#   3) (채택) 프로세스 전역 dict(_assist_results)에 도구가 결과를 담고, 요청마다 발급한 UUID를
#      ContextVar(읽기 전용 방향이라 안전하게 전파됨)로 각 Task에 전달해 그 키로 기록·회수한다.
#      dict는 Task마다 복사되는 ContextVar와 달리 같은 객체 참조를 공유하므로 자식이 쓴 값을
#      부모가 그대로 읽을 수 있다.
# ------------------------------------------------------------------
_assist_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_assist_session_id", default=None
)
_assist_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_assist_request_id", default=None
)
_assist_approver_token: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_assist_approver_token", default=None
)
_assist_results: dict[str, dict] = {}


def _protected_access_result() -> dict:
    """보호 Agent가 실제 Tool을 호출하기 전 권한을 확인한다."""
    from src.auth import check_approver_token

    status, user_hash = check_approver_token(_assist_approver_token.get())
    if status == "ok":
        return {"user_hash": user_hash}
    return {
        "status": "authorization_required" if status == "missing" else "authorization_forbidden",
        "reason": "X-Approver-Token 헤더가 필요합니다." if status == "missing" else "유효하지 않은 X-Approver-Token입니다.",
        "answer": "",
        "contexts": [],
        "trace": [],
    }


def _store_assist_result(result: dict) -> None:
    request_id = _assist_request_id.get()
    if request_id is not None:
        _assist_results[request_id] = result


@tool
async def generate_sql_draft(requirement: str) -> str:
    """비즈니스 요구사항의 처리 계획을 생성해 사람 승인을 요청한다.
    이 도구를 정확히 한 번 호출하고, 반환된 문자열을 그대로 최종 답변으로 전달해라."""
    from src.pipeline import prepare_business_requirement

    access = _protected_access_result()
    if "status" in access:
        _store_assist_result(access)
        return access["reason"]
    result = await prepare_business_requirement(requirement=requirement, session_id=_assist_session_id.get())
    result.update({"protected": True, "user_hash": access["user_hash"]})
    _store_assist_result(result)
    return result.get("answer") or "요청을 처리했습니다."


@tool
async def validate_user_sql(sql: str) -> str:
    """사용자가 입력한 단일 SELECT/WITH SQL을 accept/revise/reject로 검증한다.
    이 도구를 정확히 한 번 호출하고, 반환된 문자열을 그대로 최종 답변으로 전달해라."""
    from src.pipeline import run_sql_validation

    access = _protected_access_result()
    if "status" in access:
        _store_assist_result(access)
        return access["reason"]
    result = await run_sql_validation(sql=sql)
    result.update({"protected": True, "user_hash": access["user_hash"]})
    _store_assist_result(result)
    return result.get("answer") or "요청을 처리했습니다."


@tool
async def search_operational_candidates(question: str) -> str:
    """자연어 운영 성능 요청에서 마스킹된 SQL 후보 목록을 찾는다.
    이 도구를 정확히 한 번 호출하고, 반환된 문자열을 그대로 최종 답변으로 전달해라."""
    from src.pipeline import run_candidate_search

    access = _protected_access_result()
    if "status" in access:
        _store_assist_result(access)
        return access["reason"]
    result = await run_candidate_search(question=question, session_id=_assist_session_id.get())
    result.update({"protected": True, "user_hash": access["user_hash"]})
    _store_assist_result(result)
    return result.get("answer") or "요청을 처리했습니다."


def build_explain_agent():
    return create_agent(
        model=default_llm(),
        tools=_oracle_tools(),
        system_prompt=EXPLAIN_SYSTEM_PROMPT,
        middleware=[MaskingMiddleware(), LoggingMiddleware()],
        name="explain_agent",
    )


def build_knowledge_agent():
    from src.retriever import search_tuning_knowledge, warmup as warmup_rag
    warmup_rag()
    return create_agent(
        model=default_llm(),
        tools=[search_tuning_knowledge],  # noqa: F821 — imported above in same scope
        system_prompt=KNOWLEDGE_SYSTEM_PROMPT,
        middleware=[OutputCheckMiddleware(), LoggingMiddleware()],
        name="knowledge_agent",
    )


def build_query_planner_agent():
    return create_agent(
        model=default_llm(),
        tools=[generate_sql_draft],
        system_prompt=QUERY_PLANNER_SYSTEM_PROMPT,
        middleware=[MaskingMiddleware(), LoggingMiddleware()],
        name="query_planner_agent",
    )


def build_sql_validator_agent():
    return create_agent(
        model=default_llm(),
        tools=[validate_user_sql],
        system_prompt=SQL_VALIDATOR_SYSTEM_PROMPT,
        middleware=[MaskingMiddleware(), LoggingMiddleware()],
        name="sql_validator_agent",
    )


def build_candidate_search_agent():
    return create_agent(
        model=default_llm(),
        tools=[search_operational_candidates],
        system_prompt=CANDIDATE_SEARCH_SYSTEM_PROMPT,
        middleware=[MaskingMiddleware(), LoggingMiddleware()],
        name="candidate_search_agent",
    )


def build_general_agent():
    return create_agent(
        model=default_llm(),
        tools=[],
        system_prompt=GENERAL_SYSTEM_PROMPT,
        middleware=[OutputCheckMiddleware(), LoggingMiddleware()],
        name="general_agent",
    )


@lru_cache(maxsize=1)
def build_supervisor():
    """프로세스 전체에서 단일 인스턴스로 공유한다 — 여러 모듈이 각자 새 6-agent supervisor를
    만들면 MCP 연결·RAG 워밍업이 중복 실행된다."""
    explain_agent = build_explain_agent()
    knowledge_agent = build_knowledge_agent()
    query_planner_agent = build_query_planner_agent()
    sql_validator_agent = build_sql_validator_agent()
    candidate_search_agent = build_candidate_search_agent()
    general_agent = build_general_agent()
    supervisor = create_supervisor(
        [
            explain_agent,
            knowledge_agent,
            query_planner_agent,
            sql_validator_agent,
            candidate_search_agent,
            general_agent,
        ],
        model=default_llm(),
        prompt=_supervisor_mod.SYSTEM_PROMPT,
    )
    return supervisor.compile(name="sqlmanager_supervisor")
