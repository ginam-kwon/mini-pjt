# test_sensitive_data_masking.py
#
# AC: 이메일, 전화번호, 사번, 비밀번호와 SQL 리터럴을 포함한 fixture가
#     UI·API·LLM payload·trace·로그·두 SQLite 저장소 어느 곳에도 원문으로 남지 않고
#     후보 SQL 구조는 읽을 수 있다.
from __future__ import annotations
import asyncio

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

from src.guardrails import PII_PATTERNS, has_pii, mask_pii
from src.tools import QUERY_CATALOG, _mask_sql_literals, search_sql_candidates

# -------------------------------------------------------------------
# Fixtures with sensitive data
# -------------------------------------------------------------------
SENSITIVE_EMAIL = "secret.user@example.com"
SENSITIVE_PHONE = "010-1234-5678"
SENSITIVE_EMP_ID = "E123456"
SENSITIVE_PASSWORD_STMT = "password='s3cr3tP@ss'"

SQL_WITH_EMAIL_LITERAL = f"SELECT * FROM users WHERE email = '{SENSITIVE_EMAIL}'"
SQL_WITH_PHONE_LITERAL = f"SELECT * FROM contacts WHERE phone = '{SENSITIVE_PHONE}'"
SQL_WITH_EMP_LITERAL = f"SELECT * FROM payments WHERE ref_id = '{SENSITIVE_EMP_ID}'"
SQL_WITH_NUMBER_LITERAL = "SELECT * FROM orders WHERE customer_id = 7777 AND amount > 1000"


# ==================================================================
# 1. mask_pii() – PII 패턴 커버리지
# ==================================================================

def test_pii_patterns_cover_required_types():
    """PII_PATTERNS 딕셔너리가 이메일·전화·사번·비밀번호를 모두 포함한다."""
    assert "email" in PII_PATTERNS
    assert "phone" in PII_PATTERNS
    assert "emp_id" in PII_PATTERNS
    assert "conn_password" in PII_PATTERNS


def test_mask_pii_email():
    """이메일 주소가 mask_pii()로 마스킹된다."""
    text = f"담당자 이메일 {SENSITIVE_EMAIL} 확인"
    masked = mask_pii(text)
    assert SENSITIVE_EMAIL not in masked
    assert "MASKED" in masked


def test_mask_pii_phone():
    """한국 휴대전화 번호가 mask_pii()로 마스킹된다."""
    text = f"연락처 {SENSITIVE_PHONE} 문자 요청"
    masked = mask_pii(text)
    assert SENSITIVE_PHONE not in masked
    assert "MASKED" in masked


def test_mask_pii_emp_id():
    """사번(E+6자리)이 mask_pii()로 마스킹된다."""
    text = f"사번 {SENSITIVE_EMP_ID} 의 결제 내역 조회"
    masked = mask_pii(text)
    assert SENSITIVE_EMP_ID not in masked
    assert "MASKED" in masked


def test_mask_pii_password():
    """password= 형식의 비밀번호가 mask_pii()로 마스킹된다."""
    text = f"연결 설정: {SENSITIVE_PASSWORD_STMT}"
    masked = mask_pii(text)
    # 비밀번호 값 's3cr3tP@ss'가 평문으로 남으면 안 된다
    assert "s3cr3tP@ss" not in masked
    assert "MASKED" in masked


def test_mask_pii_idempotent():
    """이미 마스킹된 텍스트를 다시 mask_pii()해도 안전하다."""
    text = f"사용자 {SENSITIVE_EMAIL}"
    once = mask_pii(text)
    twice = mask_pii(once)
    assert SENSITIVE_EMAIL not in twice


def test_has_pii_detects_email():
    """has_pii()가 이메일을 감지한다."""
    assert has_pii(f"문의: {SENSITIVE_EMAIL}")
    assert not has_pii("문의: 일반 텍스트")


# ==================================================================
# 2. _mask_sql_literals() – SQL 구조 유지 + 리터럴 마스킹
# ==================================================================

def test_mask_sql_literals_preserves_keywords():
    """SQL 키워드, 컬럼명, 테이블명은 마스킹 후에도 유지된다."""
    masked = _mask_sql_literals(SQL_WITH_EMAIL_LITERAL)
    for kw in ("SELECT", "FROM", "users", "WHERE", "email"):
        assert kw in masked, f"'{kw}' should be preserved after masking"


def test_mask_sql_literals_removes_string_literals():
    """문자열 리터럴(단따옴표)이 :param_N 으로 치환된다."""
    masked = _mask_sql_literals(SQL_WITH_EMAIL_LITERAL)
    assert SENSITIVE_EMAIL not in masked
    assert ":param_1" in masked


def test_mask_sql_literals_removes_number_literals():
    """숫자 리터럴(비교 연산자 뒤 정수)이 :param_N 으로 치환된다."""
    masked = _mask_sql_literals(SQL_WITH_NUMBER_LITERAL)
    assert "7777" not in masked
    assert ":param_" in masked


def test_mask_sql_literals_multiple_params():
    """여러 리터럴이 순서대로 :param_1, :param_2, ... 로 치환된다."""
    sql = "SELECT * FROM orders WHERE status = 'ACTIVE' AND region = 'SEOUL' AND count > 0"
    masked = _mask_sql_literals(sql)
    assert "ACTIVE" not in masked
    assert "SEOUL" not in masked
    assert ":param_1" in masked
    assert ":param_2" in masked


def test_mask_sql_literals_result_is_readable():
    """마스킹 후 SQL 구조가 사람이 알아볼 수 있을 만큼 충분히 길다."""
    for sql_id, entry in QUERY_CATALOG.items():
        masked = _mask_sql_literals(entry["sql"])
        assert len(masked) > 10, f"Masked SQL for '{sql_id}' is suspiciously short"
        upper = masked.upper()
        assert "SELECT" in upper or "WITH" in upper, f"SQL keywords missing for '{sql_id}'"


# ==================================================================
# 3. search_sql_candidates() – 후보 SQL이 마스킹된 채로 반환된다
# ==================================================================

def test_candidate_search_masked_sql_no_string_literals():
    """search_sql_candidates()가 반환한 masked_sql에 단따옴표 문자열 리터럴이 없다."""
    candidates = search_sql_candidates("주문 고객 조인")
    assert candidates, "No candidates returned for '주문 고객 조인'"
    for c in candidates:
        masked = c["masked_sql"]
        # 단따옴표로 감싼 문자열 리터럴이 남아있으면 안 된다
        import re
        raw_literals = re.findall(r"'[^']*'", masked)
        assert not raw_literals, (
            f"sql_id={c['sql_id']}: unmasked literals found: {raw_literals}"
        )


def test_candidate_search_masked_sql_structure_readable():
    """masked_sql에 SQL 키워드(SELECT/FROM/WHERE)가 존재해 구조를 이해할 수 있다."""
    candidates = search_sql_candidates("주문 고객 조인")
    for c in candidates:
        upper = c["masked_sql"].upper()
        assert any(kw in upper for kw in ("SELECT", "FROM", "WHERE", "WITH")), (
            f"sql_id={c['sql_id']}: no SQL keywords in masked_sql"
        )


def test_candidate_search_includes_param_placeholders():
    """리터럴이 있는 후보 SQL은 :param_N 플레이스홀더를 포함한다."""
    candidates = search_sql_candidates("주문 고객 조인")
    # orders_customers_join SQL에 '2026-09-01', 'KIM%' 리터럴이 있으므로 :param_ 필요
    param_found = any(":param_" in c["masked_sql"] for c in candidates)
    assert param_found, "Expected :param_ placeholders in at least one masked SQL candidate"


# ==================================================================
# 4. FileTracer – trace.jsonl에 PII 원문이 기록되지 않는다
# ==================================================================

def test_file_tracer_masks_email_in_tool_input(tmp_path):
    """FileTracer.on_tool_start()가 이메일 PII를 마스킹한 뒤 파일에 기록한다."""
    from src.tracing import FileTracer
    trace_file = tmp_path / "trace.jsonl"
    tracer = FileTracer(path=str(trace_file))
    run_id = uuid4()

    sensitive_input = f"SELECT * FROM users WHERE email = '{SENSITIVE_EMAIL}'"
    tracer.on_tool_start(
        serialized={"name": "test_tool"},
        input_str=sensitive_input,
        run_id=run_id,
    )

    content = trace_file.read_text(encoding="utf-8")
    assert SENSITIVE_EMAIL not in content, "Email must be masked in trace file (tool_start)"
    assert "[MASKED_EMAIL]" in content, "Masked placeholder must appear in trace file"


def test_file_tracer_masks_phone_in_tool_output(tmp_path):
    """FileTracer.on_tool_end()가 전화번호 PII를 마스킹한 뒤 파일에 기록한다."""
    from src.tracing import FileTracer
    trace_file = tmp_path / "trace.jsonl"
    tracer = FileTracer(path=str(trace_file))
    run_id = uuid4()
    tracer._starts[run_id] = 0.0

    sensitive_output = f"조회 결과: 담당자 연락처 {SENSITIVE_PHONE}"
    tracer.on_tool_end(output=sensitive_output, run_id=run_id)

    content = trace_file.read_text(encoding="utf-8")
    assert SENSITIVE_PHONE not in content, "Phone must be masked in trace file (tool_end)"
    assert "[MASKED_PHONE]" in content, "Masked placeholder must appear in trace file"


def test_file_tracer_masks_emp_id_in_tool_output(tmp_path):
    """FileTracer.on_tool_end()가 사번을 마스킹한 뒤 파일에 기록한다."""
    from src.tracing import FileTracer
    trace_file = tmp_path / "trace.jsonl"
    tracer = FileTracer(path=str(trace_file))
    run_id = uuid4()
    tracer._starts[run_id] = 0.0

    sensitive_output = f"결과: 사번 {SENSITIVE_EMP_ID} 의 결제 이력 3건"
    tracer.on_tool_end(output=sensitive_output, run_id=run_id)

    content = trace_file.read_text(encoding="utf-8")
    assert SENSITIVE_EMP_ID not in content, "Emp ID must be masked in trace file"
    assert "[MASKED_EMP_ID]" in content, "Masked placeholder must appear in trace file"


def test_file_tracer_events_list_also_masked(tmp_path):
    """FileTracer.events 인메모리 목록도 마스킹된 내용을 담는다."""
    from src.tracing import FileTracer
    trace_file = tmp_path / "trace.jsonl"
    tracer = FileTracer(path=str(trace_file))
    run_id = uuid4()

    sensitive_input = f"사번 {SENSITIVE_EMP_ID} 조회"
    tracer.on_tool_start(
        serialized={"name": "lookup"},
        input_str=sensitive_input,
        run_id=run_id,
    )

    # events 배열 전체를 JSON 직렬화해서 확인
    events_json = json.dumps(tracer.events, ensure_ascii=False)
    assert SENSITIVE_EMP_ID not in events_json, "Emp ID must not appear in tracer.events"


# ==================================================================
# 5. SQLite 저장소 – 원문 PII가 DB에 기록되지 않는다
# ==================================================================

def test_run_sql_validation_persists_masked_verification(tmp_path, monkeypatch):
    """실제 운영 경로(pipeline.run_sql_validation)를 호출했을 때, 마스킹 함수를 직접 부르지
    않고도 memory.sqlite(verification_history/sql_versions)에 마스킹된 값만 남아야 한다.
    테스트가 스스로 마스킹해서 저장하는 게 아니라 프로덕션 호출 경로 자체를 검증한다."""
    monkeypatch.chdir(tmp_path)
    from src import pipeline
    from src.schemas import QueryReview
    pipeline._memory_store.cache_clear()

    sql = f"SELECT * FROM customers WHERE email = '{SENSITIVE_EMAIL}'"

    def _fake_reviewer(_prompt):
        return QueryReview(result="accept", reason="문제 없음")

    with patch("src.validator.default_llm") as mock_llm:
        mock_llm.return_value.with_structured_output.return_value.invoke.side_effect = _fake_reviewer
        result = asyncio.run(pipeline.run_sql_validation(sql))

    assert result["status"] == "ok"
    db_path = tmp_path / "memory.sqlite"
    assert db_path.exists(), "run_sql_validation이 memory.sqlite를 생성/기록해야 한다"
    with sqlite3.connect(db_path) as conn:
        verif_rows = conn.execute("SELECT note FROM verification_history").fetchall()
        version_rows = conn.execute("SELECT masked_sql FROM sql_versions").fetchall()
    assert verif_rows, "verification_history에 기록이 없다 — run_sql_validation이 저장을 호출하지 않았다"
    assert version_rows, "sql_versions에 기록이 없다"
    assert SENSITIVE_EMAIL not in version_rows[0][0]
    assert ":param_1" in version_rows[0][0]
    pipeline._memory_store.cache_clear()


def test_run_candidate_search_checkpoint_has_no_raw_email(tmp_path, monkeypatch):
    """pipeline.run_candidate_search가 실제로 checkpoints.sqlite에 남기는 상태에는
    후보 SQL이 이미 candidate_search 내부에서 마스킹되어 있으므로 원문이 남지 않는다."""
    monkeypatch.chdir(tmp_path)
    from src import pipeline
    pipeline._checkpoint_store.cache_clear()

    result = asyncio.run(pipeline.run_candidate_search("고객 주문 조인이 느린데 원인이 뭐야"))
    assert result["status"] == "ok"
    session_id = result["session_id"]

    db_path = tmp_path / "checkpoints.sqlite"
    assert db_path.exists()
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT state_json FROM checkpoints WHERE session_id = ? AND step = 'candidates'",
            (session_id,),
        ).fetchall()
    assert rows, "candidates 체크포인트가 저장되지 않았다"
    # 카탈로그 SQL 원문에는 리터럴 문자열('2026-09-01', 'KIM%')이 있는데, 저장된 상태에는
    # masked_sql만 들어 있으므로 원문 리터럴이 나타나면 안 된다.
    assert "'2026-09-01'" not in rows[0][0]
    pipeline._checkpoint_store.cache_clear()


def test_run_feedback_persists_masked_content(tmp_path, monkeypatch):
    """pipeline.run_feedback이 실제로 memory.sqlite의 feedback 테이블에 PII를 마스킹해 남긴다."""
    monkeypatch.chdir(tmp_path)
    from src import pipeline
    pipeline._memory_store.cache_clear()

    raw_content = f"사용자 피드백: {SENSITIVE_EMAIL} 가 {SENSITIVE_PHONE} 로 연락 요망"
    result = asyncio.run(pipeline.run_feedback("user_note", raw_content, session_id="sess_001"))
    assert result["status"] == "ok"

    db_path = tmp_path / "memory.sqlite"
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT content FROM feedback").fetchall()
    assert rows, "feedback row not found"
    for row in rows:
        assert SENSITIVE_EMAIL not in row[0], "Email must not be in feedback table"
        assert SENSITIVE_PHONE not in row[0], "Phone must not be in feedback table"
    pipeline._memory_store.cache_clear()


# ==================================================================
# 6. API 응답 – block_mutating_sql 응답에 PII가 노출되지 않는다
# ==================================================================

def test_block_mutating_sql_response_has_no_pii():
    """block_mutating_sql()의 차단 응답에 원본 SQL PII가 반영되지 않는다."""
    from src.guardrails import block_mutating_sql
    sql = f"UPDATE users SET email = '{SENSITIVE_EMAIL}' WHERE id = 1"
    result = block_mutating_sql(sql)
    assert result is not None, "Should be blocked"
    # 응답 딕셔너리 전체를 직렬화해서 확인
    result_json = json.dumps(result, ensure_ascii=False)
    # block_mutating_sql은 SQL 원문을 응답에 포함하지 않으므로 이메일이 없어야 한다
    assert SENSITIVE_EMAIL not in result_json, "PII must not leak in block response"


# ==================================================================
# 7. SQL 구조 가독성 – 마스킹 후에도 후보 SQL 구조가 이해 가능하다
# ==================================================================

def test_masked_candidate_structure_is_human_readable():
    """모든 QUERY_CATALOG 항목을 마스킹해도 테이블명·컬럼명·조인·조건 구조가 남는다."""
    structure_keywords = {
        "orders_customers_join": ["orders", "customers", "customer_id"],
        "payments_implicit_cast": ["payments", "emp_id"],
        "orders_stale_stats": ["orders", "status"],
        "orders_order_items_join": ["orders", "order_items", "order_id"],
    }
    for sql_id, expected_kws in structure_keywords.items():
        entry = QUERY_CATALOG.get(sql_id)
        if entry is None:
            continue
        masked = _mask_sql_literals(entry["sql"])
        for kw in expected_kws:
            assert kw in masked, (
                f"'{kw}' should survive masking for sql_id='{sql_id}'"
            )
