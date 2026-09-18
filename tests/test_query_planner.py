"""AC 7: query_planner — 비즈니스 요구사항 요청 처리 순서 검증.

처리 순서:
  1. plan_requirement  — 요구사항 계획
  2. lookup_schema     — SQLcl MCP 스키마 조회
  3. draft_sql         — SELECT 초안 생성
  4. validate_sql      — SQL 검증
  5. review_plan       — 실행계획 검토
"""
from __future__ import annotations
import asyncio

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

EXPECTED_STEPS = [
    "plan_requirement",
    "lookup_schema",
    "draft_sql",
    "validate_sql",
    "review_plan",
]


# ---------------------------------------------------------------------------
# plan_execute 모듈 구조 검증 — 5개 노드 정의 확인 (정적)
# ---------------------------------------------------------------------------

class TestQueryPlannerGraphStructure:
    """build_query_planner_graph와 5단계 상수가 plan_execute.py에 있는지 확인한다."""

    def test_build_function_importable(self):
        from src.plan_execute import build_query_planner_graph
        assert callable(build_query_planner_graph)

    def test_ordered_steps_constant_importable(self):
        from src.plan_execute import QUERY_PLANNER_ORDERED_STEPS
        assert isinstance(QUERY_PLANNER_ORDERED_STEPS, list)
        assert len(QUERY_PLANNER_ORDERED_STEPS) == 5

    def test_ordered_steps_exact_values(self):
        from src.plan_execute import QUERY_PLANNER_ORDERED_STEPS
        assert QUERY_PLANNER_ORDERED_STEPS == EXPECTED_STEPS

    def test_query_planner_state_importable(self):
        from src.plan_execute import QueryPlannerState
        assert QueryPlannerState is not None

    @pytest.mark.parametrize("node_fn", [
        "_plan_requirement_node",
        "_lookup_schema_node",
        "_draft_sql_node",
        "_validate_sql_node",
        "_review_plan_node",
    ])
    def test_node_functions_importable(self, node_fn: str):
        import src.plan_execute as m
        assert hasattr(m, node_fn), f"{node_fn}이 plan_execute에 없습니다."
        assert callable(getattr(m, node_fn))


# ---------------------------------------------------------------------------
# 그래프 컴파일 검증 — 노드 이름 포함 여부 (정적)
# ---------------------------------------------------------------------------

class TestQueryPlannerGraphCompiles:
    """build_query_planner_graph()가 컴파일되고 5개 노드를 포함한다."""

    def _get_graph(self):
        from src.plan_execute import build_query_planner_graph
        return build_query_planner_graph()

    def test_graph_compiles(self):
        graph = self._get_graph()
        assert graph is not None

    @pytest.mark.parametrize("step", EXPECTED_STEPS)
    def test_graph_has_node(self, step: str):
        graph = self._get_graph()
        # CompiledStateGraph는 graph.nodes 딕셔너리를 가진다
        node_keys = set(graph.nodes.keys())
        assert step in node_keys, (
            f"그래프에 '{step}' 노드가 없습니다. 실제 노드: {node_keys}"
        )


# ---------------------------------------------------------------------------
# 엣지 순서 검증 — plan_execute.py 소스를 AST 없이 텍스트로 확인
# ---------------------------------------------------------------------------

class TestQueryPlannerStepOrder:
    """plan_execute.py 소스에서 5단계 엣지가 올바른 순서로 add_edge 호출되는지 확인한다."""

    def _source(self) -> str:
        return (PROJECT_ROOT / "src" / "plan_execute.py").read_text(encoding="utf-8")

    def test_all_steps_present_in_source(self):
        src = self._source()
        for step in EXPECTED_STEPS:
            assert step in src, f"plan_execute.py에 '{step}'이 없습니다."

    def test_step_order_by_position(self):
        """소스에서 각 단계 이름이 EXPECTED_STEPS 순서로 등장한다."""
        src = self._source()
        positions = [src.index(step) for step in EXPECTED_STEPS]
        assert positions == sorted(positions), (
            f"EXPECTED_STEPS 순서와 소스 내 등장 순서가 다릅니다: {dict(zip(EXPECTED_STEPS, positions))}"
        )

    def test_edges_enforce_sequential_order(self):
        """add_edge 호출이 연속 단계 쌍을 모두 커버한다."""
        src = self._source()
        for i in range(len(EXPECTED_STEPS) - 1):
            pair = f'"{EXPECTED_STEPS[i]}", "{EXPECTED_STEPS[i + 1]}"'
            assert pair in src, (
                f"add_edge({pair}) 호출이 plan_execute.py에 없습니다."
            )


# ---------------------------------------------------------------------------
# run_business_requirement 함수 존재 검증 (pipeline.py)
# ---------------------------------------------------------------------------

class TestRunBusinessRequirementExists:
    def test_function_importable(self):
        from src.pipeline import run_business_requirement
        assert callable(run_business_requirement)

    def test_empty_requirement_returns_no_answer(self):
        from src.pipeline import run_business_requirement
        result = asyncio.run(run_business_requirement(""))
        assert result["status"] == "no_answer"

    def test_whitespace_only_returns_no_answer(self):
        from src.pipeline import run_business_requirement
        result = asyncio.run(run_business_requirement("   "))
        assert result["status"] == "no_answer"

    def test_non_string_returns_no_answer(self):
        from src.pipeline import run_business_requirement
        result = asyncio.run(run_business_requirement(None))  # type: ignore[arg-type]
        assert result["status"] == "no_answer"


# ---------------------------------------------------------------------------
# run_business_requirement 결과 구조 검증 (mock 그래프)
# ---------------------------------------------------------------------------

class TestRunBusinessRequirementOutput:
    """run_business_requirement가 올바른 응답 구조를 반환한다 (mock 그래프 사용)."""

    def _mock_graph_result(self):
        """5단계를 모두 완료한 mock 결과."""
        return {
            "steps_completed": EXPECTED_STEPS[:],
            "requirement_plan": ["스키마 조회", "초안 작성", "검증", "실행계획 검토"],
            "sql_draft": "SELECT order_id FROM orders WHERE status = 'PENDING'",
            "validation": {"result": "accept", "reason": "문제 없음", "query_name": "select_orders",
                           "reviewed_at": "2026-09-18T00:00:00Z", "review_id": "abc123",
                           "annotated_sql": "/* ... */ SELECT order_id FROM orders"},
            "explain_plan": "INDEX RANGE SCAN Cost=5",
            "risk_assessment": {"risk_level": "LOW", "reason": "인덱스 사용", "allow_execution": True},
            "answer": "[생성 SQL]\nSELECT order_id FROM orders",
        }

    def _run_with_mock(self, requirement: str = "미결 주문 목록을 조회해라") -> dict:
        from src import pipeline
        mock_graph = MagicMock()
        mock_graph.ainvoke = AsyncMock(return_value=self._mock_graph_result())
        with patch.object(pipeline, "_query_planner_graph", return_value=mock_graph):
            return asyncio.run(pipeline.run_business_requirement(requirement))

    def test_status_ok(self):
        result = self._run_with_mock()
        assert result["status"] == "ok"

    def test_mode_business_requirement(self):
        result = self._run_with_mock()
        assert result["mode"] == "business_requirement"

    def test_steps_completed_in_result(self):
        result = self._run_with_mock()
        assert "steps_completed" in result
        assert result["steps_completed"] == EXPECTED_STEPS

    def test_sql_draft_in_result(self):
        result = self._run_with_mock()
        assert "sql_draft" in result
        assert "SELECT" in result["sql_draft"].upper()

    def test_validation_in_result(self):
        result = self._run_with_mock()
        assert "validation" in result
        assert result["validation"] is not None

    def test_risk_assessment_in_result(self):
        result = self._run_with_mock()
        assert "risk_assessment" in result
        assert result["risk_assessment"] is not None

    def test_answer_in_result(self):
        result = self._run_with_mock()
        assert "answer" in result
        assert result["answer"] != ""

    def test_trace_in_result(self):
        result = self._run_with_mock()
        assert "trace" in result
        assert isinstance(result["trace"], list)

    def test_contexts_in_result(self):
        result = self._run_with_mock()
        assert "contexts" in result
        assert isinstance(result["contexts"], list)


# ---------------------------------------------------------------------------
# 5단계 실행 순서 검증 (개별 노드 함수 mock)
# ---------------------------------------------------------------------------

class TestStepExecutionOrder:
    """5개 노드 함수가 EXPECTED_STEPS 순서로 호출된다."""

    def test_node_functions_called_in_order(self):
        """실제 그래프를 실행할 때 nodes가 올바른 순서로 실행되는지 call_order로 추적한다."""
        call_order: list[str] = []
        import src.plan_execute as pe
        import src.tools as _tools

        # 각 노드 함수를 step 이름을 기록하는 wrapper로 대체
        def make_tracker(step_name: str, return_extra: dict | None = None):
            def _tracker(state):
                call_order.append(step_name)
                out: dict = {"steps_completed": [step_name]}
                if return_extra:
                    out.update(return_extra)
                return out
            return _tracker

        validate_return = {"validation": {"result": "accept", "reason": "ok", "query_name": "q",
                                          "reviewed_at": "2026-09-18T00:00:00Z", "review_id": "x",
                                          "annotated_sql": "/* x */ SELECT 1"}}
        review_return = {"explain_plan": "INDEX Cost=1",
                         "risk_assessment": {"risk_level": "LOW", "reason": "ok", "allow_execution": True},
                         "answer": "완료"}

        with (
            patch.object(pe, "_plan_requirement_node", side_effect=make_tracker("plan_requirement")),
            patch.object(pe, "_lookup_schema_node", side_effect=make_tracker("lookup_schema")),
            patch.object(pe, "_draft_sql_node",
                         side_effect=make_tracker("draft_sql", {"sql_draft": "SELECT 1 FROM dual"})),
            patch.object(pe, "_validate_sql_node", side_effect=make_tracker("validate_sql", validate_return)),
            # _review_plan_node는 이제 async(진단 그래프도 함께 호출하므로) — patch.object가
            # 원본이 코루틴 함수임을 감지해 side_effect를 AsyncMock으로 감싼다. 그러면 이 노드가
            # ainvoke 전용으로 등록돼 graph.invoke(동기)가 "No synchronous function" 에러를
            # 낸다 — 그래서 이 테스트도 ainvoke로 바꾼다(실제 run_business_requirement도 항상
            # ainvoke만 쓴다).
            patch.object(pe, "_review_plan_node", side_effect=make_tracker("review_plan", review_return)),
        ):
            graph = pe.build_query_planner_graph()
            asyncio.run(graph.ainvoke({
                "requirement": "미결 주문 조회",
                "steps_completed": [],
                "schema_info": "",
                "sql_draft": "",
                "validation": None,
                "explain_plan": "",
                "risk_assessment": None,
                "analysis": None,
                "answer": "",
            }))

        assert call_order == EXPECTED_STEPS, (
            f"노드 실행 순서가 다릅니다.\n기대: {EXPECTED_STEPS}\n실제: {call_order}"
        )


# ---------------------------------------------------------------------------
# 1단계 plan_requirement — Plan-Execute의 Plan 단계 검증
# ---------------------------------------------------------------------------

class TestPlanRequirementNode:
    """_plan_requirement_node가 실제 계획을 만들고, LLM 실패 시에도 기본 계획으로 진행한다."""

    def _state(self, requirement: str = "지난달 결제 실패 주문을 고객별로 집계해라") -> dict:
        return {
            "requirement": requirement,
            "steps_completed": [],
            "requirement_plan": [],
            "schema_info": "",
            "sql_draft": "",
            "validation": None,
            "explain_plan": "",
            "risk_assessment": None,
            "answer": "",
        }

    def test_uses_query_planner_prompt_module(self):
        """query_planner 역할 프롬프트 모듈을 system prompt로 사용한다."""
        import src.plan_execute as pe
        from src.prompts.query_planner import SYSTEM_PROMPT
        assert pe.QUERY_PLANNER_SYSTEM_PROMPT == SYSTEM_PROMPT

    def test_state_has_requirement_plan_field(self):
        from src.plan_execute import QueryPlannerState
        assert "requirement_plan" in QueryPlannerState.__annotations__

    def test_plan_from_llm_is_used(self):
        import src.plan_execute as pe

        planned = ["스키마 조회하기", "SELECT 초안 작성하기", "검증하기"]
        structured = MagicMock()
        structured.invoke.return_value = SimpleNamespace(steps=planned)
        llm = MagicMock()
        llm.with_structured_output.return_value = structured

        with patch.object(pe, "default_llm", return_value=llm):
            out = pe._plan_requirement_node(self._state())

        assert out["requirement_plan"] == planned
        assert out["steps_completed"] == ["plan_requirement"]

    def test_plan_falls_back_when_llm_fails(self):
        """LLM 호출이 실패해도 비어 있지 않은 기본 계획을 반환한다."""
        import src.plan_execute as pe

        with patch.object(pe, "default_llm", side_effect=RuntimeError("no llm")):
            out = pe._plan_requirement_node(self._state())

        assert out["steps_completed"] == ["plan_requirement"]
        assert isinstance(out["requirement_plan"], list)
        assert len(out["requirement_plan"]) >= 2

    def test_plan_falls_back_when_llm_returns_empty(self):
        import src.plan_execute as pe

        structured = MagicMock()
        structured.invoke.return_value = SimpleNamespace(steps=[])
        llm = MagicMock()
        llm.with_structured_output.return_value = structured

        with patch.object(pe, "default_llm", return_value=llm):
            out = pe._plan_requirement_node(self._state())

        assert len(out["requirement_plan"]) >= 2

    def test_plan_precedes_schema_lookup_in_graph(self):
        """Plan 단계 결과가 이후 단계에서 사용 가능하도록 첫 노드로 실행된다."""
        from src.plan_execute import QUERY_PLANNER_ORDERED_STEPS
        assert QUERY_PLANNER_ORDERED_STEPS[0] == "plan_requirement"
        assert QUERY_PLANNER_ORDERED_STEPS[1] == "lookup_schema"


class TestRequirementPlanSurfaced:
    """run_business_requirement 응답에 requirement_plan이 포함된다."""

    def test_requirement_plan_in_result(self):
        from src import pipeline
        mock_graph = MagicMock()
        mock_graph.ainvoke = AsyncMock(return_value={
            "steps_completed": EXPECTED_STEPS[:],
            "requirement_plan": ["스키마 조회", "초안 작성"],
            "sql_draft": "SELECT 1 FROM dual",
            "validation": {"result": "accept"},
            "explain_plan": "INDEX",
            "risk_assessment": {"risk_level": "LOW"},
            "answer": "ok",
        })
        with patch.object(pipeline, "_query_planner_graph", return_value=mock_graph):
            result = asyncio.run(pipeline.run_business_requirement("미결 주문 조회"))
        assert result["requirement_plan"] == ["스키마 조회", "초안 작성"]

    def test_graph_invoked_with_requirement_plan_seed(self):
        from src import pipeline
        mock_graph = MagicMock()
        mock_graph.ainvoke = AsyncMock(return_value={"steps_completed": [], "answer": ""})
        with patch.object(pipeline, "_query_planner_graph", return_value=mock_graph):
            asyncio.run(pipeline.run_business_requirement("미결 주문 조회"))
        initial_state = mock_graph.ainvoke.call_args[0][0]
        assert initial_state["requirement_plan"] == []
        assert initial_state["requirement"] == "미결 주문 조회"
