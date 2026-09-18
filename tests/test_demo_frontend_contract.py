"""AC 17: 데모 프런트엔드 웹 UI 검증

AC: 웹 UI에서 비즈니스 요구사항 입력, 직접 SQL 검증, 자연어 운영 성능 요청을 각각 시작할 수 있고,
    후보 SQL 목록·사용자 선택·accept/revise/reject 결과·실행계획 진단을 표시한다.

seed v2.6.0부터 SQL 생성/검증/후보탐색은 탭도, 별도 엔드포인트(/assist)도 없이 단일 입력창 +
POST /query로 통합됐다 — 별도 분류기 없이 기존 Multi-Agent Supervisor(src/agents.py
build_supervisor)가 그 자체로 의도를 분류해 담당 에이전트에 위임한다(run_via_supervisor 참고).
X-Approver-Token 헤더가 있을 때만 이 라우팅이 열리고, 헤더가 없으면 기존 /query 계약
(run_query)이 그대로 동작한다. seed v2.7.0부터 후보 진단(sql_id)은 다시 전용 엔드포인트
POST /candidates/diagnose로 분리했다 — 분류할 자연어가 없는 결정적 선택 액션이라 Supervisor를
거칠 이유가 없기 때문이다. "demonstrable_workflow" 원칙은 결과 카드의 흐름별 배지(mode)로
계속 만족한다.

검증 항목:
1. GET / 는 단일 입력창 UI를 반환하며, 흐름별 결과 배지 라벨(SQL 생성/검증/운영 SQL 탐색/실행계획
   진단)을 모두 포함한다.
2. POST /query에 토큰 + SELECT SQL 입력 → accept/revise/reject 결과 구조를 반환한다.
3. POST /query에 토큰 + UPDATE SQL 입력 → blocked 상태를 반환한다 (MCP 호출 없음).
4. POST /query에 토큰 + 자연어(후보 탐색 의도) → candidates 목록 (masked_sql 포함) 반환.
5. POST /candidates/diagnose에 토큰 + sql_id → 실행계획 진단 결과를 반환한다.
6. /candidates/diagnose는 토큰 없이 401. /query(question만)는 토큰 헤더 없으면 기존 계약대로 200.
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
# 1. 정적 UI — 단일 입력창 + 흐름별 배지 라벨
# ---------------------------------------------------------------------------

class TestStaticUiUnifiedInput:
    """GET / 가 네 흐름을 하나의 입력창에서 시작하고 결과를 구분해 보여주는 HTML을 반환한다."""

    def test_index_html_has_flow_badge_labels(self):
        """탭 대신, 서버가 분류한 흐름을 결과 카드 배지로 구분해 보여줘야 한다."""
        index_html = PROJECT_ROOT / "static" / "index.html"
        assert index_html.exists(), "static/index.html이 존재해야 한다"
        content = index_html.read_text(encoding="utf-8")
        assert "SQL 생성" in content, "SQL 생성 흐름 라벨이 있어야 한다"
        assert "SQL 검증" in content, "SQL 검증 흐름 라벨이 있어야 한다"
        assert "운영 SQL 탐색" in content, "운영 SQL 탐색 흐름 라벨이 있어야 한다"
        assert "실행계획 진단" in content, "실행계획 진단 흐름 라벨이 있어야 한다"

    def test_index_html_has_single_composer(self):
        """네 흐름을 시작하는 입력창이 하나만 있어야 한다(탭별 입력창 4개가 아니라)."""
        index_html = PROJECT_ROOT / "static" / "index.html"
        content = index_html.read_text(encoding="utf-8")
        assert content.count('id="req-input"') == 1, "통합 입력창(#req-input)이 정확히 하나 있어야 한다"
        assert "data-tab=" not in content, "탭 구조는 통합 진입점으로 대체되어 더 이상 없어야 한다"

    def test_ui_calls_unified_query_endpoint(self):
        index_html = PROJECT_ROOT / "static" / "index.html"
        content = index_html.read_text(encoding="utf-8")
        assert '"/query"' in content, "통합 진입점 /query 호출이 UI에 있어야 한다"
        assert "sql_id" in content, "후보 선택 시 sql_id를 보내는 코드가 있어야 한다"

    def test_ui_no_longer_calls_retired_routes(self):
        """폐기된 전용 라우트 호출이 UI에 남아 있으면 안 된다(회귀 방지).

        /candidates/diagnose는 seed v2.7.0에서 다시 분리된 라우트라 여기 포함하지 않는다 —
        분류할 자연어가 없는 결정적 액션(사용자가 목록에서 후보를 클릭)이라 /query(Supervisor
        경유)에 억지로 끼워 넣지 않고 전용 엔드포인트로 둔다.
        """
        index_html = PROJECT_ROOT / "static" / "index.html"
        content = index_html.read_text(encoding="utf-8")
        for retired in ('"/generate"', '"/validate"', '"/candidates"', '"/assist"'):
            assert retired not in content, f"폐기된 라우트 호출이 UI에 남아 있음: {retired}"
        assert '"/candidates/diagnose"' in content, "후보 진단 전용 엔드포인트 호출이 UI에 있어야 한다"

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

    def test_run_via_supervisor_importable(self):
        """통합 진입점이 Supervisor에 위임하는 함수가 존재한다."""
        from src.pipeline import run_via_supervisor
        assert callable(run_via_supervisor)


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
# 7. FastAPI 엔드포인트 — 통합된 POST /query의 토큰 보호 및 구조
# ---------------------------------------------------------------------------

class TestApiEndpoints:
    """FastAPI 앱의 /query(자연어)와 /candidates/diagnose(후보 선택)에서 흐름별 라우팅과
    토큰 보호가 적용된다."""

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from src.api import app
        return TestClient(app, raise_server_exceptions=False)

    def test_get_index_returns_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    def test_candidates_diagnose_no_token_returns_401(self, client):
        """/candidates/diagnose는 항상 토큰을 먼저 검사한다."""
        resp = client.post("/candidates/diagnose", json={"sql_id": "orders_customers_join"})
        assert resp.status_code == 401

    def test_question_only_no_token_falls_back_to_legacy(self, client):
        """question만 있고 토큰 헤더가 없으면 기존 /query 계약대로 200으로 동작한다(run_query에
        위임 — 실제 MCP/LLM 호출 없이 HTTP 레벨 동작만 결정적으로 확인)."""
        with patch("src.api.run_query") as mock_run_query:
            mock_run_query.return_value = {"status": "ok", "answer": "", "contexts": [], "trace": []}
            resp = client.post("/query", json={"question": "SELECT 1 FROM dual"})
        assert resp.status_code == 200

    def test_validate_update_sql_blocked(self, client):
        """UPDATE SQL은 토큰 인증 후 blocked 상태로 반환된다.

        Supervisor의 실제 LLM 분류는 여기서 검증하지 않는다(별도의 결정적 단위 테스트 —
        TestRunSqlValidationGuardrail — 이 이미 run_sql_validation 자체의 차단 로직을 검증한다).
        여기서는 /query 라우트가 run_via_supervisor의 결과를 그대로 전달하는지만 확인한다.
        """
        with patch("src.api.run_via_supervisor") as mock_sup:
            mock_sup.return_value = {
                "status": "blocked", "reason": "[규칙] SELECT 조회문만 입력할 수 있습니다.",
                "answer": "", "contexts": [], "trace": [],
            }
            resp = client.post(
                "/query",
                json={"question": "UPDATE orders SET status='DONE' WHERE order_id=1"},
                headers=AUTH_HEADER,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "blocked"

    def test_candidates_returns_list(self, client):
        """유효한 토큰으로 후보 탐색 결과(candidates 목록)가 그대로 전달된다."""
        with patch("src.api.run_via_supervisor") as mock_sup:
            mock_sup.return_value = {
                "status": "ok", "mode": "candidate_search",
                "candidates": [{"sql_id": "orders_customers_join", "masked_sql": "SELECT ...", "rank": 1}],
                "answer": "1개의 후보 SQL을 찾았습니다.", "contexts": [], "trace": [],
            }
            resp = client.post(
                "/query",
                json={"question": "주문 고객 조인 쿼리가 느려요"},
                headers=AUTH_HEADER,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert isinstance(data["candidates"], list)
        assert len(data["candidates"]) > 0

    def test_candidates_have_masked_sql(self, client):
        """후보 목록의 각 항목에 masked_sql이 있다(run_via_supervisor 결과 통과 확인)."""
        with patch("src.api.run_via_supervisor") as mock_sup:
            mock_sup.return_value = {
                "status": "ok", "mode": "candidate_search",
                "candidates": [{"sql_id": "orders_customers_join", "masked_sql": "SELECT ...", "rank": 1}],
                "answer": "1개의 후보 SQL을 찾았습니다.", "contexts": [], "trace": [],
            }
            resp = client.post(
                "/query",
                json={"question": "주문 고객 조인"},
                headers=AUTH_HEADER,
            )
        data = resp.json()
        for c in data.get("candidates", []):
            assert "masked_sql" in c
            assert "sql_id" in c

    def test_candidates_diagnose_unknown_id(self, client):
        """존재하지 않는 sql_id → no_answer(V$SQL·카탈로그 모두 확인 후)."""
        resp = client.post(
            "/candidates/diagnose",
            json={"sql_id": "nonexistent_sql_xyz"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "no_answer"

    def test_candidates_diagnose_known_id_with_mock(self, client):
        """알려진 카탈로그 키 sql_id(밑줄 포함 → V$SQL 조회 없이 카탈로그로 직행) + mock LLM → ok 구조."""
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
        """SELECT SQL 입력 → accept/revise/reject 중 하나가 그대로 전달된다(run_via_supervisor 결과 통과 확인).

        Supervisor의 실제 분류·validate_user_sql 도구·review_sql LLM 호출 체인은 별도로
        TestRunSqlValidationSelect(run_sql_validation 직접 호출)가 이미 검증한다.
        """
        with patch("src.api.run_via_supervisor") as mock_sup:
            mock_sup.return_value = {
                "status": "ok", "mode": "sql_validation", "result": "accept",
                "reason": "문제 없음", "answer": "[ACCEPT] 문제 없음", "contexts": [], "trace": [],
            }
            resp = client.post(
                "/query",
                json={"question": "SELECT * FROM orders WHERE status = 'PENDING'"},
                headers=AUTH_HEADER,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["result"] in ("accept", "revise", "reject")

    def test_query_response_relays_mode_for_badge_display(self, client):
        """/query 응답에는 흐름을 구분해 보여줄 mode 필드가 그대로 담겨 있다(demonstrable_workflow)."""
        with patch("src.api.run_via_supervisor") as mock_sup:
            mock_sup.return_value = {
                "status": "ok", "mode": "sql_validation", "result": "accept",
                "answer": "", "contexts": [], "trace": [],
            }
            resp = client.post(
                "/query",
                json={"question": "SELECT * FROM orders WHERE status = 'PENDING'"},
                headers=AUTH_HEADER,
            )
        assert resp.json().get("mode") == "sql_validation"


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

    def test_ui_has_diagnosis_flow_label(self, ui):
        """실행계획 진단 흐름을 통합 입력창 하나로 시작할 수 있고, 결과 배지로 구분되어야 한다."""
        assert "실행계획 진단" in ui
        assert 'id="req-input"' in ui, "통합 입력창이 있어야 한다"

    def test_ui_renders_plan_risk_section(self, ui):
        """실행계획 위험 평가 결과 영역이 렌더링되어야 한다."""
        assert "실행계획 위험 평가" in ui

    def test_ui_renders_candidate_list_section(self, ui):
        assert "후보 SQL 목록" in ui

    def test_ui_starts_three_flows_via_unified_query(self, ui):
        """SQL 생성/검증/후보탐색 세 흐름이 같은 통합 진입점(/query)으로 시작된다."""
        assert '"/query"' in ui, "/query 호출이 UI에 있어야 한다"
        for retired in ('"/generate"', '"/validate"', '"/candidates"', '"/assist"'):
            assert retired not in ui, f"폐기된 전용 엔드포인트가 UI에 남아 있음: {retired}"

    def test_selected_candidate_drives_diagnose_call(self, ui):
        """선택한 후보의 sql_id로 전용 진단 엔드포인트를 호출한다."""
        assert '"/candidates/diagnose"' in ui
        assert "sql_id" in ui
