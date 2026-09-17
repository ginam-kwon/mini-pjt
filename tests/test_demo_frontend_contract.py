"""AC 17: 데모 프런트엔드 웹 UI 검증

AC: 웹 UI에서 비즈니스 요구사항 입력, 직접 SQL 검증, 자연어 운영 성능 요청을 각각 시작할 수 있고,
    후보 SQL 목록·사용자 선택·accept/revise/reject 결과·실행계획 진단을 표시한다.

검증 항목:
1. GET / 는 세 탭(SQL 생성, SQL 검증, 운영 SQL 탐색)을 포함한 HTML을 반환한다.
2. POST /validate SELECT SQL → accept/revise/reject 결과 구조를 반환한다.
3. POST /validate UPDATE SQL → blocked 상태를 반환한다 (MCP 호출 없음).
4. POST /candidates 자연어 → candidates 목록 (masked_sql 포함) 반환.
5. POST /candidates/diagnose sql_id → 실행계획 진단 결과 반환.
6. POST /generate, /validate, /candidates, /candidates/diagnose 는 토큰 없으면 401.
7. pipeline.run_sql_validation, run_candidate_search, run_candidate_diagnose 가 존재한다.
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

DEMO_TOKEN = "demo-approver-token"
AUTH_HEADER = {"X-Approver-Token": DEMO_TOKEN}


# ---------------------------------------------------------------------------
# 1. 정적 UI — 세 탭 포함 여부
# ---------------------------------------------------------------------------

class TestStaticUiHasTabs:
    """GET / 가 세 가지 흐름 탭을 포함한 HTML을 반환한다."""

    def test_index_html_has_three_flow_tabs(self):
        index_html = PROJECT_ROOT / "static" / "index.html"
        assert index_html.exists(), "static/index.html이 존재해야 한다"
        content = index_html.read_text(encoding="utf-8")
        # 세 흐름 탭이 모두 있는지 확인
        assert "SQL 생성" in content, "SQL 생성 탭이 있어야 한다"
        assert "SQL 검증" in content, "SQL 검증 탭이 있어야 한다"
        assert "운영 SQL 탐색" in content, "운영 SQL 탐색 탭이 있어야 한다"

    def test_ui_has_candidate_diagnose_endpoint(self):
        index_html = PROJECT_ROOT / "static" / "index.html"
        content = index_html.read_text(encoding="utf-8")
        assert "/candidates/diagnose" in content, "후보 진단 엔드포인트가 UI에 있어야 한다"

    def test_ui_has_validate_endpoint(self):
        index_html = PROJECT_ROOT / "static" / "index.html"
        content = index_html.read_text(encoding="utf-8")
        assert '"/validate"' in content or "'/validate'" in content or "/validate" in content

    def test_ui_has_approver_token_input(self):
        index_html = PROJECT_ROOT / "static" / "index.html"
        content = index_html.read_text(encoding="utf-8")
        assert "approver-token" in content or "Approver-Token" in content, "토큰 입력 필드가 있어야 한다"

    def test_ui_shows_candidate_list(self):
        """후보 SQL 목록 렌더링 코드가 UI에 있는지 확인."""
        index_html = PROJECT_ROOT / "static" / "index.html"
        content = index_html.read_text(encoding="utf-8")
        assert "candidates" in content
        assert "masked_sql" in content or "candidate-card" in content

    def test_ui_shows_accept_revise_reject(self):
        """accept/revise/reject 뱃지 렌더링 코드가 있는지 확인."""
        index_html = PROJECT_ROOT / "static" / "index.html"
        content = index_html.read_text(encoding="utf-8")
        assert "accept" in content
        assert "revise" in content
        assert "reject" in content


# ---------------------------------------------------------------------------
# 2. pipeline 함수 존재 확인
# ---------------------------------------------------------------------------

class TestPipelineFunctionsExist:
    """pipeline.py에 세 흐름에 필요한 함수가 정의되어 있다."""

    def test_run_sql_validation_importable(self):
        from src.pipeline import run_sql_validation
        assert callable(run_sql_validation)

    def test_run_candidate_search_importable(self):
        from src.pipeline import run_candidate_search
        assert callable(run_candidate_search)

    def test_run_candidate_diagnose_importable(self):
        from src.pipeline import run_candidate_diagnose
        assert callable(run_candidate_diagnose)

    def test_run_business_requirement_importable(self):
        from src.pipeline import run_business_requirement
        assert callable(run_business_requirement)


# ---------------------------------------------------------------------------
# 3. run_sql_validation — UPDATE/DELETE 차단 (LLM 호출 없음)
# ---------------------------------------------------------------------------

class TestRunSqlValidationGuardrail:
    """run_sql_validation은 변경 SQL을 DB/MCP 호출 없이 즉시 차단한다."""

    def test_update_sql_blocked(self):
        from src.pipeline import run_sql_validation
        result = asyncio.run(run_sql_validation("UPDATE orders SET status='DONE' WHERE order_id=1"))
        assert result["status"] == "blocked", f"UPDATE SQL은 blocked여야 한다: {result}"

    def test_delete_sql_blocked(self):
        from src.pipeline import run_sql_validation
        result = asyncio.run(run_sql_validation("DELETE FROM orders WHERE order_id=1"))
        assert result["status"] == "blocked", f"DELETE SQL은 blocked여야 한다: {result}"

    def test_empty_sql_no_answer(self):
        from src.pipeline import run_sql_validation
        result = asyncio.run(run_sql_validation(""))
        assert result["status"] == "no_answer"


# ---------------------------------------------------------------------------
# 4. run_sql_validation — SELECT SQL LLM 호출 (mock)
# ---------------------------------------------------------------------------

class TestRunSqlValidationSelect:
    """run_sql_validation은 SELECT SQL을 LLM으로 검증하고 accept/revise/reject를 반환한다."""

    def test_select_returns_review_structure(self):
        from src.schemas import QueryReview
        from src.pipeline import run_sql_validation

        mock_review = QueryReview(result="accept", reason="문제 없음")
        # review_sql은 run_sql_validation 안에서 from src.validator import review_sql로 가져오므로
        # src.validator.review_sql을 patch한다
        with patch("src.validator.review_sql", return_value=mock_review):
            result = asyncio.run(run_sql_validation("SELECT * FROM orders WHERE status = 'PENDING'"))
        assert result["status"] == "ok"
        assert result["mode"] == "sql_validation"
        assert result["result"] in ("accept", "revise", "reject")

    def test_select_validation_with_mock_accept(self):
        """mock accept 결과가 올바른 구조로 반환된다."""
        from src.schemas import QueryReview
        from src.pipeline import run_sql_validation

        mock_review = QueryReview(
            result="accept",
            reason="성능 위험 없음",
            query_name="select_orders",
            reviewed_at="2026-09-18T00:00:00Z",
            review_id="abc123def",
            annotated_sql="/* q=select_orders */ SELECT * FROM orders",
        )
        with patch("src.validator.default_llm") as mock_llm:
            mock_model = MagicMock()
            mock_model.with_structured_output.return_value.invoke.return_value = mock_review
            mock_llm.return_value = mock_model
            result = asyncio.run(run_sql_validation("SELECT * FROM orders WHERE status = 'PENDING'"))

        assert result["status"] == "ok"
        assert "result" in result
        assert "reason" in result
        assert "trace" in result


# ---------------------------------------------------------------------------
# 5. run_candidate_search — 후보 목록 반환
# ---------------------------------------------------------------------------

class TestRunCandidateSearch:
    """run_candidate_search는 자연어 질문에서 masked_sql 포함 후보 목록을 반환한다."""

    def test_returns_ok_with_known_keyword(self):
        from src.pipeline import run_candidate_search
        result = asyncio.run(run_candidate_search("주문 고객 조인 쿼리가 느려요"))
        assert result["status"] == "ok"
        assert result["mode"] == "candidate_search"
        assert isinstance(result["candidates"], list)

    def test_candidates_have_required_fields(self):
        from src.pipeline import run_candidate_search
        result = asyncio.run(run_candidate_search("주문 고객 조인 쿼리가 느려요"))
        candidates = result["candidates"]
        assert len(candidates) > 0, "주문·고객 키워드로 후보가 있어야 한다"
        for c in candidates:
            assert "sql_id" in c
            assert "masked_sql" in c
            assert "rank" in c

    def test_masked_sql_has_no_literals(self):
        """masked_sql에 원본 문자열 리터럴이 없어야 한다."""
        from src.pipeline import run_candidate_search
        result = asyncio.run(run_candidate_search("주문 고객 조인"))
        for c in result["candidates"]:
            # 싱글쿼트 리터럴이 :param_N으로 바뀌어 있어야 한다
            masked = c["masked_sql"]
            # 원본 리터럴이 남아있으면 안된다 (KIM% 같은 값)
            assert "KIM%" not in masked, "문자열 리터럴이 마스킹되어야 한다"

    def test_no_match_returns_empty_candidates(self):
        from src.pipeline import run_candidate_search
        result = asyncio.run(run_candidate_search("전혀관계없는xyz쿼리"))
        assert result["status"] == "ok"
        assert result["candidates"] == []

    def test_empty_question_returns_no_answer(self):
        from src.pipeline import run_candidate_search
        result = asyncio.run(run_candidate_search(""))
        assert result["status"] == "no_answer"


# ---------------------------------------------------------------------------
# 6. run_candidate_diagnose — 선택한 후보 진단
# ---------------------------------------------------------------------------

class TestRunCandidateDiagnose:
    """run_candidate_diagnose는 sql_id로 QUERY_CATALOG를 조회해 진단 결과를 반환한다."""

    def test_unknown_sql_id_returns_no_answer(self):
        from src.pipeline import run_candidate_diagnose
        result = asyncio.run(run_candidate_diagnose("nonexistent_sql_id_xyz"))
        assert result["status"] == "no_answer"
        assert "찾을 수 없습니다" in result["reason"]

    def test_empty_sql_id_returns_no_answer(self):
        from src.pipeline import run_candidate_diagnose
        result = asyncio.run(run_candidate_diagnose(""))
        assert result["status"] == "no_answer"

    def test_known_sql_id_returns_diagnosis_structure(self):
        """알려진 sql_id로 진단하면 mode=candidate_diagnose 구조를 반환한다."""
        from src.pipeline import run_candidate_diagnose

        mock_analysis = {
            "summary": "풀 테이블 스캔이 발생합니다",
            "root_causes": [],
            "improvements": [],
        }

        with patch("src.pipeline._diagnosis_graph") as mock_graph_factory:
            mock_graph = MagicMock()
            mock_graph.ainvoke = AsyncMock(return_value={"analysis": mock_analysis})
            mock_graph_factory.return_value = mock_graph

            result = asyncio.run(run_candidate_diagnose("orders_customers_join"))

        assert result["status"] == "ok"
        assert result["mode"] == "candidate_diagnose"
        assert result["sql_id"] == "orders_customers_join"
        assert "analysis" in result
        assert "trace" in result


# ---------------------------------------------------------------------------
# 7. FastAPI 엔드포인트 — 토큰 보호 및 구조
# ---------------------------------------------------------------------------

class TestApiEndpoints:
    """FastAPI 앱에 세 흐름 엔드포인트가 등록되어 있고 토큰 보호가 동작한다."""

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from src.agent import app
        return TestClient(app, raise_server_exceptions=False)

    def test_get_index_returns_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    def test_validate_no_token_returns_401(self, client):
        resp = client.post("/validate", json={"sql": "SELECT 1 FROM dual"})
        assert resp.status_code == 401

    def test_candidates_no_token_returns_401(self, client):
        resp = client.post("/candidates", json={"question": "주문 고객 조인"})
        assert resp.status_code == 401

    def test_generate_no_token_returns_401(self, client):
        resp = client.post("/generate", json={"requirement": "고객별 주문 집계"})
        assert resp.status_code == 401

    def test_candidates_diagnose_no_token_returns_401(self, client):
        resp = client.post("/candidates/diagnose", json={"sql_id": "orders_customers_join"})
        assert resp.status_code == 401

    def test_validate_update_sql_blocked(self, client):
        """UPDATE SQL은 토큰 인증 후 blocked 상태로 반환된다."""
        resp = client.post(
            "/validate",
            json={"sql": "UPDATE orders SET status='DONE' WHERE order_id=1"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "blocked"

    def test_candidates_returns_list(self, client):
        """유효한 토큰으로 후보 탐색하면 candidates 목록을 반환한다."""
        resp = client.post(
            "/candidates",
            json={"question": "주문 고객 조인 쿼리가 느려요"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert isinstance(data["candidates"], list)
        assert len(data["candidates"]) > 0

    def test_candidates_have_masked_sql(self, client):
        """후보 목록의 각 항목에 masked_sql이 있다."""
        resp = client.post(
            "/candidates",
            json={"question": "주문 고객 조인"},
            headers=AUTH_HEADER,
        )
        data = resp.json()
        for c in data.get("candidates", []):
            assert "masked_sql" in c
            assert "sql_id" in c

    def test_candidates_diagnose_unknown_id(self, client):
        """존재하지 않는 sql_id → no_answer."""
        resp = client.post(
            "/candidates/diagnose",
            json={"sql_id": "nonexistent_sql_xyz"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "no_answer"

    def test_candidates_diagnose_known_id_with_mock(self, client):
        """알려진 sql_id + mock LLM → ok 구조."""
        mock_analysis = {
            "summary": "풀스캔 발생",
            "root_causes": [],
            "improvements": [],
        }
        with patch("src.pipeline._diagnosis_graph") as mock_gf:
            mock_g = MagicMock()
            mock_g.ainvoke = AsyncMock(return_value={"analysis": mock_analysis})
            mock_gf.return_value = mock_g

            resp = client.post(
                "/candidates/diagnose",
                json={"sql_id": "orders_customers_join"},
                headers=AUTH_HEADER,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["mode"] == "candidate_diagnose"
        assert data["sql_id"] == "orders_customers_join"

    def test_validate_select_with_mock_llm(self, client):
        """SELECT SQL + mock LLM → accept/revise/reject 중 하나."""
        from src.schemas import QueryReview
        mock_review = QueryReview(result="accept", reason="문제 없음")
        with patch("src.validator.default_llm") as mock_llm:
            mock_model = MagicMock()
            mock_model.with_structured_output.return_value.invoke.return_value = mock_review
            mock_llm.return_value = mock_model

            resp = client.post(
                "/validate",
                json={"sql": "SELECT * FROM orders WHERE status = 'PENDING'"},
                headers=AUTH_HEADER,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["result"] in ("accept", "revise", "reject")


# ---------------------------------------------------------------------------
# 8. 후보 선택 상태 및 실행계획 진단 표시 (AC 명시 요구사항)
# ---------------------------------------------------------------------------

class TestCandidateSelectionAndPlanDisplay:
    """UI가 사용자 후보 선택 상태와 실행계획 진단 결과를 표시한다."""

    @pytest.fixture
    def ui(self):
        return (PROJECT_ROOT / "static" / "index.html").read_text(encoding="utf-8")

    def test_ui_highlights_selected_candidate(self, ui):
        """선택된 후보 카드를 구분하는 selected 상태 스타일과 토글 코드가 있어야 한다."""
        assert ".candidate-card.selected" in ui, "선택 후보 강조 스타일이 있어야 한다"
        assert 'classList.add("selected")' in ui or "classList.add('selected')" in ui, \
            "후보 선택 시 selected 상태를 적용해야 한다"

    def test_ui_has_plan_diagnosis_tab(self, ui):
        """실행계획 진단 흐름을 시작할 수 있는 탭이 있어야 한다."""
        assert "실행계획 진단" in ui
        assert 'data-tab="diagnosis"' in ui

    def test_ui_renders_plan_risk_section(self, ui):
        """실행계획 위험 평가 결과 영역이 렌더링되어야 한다."""
        assert "실행계획 위험 평가" in ui

    def test_ui_renders_candidate_list_section(self, ui):
        assert "후보 SQL 목록" in ui

    def test_ui_starts_three_flows_via_distinct_endpoints(self, ui):
        """세 흐름이 각각 별도 엔드포인트로 시작된다."""
        for endpoint in ('"/generate"', '"/validate"', '"/candidates"'):
            assert endpoint in ui, f"{endpoint} 호출이 UI에 있어야 한다"

    def test_selected_candidate_drives_diagnose_call(self, ui):
        """선택한 후보의 sql_id로 진단 엔드포인트를 호출한다."""
        assert "/candidates/diagnose" in ui
        assert "sql_id" in ui
