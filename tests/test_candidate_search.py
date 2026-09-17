"""AC: 자연어 운영 성능 요청은 마스킹된 SQL 원문을 포함한 후보 목록을 반환하고,
후보 검색 평가 fixture의 기대 SQL은 반환 후보 목록에 포함된다.

검증 항목:
1. search_sql_candidates가 자연어 질문에 대해 후보 목록을 반환한다.
2. 각 후보는 sql_id, masked_sql, rank 필드를 포함한다.
3. masked_sql에 원본 문자열 리터럴(비교 목적 단일따옴표 값)이 제거되고 :param_N으로 대체된다.
4. candidate_fixtures.csv의 기대 sql_id가 실제 반환 후보 목록에 포함된다.
5. 매칭이 없는 질문에 대해 빈 리스트를 반환한다.
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

FIXTURES_CSV = PROJECT_ROOT / "evaluation" / "candidate_fixtures.csv"


def _load_fixtures() -> list[dict]:
    with FIXTURES_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _expected_ids(row: dict) -> list[str]:
    return [s.strip() for s in row["expected_sql_ids"].split(";") if s.strip()]


# ---------------------------------------------------------------------------
# 기본 함수 존재 및 인터페이스 확인
# ---------------------------------------------------------------------------

class TestSearchSqlCandidatesInterface:
    """search_sql_candidates 함수가 도구 파일에 존재하고 올바른 시그니처를 가지는지 확인한다."""

    def test_function_importable(self):
        """search_sql_candidates가 src.tools에서 import 가능해야 한다."""
        from src.tools import search_sql_candidates  # noqa: F401
        assert callable(search_sql_candidates)

    def test_mask_sql_literals_importable(self):
        """_mask_sql_literals가 src.tools에 정의되어 있어야 한다."""
        from src.tools import _mask_sql_literals  # noqa: F401
        assert callable(_mask_sql_literals)

    def test_returns_list(self):
        """search_sql_candidates는 항상 리스트를 반환해야 한다."""
        from src.tools import search_sql_candidates
        result = search_sql_candidates("주문 고객 조인 성능 문제")
        assert isinstance(result, list)

    def test_candidate_has_required_fields(self):
        """각 후보 딕셔너리는 sql_id, masked_sql, rank 필드를 포함해야 한다."""
        from src.tools import search_sql_candidates
        result = search_sql_candidates("주문 고객 조인 성능 문제")
        assert len(result) > 0, "주문 고객 조인 질문에 후보가 1개 이상 있어야 한다."
        for candidate in result:
            assert "sql_id" in candidate, f"후보에 sql_id 필드가 없습니다: {candidate}"
            assert "masked_sql" in candidate, f"후보에 masked_sql 필드가 없습니다: {candidate}"
            assert "rank" in candidate, f"후보에 rank 필드가 없습니다: {candidate}"

    def test_returns_empty_list_for_no_match(self):
        """매칭되는 카탈로그 항목이 없으면 빈 리스트를 반환해야 한다."""
        from src.tools import search_sql_candidates
        result = search_sql_candidates("날씨가 어떻게 되나요")
        assert result == [], f"매칭 없는 질문에 빈 리스트를 반환해야 합니다: {result}"

    def test_rank_starts_from_one(self):
        """rank는 1부터 시작해야 한다."""
        from src.tools import search_sql_candidates
        result = search_sql_candidates("주문 고객 조인")
        assert len(result) > 0
        assert result[0]["rank"] == 1

    def test_rank_is_ordered(self):
        """rank는 오름차순으로 정렬되어야 한다."""
        from src.tools import search_sql_candidates
        result = search_sql_candidates("주문 고객 조인 결제 형변환")
        ranks = [c["rank"] for c in result]
        assert ranks == sorted(ranks), f"rank가 오름차순이 아닙니다: {ranks}"


# ---------------------------------------------------------------------------
# SQL 마스킹 검증
# ---------------------------------------------------------------------------

class TestSqlMasking:
    """_mask_sql_literals가 SQL 리터럴을 :param_N 형식으로 올바르게 마스킹하는지 확인한다."""

    def test_string_literal_masked(self):
        """단일따옴표 문자열 리터럴이 :param_N으로 대체되어야 한다."""
        from src.tools import _mask_sql_literals
        sql = "SELECT * FROM orders WHERE status = 'PENDING'"
        result = _mask_sql_literals(sql)
        assert "'PENDING'" not in result, f"문자열 리터럴이 마스킹되지 않음: {result}"
        assert ":param_" in result, f":param_N 형식이 없음: {result}"

    def test_numeric_literal_masked(self):
        """비교 연산자 뒤 숫자 리터럴이 :param_N으로 대체되어야 한다."""
        from src.tools import _mask_sql_literals
        sql = "SELECT * FROM payments WHERE emp_id = 1001"
        result = _mask_sql_literals(sql)
        assert "1001" not in result, f"숫자 리터럴이 마스킹되지 않음: {result}"
        assert ":param_" in result, f":param_N 형식이 없음: {result}"

    def test_multiple_literals_masked(self):
        """여러 리터럴이 순서대로 :param_1, :param_2, ... 형식으로 마스킹되어야 한다."""
        from src.tools import _mask_sql_literals
        sql = "SELECT * FROM orders WHERE region = 'SEOUL' OR customer_id = 500"
        result = _mask_sql_literals(sql)
        assert "'SEOUL'" not in result, f"첫 번째 문자열 리터럴이 마스킹되지 않음: {result}"
        assert ":param_1" in result, f":param_1이 없음: {result}"
        assert ":param_2" in result, f":param_2가 없음: {result}"

    def test_masked_sql_is_still_select(self):
        """마스킹 후에도 SELECT 키워드가 유지되어야 한다."""
        from src.tools import _mask_sql_literals
        sql = "SELECT o.order_id FROM orders o WHERE status = 'PENDING'"
        result = _mask_sql_literals(sql)
        assert result.upper().startswith("SELECT"), f"SELECT 키워드가 사라짐: {result}"

    def test_candidate_masked_sql_has_no_obvious_literals(self):
        """후보 SQL의 masked_sql에 원본 비교 리터럴 값이 남아 있으면 안 된다."""
        from src.tools import search_sql_candidates
        result = search_sql_candidates("주문 상태 pending 통계")
        assert len(result) > 0
        for cand in result:
            if cand["sql_id"] == "orders_stale_stats":
                masked = cand["masked_sql"]
                assert "'PENDING'" not in masked, (
                    f"orders_stale_stats masked_sql에 'PENDING' 리터럴이 남아 있음: {masked}"
                )


# ---------------------------------------------------------------------------
# 후보 검색 평가 fixture 검증
# ---------------------------------------------------------------------------

class TestCandidateFixtureFileExists:
    """평가 fixture 파일이 존재하고 올바른 구조를 가지는지 확인한다."""

    def test_fixture_file_exists(self):
        """evaluation/candidate_fixtures.csv 파일이 존재해야 한다."""
        assert FIXTURES_CSV.exists(), (
            f"{FIXTURES_CSV} 파일이 없습니다. 후보 검색 평가 fixture를 생성하세요."
        )

    def test_fixture_has_required_columns(self):
        """fixture CSV에 natural_language_query, expected_sql_ids 컬럼이 있어야 한다."""
        rows = _load_fixtures()
        assert len(rows) > 0, "fixture CSV가 비어 있습니다."
        assert "natural_language_query" in rows[0], "natural_language_query 컬럼이 없습니다."
        assert "expected_sql_ids" in rows[0], "expected_sql_ids 컬럼이 없습니다."

    def test_fixture_has_minimum_rows(self):
        """fixture에 최소 7개 이상의 테스트 케이스가 있어야 한다."""
        rows = _load_fixtures()
        assert len(rows) >= 7, f"fixture 케이스가 {len(rows)}개뿐입니다. 최소 7개가 필요합니다."

    def test_fixture_expected_ids_reference_catalog(self):
        """fixture의 expected_sql_ids가 QUERY_CATALOG의 유효한 키를 참조해야 한다."""
        from src.tools import QUERY_CATALOG
        rows = _load_fixtures()
        for row in rows:
            for expected_id in _expected_ids(row):
                assert expected_id in QUERY_CATALOG, (
                    f"fixture의 expected_sql_id='{expected_id}'가 QUERY_CATALOG에 없습니다."
                )


class TestCandidateSearchFixtureCoverage:
    """평가 fixture의 각 자연어 질문에 대해 기대 SQL이 반환 후보 목록에 포함되는지 확인한다."""

    @pytest.fixture
    def fixtures(self) -> list[dict]:
        return _load_fixtures()

    def _candidate_sql_ids(self, question: str) -> list[str]:
        from src.tools import search_sql_candidates
        return [c["sql_id"] for c in search_sql_candidates(question)]

    @pytest.mark.parametrize("row", _load_fixtures(), ids=[r["id"] for r in _load_fixtures()])
    def test_expected_sql_id_in_candidates(self, row: dict):
        """각 fixture 케이스의 기대 SQL ID가 반환 후보 목록에 포함되어야 한다."""
        question = row["natural_language_query"]
        expected_ids = _expected_ids(row)
        returned_ids = self._candidate_sql_ids(question)

        for expected_id in expected_ids:
            assert expected_id in returned_ids, (
                f"[{row['id']}] 기대 SQL '{expected_id}'가 후보 목록에 없습니다.\n"
                f"  질문: {question}\n"
                f"  반환된 후보: {returned_ids}"
            )

    @pytest.mark.parametrize("row", _load_fixtures(), ids=[r["id"] for r in _load_fixtures()])
    def test_candidate_masked_sql_not_empty(self, row: dict):
        """각 후보의 masked_sql이 비어 있지 않아야 한다."""
        from src.tools import search_sql_candidates
        question = row["natural_language_query"]
        candidates = search_sql_candidates(question)
        for cand in candidates:
            assert cand["masked_sql"].strip(), (
                f"[{row['id']}] sql_id='{cand['sql_id']}'의 masked_sql이 비어 있습니다."
            )

    @pytest.mark.parametrize("row", _load_fixtures(), ids=[r["id"] for r in _load_fixtures()])
    def test_candidate_masked_sql_uses_param_placeholders(self, row: dict):
        """후보 SQL에 리터럴이 있는 경우 :param_N 형식으로 마스킹되어야 한다."""
        from src.tools import search_sql_candidates, QUERY_CATALOG
        question = row["natural_language_query"]
        candidates = search_sql_candidates(question)
        for cand in candidates:
            original_sql = QUERY_CATALOG[cand["sql_id"]]["sql"]
            # 원본에 단일따옴표 리터럴이 있으면 masked_sql에서 사라져야 한다
            original_literals = re.findall(r"'[^']{1,50}'", original_sql)
            if original_literals:
                for lit in original_literals:
                    # 포맷 문자열 패턴('YYYY-MM-DD' 등)은 예외
                    if re.match(r"'[A-Z]{4}-[A-Z]{2}-[A-Z]{2}'", lit):
                        continue
                    assert lit not in cand["masked_sql"], (
                        f"[{row['id']}] sql_id='{cand['sql_id']}' masked_sql에 리터럴 {lit}이 남아 있습니다."
                    )
