"""src/prompts 패키지 — 역할별 system prompt 모듈.

각 역할은 독립 모듈에서 SYSTEM_PROMPT 상수를 정의한다.
ROLE_PROMPT_MAP은 역할명과 SYSTEM_PROMPT의 일대일 매핑을 제공한다.
"""
from src.prompts import (
    candidate_search,
    explain,
    general,
    knowledge,
    plan_risk,
    query_planner,
    router,
    sql_validator,
)

ROLE_PROMPT_MAP: dict[str, str] = {
    "router": router.SYSTEM_PROMPT,
    "query_planner": query_planner.SYSTEM_PROMPT,
    "sql_validator": sql_validator.SYSTEM_PROMPT,
    "candidate_search": candidate_search.SYSTEM_PROMPT,
    "plan_risk": plan_risk.SYSTEM_PROMPT,
    "explain": explain.SYSTEM_PROMPT,
    "knowledge": knowledge.SYSTEM_PROMPT,
    "general": general.SYSTEM_PROMPT,
}

REQUIRED_ROLES = frozenset(ROLE_PROMPT_MAP.keys())

__all__ = [
    "ROLE_PROMPT_MAP",
    "REQUIRED_ROLES",
    "router",
    "query_planner",
    "sql_validator",
    "candidate_search",
    "plan_risk",
    "explain",
    "knowledge",
    "general",
]
