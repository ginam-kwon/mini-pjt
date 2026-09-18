"""AC 19 — 미니 프로젝트 제출 계약 검증.

기존 제출 구조(README·SERVICE·evaluation 산출물·src/data/evaluation 디렉터리)와
FastAPI `POST /query` 의 `answer`·`contexts`·`trace` 응답 계약이 유지되는지 확인한다.
SQL 생성·SQL 검증·후보 탐색은 `POST /query`가 처리하고, 후보 진단은
`POST /candidates/diagnose`가 처리한다. `POST /query`는 토큰 유무와 관계없이 Supervisor가
라우팅하며, 보호 Agent의 전용 Tool 경계에서 인증한다.

LLM/DB 호출 없이 결정적으로 재현 가능한 경로(빈 입력, 변경 SQL 차단)만 사용한다.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
SERVICE = ROOT / "SERVICE.md"

CONTRACT_FIELDS = ("answer", "contexts", "trace")


@pytest.fixture(scope="module")
def readme_text() -> str:
    return README.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def service_text() -> str:
    return SERVICE.read_text(encoding="utf-8")


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from src.api import app

    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. 기존 제출 구조 유지
# ---------------------------------------------------------------------------

class TestSubmissionStructure:
    """미니 프로젝트 제출 규약의 파일·디렉터리 구조가 그대로 남아 있어야 한다."""

    @pytest.mark.parametrize(
        "relpath",
        [
            "README.md",
            "SERVICE.md",
            "run_eval.py",
            "requirements.txt",
            "docker-compose.yml",
            "static/index.html",
        ],
    )
    def test_required_files_exist(self, relpath):
        assert (ROOT / relpath).is_file(), f"제출 규약 파일 누락: {relpath}"

    @pytest.mark.parametrize("relpath", ["src", "data", "evaluation", "tests"])
    def test_required_directories_exist(self, relpath):
        assert (ROOT / relpath).is_dir(), f"제출 규약 디렉터리 누락: {relpath}"

    @pytest.mark.parametrize(
        "relpath",
        [
            "evaluation/test_queries.csv",
            "evaluation/candidate_fixtures.csv",
            "evaluation/round1_report.md",
            "evaluation/round2_report.md",
        ],
    )
    def test_evaluation_artifacts_exist(self, relpath):
        assert (ROOT / relpath).is_file(), f"평가 산출물 누락: {relpath}"

    def test_query_entrypoint_module_exists(self):
        assert (ROOT / "src" / "api.py").is_file(), "진입점 src/api.py 누락"


# ---------------------------------------------------------------------------
# 2. POST /query 응답 계약 (answer·contexts·trace) 유지
# ---------------------------------------------------------------------------

class TestQueryResponseContract:
    """기존 계약: POST /query {"question": str} -> answer / contexts / trace."""

    def test_query_route_registered(self, client):
        paths = {r.path for r in client.app.routes}
        assert "/query" in paths, "POST /query 라우트가 사라짐"

    def test_query_accepts_question_session_id_fields(self):
        """seed v2.6.0: /assist 흡수로 question 외에 session_id가 하위호환 확장으로 추가됐다 —
        기존처럼 question만 보내는 호출은 여전히 동일하게 동작한다. seed v2.7.0에서 sql_id는
        다시 /candidates/diagnose 전용 필드로 분리했다(자연어가 아닌 결정적 선택이라 /query와
        섞지 않는다)."""
        from src.api import QueryRequest

        assert set(QueryRequest.model_fields) == {"question", "session_id"}, (
            "POST /query 요청 계약은 question/session_id만 허용해야 한다"
        )
        # 기본값이 있어야 기존처럼 {"question": str}만 보내도 그대로 동작한다
        req = QueryRequest(question="hi")
        assert req.session_id == ""

    def test_candidates_diagnose_request_fields(self):
        """POST /candidates/diagnose는 sql_id(필수)/session_id(선택)만 받는다."""
        from src.api import CandidateDiagnoseRequest

        assert set(CandidateDiagnoseRequest.model_fields) == {"sql_id", "session_id"}
        req = CandidateDiagnoseRequest(sql_id="abc123")
        assert req.session_id == ""

    def test_query_does_not_require_approver_token(self, client):
        """기존 제출 계약 보존 — /query 는 토큰 없이도 계약대로 응답한다(401/403 아님)."""
        resp = client.post("/query", json={"question": ""})
        assert resp.status_code == 200, f"예상 200, 실제 {resp.status_code}"

    def test_empty_question_keeps_contract_fields(self, client):
        body = client.post("/query", json={"question": ""}).json()
        for field in CONTRACT_FIELDS:
            assert field in body, f"빈 질문 응답에 {field} 필드 누락"
        assert body["contexts"] == []
        assert body["trace"] == []

    def test_blocked_mutating_sql_keeps_contract_fields(self, client):
        body = client.post(
            "/query", json={"question": "UPDATE orders SET status = 'X' WHERE id = 1"}
        ).json()
        assert body.get("status") == "blocked", f"변경 SQL이 차단되지 않음: {body.get('status')}"
        for field in CONTRACT_FIELDS:
            assert field in body, f"차단 응답에 {field} 필드 누락"

    def test_contract_fields_have_expected_types(self, client):
        body = client.post("/query", json={"question": ""}).json()
        assert isinstance(body["answer"], str)
        assert isinstance(body["contexts"], list)
        assert isinstance(body["trace"], list)


# ---------------------------------------------------------------------------
# 3. /query가 흡수한 요청 유형들도 동일한 응답 계약을 따른다
# ---------------------------------------------------------------------------

class TestAbsorbedRequestTypesShareContract:
    """/query에 흡수된 SQL 생성·검증·후보 탐색·후보 진단도 answer·contexts·trace 계약을 공유한다."""

    @pytest.mark.parametrize(
        "func_name,arg",
        [
            ("run_business_requirement", ""),
            ("run_sql_validation", ""),
            ("run_candidate_search", ""),
            ("run_candidate_diagnose", ""),
        ],
    )
    def test_guard_paths_return_contract_fields(self, func_name, arg):
        import asyncio

        import src.pipeline as pipeline

        func = getattr(pipeline, func_name)
        body = asyncio.run(func(arg))
        for field in CONTRACT_FIELDS:
            assert field in body, f"{func_name} 응답에 {field} 필드 누락"
        assert "status" in body, f"{func_name} 응답에 status 필드 누락"

    def test_mutating_sql_validation_keeps_contract_fields(self):
        import asyncio

        from src.pipeline import run_sql_validation

        body = asyncio.run(run_sql_validation("DELETE FROM orders WHERE id = 1"))
        assert body.get("status") == "blocked"
        for field in CONTRACT_FIELDS:
            assert field in body, f"차단된 검증 응답에 {field} 필드 누락"


# ---------------------------------------------------------------------------
# 4. README·SERVICE·평가 산출물 문서 일관성
# ---------------------------------------------------------------------------

class TestDocumentationConsistency:
    """/query가 흡수한 요청 유형이 README·SERVICE·평가 리포트에 일관되게 기록되어야 한다."""

    def test_readme_documents_query_contract(self, readme_text):
        assert "/query" in readme_text, "README에 기존 /query 진입점 설명 없음"
        for field in CONTRACT_FIELDS:
            assert field in readme_text, f"README에 응답 계약 필드 {field} 설명 없음"

    def test_service_documents_query_contract(self, service_text):
        assert "/query" in service_text, "SERVICE.md에 기존 /query 진입점 설명 없음"
        for field in CONTRACT_FIELDS:
            assert field in service_text, f"SERVICE.md에 응답 계약 필드 {field} 설명 없음"

    @pytest.mark.parametrize("doc", ["README.md", "SERVICE.md"])
    def test_docs_drop_retired_paste_ban_policy(self, doc):
        """폐기된 'SQL 직접 입력 자체를 금지한다'는 취지의 정책 서술이 남아 있으면 안 된다.
        seed v2.4.0부터 POST /validate로 SQL 원문 직접 입력이 허용되므로, "SQL을 붙여넣지
        않는다/입력받지 않는다"류 문구 자체가 등장하면 안 된다(실행계획 원문 미입력 정책과는
        별개 — 그건 test_docs_keep_plan_input_ban이 확인한다)."""
        text = (ROOT / doc).read_text(encoding="utf-8")
        retired = [
            "SQL/실행계획 텍스트를 직접 붙여넣는 입력 경로는 없다",
            "SQL이나 실행계획을 직접 붙여넣지 않고",
            "SQL/실행계획을 직접 붙여넣지 않는다",
            "SQL/실행계획을 사용자가 직접 주는 입력 경로는 존재하지 않는다",
            "SQL/실행계획을 사용자가 직접 주는 경로는 없다",
            "붙여넣기 금지 정책 자체는 그대로 유지된다",
        ]
        for phrase in retired:
            assert phrase not in text, f"{doc}에 폐기된 정책 서술이 남아 있음: {phrase}"

    @pytest.mark.parametrize("doc", ["README.md", "SERVICE.md"])
    def test_docs_keep_plan_input_ban(self, doc):
        """실행계획 원문은 여전히 사용자 입력이 아니라 SQLcl MCP로 조회한다는 정책이 남아야 한다."""
        text = (ROOT / doc).read_text(encoding="utf-8")
        assert "실행계획 원문은" in text, f"{doc}에 실행계획 원문 미입력 정책 서술 없음"
        assert "SQLcl MCP" in text, f"{doc}에 SQLcl MCP 경계 서술 없음"

    @pytest.mark.parametrize("doc", ["README.md", "SERVICE.md"])
    def test_docs_do_not_reference_removed_query_subtypes(self, doc):
        """`POST /query (type=generate)` 같은 존재하지 않는 요청 계약 서술이 없어야 한다."""
        text = (ROOT / doc).read_text(encoding="utf-8")
        for phrase in ["type=generate", "type=validate", "type=diagnose"]:
            assert phrase not in text, f"{doc}에 실제 API와 불일치하는 서술: {phrase}"

    def test_round2_report_documents_request_types(self):
        text = (ROOT / "evaluation" / "round2_report.md").read_text(encoding="utf-8")
        assert "/query" in text, "round2_report.md에 /query 요청 유형 기록 없음"
        for field in CONTRACT_FIELDS:
            assert field in text, f"round2_report.md에 응답 계약 필드 {field} 기록 없음"

    def test_readme_documents_evaluation_artifacts(self, readme_text):
        for name in ["round1_report.md", "round2_report.md", "test_queries.csv", "candidate_fixtures.csv"]:
            assert name in readme_text, f"README에 평가 산출물 {name} 설명 없음"


# ---------------------------------------------------------------------------
# 5. 문서에 적힌 라우트가 실제 앱에 존재한다
# ---------------------------------------------------------------------------

class TestDocsMatchImplementation:
    """문서에 기록한 요청 유형이 실제 FastAPI 앱 라우트와 일치해야 한다."""

    def test_all_documented_routes_exist_in_app(self, client, readme_text):
        paths = {r.path for r in client.app.routes}
        documented = ["/query", "/candidates/diagnose", "/actions/apply"]
        for path in documented:
            assert path in readme_text, f"README에 {path} 설명 없음"
            assert path in paths, f"README가 설명한 {path} 라우트가 앱에 없음"
        assert "/assist" not in paths, "폐기된 /assist 라우트가 앱에 남아 있음"

    def test_sql_id_field_documented(self, readme_text):
        assert '"sql_id"' in readme_text, "README에 /candidates/diagnose의 sql_id 요청 본문 필드 설명 없음"
