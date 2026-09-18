"""AC 15 — 평가 실행 및 문서 정책 계약.

이 파일은 "선언"이 아니라 "실행"으로 계약을 검증한다.

1. evaluation/test_queries.csv의 positive/edge/negative/guardrail 4개 카테고리를 실제로 실행한다.
   - negative/guardrail: seed 제약대로 규칙 기반(거부·마스킹 여부)으로 즉시 실행·채점한다.
   - positive/edge: run_eval.grade_item 채점기를 실행하고, RAGAS 4지표가 이 두 카테고리에만
     적용되었는지 라운드 요약에서 확인한다.
2. evaluation/candidate_fixtures.csv의 후보 검색 fixture를 search_sql_candidates로 실제 실행해
   기대 SQL ID가 후보 목록에 포함되는지 확인한다.
3. round1/round2 리포트와 README·SERVICE 문서가 현재 정책(세 흐름, SQLcl MCP 경계,
   승인책임자 토큰, 마스킹, HITL)을 설명하는지 확인한다.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EVAL_DIR = ROOT / "evaluation"
QUERIES_CSV = EVAL_DIR / "test_queries.csv"
CANDIDATES_CSV = EVAL_DIR / "candidate_fixtures.csv"

REQUIRED_CATEGORIES = ("positive", "edge", "negative", "guardrail")


def _split(field: str) -> list[str]:
    return [x.strip() for x in (field or "").split(";") if x.strip()]


def _load_csv(path: Path) -> list[dict]:
    assert path.exists(), f"{path.relative_to(ROOT)} 파일이 없음"
    with path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows, f"{path.relative_to(ROOT)}에 데이터가 없음"
    return rows


@pytest.fixture(scope="module")
def query_rows() -> list[dict]:
    return _load_csv(QUERIES_CSV)


@pytest.fixture(scope="module")
def candidate_rows() -> list[dict]:
    return _load_csv(CANDIDATES_CSV)


@pytest.fixture(scope="module")
def round1_summary() -> dict:
    path = EVAL_DIR / "_round1_summary.json"
    assert path.exists(), "evaluation/_round1_summary.json 없음"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def round2_summary() -> dict:
    path = EVAL_DIR / "_round2_summary.json"
    assert path.exists(), "evaluation/_round2_summary.json 없음"
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1) 4개 카테고리 fixture 구조
# ---------------------------------------------------------------------------

class TestEvaluationFixtureShape:
    def test_required_columns(self, query_rows):
        required = {"id", "category", "input", "expected_traits", "forbidden"}
        missing = required - set(query_rows[0].keys())
        assert not missing, f"test_queries.csv 필수 컬럼 누락: {missing}"

    def test_all_four_categories_present(self, query_rows):
        cats = {r["category"] for r in query_rows}
        missing = set(REQUIRED_CATEGORIES) - cats
        assert not missing, f"카테고리 누락: {missing}"

    def test_ids_unique(self, query_rows):
        ids = [r["id"] for r in query_rows]
        dupes = {i for i in ids if ids.count(i) > 1}
        assert not dupes, f"중복 id: {dupes}"

    @pytest.mark.parametrize("category", REQUIRED_CATEGORIES)
    def test_each_category_has_cases(self, query_rows, category):
        rows = [r for r in query_rows if r["category"] == category]
        assert rows, f"{category} 케이스가 없음"


# ---------------------------------------------------------------------------
# 2) guardrail 카테고리 — 규칙 기반으로 실제 실행해 거부·마스킹을 확인한다
# ---------------------------------------------------------------------------

class TestGuardrailCategoryExecution:
    """seed 제약: negative/guardrail은 거부·마스킹 여부를 규칙 기반으로 평가한다."""

    def test_every_guardrail_case_is_refused_or_masked(self, query_rows):
        from src.guardrails import block_mutating_sql, has_pii, is_off_topic, mask_pii, rule_check

        rows = [r for r in query_rows if r["category"] == "guardrail"]
        unhandled = []
        for row in rows:
            text = row["input"]
            blocked, _reason = rule_check(text)
            handled = blocked or is_off_topic(text) or block_mutating_sql(text) is not None
            if not handled and has_pii(text):
                # 차단 대상이 아니면 최소한 마스킹으로 처리되어야 한다.
                handled = mask_pii(text) != text
            if not handled:
                unhandled.append(row["id"])
        assert not unhandled, f"규칙 기반으로 거부·마스킹되지 않은 guardrail 케이스: {unhandled}"

    def test_forbidden_strings_never_survive_masking(self, query_rows):
        from src.guardrails import mask_pii, rule_check

        rows = [r for r in query_rows if r["category"] == "guardrail" and _split(r.get("forbidden", ""))]
        leaked = []
        for row in rows:
            blocked, _ = rule_check(row["input"])
            if blocked:
                continue  # 차단된 입력은 애초에 하류로 흐르지 않는다
            masked = mask_pii(row["input"])
            for bad in _split(row["forbidden"]):
                if bad in masked:
                    leaked.append((row["id"], bad))
        assert not leaked, f"마스킹 후에도 금지 문자열이 남음: {leaked}"

    def test_mutating_sql_case_blocked_before_mcp(self, query_rows):
        from src.guardrails import block_mutating_sql, rule_check

        rows = [r for r in query_rows if r["category"] == "guardrail"]
        destructive = [r for r in rows if "DROP TABLE" in r["input"].upper()]
        assert destructive, "파괴적 DDL guardrail 케이스가 fixture에 없음"
        for row in destructive:
            blocked, _ = rule_check(row["input"])
            assert blocked or block_mutating_sql(row["input"]) is not None, \
                f"{row['id']}: 파괴적 명령이 규칙 단계에서 차단되지 않음"


# ---------------------------------------------------------------------------
# 3) negative 카테고리 — 규칙 기반 채점기를 실제로 실행한다
# ---------------------------------------------------------------------------

class TestNegativeCategoryExecution:
    def test_empty_input_case_requires_no_answer(self, query_rows):
        from run_eval import grade_item

        rows = [r for r in query_rows if r["category"] == "negative" and not r["input"].strip()]
        assert rows, "빈 입력 negative 케이스가 fixture에 없음"
        for row in rows:
            ok, _ = grade_item(row, {"status": "no_answer", "answer": ""})
            assert ok == "PASS", f"{row['id']}: no_answer 응답이 통과로 채점되지 않음"
            bad, _ = grade_item(row, {"status": "ok", "answer": "임의의 답변"})
            assert bad == "FAIL", f"{row['id']}: 환각 답변이 실패로 채점되지 않음"

    def test_out_of_scope_case_passes_only_when_declining(self, query_rows):
        from run_eval import grade_item

        rows = [
            r for r in query_rows
            if r["category"] == "negative" and r["input"].strip() and _split(r["expected_traits"])
        ]
        assert rows, "범위 밖 negative 케이스가 fixture에 없음"
        for row in rows:
            trait = _split(row["expected_traits"])[0]
            ok, _ = grade_item(row, {"status": "ok", "answer": f"해당 질문은 {trait}겠습니다."})
            assert ok == "PASS", f"{row['id']}: 정직한 거부 응답이 통과로 채점되지 않음"
            bad, _ = grade_item(row, {"status": "ok", "answer": "확실하게 알려드리겠습니다."})
            assert bad == "FAIL", f"{row['id']}: 환각 응답이 실패로 채점되지 않음"


# ---------------------------------------------------------------------------
# 4) positive/edge 카테고리 — 채점기 실행 + RAGAS 적용 범위
# ---------------------------------------------------------------------------

class TestPositiveEdgeCategoryExecution:
    def test_grader_requires_expected_traits(self, query_rows):
        from run_eval import grade_item

        rows = [r for r in query_rows if r["category"] in ("positive", "edge")]
        assert rows, "positive/edge 케이스가 없음"
        for row in rows:
            traits = _split(row["expected_traits"])
            assert traits, f"{row['id']}: expected_traits가 비어있음"
            ok, _ = grade_item(row, {"status": "ok", "answer": " ".join(traits)})
            assert ok == "PASS", f"{row['id']}: 기대 키워드를 모두 담은 답변이 통과하지 않음"
            bad, _ = grade_item(row, {"status": "error", "answer": " ".join(traits)})
            assert bad == "FAIL", f"{row['id']}: 실패 status가 FAIL로 채점되지 않음"

    @pytest.mark.parametrize("round_no", [1, 2])
    def test_ragas_applied_only_to_positive_and_edge(self, round_no, round1_summary, round2_summary):
        summary = round1_summary if round_no == 1 else round2_summary
        offenders = [
            r["id"] for r in summary["results"]
            if r["category"] in ("negative", "guardrail") and r.get("ragas")
        ]
        assert not offenders, f"round{round_no}: negative/guardrail에 RAGAS가 적용됨: {offenders}"

    @pytest.mark.parametrize("round_no", [1, 2])
    def test_every_fixture_id_was_executed(self, round_no, query_rows, round1_summary, round2_summary):
        summary = round1_summary if round_no == 1 else round2_summary
        executed = {r["id"] for r in summary["results"]}
        missing = {r["id"] for r in query_rows} - executed
        assert not missing, f"round{round_no}에서 실행되지 않은 케이스: {missing}"


# ---------------------------------------------------------------------------
# 5) 후보 검색 fixture — search_sql_candidates를 실제로 실행한다
# ---------------------------------------------------------------------------

class TestCandidateFixtureExecution:
    """candidate_fixtures.csv는 재현 가능한 오프라인 평가용이다(reproducible_evaluation 원칙) —
    seed v2.6.0의 실제 V$SQL 동적 조회 경로는 인스턴스 상태에 따라 달라지므로, 이 fixture는
    Oracle 미설정 시의 결정적 카탈로그 폴백(_fallback_search_sql_candidates)을 검증한다."""

    @pytest.fixture(autouse=True)
    def _force_offline_fallback(self, monkeypatch):
        monkeypatch.setattr("src.tools._oracle_configured", lambda: False)

    def test_required_columns(self, candidate_rows):
        required = {"id", "natural_language_query", "expected_sql_ids"}
        missing = required - set(candidate_rows[0].keys())
        assert not missing, f"candidate_fixtures.csv 필수 컬럼 누락: {missing}"

    def test_expected_sql_ids_present_in_search_results(self, candidate_rows):
        from src.tools import search_sql_candidates

        failures = []
        for row in candidate_rows:
            candidates = search_sql_candidates(row["natural_language_query"])
            found = {c["sql_id"] for c in candidates}
            for expected in _split(row["expected_sql_ids"]):
                if expected not in found:
                    failures.append((row["id"], expected, sorted(found)))
        assert not failures, f"후보 목록에 기대 SQL ID가 없음: {failures}"

    def test_candidates_expose_masked_sql(self, candidate_rows):
        from src.tools import search_sql_candidates

        for row in candidate_rows:
            for cand in search_sql_candidates(row["natural_language_query"]):
                assert cand.get("masked_sql", "").strip(), \
                    f"{row['id']}/{cand.get('sql_id')}: 마스킹된 SQL 원문이 비어있음"
                assert "rank" in cand, f"{row['id']}/{cand.get('sql_id')}: rank 누락"


# ---------------------------------------------------------------------------
# 6) round1/round2 리포트
# ---------------------------------------------------------------------------

class TestRoundReports:
    def test_round1_threshold(self, round1_summary):
        rate = round1_summary.get("overall_pass_rate", 0)
        assert rate >= 0.70, f"round1 통과율 {rate:.1%} < 70%"

    def test_round2_threshold(self, round2_summary):
        rate = round2_summary.get("overall_pass_rate", 0)
        assert rate >= 0.90, f"round2 통과율 {rate:.1%} < 90%"

    def test_round2_not_worse_than_round1(self, round1_summary, round2_summary):
        assert round2_summary["overall_pass_rate"] >= round1_summary["overall_pass_rate"], \
            "round2 통과율이 round1보다 낮음"

    def test_round1_report_covers_all_categories(self):
        text = (EVAL_DIR / "round1_report.md").read_text(encoding="utf-8")
        missing = [c for c in REQUIRED_CATEGORIES if c not in text]
        assert not missing, f"round1_report.md에 누락된 카테고리: {missing}"

    def test_round2_report_covers_all_categories(self):
        text = (EVAL_DIR / "round2_report.md").read_text(encoding="utf-8")
        missing = [c for c in REQUIRED_CATEGORIES if c not in text]
        assert not missing, f"round2_report.md에 누락된 카테고리: {missing}"

    def test_round2_report_describes_improvement(self):
        text = (EVAL_DIR / "round2_report.md").read_text(encoding="utf-8")
        assert "개선" in text or "Round 1 대비" in text, "round2_report.md에 개선 서술 없음"

    def test_round2_report_references_candidate_fixture(self):
        text = (EVAL_DIR / "round2_report.md").read_text(encoding="utf-8")
        assert "candidate" in text.lower() or "후보" in text, \
            "round2_report.md에 후보 검색 fixture 언급 없음"


# ---------------------------------------------------------------------------
# 7) README·SERVICE 문서가 현재 정책을 설명한다
# ---------------------------------------------------------------------------

README_POLICY_KEYWORDS = {
    "SQL 생성 흐름": ["SQL 생성", "query_planner", "비즈니스 요구사항"],
    "SQL 검증 흐름": ["SQL 검증", "sql_validator", "직접 SQL"],
    "운영 성능 진단 흐름": ["운영 성능", "후보", "candidate_search"],
    "승인책임자 토큰": ["X-Approver-Token", "승인책임자"],
    "SQLcl MCP 경계": ["SQLcl MCP", "sqlcl"],
    "마스킹 정책": ["마스킹", "개인정보"],
    "평가 리포트": ["round1_report", "round2_report"],
    "Docker Compose 기동": ["docker compose", "docker-compose"],
}

SERVICE_POLICY_KEYWORDS = {
    "세 흐름": ["SQL 생성", "SQL 검증", "운영 성능"],
    "승인책임자 역할": ["승인책임자", "X-Approver-Token"],
    "SQLcl MCP 경계": ["SQLcl MCP", "MCP"],
    "마스킹 정책": ["마스킹", "개인정보"],
    "HITL 승인": ["HITL", "승인"],
    "성공 기준": ["성공 기준", "통과율", "round1", "round2"],
}


@pytest.mark.parametrize("topic,keywords", sorted(README_POLICY_KEYWORDS.items()))
def test_readme_describes_current_policy(topic, keywords):
    path = ROOT / "README.md"
    assert path.exists(), "README.md 없음"
    text = path.read_text(encoding="utf-8").lower()
    assert any(k.lower() in text for k in keywords), \
        f"README.md에 '{topic}' 정책 설명 없음 (기대 키워드 중 하나: {keywords})"


@pytest.mark.parametrize("topic,keywords", sorted(SERVICE_POLICY_KEYWORDS.items()))
def test_service_describes_current_policy(topic, keywords):
    path = ROOT / "SERVICE.md"
    assert path.exists(), "SERVICE.md 없음"
    text = path.read_text(encoding="utf-8").lower()
    assert any(k.lower() in text for k in keywords), \
        f"SERVICE.md에 '{topic}' 정책 설명 없음 (기대 키워드 중 하나: {keywords})"
