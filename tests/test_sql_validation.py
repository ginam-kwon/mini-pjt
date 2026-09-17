"""SQL 사전 검증 AC 검증

AC: 단일 SELECT 또는 WITH SQL 입력은 accept, revise, reject 중 하나와 근거를 반환한다.
    accept 결과에는 query_name, reviewed_at, review_id가 들어간 SQL 주석이 포함된다.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# QueryReview 스키마 단위 테스트
# ---------------------------------------------------------------------------

class TestQueryReviewSchema:
    """QueryReview 스키마가 올바른 필드를 가진다."""

    def test_schema_importable(self):
        from src.schemas import QueryReview
        assert QueryReview is not None

    def test_accept_fields(self):
        from src.schemas import QueryReview
        r = QueryReview(
            result="accept",
            reason="문제 없음",
            query_name="select_orders",
            reviewed_at="2026-09-18T00:00:00Z",
            review_id="abc123",
            annotated_sql="/* q=x */ SELECT 1 FROM dual",
        )
        assert r.result == "accept"
        assert r.query_name == "select_orders"
        assert r.reviewed_at == "2026-09-18T00:00:00Z"
        assert r.review_id == "abc123"
        assert "/* " in r.annotated_sql

    def test_reject_minimal(self):
        from src.schemas import QueryReview
        r = QueryReview(result="reject", reason="변경 SQL")
        assert r.result == "reject"
        assert r.annotated_sql == ""

    def test_revise_minimal(self):
        from src.schemas import QueryReview
        r = QueryReview(result="revise", reason="풀스캔 위험")
        assert r.result == "revise"


# ---------------------------------------------------------------------------
# review_sql 코드 기반 차단 (LLM 호출 없음)
# ---------------------------------------------------------------------------

class TestReviewSqlGuardrailsNollm:
    """UPDATE/DELETE/빈 입력은 LLM 없이 즉시 reject를 반환한다."""

    def test_update_rejected_without_llm(self):
        from src.validator import review_sql
        mock_llm = MagicMock()
        result = review_sql("UPDATE orders SET status='done'", llm=mock_llm)
        assert result.result == "reject"
        mock_llm.with_structured_output.assert_not_called()

    def test_delete_rejected_without_llm(self):
        from src.validator import review_sql
        mock_llm = MagicMock()
        result = review_sql("DELETE FROM orders WHERE id=1", llm=mock_llm)
        assert result.result == "reject"
        mock_llm.with_structured_output.assert_not_called()

    def test_empty_sql_rejected_without_llm(self):
        from src.validator import review_sql
        mock_llm = MagicMock()
        result = review_sql("", llm=mock_llm)
        assert result.result == "reject"
        mock_llm.with_structured_output.assert_not_called()

    def test_semicolon_multiple_stmts_rejected(self):
        from src.validator import review_sql
        mock_llm = MagicMock()
        result = review_sql("SELECT 1 FROM dual; DROP TABLE orders", llm=mock_llm)
        assert result.result == "reject"
        mock_llm.with_structured_output.assert_not_called()


# ---------------------------------------------------------------------------
# review_sql — accept 판정: 승인 주석 포함 여부
# ---------------------------------------------------------------------------

class TestReviewSqlAcceptAnnotation:
    """accept 판정 시 annotated_sql에 query_name/reviewed_at/review_id 주석이 포함된다."""

    def _make_accept_llm(self):
        """LLM이 accept를 반환하는 mock."""
        from src.schemas import QueryReview
        mock_chain = MagicMock()
        mock_chain.invoke.return_value = QueryReview(result="accept", reason="문제 없음")
        mock_llm = MagicMock()
        mock_llm.with_structured_output.return_value = mock_chain
        return mock_llm

    def test_accept_has_query_name(self):
        from src.validator import review_sql
        r = review_sql("SELECT * FROM orders WHERE order_id = 1", llm=self._make_accept_llm())
        assert r.result == "accept"
        assert r.query_name != ""

    def test_accept_has_reviewed_at_iso(self):
        from src.validator import review_sql
        r = review_sql("SELECT * FROM orders WHERE order_id = 1", llm=self._make_accept_llm())
        assert r.reviewed_at != ""
        # ISO-8601 형식 (YYYY-MM-DDTHH:MM:SSZ)
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", r.reviewed_at)

    def test_accept_has_review_id(self):
        from src.validator import review_sql
        r = review_sql("SELECT * FROM orders WHERE order_id = 1", llm=self._make_accept_llm())
        assert r.review_id != ""

    def test_accept_annotated_sql_has_comment(self):
        from src.validator import review_sql
        sql = "SELECT * FROM orders WHERE order_id = 1"
        r = review_sql(sql, llm=self._make_accept_llm())
        assert r.annotated_sql != ""
        assert "query_name=" in r.annotated_sql
        assert "reviewed_at=" in r.annotated_sql
        assert "review_id=" in r.annotated_sql

    def test_accept_annotated_sql_preserves_structure_but_masks_literal(self):
        # annotated_sql은 API/UI로 나가므로 리터럴을 마스킹한 사본이어야 한다(AC: 민감정보 마스킹).
        # 구조(테이블/컬럼)는 그대로 읽히지만 리터럴 값(1)은 :param_N으로 치환된다.
        from src.validator import review_sql
        sql = "SELECT * FROM orders WHERE order_id = 1"
        r = review_sql(sql, llm=self._make_accept_llm())
        assert "FROM orders WHERE order_id" in r.annotated_sql
        assert "= 1" not in r.annotated_sql
        assert ":param_1" in r.annotated_sql

    def test_accept_query_name_inferred_from_table(self):
        from src.validator import review_sql
        sql = "SELECT order_id FROM orders"
        r = review_sql(sql, llm=self._make_accept_llm())
        assert "orders" in r.query_name

    def test_accept_with_clause(self):
        from src.validator import review_sql
        sql = "WITH cte AS (SELECT id FROM orders) SELECT * FROM cte"
        r = review_sql(sql, llm=self._make_accept_llm())
        assert r.result == "accept"
        assert r.annotated_sql != ""


# ---------------------------------------------------------------------------
# review_sql — revise/reject 판정
# ---------------------------------------------------------------------------

class TestReviewSqlRejectRevise:
    """revise/reject 판정 시 annotated_sql이 비어 있고 reason이 채워진다."""

    def _make_llm(self, verdict: str, reason: str = "이유"):
        from src.schemas import QueryReview
        mock_chain = MagicMock()
        mock_chain.invoke.return_value = QueryReview(result=verdict, reason=reason)
        mock_llm = MagicMock()
        mock_llm.with_structured_output.return_value = mock_chain
        return mock_llm

    def test_reject_no_annotated_sql(self):
        from src.validator import review_sql
        r = review_sql("SELECT 1 FROM dual", llm=self._make_llm("reject", "거부 이유"))
        assert r.result == "reject"
        assert r.annotated_sql == ""

    def test_revise_no_annotated_sql(self):
        from src.validator import review_sql
        r = review_sql("SELECT * FROM orders", llm=self._make_llm("revise", "풀스캔"))
        assert r.result == "revise"
        assert r.annotated_sql == ""

    def test_reject_reason_populated(self):
        from src.validator import review_sql
        r = review_sql("SELECT 1 FROM dual", llm=self._make_llm("reject", "거부 이유"))
        assert r.reason != ""

    def test_revise_reason_populated(self):
        from src.validator import review_sql
        r = review_sql("SELECT * FROM orders", llm=self._make_llm("revise", "풀스캔 위험"))
        assert "풀스캔" in r.reason


# ---------------------------------------------------------------------------
# _infer_query_name 단위 테스트
# ---------------------------------------------------------------------------

class TestInferQueryName:
    def test_extracts_table_name(self):
        from src.validator import _infer_query_name
        assert _infer_query_name("SELECT * FROM orders WHERE id=1") == "select_orders"

    def test_lowercases_table(self):
        from src.validator import _infer_query_name
        assert _infer_query_name("SELECT id FROM CUSTOMERS") == "select_customers"

    def test_fallback_when_no_from(self):
        from src.validator import _infer_query_name
        assert _infer_query_name("SELECT 1") == "select_query"

    def test_with_clause_picks_cte_source(self):
        from src.validator import _infer_query_name
        # WITH cte ... FROM orders — FROM orders가 먼저 나타남
        name = _infer_query_name("WITH cte AS (SELECT id FROM orders) SELECT * FROM cte")
        assert "orders" in name or "cte" in name


# ---------------------------------------------------------------------------
# _build_annotated_sql 단위 테스트
# ---------------------------------------------------------------------------

class TestBuildAnnotatedSql:
    def test_comment_block_format(self):
        from src.schemas import QueryReview
        from src.validator import _build_annotated_sql
        r = QueryReview(
            result="accept",
            reason="ok",
            query_name="select_orders",
            reviewed_at="2026-09-18T00:00:00Z",
            review_id="deadbeef1234",
        )
        result = _build_annotated_sql("SELECT 1 FROM dual", r)
        assert result.startswith("/*")
        assert "query_name=select_orders" in result
        assert "reviewed_at=2026-09-18T00:00:00Z" in result
        assert "review_id=deadbeef1234" in result
        # SQL 본문은 마스킹된 사본이다 — 구조(FROM dual)는 남고 리터럴(1)은 :param_N으로 치환된다.
        assert "FROM dual" in result
        assert ":param_1" in result
