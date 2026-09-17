# agents.py - 서브 에이전트(explain/knowledge/query_planner/sql_validator/candidate_search/general) + Supervisor 조립
from __future__ import annotations

from functools import lru_cache

from langchain.agents import create_agent
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
)

EXPLAIN_SYSTEM_PROMPT = _explain_mod.SYSTEM_PROMPT
KNOWLEDGE_SYSTEM_PROMPT = _knowledge_mod.SYSTEM_PROMPT
QUERY_PLANNER_SYSTEM_PROMPT = _query_planner_mod.SYSTEM_PROMPT
SQL_VALIDATOR_SYSTEM_PROMPT = _sql_validator_mod.SYSTEM_PROMPT
CANDIDATE_SEARCH_SYSTEM_PROMPT = _candidate_search_mod.SYSTEM_PROMPT
GENERAL_SYSTEM_PROMPT = _general_mod.SYSTEM_PROMPT

SUPERVISOR_PROMPT = """너는 Oracle SQL Copilot 서비스의 supervisor다.
사용자 요청을 분석해 반드시 아래 담당 에이전트 중 하나에게만 위임해라.
supervisor는 사용자에게 직접 답하지 않는다 — 반드시 에이전트에게 위임해라.

담당 에이전트:
- query_planner_agent: 비즈니스 요구사항에서 SELECT SQL을 설계하는 요청
- sql_validator_agent: 사용자가 직접 입력한 SQL을 검증·실행계획 분석하는 요청
- candidate_search_agent: 자연어로 운영 중인 시스템의 SQL 후보를 탐색하는 요청
- explain_agent: SQL 실행계획의 연산자·비용·조건 문제를 분석하는 요청
- knowledge_agent: Oracle SQL 튜닝 지식·개선 패턴을 조회하는 요청
- general_agent: 위 다섯 범주에 해당하지 않는 모든 요청(범위 밖 질문, 인사, 잡담 등)

규칙:
- UPDATE, DELETE, INSERT, DROP, TRUNCATE, ALTER가 포함된 SQL은 어떤 에이전트로도 보내지 말고
  즉시 거부 메시지를 sql_validator_agent에 전달해 처리하게 해라.
- 분류가 불명확하면 general_agent로 라우팅해라.
- supervisor 자신이 직접 사용자 질문에 답하는 것은 금지다. 항상 에이전트를 통해 답해야 한다."""

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
        tools=_oracle_tools(),
        system_prompt=QUERY_PLANNER_SYSTEM_PROMPT,
        middleware=[MaskingMiddleware(), LoggingMiddleware()],
        name="query_planner_agent",
    )


def build_sql_validator_agent():
    return create_agent(
        model=default_llm(),
        tools=_oracle_tools(),
        system_prompt=SQL_VALIDATOR_SYSTEM_PROMPT,
        middleware=[MaskingMiddleware(), LoggingMiddleware()],
        name="sql_validator_agent",
    )


def build_candidate_search_agent():
    return create_agent(
        model=default_llm(),
        tools=_oracle_tools(),
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
        prompt=SUPERVISOR_PROMPT,
    )
    return supervisor.compile(name="sql_perf_supervisor")


if __name__ == "__main__":
    from langchain_core.messages import HumanMessage

    from src.common import last_nonempty_text

    app = build_supervisor()
    sql = open("data/samples/sample_query.sql", encoding="utf-8").read()
    plan = open("data/samples/sample_plan.txt", encoding="utf-8").read()
    result = app.invoke({"messages": [HumanMessage(
        f"다음 SQL과 실행계획을 분석해줘.\n\n[SQL]\n{sql}\n\n[실행계획]\n{plan}"
    )]})
    print(last_nonempty_text(result["messages"]))
