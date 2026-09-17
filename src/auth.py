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

from fastapi import Header, HTTPException

APPROVER_TOKEN_ENV = "APPROVER_TOKEN"
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


def verify_approver_token(x_approver_token: str | None = Header(default=None)) -> str:
    """FastAPI Depends: X-Approver-Token 헤더를 검증하고 마스킹된 사용자 식별자를 반환한다.

    - 헤더 없음 → 401
    - 토큰 불일치 → 403
    - 일치 → 마스킹된 사용자 식별자 (원문 토큰은 절대 반환하지 않음)
    """
    if x_approver_token is None:
        raise HTTPException(
            status_code=401,
            detail="X-Approver-Token 헤더가 필요합니다.",
        )
    expected = _get_expected_token()
    if x_approver_token != expected:
        raise HTTPException(
            status_code=403,
            detail="유효하지 않은 X-Approver-Token입니다.",
        )
    return _mask_token(x_approver_token)
