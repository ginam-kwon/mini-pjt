# auth.py - X-Approver-Token 검증 + 마스킹된 감사 기록
#
# 정책: 누락 → 401, 불일치 → 403, 성공 → 감사 기록 저장 후 진행
# SQLcl MCP 호출과 SQLite 쓰기는 토큰 검증 이후에만 발생한다.
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader

APPROVER_TOKEN_ENV = "APPROVER_TOKEN"

# Swagger UI의 Authorize 버튼에 노출되는 보안 스키마. auto_error=False로 두어 누락 시 FastAPI가
# 곧바로 403을 던지지 않고, verify_approver_token이 401/403을 정책대로 구분해 던지게 한다.
approver_token_scheme = APIKeyHeader(
    name="X-Approver-Token",
    auto_error=False,
    description="보호된 작업(SQL 생성/검증, 후보 진단, 계획 승인, 피드백)에 필요한 승인자 토큰.",
)
AUDIT_DB = Path("checkpoints.sqlite")

_AUDIT_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_hash   TEXT    NOT NULL,
    action      TEXT    NOT NULL,
    result      TEXT    NOT NULL,
    extra_json  TEXT,
    created_at  TEXT    NOT NULL
)
"""


def _get_expected_token() -> str:
    token = os.environ.get(APPROVER_TOKEN_ENV, "").strip()
    if not token:
        token = "demo-approver-token"
    return token


def _mask_token(token: str) -> str:
    """토큰을 SHA-256 앞 8자리로 마스킹한다 — 원문을 저장·노출하지 않는다."""
    return hashlib.sha256(token.encode()).hexdigest()[:8]


def _ensure_audit_table(db_path: Path) -> None:
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(_AUDIT_TABLE_DDL)
    except Exception:
        pass


def write_audit(user_hash: str, action: str, result: str, extra: dict | None = None) -> None:
    """감사 기록을 checkpoints.sqlite에 저장한다. 실패해도 API 흐름을 막지 않는다."""
    try:
        _ensure_audit_table(AUDIT_DB)
        with sqlite3.connect(AUDIT_DB) as conn:
            conn.execute(
                """
                INSERT INTO audit_log (user_hash, action, result, extra_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    user_hash,
                    action,
                    result,
                    json.dumps(extra, ensure_ascii=False) if extra else None,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
    except Exception:
        pass


def verify_approver_token(x_approver_token: str | None = Depends(approver_token_scheme)) -> str:
    """FastAPI Depends: X-Approver-Token 헤더를 검증하고 마스킹된 사용자 식별자를 반환한다.

    - 헤더 없음 → 401
    - 토큰 불일치 → 403
    - 일치 → 마스킹된 사용자 식별자 (원문 토큰은 절대 반환하지 않음)
    """
    status, value = check_approver_token(x_approver_token)
    if status == "missing":
        raise HTTPException(
            status_code=401,
            detail="X-Approver-Token 헤더가 필요합니다.",
        )
    if status == "invalid":
        raise HTTPException(
            status_code=403,
            detail="유효하지 않은 X-Approver-Token입니다.",
        )
    return value


def check_approver_token(token: str | None) -> tuple[str, str]:
    """Agent 정책 게이트용 비예외 인증 검사.

    Supervisor는 어떤 Agent가 필요한지만 고르고, 보호 Agent의 Tool이 실제 작업을 시작하기
    직전에 이 함수를 쓴다. 이 단계는 DB/MCP/SQLite에 접근하지 않는다.
    """
    if token is None:
        return "missing", ""
    if token != _get_expected_token():
        return "invalid", ""
    return "ok", _mask_token(token)
