# validator.py - SELECT/WITH SQL 사전 검증: accept / revise / reject 판정 + 승인 주석 생성
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from src.common import default_llm
from src.guardrails import block_mutating_sql, mask_sql_for_external
from src.schemas import QueryReview
from src.tools import is_safe_select, looks_like_sql

_REVIEW_PROMPT = """너는 Oracle SELECT 쿼리 사전 검증 전문가다. 아래 SQL을 안전성과 성능 관점에서
검토하고 결과를 JSON으로 반환한다.

판정 기준:
- accept: SELECT/WITH 단일 문장이며 명백한 성능 위험(TABLE ACCESS FULL + 고비용 조인, 함수 기반
  조건으로 인덱스 무효화 등)이 없거나 경미해 그대로 사용해도 된다.
- revise: 성능 위험이 있지만 수정하면 사용 가능하다. 구체적인 수정 방향을 reason에 포함한다.
- reject: 심각한 안전성 문제(변경 문장, 다중 세미콜론, 주입 패턴 등)가 있어 사용을 금지해야 한다.

규칙:
- UPDATE, DELETE, INSERT, DROP, TRUNCATE, ALTER 키워드가 있으면 반드시 reject한다.
- 실행계획 없이 SQL 텍스트만으로 판단한다 — 실행계획은 이 단계에서 조회하지 않는다.
- query_name, reviewed_at, review_id는 빈 문자열로 두어라 — 시스템이 채운다.
- annotated_sql도 빈 문자열로 두어라 — 시스템이 채운다.

SQL:
{sql}
"""


def _build_annotated_sql(sql: str, review: QueryReview) -> str:
    """accept 판정된 SQL 앞에 /* ... */ 블록 주석으로 메타데이터를 추가한다.
    반환값은 API/UI로 나가므로 리터럴·PII를 마스킹한 사본을 쓴다 — 구조(테이블/컬럼/조건)는
    그대로 읽히지만 실제 값은 남지 않는다."""
    comment = (
        f"/* query_name={review.query_name}"
        f" reviewed_at={review.reviewed_at}"
        f" review_id={review.review_id} */"
    )
    return f"{comment}\n{mask_sql_for_external(sql)}"


def review_sql(sql: str, llm=None) -> QueryReview:
    """단일 SELECT 또는 WITH SQL을 검증하고 QueryReview를 반환한다.

    - SELECT/WITH가 아닌 입력은 LLM 호출 없이 즉시 reject를 반환한다.
    - accept이면 query_name, reviewed_at, review_id, annotated_sql을 자동으로 채운다.
    """
    if not isinstance(sql, str) or not sql.strip():
        return QueryReview(result="reject", reason="빈 SQL은 검증할 수 없습니다.")

    # 변경 SQL 코드 기반 사전 차단 (SQLcl MCP / LLM 호출 전)
    blocked = block_mutating_sql(sql)
    if blocked is not None:
        return QueryReview(
            result="reject",
            reason=blocked["reason"],
        )

    # SELECT/WITH 단일 문장이 아니면 reject
    if looks_like_sql(sql) and not is_safe_select(sql):
        return QueryReview(
            result="reject",
            reason="SELECT 또는 WITH 단일 조회문만 검증할 수 있습니다. 세미콜론으로 구분된 다중 문장은 허용되지 않습니다.",
        )

    model = llm or default_llm()
    reviewer = model.with_structured_output(QueryReview)
    # LLM payload/trace(Langfuse 포함)로 나가기 전에 리터럴·PII를 마스킹한다 — 판정에 필요한
    # 구조(컬럼/조인/함수)는 그대로 남고 실제 값만 :param_N/[MASKED_*]로 치환된다.
    review: QueryReview = reviewer.invoke(_REVIEW_PROMPT.format(sql=mask_sql_for_external(sql)))

    if review.result == "accept":
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        safe_name = _infer_query_name(sql)
        rid = uuid.uuid4().hex[:12]
        review = review.model_copy(update={
            "query_name": safe_name,
            "reviewed_at": now_iso,
            "review_id": rid,
            "annotated_sql": _build_annotated_sql(sql, review.model_copy(update={
                "query_name": safe_name,
                "reviewed_at": now_iso,
                "review_id": rid,
            })),
        })

    return review


def _infer_query_name(sql: str) -> str:
    """SQL에서 첫 번째 테이블 이름을 추출해 스네이크케이스 쿼리 이름을 만든다."""
    import re
    # WITH cte AS ... 또는 FROM <table> 패턴에서 첫 이름 추출
    m = re.search(r"\bFROM\s+([A-Za-z_][A-Za-z0-9_]*)", sql, re.IGNORECASE)
    if m:
        return f"select_{m.group(1).lower()}"
    return "select_query"
