"""실행계획 위험 게이트 AC 검증

AC: SQLcl MCP 실행계획 평가가 안전 기준을 통과한 SELECT만 제한 실행 및 실제 통계 수집을
    요청한다. 기준을 넘는 SELECT는 실행하지 않고 예상 실행계획 기반 진단을 반환한다.
"""
from __future__ import annotations
import asyncio

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# RiskAssessment 스키마 단위 테스트
# ---------------------------------------------------------------------------

class TestRiskAssessmentSchema:
    def test_schema_importable(self):
        from src.schemas import RiskAssessment
        assert RiskAssessment is not None

    def test_high_risk_fields(self):
        from src.schemas import RiskAssessment
        r = RiskAssessment(
            risk_level="HIGH",
            reason="Cost=50000 초과",
            allow_execution=False,
            recommendation="인덱스 최적화 필요",
        )
        assert r.risk_level == "HIGH"
        assert r.allow_execution is False
        assert r.reason != ""

    def test_low_risk_allows_execution(self):
        from src.schemas import RiskAssessment
        r = RiskAssessment(risk_level="LOW", reason="인덱스 사용", allow_execution=True)
        assert r.allow_execution is True
        assert r.recommendation == ""

    def test_medium_risk_allows_execution(self):
        from src.schemas import RiskAssessment
        r = RiskAssessment(
            risk_level="MEDIUM",
            reason="TABLE ACCESS FULL (small table)",
            allow_execution=True,
        )
        assert r.allow_execution is True

    def test_invalid_risk_level_raises(self):
        from src.schemas import RiskAssessment
        with pytest.raises(Exception):
            RiskAssessment(risk_level="UNKNOWN", reason="x", allow_execution=False)


# ---------------------------------------------------------------------------
# assess_risk 규칙 기반 단위 테스트 (LLM 없음)
# ---------------------------------------------------------------------------

class TestAssessRiskRuleBased:
    """assess_risk(plan_text, llm=None) — LLM 없이 규칙 기반으로 동작한다."""

    def test_high_cost_returns_high_risk(self):
        from src.plan_risk import assess_risk
        plan = "TABLE ACCESS FULL ORDERS Cost=50000 Rows=200000"
        result = assess_risk(plan)
        assert result.risk_level == "HIGH"
        assert result.allow_execution is False

    def test_high_rows_returns_high_risk(self):
        from src.plan_risk import assess_risk
        plan = "TABLE ACCESS FULL ORDERS Cost=100 Rows=2000000"
        result = assess_risk(plan)
        assert result.risk_level == "HIGH"
        assert result.allow_execution is False

    def test_cartesian_returns_high_risk(self):
        from src.plan_risk import assess_risk
        plan = "CARTESIAN JOIN ORDERS CUSTOMERS Cost=5000 Rows=1000"
        result = assess_risk(plan)
        assert result.risk_level == "HIGH"
        assert result.allow_execution is False

    def test_full_scan_medium_cost_returns_medium(self):
        from src.plan_risk import assess_risk
        plan = "TABLE ACCESS FULL ORDERS Cost=500 Rows=100"
        result = assess_risk(plan)
        assert result.risk_level == "MEDIUM"
        assert result.allow_execution is True

    def test_index_scan_returns_low(self):
        from src.plan_risk import assess_risk
        plan = "INDEX RANGE SCAN IDX_ORDERS_ID Cost=5 Rows=10"
        result = assess_risk(plan)
        assert result.risk_level == "LOW"
        assert result.allow_execution is True

    def test_no_cost_info_defaults_to_low(self):
        from src.plan_risk import assess_risk
        plan = "INDEX UNIQUE SCAN pk_orders"
        result = assess_risk(plan)
        assert result.allow_execution is True


# ---------------------------------------------------------------------------
# assess_risk LLM 기반 단위 테스트 (mock LLM)
# ---------------------------------------------------------------------------

class TestAssessRiskLlmBased:
    def _make_llm(self, risk_level: str, allow_execution: bool):
        from src.schemas import RiskAssessment
        mock_chain = MagicMock()
        mock_chain.invoke.return_value = RiskAssessment(
            risk_level=risk_level,
            reason="LLM 판정 근거",
            allow_execution=allow_execution,
        )
        mock_llm = MagicMock()
        mock_llm.with_structured_output.return_value = mock_chain
        return mock_llm

    def test_llm_high_risk_blocks_execution(self):
        from src.plan_risk import assess_risk
        llm = self._make_llm("HIGH", allow_execution=False)
        result = assess_risk("TABLE ACCESS FULL Cost=50000", llm=llm)
        assert result.risk_level == "HIGH"
        assert result.allow_execution is False
        llm.with_structured_output.assert_called_once()

    def test_llm_low_risk_allows_execution(self):
        from src.plan_risk import assess_risk
        llm = self._make_llm("LOW", allow_execution=True)
        result = assess_risk("INDEX RANGE SCAN Cost=5", llm=llm)
        assert result.allow_execution is True


# ---------------------------------------------------------------------------
# get_user_sql_explain_plan 단위 테스트
# ---------------------------------------------------------------------------

class TestGetUserSqlExplainPlan:
    def test_returns_string(self):
        from src.plan_risk import get_user_sql_explain_plan
        from src import tools

        with (
            patch.object(tools, "sqlcl_available", return_value=False),
        ):
            plan = get_user_sql_explain_plan("SELECT 1 FROM dual")
        assert isinstance(plan, str)
        assert len(plan) > 0

    def test_fallback_when_sqlcl_unavailable(self):
        from src.plan_risk import get_user_sql_explain_plan
        from src import tools

        with (
            patch.object(tools, "sqlcl_available", return_value=False),
        ):
            plan = get_user_sql_explain_plan("SELECT * FROM orders")
        assert "mock" in plan.lower() or "explain" in plan.lower() or len(plan) > 0

    def test_calls_mcp_when_oracle_available(self):
        from src.plan_risk import get_user_sql_explain_plan
        import src.tools as _tools

        mock_plan = "TABLE ACCESS FULL ORDERS Cost=8420 Rows=1"
        with (
            patch.object(_tools, "sqlcl_available", return_value=True),
            patch.object(_tools, "_oracle_configured", return_value=True),
            patch.object(
                _tools,
                "_run_async_in_new_thread",
                return_value=mock_plan,
            ) as mock_runner,
        ):
            plan = get_user_sql_explain_plan("SELECT * FROM orders")

        assert plan == mock_plan
        mock_runner.assert_called_once()

    def test_falls_back_on_mcp_failure(self):
        from src.plan_risk import get_user_sql_explain_plan
        from src import tools

        with (
            patch.object(tools, "sqlcl_available", return_value=True),
            patch.object(tools, "_oracle_configured", return_value=True),
            patch.object(
                tools,
                "_run_async_in_new_thread",
                side_effect=RuntimeError("MCP connection failed"),
            ),
        ):
            plan = get_user_sql_explain_plan("SELECT * FROM orders")
        # 폴백이 문자열을 반환해야 함
        assert isinstance(plan, str)
        assert len(plan) > 0


# ---------------------------------------------------------------------------
# 파이프라인 통합 테스트: HIGH 위험 → run_user_sql 호출 안 함
# ---------------------------------------------------------------------------

class TestPipelineRiskGate:
    """pipeline.run_query가 실행계획 위험도에 따라 제한 실행 여부를 제어하는지 검증한다."""

    def _mock_diagnosis(self):
        """diagnosis_graph 결과를 mock."""
        return {"analysis": {
            "summary": "진단 완료",
            "root_causes": [],
            "improvements": [],
        }}

    def test_high_risk_sql_skips_execution(self):
        """HIGH 위험 판정 시 run_user_sql을 호출하지 않고 explain_only 모드를 반환한다."""
        from src import pipeline, tools
        from src.schemas import RiskAssessment

        high_risk = RiskAssessment(
            risk_level="HIGH",
            reason="Cost=50000 초과",
            allow_execution=False,
        )

        with (
            patch.object(tools, "looks_like_sql", return_value=True),
            patch.object(tools, "is_safe_select", return_value=True),
            patch.object(pipeline, "block_mutating_sql", return_value=None),
            patch.object(pipeline, "get_user_sql_explain_plan",
                         return_value="TABLE ACCESS FULL Cost=50000"),
            patch.object(pipeline, "assess_risk", return_value=high_risk),
            patch.object(pipeline, "run_user_sql") as mock_run_sql,
            patch.object(pipeline, "_diagnosis_graph") as mock_graph_fn,
        ):
            mock_graph = MagicMock()
            mock_graph.ainvoke = AsyncMock(return_value=self._mock_diagnosis())
            mock_graph_fn.return_value = mock_graph

            result = asyncio.run(pipeline.run_query("SELECT * FROM orders WHERE 1=1"))

        mock_run_sql.assert_not_called()
        assert result.get("execution_mode") == "explain_only"
        assert result.get("risk_level") == "HIGH"

    def test_low_risk_sql_runs_actual_stats(self):
        """LOW 위험 판정 시 run_user_sql을 호출해 실측 통계를 수집한다."""
        from src import pipeline, tools
        from src.schemas import RiskAssessment

        low_risk = RiskAssessment(
            risk_level="LOW",
            reason="INDEX RANGE SCAN",
            allow_execution=True,
        )
        mock_lookup = {
            "key": "user_sql",
            "sql": "SELECT id FROM orders WHERE id = 1",
            "execution_plan": "INDEX RANGE SCAN pk_orders Cost=5",
        }

        with (
            patch.object(tools, "looks_like_sql", return_value=True),
            patch.object(tools, "is_safe_select", return_value=True),
            patch.object(pipeline, "block_mutating_sql", return_value=None),
            patch.object(pipeline, "get_user_sql_explain_plan",
                         return_value="INDEX RANGE SCAN Cost=5"),
            patch.object(pipeline, "assess_risk", return_value=low_risk),
            patch.object(pipeline, "run_user_sql", return_value=mock_lookup) as mock_run_sql,
            patch.object(pipeline, "_diagnosis_graph") as mock_graph_fn,
        ):
            mock_graph = MagicMock()
            mock_graph.ainvoke = AsyncMock(return_value=self._mock_diagnosis())
            mock_graph_fn.return_value = mock_graph

            result = asyncio.run(pipeline.run_query("SELECT id FROM orders WHERE id = 1"))

        mock_run_sql.assert_called_once()
        assert result.get("execution_mode") == "actual_stats"
        assert result.get("risk_level") == "LOW"

    def test_medium_risk_sql_runs_actual_stats(self):
        """MEDIUM 위험 판정 시도 run_user_sql을 호출한다."""
        from src import pipeline, tools
        from src.schemas import RiskAssessment

        medium_risk = RiskAssessment(
            risk_level="MEDIUM",
            reason="TABLE ACCESS FULL small table",
            allow_execution=True,
        )
        mock_lookup = {
            "key": "user_sql",
            "sql": "SELECT * FROM small_table",
            "execution_plan": "TABLE ACCESS FULL SMALL_TABLE Cost=50",
        }

        with (
            patch.object(tools, "looks_like_sql", return_value=True),
            patch.object(tools, "is_safe_select", return_value=True),
            patch.object(pipeline, "block_mutating_sql", return_value=None),
            patch.object(pipeline, "get_user_sql_explain_plan",
                         return_value="TABLE ACCESS FULL Cost=50"),
            patch.object(pipeline, "assess_risk", return_value=medium_risk),
            patch.object(pipeline, "run_user_sql", return_value=mock_lookup) as mock_run_sql,
            patch.object(pipeline, "_diagnosis_graph") as mock_graph_fn,
        ):
            mock_graph = MagicMock()
            mock_graph.ainvoke = AsyncMock(return_value=self._mock_diagnosis())
            mock_graph_fn.return_value = mock_graph

            result = asyncio.run(pipeline.run_query("SELECT * FROM small_table"))

        mock_run_sql.assert_called_once()
        assert result.get("execution_mode") == "actual_stats"
        assert result.get("risk_level") == "MEDIUM"

    def test_response_includes_risk_fields(self):
        """정상 응답에 execution_mode, risk_level, risk_reason 필드가 포함된다."""
        from src import pipeline, tools
        from src.schemas import RiskAssessment

        risk = RiskAssessment(
            risk_level="LOW",
            reason="인덱스 사용",
            allow_execution=True,
        )
        mock_lookup = {
            "key": "user_sql",
            "sql": "SELECT 1 FROM dual",
            "execution_plan": "INDEX Cost=1",
        }

        with (
            patch.object(tools, "looks_like_sql", return_value=True),
            patch.object(tools, "is_safe_select", return_value=True),
            patch.object(pipeline, "block_mutating_sql", return_value=None),
            patch.object(pipeline, "get_user_sql_explain_plan",
                         return_value="INDEX Cost=1"),
            patch.object(pipeline, "assess_risk", return_value=risk),
            patch.object(pipeline, "run_user_sql", return_value=mock_lookup),
            patch.object(pipeline, "_diagnosis_graph") as mock_graph_fn,
        ):
            mock_graph = MagicMock()
            mock_graph.ainvoke = AsyncMock(return_value=self._mock_diagnosis())
            mock_graph_fn.return_value = mock_graph

            result = asyncio.run(pipeline.run_query("SELECT 1 FROM dual"))

        assert "execution_mode" in result
        assert "risk_level" in result
        assert "risk_reason" in result

    def test_explain_plan_fetched_before_execution(self):
        """get_user_sql_explain_plan이 run_user_sql보다 먼저 호출된다."""
        from src import pipeline, tools
        from src.schemas import RiskAssessment

        call_order = []

        def track_explain(sql):
            call_order.append("explain")
            return "INDEX Cost=5"

        def track_run_sql(sql):
            call_order.append("run_sql")
            return {"key": "user_sql", "sql": sql, "execution_plan": "INDEX Cost=5"}

        risk = RiskAssessment(risk_level="LOW", reason="ok", allow_execution=True)

        with (
            patch.object(tools, "looks_like_sql", return_value=True),
            patch.object(tools, "is_safe_select", return_value=True),
            patch.object(pipeline, "block_mutating_sql", return_value=None),
            patch.object(pipeline, "get_user_sql_explain_plan", side_effect=track_explain),
            patch.object(pipeline, "assess_risk", return_value=risk),
            patch.object(pipeline, "run_user_sql", side_effect=track_run_sql),
            patch.object(pipeline, "_diagnosis_graph") as mock_graph_fn,
        ):
            mock_graph = MagicMock()
            mock_graph.ainvoke = AsyncMock(return_value=self._mock_diagnosis())
            mock_graph_fn.return_value = mock_graph

            asyncio.run(pipeline.run_query("SELECT 1 FROM dual"))

        assert call_order.index("explain") < call_order.index("run_sql")


# ---------------------------------------------------------------------------
# plan_risk 모듈 구조 검증
# ---------------------------------------------------------------------------

class TestPlanRiskModuleStructure:
    def test_module_importable(self):
        import src.plan_risk as m
        assert hasattr(m, "assess_risk")
        assert hasattr(m, "get_user_sql_explain_plan")

    def test_assess_risk_callable(self):
        from src.plan_risk import assess_risk
        result = assess_risk("INDEX RANGE SCAN Cost=5 Rows=10")
        assert result is not None

    def test_high_risk_threshold_constant_present(self):
        import src.plan_risk as m
        assert hasattr(m, "_COST_THRESHOLD")
        assert m._COST_THRESHOLD > 0

    def test_rows_threshold_constant_present(self):
        import src.plan_risk as m
        assert hasattr(m, "_ROWS_THRESHOLD")
        assert m._ROWS_THRESHOLD > 0

    def test_plan_risk_prompt_module_used(self):
        """plan_risk.py가 src/prompts/plan_risk.py의 system prompt를 import한다."""
        import ast
        plan_risk_src = (PROJECT_ROOT / "src" / "plan_risk.py").read_text(encoding="utf-8")
        assert "plan_risk" in plan_risk_src
        # plan_risk 모듈에서 prompts.plan_risk를 참조해야 함
        assert "_plan_risk_mod" in plan_risk_src or "plan_risk as" in plan_risk_src
