"""변경 SQL(UPDATE/DELETE) 차단 가드레일 검증

AC: UPDATE 또는 DELETE를 포함한 SQL 입력은 SQLcl MCP를 호출하지 않고
    status=blocked 및 SELECT 조회문만 입력하라는 이유를 반환한다.
"""
from __future__ import annotations
import asyncio

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# guardrails.contains_mutating_sql / block_mutating_sql 단위 테스트
# ---------------------------------------------------------------------------

class TestContainsMutatingSql:
    """contains_mutating_sql이 UPDATE·DELETE 키워드를 올바르게 감지한다."""

    def test_update_detected(self):
        from src.guardrails import contains_mutating_sql
        assert contains_mutating_sql("UPDATE orders SET status = 'done' WHERE id = 1")

    def test_delete_detected(self):
        from src.guardrails import contains_mutating_sql
        assert contains_mutating_sql("DELETE FROM orders WHERE id = 1")

    def test_update_lowercase_detected(self):
        from src.guardrails import contains_mutating_sql
        assert contains_mutating_sql("update orders set status = 'done'")

    def test_delete_embedded_in_with_detected(self):
        from src.guardrails import contains_mutating_sql
        sql = "WITH deleted AS (DELETE FROM orders WHERE id = 1 RETURNING id) SELECT * FROM deleted"
        assert contains_mutating_sql(sql)

    def test_update_embedded_in_subquery_detected(self):
        from src.guardrails import contains_mutating_sql
        sql = "SELECT * FROM (UPDATE orders SET x=1 WHERE id=1 RETURNING *)"
        assert contains_mutating_sql(sql)

    def test_select_not_flagged(self):
        from src.guardrails import contains_mutating_sql
        assert not contains_mutating_sql("SELECT * FROM orders WHERE status = 'PENDING'")

    def test_with_select_not_flagged(self):
        from src.guardrails import contains_mutating_sql
        sql = "WITH cte AS (SELECT id FROM orders) SELECT * FROM cte"
        assert not contains_mutating_sql(sql)

    def test_column_name_update_time_not_flagged(self):
        """update_time 같은 컬럼명은 word boundary로 구분되어 걸리지 않는다."""
        from src.guardrails import contains_mutating_sql
        assert not contains_mutating_sql("SELECT update_time, delete_flag FROM orders")

    def test_literal_string_update_flagged(self):
        """문자열 리터럴 'UPDATE'도 보수적으로 차단한다."""
        from src.guardrails import contains_mutating_sql
        assert contains_mutating_sql("SELECT * FROM t WHERE action = 'UPDATE'")


class TestBlockMutatingSql:
    """block_mutating_sql이 올바른 차단 응답 구조를 반환한다."""

    def test_update_returns_blocked_status(self):
        from src.guardrails import block_mutating_sql
        result = block_mutating_sql("UPDATE orders SET status = 'done'")
        assert result is not None
        assert result["status"] == "blocked"

    def test_delete_returns_blocked_status(self):
        from src.guardrails import block_mutating_sql
        result = block_mutating_sql("DELETE FROM orders WHERE id = 1")
        assert result is not None
        assert result["status"] == "blocked"

    def test_blocked_reason_mentions_select(self):
        """차단 이유에 SELECT 조회문만 허용한다는 안내가 포함된다."""
        from src.guardrails import block_mutating_sql
        result = block_mutating_sql("UPDATE orders SET x = 1")
        assert result is not None
        reason_lower = result["reason"].lower()
        assert "select" in reason_lower

    def test_blocked_response_has_required_fields(self):
        from src.guardrails import block_mutating_sql
        result = block_mutating_sql("DELETE FROM orders")
        assert result is not None
        assert "status" in result
        assert "reason" in result
        assert "answer" in result
        assert "contexts" in result
        assert "trace" in result

    def test_select_returns_none(self):
        from src.guardrails import block_mutating_sql
        assert block_mutating_sql("SELECT * FROM orders") is None

    def test_with_select_returns_none(self):
        from src.guardrails import block_mutating_sql
        assert block_mutating_sql("WITH cte AS (SELECT 1 FROM dual) SELECT * FROM cte") is None


# ---------------------------------------------------------------------------
# pipeline.run_query 통합 테스트: MCP 호출 없이 차단되는지 확인
# ---------------------------------------------------------------------------

class TestRunQueryBlocksMutatingSqlBeforeMcp:
    """run_query가 UPDATE/DELETE SQL을 MCP 호출 없이 차단한다."""

    def _patch_mcp(self):
        """MCP 및 DB 관련 함수를 모두 모의로 대체해 실제 호출을 감지한다."""
        from src import tools
        return (
            patch.object(tools, "_run_async_in_new_thread"),
            patch.object(tools, "sqlcl_available", return_value=True),
            patch.object(tools, "_oracle_configured", return_value=True),
        )

    def test_update_sql_blocked_status(self):
        mcp_patch, sqcl_patch, oracle_patch = self._patch_mcp()
        with mcp_patch as mock_mcp, sqcl_patch, oracle_patch:
            from src.pipeline import run_query
            result = asyncio.run(run_query("UPDATE orders SET status = 'done' WHERE id = 1"))
        assert result["status"] == "blocked"
        mock_mcp.assert_not_called()

    def test_delete_sql_blocked_status(self):
        mcp_patch, sqcl_patch, oracle_patch = self._patch_mcp()
        with mcp_patch as mock_mcp, sqcl_patch, oracle_patch:
            from src.pipeline import run_query
            result = asyncio.run(run_query("DELETE FROM orders WHERE id = 1"))
        assert result["status"] == "blocked"
        mock_mcp.assert_not_called()

    def test_update_embedded_in_with_blocked(self):
        sql = "WITH d AS (DELETE FROM orders WHERE id=1 RETURNING id) SELECT * FROM d"
        mcp_patch, sqcl_patch, oracle_patch = self._patch_mcp()
        with mcp_patch as mock_mcp, sqcl_patch, oracle_patch:
            from src.pipeline import run_query
            result = asyncio.run(run_query(sql))
        assert result["status"] == "blocked"
        mock_mcp.assert_not_called()

    def test_blocked_reason_instructs_select_only(self):
        mcp_patch, sqcl_patch, oracle_patch = self._patch_mcp()
        with mcp_patch, sqcl_patch, oracle_patch:
            from src.pipeline import run_query
            result = asyncio.run(run_query("UPDATE orders SET x = 1"))
        assert "select" in result["reason"].lower()

    def test_select_sql_not_blocked(self):
        """SELECT 단독 쿼리는 차단 응답을 반환하지 않는다(mcp 호출 시도는 무관)."""
        from src import tools
        with (
            patch.object(tools, "_oracle_configured", return_value=False),
            patch.object(tools, "sqlcl_available", return_value=False),
        ):
            from src.pipeline import run_query
            result = asyncio.run(run_query("SELECT * FROM orders WHERE status = 'PENDING'"))
        assert result["status"] != "blocked"
