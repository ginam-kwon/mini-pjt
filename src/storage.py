# storage.py - SQLite-based persistence for session checkpoints and long-term memory
#
# checkpoints.sqlite: 현재 세션의 후보·선택·계획 단계 상태를 저장한다.
# memory.sqlite: 검증 이력, SQL 버전, 성능 기준선, 비즈니스 용어, 피드백을 영속 저장한다.
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

CHECKPOINTS_DB = Path("checkpoints.sqlite")
MEMORY_DB = Path("memory.sqlite")


class CheckpointStore:
    """현재 세션 요청 상태를 checkpoints.sqlite에 저장한다.
    저장 대상: 후보 목록, 사용자 선택, 계획 단계.
    """

    def __init__(self, db_path: Path = CHECKPOINTS_DB):
        self.db_path = Path(db_path)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    session_id TEXT NOT NULL,
                    step       TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (session_id, step)
                )
                """
            )

    def save(self, session_id: str, step: str, state: dict) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO checkpoints (session_id, step, state_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    session_id,
                    step,
                    json.dumps(state, ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def load(self, session_id: str, step: str) -> dict | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT state_json FROM checkpoints WHERE session_id = ? AND step = ?",
                (session_id, step),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def load_latest(self, session_id: str) -> dict | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT step, state_json FROM checkpoints
                WHERE session_id = ? ORDER BY created_at DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        if row:
            return {"step": row[0], "state": json.loads(row[1])}
        return None

    def list_steps(self, session_id: str) -> list[str]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT step FROM checkpoints WHERE session_id = ? ORDER BY created_at",
                (session_id,),
            ).fetchall()
        return [r[0] for r in rows]


class MemoryStore:
    """세션 간 장기 기억을 memory.sqlite에 영속 저장한다.
    저장 대상: 검증 이력, SQL 버전, 성능 기준선, 비즈니스 용어, 피드백.
    """

    def __init__(self, db_path: Path = MEMORY_DB):
        self.db_path = Path(db_path)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS verification_history (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    sql_fingerprint TEXT    NOT NULL,
                    query_name      TEXT,
                    result          TEXT    NOT NULL,
                    review_id       TEXT,
                    reviewed_at     TEXT    NOT NULL,
                    reviewer        TEXT,
                    note            TEXT
                );

                CREATE TABLE IF NOT EXISTS sql_versions (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    sql_fingerprint TEXT    NOT NULL,
                    version         INTEGER NOT NULL DEFAULT 1,
                    masked_sql      TEXT    NOT NULL,
                    created_at      TEXT    NOT NULL,
                    UNIQUE(sql_fingerprint, version)
                );

                CREATE TABLE IF NOT EXISTS performance_baselines (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    sql_fingerprint TEXT    NOT NULL UNIQUE,
                    plan_hash       TEXT,
                    cost_estimate   REAL,
                    rows_estimate   INTEGER,
                    baseline_plan   TEXT,
                    captured_at     TEXT    NOT NULL
                );

                CREATE TABLE IF NOT EXISTS business_terms (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    term        TEXT NOT NULL UNIQUE,
                    definition  TEXT NOT NULL,
                    domain      TEXT,
                    created_at  TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS feedback (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id      TEXT,
                    sql_fingerprint TEXT,
                    feedback_type   TEXT NOT NULL,
                    content         TEXT NOT NULL,
                    created_at      TEXT NOT NULL
                );
                """
            )

    # --- verification_history ---

    def save_verification(self, sql_fingerprint: str, result: str, **kwargs) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO verification_history
                    (sql_fingerprint, result, reviewed_at, query_name, review_id, reviewer, note)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sql_fingerprint,
                    result,
                    datetime.now(timezone.utc).isoformat(),
                    kwargs.get("query_name"),
                    kwargs.get("review_id"),
                    kwargs.get("reviewer"),
                    kwargs.get("note"),
                ),
            )
            return cur.lastrowid

    def get_verification_history(self, sql_fingerprint: str) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, sql_fingerprint, query_name, result, review_id, reviewed_at, reviewer, note
                FROM verification_history
                WHERE sql_fingerprint = ? ORDER BY reviewed_at DESC
                """,
                (sql_fingerprint,),
            ).fetchall()
        return [
            {
                "id": r[0], "sql_fingerprint": r[1], "query_name": r[2],
                "result": r[3], "review_id": r[4], "reviewed_at": r[5],
                "reviewer": r[6], "note": r[7],
            }
            for r in rows
        ]

    # --- sql_versions ---

    def save_sql_version(self, sql_fingerprint: str, masked_sql: str) -> int:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT MAX(version) FROM sql_versions WHERE sql_fingerprint = ?",
                (sql_fingerprint,),
            ).fetchone()
            next_version = (row[0] or 0) + 1
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO sql_versions (sql_fingerprint, version, masked_sql, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    sql_fingerprint,
                    next_version,
                    masked_sql,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            return cur.lastrowid

    def get_sql_versions(self, sql_fingerprint: str) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, sql_fingerprint, version, masked_sql, created_at
                FROM sql_versions WHERE sql_fingerprint = ? ORDER BY version
                """,
                (sql_fingerprint,),
            ).fetchall()
        return [
            {
                "id": r[0], "sql_fingerprint": r[1], "version": r[2],
                "masked_sql": r[3], "created_at": r[4],
            }
            for r in rows
        ]

    # --- performance_baselines ---

    def save_performance_baseline(self, sql_fingerprint: str, **kwargs) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO performance_baselines
                    (sql_fingerprint, plan_hash, cost_estimate, rows_estimate, baseline_plan, captured_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    sql_fingerprint,
                    kwargs.get("plan_hash"),
                    kwargs.get("cost_estimate"),
                    kwargs.get("rows_estimate"),
                    kwargs.get("baseline_plan"),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def get_performance_baseline(self, sql_fingerprint: str) -> dict | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT sql_fingerprint, plan_hash, cost_estimate, rows_estimate, baseline_plan, captured_at
                FROM performance_baselines WHERE sql_fingerprint = ?
                """,
                (sql_fingerprint,),
            ).fetchone()
        if row:
            return {
                "sql_fingerprint": row[0], "plan_hash": row[1],
                "cost_estimate": row[2], "rows_estimate": row[3],
                "baseline_plan": row[4], "captured_at": row[5],
            }
        return None

    # --- business_terms ---

    def save_business_term(self, term: str, definition: str, domain: str | None = None) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO business_terms (term, definition, domain, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (term, definition, domain, datetime.now(timezone.utc).isoformat()),
            )

    def get_business_term(self, term: str) -> dict | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT term, definition, domain, created_at FROM business_terms WHERE term = ?",
                (term,),
            ).fetchone()
        if row:
            return {"term": row[0], "definition": row[1], "domain": row[2], "created_at": row[3]}
        return None

    # --- feedback ---

    def save_feedback(self, feedback_type: str, content: str, **kwargs) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO feedback (session_id, sql_fingerprint, feedback_type, content, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    kwargs.get("session_id"),
                    kwargs.get("sql_fingerprint"),
                    feedback_type,
                    content,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            return cur.lastrowid

    def get_feedback(
        self,
        sql_fingerprint: str | None = None,
        session_id: str | None = None,
    ) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            if sql_fingerprint:
                rows = conn.execute(
                    """
                    SELECT id, session_id, sql_fingerprint, feedback_type, content, created_at
                    FROM feedback WHERE sql_fingerprint = ? ORDER BY created_at DESC
                    """,
                    (sql_fingerprint,),
                ).fetchall()
            elif session_id:
                rows = conn.execute(
                    """
                    SELECT id, session_id, sql_fingerprint, feedback_type, content, created_at
                    FROM feedback WHERE session_id = ? ORDER BY created_at DESC
                    """,
                    (session_id,),
                ).fetchall()
            else:
                rows = []
        return [
            {
                "id": r[0], "session_id": r[1], "sql_fingerprint": r[2],
                "feedback_type": r[3], "content": r[4], "created_at": r[5],
            }
            for r in rows
        ]
