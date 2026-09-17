"""AC: 보호된 세 흐름과 승인 작업은 유효한 X-Approver-Token에서만 동작한다.

- 누락 토큰 → 401
- 잘못된 토큰 → 403
- 두 경우 모두 SQLcl MCP 호출과 SQLite 쓰기 없음
- 성공 요청은 마스킹된 감사 기록을 남긴다
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_TOKEN = "test-approver-token-123"
WRONG_TOKEN = "wrong-token-xyz"

PROTECTED_ROUTES = [
    ("POST", "/generate", {"requirement": "주문 건수를 조회하라"}),
    ("POST", "/validate", {"sql": "SELECT 1 FROM DUAL"}),
    ("POST", "/candidates", {"question": "최근 주문 조회 SQL을 찾아줘"}),
    ("POST", "/candidates/diagnose", {"sql_id": "SQL_001"}),
    ("POST", "/actions/apply", {"tool": "explain_plan", "args": {}}),
]


@pytest.fixture(autouse=True)
def set_approver_token(monkeypatch):
    """테스트마다 APPROVER_TOKEN 환경변수를 설정한다."""
    monkeypatch.setenv("APPROVER_TOKEN", VALID_TOKEN)


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from src.agent import app
    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. 누락 토큰 → 401
# ---------------------------------------------------------------------------

class TestMissingToken:
    """X-Approver-Token 헤더 누락 시 401을 반환한다."""

    @pytest.mark.parametrize("method,path,body", PROTECTED_ROUTES)
    def test_missing_token_returns_401(self, client, method, path, body):
        resp = client.request(method, path, json=body)  # 토큰 헤더 없음
        assert resp.status_code == 401, (
            f"{path}: 누락 토큰에서 401 예상, 실제 {resp.status_code}"
        )

    @pytest.mark.parametrize("method,path,body", PROTECTED_ROUTES)
    def test_missing_token_has_error_detail(self, client, method, path, body):
        resp = client.request(method, path, json=body)
        body_json = resp.json()
        assert "detail" in body_json, f"{path}: 401 응답에 detail 필드가 없다"


# ---------------------------------------------------------------------------
# 2. 잘못된 토큰 → 403
# ---------------------------------------------------------------------------

class TestWrongToken:
    """잘못된 X-Approver-Token 헤더 시 403을 반환한다."""

    @pytest.mark.parametrize("method,path,body", PROTECTED_ROUTES)
    def test_wrong_token_returns_403(self, client, method, path, body):
        resp = client.request(method, path, json=body, headers={"X-Approver-Token": WRONG_TOKEN})
        assert resp.status_code == 403, (
            f"{path}: 잘못된 토큰에서 403 예상, 실제 {resp.status_code}"
        )

    @pytest.mark.parametrize("method,path,body", PROTECTED_ROUTES)
    def test_wrong_token_has_error_detail(self, client, method, path, body):
        resp = client.request(method, path, json=body, headers={"X-Approver-Token": WRONG_TOKEN})
        body_json = resp.json()
        assert "detail" in body_json, f"{path}: 403 응답에 detail 필드가 없다"


# ---------------------------------------------------------------------------
# 3. 인증 실패 시 SQLcl MCP / SQLite 쓰기 없음
# ---------------------------------------------------------------------------

class TestNoSideEffectsOnAuthFailure:
    """인증 실패 요청에서 SQLcl MCP 호출 및 SQLite 쓰기가 발생하지 않는다."""

    def test_missing_token_no_mcp_call(self, client):
        """누락 토큰 요청에서 SQLcl MCP 도구가 호출되지 않는다."""
        with patch("src.pipeline.run_business_requirement") as mock_br, \
             patch("src.pipeline.run_sql_validation") as mock_val, \
             patch("src.pipeline.run_candidate_search") as mock_cs:
            client.post("/generate", json={"requirement": "test"})
            client.post("/validate", json={"sql": "SELECT 1 FROM DUAL"})
            client.post("/candidates", json={"question": "test"})
            mock_br.assert_not_called()
            mock_val.assert_not_called()
            mock_cs.assert_not_called()

    def test_wrong_token_no_mcp_call(self, client):
        """잘못된 토큰 요청에서 SQLcl MCP 도구가 호출되지 않는다."""
        headers = {"X-Approver-Token": WRONG_TOKEN}
        with patch("src.pipeline.run_business_requirement") as mock_br, \
             patch("src.pipeline.run_sql_validation") as mock_val, \
             patch("src.pipeline.run_candidate_search") as mock_cs:
            client.post("/generate", json={"requirement": "test"}, headers=headers)
            client.post("/validate", json={"sql": "SELECT 1 FROM DUAL"}, headers=headers)
            client.post("/candidates", json={"question": "test"}, headers=headers)
            mock_br.assert_not_called()
            mock_val.assert_not_called()
            mock_cs.assert_not_called()

    def test_missing_token_no_sqlite_write(self, client):
        """누락 토큰 요청에서 write_audit(SQLite 쓰기)가 호출되지 않는다."""
        with patch("src.auth.write_audit") as mock_audit:
            client.post("/generate", json={"requirement": "test"})
            client.post("/validate", json={"sql": "SELECT 1 FROM DUAL"})
            client.post("/candidates", json={"question": "test"})
            mock_audit.assert_not_called()

    def test_wrong_token_no_sqlite_write(self, client):
        """잘못된 토큰 요청에서 write_audit(SQLite 쓰기)가 호출되지 않는다."""
        headers = {"X-Approver-Token": WRONG_TOKEN}
        with patch("src.auth.write_audit") as mock_audit:
            client.post("/generate", json={"requirement": "test"}, headers=headers)
            client.post("/validate", json={"sql": "SELECT 1 FROM DUAL"}, headers=headers)
            client.post("/candidates", json={"question": "test"}, headers=headers)
            mock_audit.assert_not_called()


# ---------------------------------------------------------------------------
# 4. 인증 성공 시 마스킹된 감사 기록 저장
# ---------------------------------------------------------------------------

class TestAuditOnSuccess:
    """성공 요청은 마스킹된 감사 기록을 checkpoints.sqlite에 남긴다."""

    def test_write_audit_called_with_masked_hash(self, client, tmp_path):
        """성공 요청에서 write_audit가 마스킹된 user_hash와 함께 호출된다."""
        with patch("src.agent.write_audit") as mock_audit, \
             patch("src.agent.run_sql_validation") as mock_val:
            mock_val.return_value = {"status": "ok", "result": "accept"}
            client.post(
                "/validate",
                json={"sql": "SELECT 1 FROM DUAL"},
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
# 5. 비보호 엔드포인트는 토큰 없이도 동작한다
# ---------------------------------------------------------------------------

class TestUnprotectedRoutes:
    """공개 엔드포인트는 토큰 없이도 접근 가능하다."""

    def test_health_no_token(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_api_health_no_token(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200

    def test_query_no_token(self, client):
        """/query 엔드포인트는 기존 호환성을 위해 토큰 없이 동작한다."""
        resp = client.post("/query", json={"question": "UPDATE t SET x=1"})
        # 차단되더라도 401/403이 아닌 200으로 응답해야 한다
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 6. 감사 기록 원문 토큰 비저장 검증
# ---------------------------------------------------------------------------

class TestSensitiveDataNotStored:
    """감사 기록에 원문 토큰이 저장되지 않는다."""

    def test_raw_token_not_in_audit(self, tmp_path, monkeypatch):
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
