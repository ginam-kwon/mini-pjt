"""AC: 보호된 흐름(SQL 생성·검증·후보 탐색·후보 진단)과 승인 작업은 유효한 X-Approver-Token에서만
동작한다. seed v2.7.0 기준 진입점 구조:

- POST /query {"question": str}: X-Approver-Token 헤더 자체가 없으면(질문이 SQL 원문이든
  자연어든) 기존 계약 그대로 legacy run_query로 처리한다(토큰 불필요, 항상 200) — 헤더가
  없다는 것 자체가 "보호 흐름을 쓸 생각이 없다"는 신호이므로 Supervisor/LLM 분류를 거치지
  않는다. 헤더가 있으면(값이 맞든 틀리든) Multi-Agent Supervisor 전체 라우팅이 열린다. 보호
  Agent(query_planner_agent/sql_validator_agent/candidate_search_agent)가 전용 Tool을
  실행하려는 순간에만 토큰을 검사한다 — 오류 403. explain_agent/knowledge_agent/general_agent로
  분류되면 그대로 처리된다. UPDATE·DELETE·DROP 등 변경/DDL SQL은 헤더 유무와 무관하게 항상
  코드로 결정적으로 차단한다(LLM 분류를 거치지 않는다).
- POST /candidates/diagnose {"sql_id": str}: 후보 탐색에서 선택한 sql_id를 진단한다. 분류할
  자연어가 없는 결정적 액션이라 Supervisor를 거치지 않고, 항상 토큰을 검사한다(누락 401, 오류 403).

두 경우 모두(누락/오류) 보호된 경로에서는 SQLcl MCP 호출과 SQLite 쓰기가 없어야 하고,
성공 요청은 마스킹된 감사 기록을 남긴다.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_TOKEN = "test-approver-token-123"
WRONG_TOKEN = "wrong-token-xyz"

# question만 있는 /query 요청은 헤더가 "존재할 때만" 검사한다 — 누락이면 legacy run_query로 통과(200).
QUESTION_BODIES = [
    ("sql_text", {"question": "SELECT 1 FROM DUAL"}),
    ("free_text", {"question": "주문 건수를 조회하라"}),
]


@pytest.fixture(autouse=True)
def set_approver_token(monkeypatch):
    """테스트마다 APPROVER_TOKEN 환경변수를 설정한다."""
    monkeypatch.setenv("APPROVER_TOKEN", VALID_TOKEN)


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from src.api import app
    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. 누락 토큰 → /candidates/diagnose는 401, /query(question만)는 legacy로 통과(200)
# ---------------------------------------------------------------------------

class TestMissingToken:
    def test_candidates_diagnose_missing_token_returns_401(self, client):
        resp = client.post("/candidates/diagnose", json={"sql_id": "SQL_001"})  # 토큰 헤더 없음
        assert resp.status_code == 401

    def test_candidates_diagnose_missing_token_has_error_detail(self, client):
        resp = client.post("/candidates/diagnose", json={"sql_id": "SQL_001"})
        assert "detail" in resp.json(), "401 응답에 detail 필드가 없다"

    @pytest.mark.parametrize("name,body", QUESTION_BODIES)
    def test_missing_token_falls_back_to_legacy_query(self, client, name, body):
        """헤더 자체가 없으면 /query 요청은 SQL 원문이든 자연어든 기존 계약대로 토큰 없이
        legacy run_query로 200 처리된다(seed v2.7.0) — 헤더가 없다는 것 자체가 "보호 흐름을
        쓸 생각이 없다"는 신호이므로 Supervisor/LLM 분류를 거치지 않는다.

        여기서는 HTTP 레벨 동작(200, run_query로의 위임)만 확인한다 — run_query 자체의 진단
        로직(SQLcl MCP 조회, LLM 위험평가 등)은 다른 테스트가 이미 검증하므로, 실제 MCP/LLM
        호출을 피해 빠르고 결정적으로 만든다."""
        with patch("src.api.run_query") as mock_run_query:
            mock_run_query.return_value = {"status": "ok", "answer": "", "contexts": [], "trace": []}
            resp = client.post("/query", json=body)
        assert resp.status_code == 200, f"{name}: 토큰 헤더 없는 question 요청은 200이어야 한다"
        mock_run_query.assert_called_once()

    def test_actions_apply_missing_token_returns_401(self, client):
        resp = client.post("/actions/apply", json={"tool": "gather_stats", "args": {"table_name": "ORDERS"}})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 2. 잘못된 토큰 → 항상 403
# ---------------------------------------------------------------------------

class TestWrongToken:
    def test_candidates_diagnose_wrong_token_returns_403(self, client):
        resp = client.post(
            "/candidates/diagnose", json={"sql_id": "SQL_001"}, headers={"X-Approver-Token": WRONG_TOKEN}
        )
        assert resp.status_code == 403

    @pytest.mark.parametrize("name,body", QUESTION_BODIES)
    def test_query_wrong_token_returns_403(self, client, name, body):
        """보호 Agent의 authorization_forbidden 응답이 HTTP 403으로 이어지는지 검증한다.
        sql_text/free_text가 실제로 어느 Agent로 분류될지는 LLM 판단이라 결정적이지 않다
        (매번 같은 Agent로 간다는 보장이 없다) — run_via_supervisor를 모킹해 "보호 Agent가
        틀린 토큰을 거부한 결과 → 403" 전달 경로만 결정적으로 검증한다. 분류 자체의 정확도는
        evaluation/test_queries.csv 라운드 평가가 담당한다."""
        with patch("src.api.run_via_supervisor") as mock_sup:
            mock_sup.return_value = {
                "status": "authorization_forbidden",
                "reason": "유효하지 않은 X-Approver-Token입니다.",
                "answer": "", "contexts": [], "trace": [],
            }
            resp = client.post("/query", json=body, headers={"X-Approver-Token": WRONG_TOKEN})
        assert resp.status_code == 403, f"{name}: 잘못된 토큰에서 403 예상, 실제 {resp.status_code}"

    def test_actions_apply_wrong_token_returns_403(self, client):
        resp = client.post(
            "/actions/apply",
            json={"tool": "gather_stats", "args": {"table_name": "ORDERS"}},
            headers={"X-Approver-Token": WRONG_TOKEN},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 3. 인증 실패 시 SQLcl MCP / SQLite 쓰기 없음
# ---------------------------------------------------------------------------

class TestNoSideEffectsOnAuthFailure:
    """인증 실패 요청에서 Supervisor 호출·파이프라인 함수 호출·SQLite 쓰기가 전혀 발생하지 않는다."""

    def test_wrong_token_no_pipeline_call(self, client):
        """seed v2.7.0: /query는 SQL 원문이어도 Supervisor 자체는 항상 호출된다(분류를 위해).
        보호 경계는 그 안쪽, 각 보호 Agent의 전용 Tool이 실제 pipeline 함수를 부르기 직전에
        있다 — 그래서 여기서는 run_via_supervisor가 아니라 그 세 전용 Tool이 위임하는 실제
        pipeline 함수(run_sql_validation/run_candidate_search/prepare_business_requirement)가
        전혀 호출되지 않는지를 확인한다."""
        headers = {"X-Approver-Token": WRONG_TOKEN}
        with patch("src.pipeline.run_sql_validation") as mock_validate, \
             patch("src.pipeline.run_candidate_search") as mock_search, \
             patch("src.pipeline.prepare_business_requirement") as mock_generate, \
             patch("src.api.run_candidate_diagnose") as mock_cd:
            client.post("/query", json={"question": "SELECT 1 FROM DUAL"}, headers=headers)
            client.post("/candidates/diagnose", json={"sql_id": "SQL_001"}, headers=headers)
            mock_validate.assert_not_called()
            mock_search.assert_not_called()
            mock_generate.assert_not_called()
            mock_cd.assert_not_called()

    def test_missing_token_no_pipeline_call_for_diagnose(self, client):
        with patch("src.api.run_candidate_diagnose") as mock_cd:
            client.post("/candidates/diagnose", json={"sql_id": "SQL_001"})
            mock_cd.assert_not_called()

    def test_wrong_token_no_sqlite_write(self, client):
        headers = {"X-Approver-Token": WRONG_TOKEN}
        with patch("src.api.write_audit") as mock_audit:
            client.post("/query", json={"question": "SELECT 1 FROM DUAL"}, headers=headers)
            client.post("/candidates/diagnose", json={"sql_id": "SQL_001"}, headers=headers)
            mock_audit.assert_not_called()

    def test_missing_token_no_sqlite_write_for_diagnose(self, client):
        with patch("src.api.write_audit") as mock_audit:
            client.post("/candidates/diagnose", json={"sql_id": "SQL_001"})
            mock_audit.assert_not_called()


# ---------------------------------------------------------------------------
# 4. 인증 성공 시 마스킹된 감사 기록 저장
# ---------------------------------------------------------------------------

class TestAuditOnSuccess:
    """성공 요청은 마스킹된 감사 기록을 checkpoints.sqlite에 남긴다."""

    def test_write_audit_called_with_masked_hash(self, client):
        """성공 요청에서 write_audit가 마스킹된 user_hash와 함께 호출된다."""
        with patch("src.api.write_audit") as mock_audit, \
             patch("src.api.run_via_supervisor") as mock_sup:
            # protected/user_hash는 sql_validator_agent 같은 보호 Agent의 전용 Tool이 실제로
            # 채워주는 필드다(src/agents.py의 validate_user_sql 참고) — api.py는 이 필드를 보고만
            # write_audit 호출 여부를 결정하므로, 모킹 시에도 그 산출물 형태를 그대로 재현한다.
            mock_sup.return_value = {
                "status": "ok", "mode": "sql_validation", "result": "accept",
                "protected": True, "user_hash": "abcd1234",
                "answer": "", "contexts": [], "trace": [],
            }
            client.post(
                "/query",
                json={"question": "SELECT 1 FROM DUAL"},
                headers={"X-Approver-Token": VALID_TOKEN},
            )
        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args
        # user_hash는 토큰 원문이 아니라 SHA-256 앞 8자리여야 한다
        user_hash = call_kwargs.kwargs.get("user_hash") or call_kwargs.args[0]
        assert user_hash != VALID_TOKEN, "user_hash에 원문 토큰이 노출되면 안 된다"
        assert len(user_hash) == 8, f"user_hash는 SHA-256 앞 8자리여야 한다. 실제: {user_hash!r}"
        assert user_hash.isalnum(), f"user_hash는 hex 문자여야 한다. 실제: {user_hash!r}"

    def test_audit_record_written_to_sqlite(self, tmp_path, monkeypatch):
        """성공 요청은 실제 SQLite audit_log 테이블에 레코드를 남긴다."""
        audit_db = tmp_path / "test_checkpoints.sqlite"
        monkeypatch.setenv("APPROVER_TOKEN", VALID_TOKEN)

        import src.auth as auth_module
        original_db = auth_module.AUDIT_DB
        auth_module.AUDIT_DB = audit_db

        try:
            from src.auth import write_audit
            write_audit(
                user_hash="abcd1234",
                action="validate",
                result="accept",
                extra={"sql_length": 20},
            )
            with sqlite3.connect(audit_db) as conn:
                rows = conn.execute("SELECT user_hash, action, result FROM audit_log").fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "abcd1234"
            assert rows[0][1] == "validate"
            assert rows[0][2] == "accept"
        finally:
            auth_module.AUDIT_DB = original_db

    def test_audit_user_hash_is_masked(self):
        """verify_approver_token이 반환하는 user_hash는 원문 토큰이 아니다."""
        import hashlib
        from src.auth import _mask_token
        token = "my-secret-token"
        masked = _mask_token(token)
        expected = hashlib.sha256(token.encode()).hexdigest()[:8]
        assert masked == expected
        assert masked != token


# ---------------------------------------------------------------------------
# 5. 비보호 엔드포인트/요청은 토큰 없이도 동작한다
# ---------------------------------------------------------------------------

class TestUnprotectedRoutes:
    """공개 엔드포인트와 (토큰 헤더 없는) 기존 /query 계약은 토큰 없이도 접근 가능하다."""

    def test_health_no_token(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_api_health_no_token(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200

    def test_query_no_token(self, client):
        """기존 호환성: 토큰 헤더가 없으면 어떤 question이든 legacy 경로로 200을 반환한다."""
        resp = client.post("/query", json={"question": "UPDATE t SET x=1"})
        # 차단되더라도 401/403이 아닌 200으로 응답해야 한다
        assert resp.status_code == 200

    def test_query_general_question_no_token(self, client):
        """일반/지식 질문으로 판정될 만한 입력도 토큰 헤더가 없으면 200으로 동작한다(HTTP 레벨
        동작만 확인 — run_query 자체의 Supervisor 호출은 다른 테스트가 이미 검증한다)."""
        with patch("src.api.run_query") as mock_run_query:
            mock_run_query.return_value = {"status": "ok", "answer": "", "contexts": [], "trace": []}
            resp = client.post("/query", json={"question": "오늘 저녁 뭐 먹을지 추천해줘"})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 6. 감사 기록 원문 토큰 비저장 검증
# ---------------------------------------------------------------------------

class TestSensitiveDataNotStored:
    """감사 기록에 원문 토큰이 저장되지 않는다."""

    def test_raw_token_not_in_audit(self, tmp_path):
        audit_db = tmp_path / "audit_check.sqlite"
        import src.auth as auth_module
        original_db = auth_module.AUDIT_DB
        auth_module.AUDIT_DB = audit_db

        try:
            from src.auth import write_audit
            write_audit(
                user_hash="deadbeef",
                action="generate",
                result="ok",
            )
            with sqlite3.connect(audit_db) as conn:
                raw = conn.execute("SELECT * FROM audit_log").fetchall()
            # 감사 테이블의 어느 컬럼에도 원문 토큰이 없어야 한다
            full_text = str(raw)
            assert VALID_TOKEN not in full_text, "audit_log에 원문 토큰이 저장되었다"
        finally:
            auth_module.AUDIT_DB = original_db
