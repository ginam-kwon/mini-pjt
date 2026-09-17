"""AC 15 — 평가 fixture와 문서 정책 계약 검증.

positive, edge, negative, guardrail 평가 케이스와 후보 검색 fixture가 올바르게 구성되었고,
round1/round2 리포트와 README·SERVICE 문서가 현재 정책을 설명하는지 확인한다.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = ROOT / "evaluation"


# ---------------------------------------------------------------------------
# 평가 CSV 구조 검증
# ---------------------------------------------------------------------------

class TestTestQueriesFixture:
    """evaluation/test_queries.csv가 4개 카테고리를 모두 포함해야 한다."""

    @pytest.fixture(scope="class")
    def rows(self):
        path = EVAL_DIR / "test_queries.csv"
        assert path.exists(), "evaluation/test_queries.csv 파일이 없음"
        with path.open(encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def test_has_required_columns(self, rows):
        required = {"id", "category", "input", "expected_traits", "forbidden"}
        assert required <= set(rows[0].keys()), f"필수 컬럼 누락: {required - set(rows[0].keys())}"

    def test_has_positive_cases(self, rows):
        ids = [r["id"] for r in rows if r["category"] == "positive"]
        assert len(ids) >= 5, f"positive 케이스가 부족함: {ids}"

    def test_has_edge_cases(self, rows):
        ids = [r["id"] for r in rows if r["category"] == "edge"]
        assert len(ids) >= 1, f"edge 케이스가 없음"

    def test_has_negative_cases(self, rows):
        ids = [r["id"] for r in rows if r["category"] == "negative"]
        assert len(ids) >= 1, f"negative 케이스가 없음"

    def test_has_guardrail_cases(self, rows):
        ids = [r["id"] for r in rows if r["category"] == "guardrail"]
        assert len(ids) >= 1, f"guardrail 케이스가 없음"

    def test_all_categories_present(self, rows):
        cats = {r["category"] for r in rows}
        required_cats = {"positive", "edge", "negative", "guardrail"}
        missing = required_cats - cats
        assert not missing, f"카테고리 누락: {missing}"

    def test_ids_are_unique(self, rows):
        ids = [r["id"] for r in rows]
        assert len(ids) == len(set(ids)), "중복 id 존재"


class TestCandidateFixtures:
    """evaluation/candidate_fixtures.csv가 후보 검색 fixture를 포함해야 한다."""

    @pytest.fixture(scope="class")
    def rows(self):
        path = EVAL_DIR / "candidate_fixtures.csv"
        assert path.exists(), "evaluation/candidate_fixtures.csv 파일이 없음"
        with path.open(encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def test_has_required_columns(self, rows):
        required = {"id", "natural_language_query", "expected_sql_ids"}
        assert required <= set(rows[0].keys()), f"필수 컬럼 누락: {required - set(rows[0].keys())}"

    def test_has_at_least_one_row(self, rows):
        assert len(rows) >= 1, "candidate_fixtures.csv에 데이터가 없음"

    def test_natural_language_queries_nonempty(self, rows):
        for r in rows:
            assert r["natural_language_query"].strip(), f"id={r['id']}: natural_language_query가 비어있음"

    def test_expected_sql_ids_nonempty(self, rows):
        for r in rows:
            assert r["expected_sql_ids"].strip(), f"id={r['id']}: expected_sql_ids가 비어있음"

    def test_ids_are_unique(self, rows):
        ids = [r["id"] for r in rows]
        assert len(ids) == len(set(ids)), "중복 id 존재"


# ---------------------------------------------------------------------------
# Round 리포트 검증
# ---------------------------------------------------------------------------

class TestEvaluationReports:
    """round1_report.md와 round2_report.md가 존재하고 정책을 반영해야 한다."""

    def test_round1_report_exists(self):
        assert (EVAL_DIR / "round1_report.md").exists(), "round1_report.md 없음"

    def test_round2_report_exists(self):
        assert (EVAL_DIR / "round2_report.md").exists(), "round2_report.md 없음"

    def test_round1_summary_json_exists(self):
        assert (EVAL_DIR / "_round1_summary.json").exists(), "_round1_summary.json 없음"

    def test_round2_summary_json_exists(self):
        assert (EVAL_DIR / "_round2_summary.json").exists(), "_round2_summary.json 없음"

    def test_round1_pass_rate_above_threshold(self):
        data = json.loads((EVAL_DIR / "_round1_summary.json").read_text(encoding="utf-8"))
        rate = data.get("overall_pass_rate", 0)
        assert rate >= 0.70, f"round1 통과율 {rate:.1%}이 70% 미달"

    def test_round2_pass_rate_above_threshold(self):
        data = json.loads((EVAL_DIR / "_round2_summary.json").read_text(encoding="utf-8"))
        rate = data.get("overall_pass_rate", 0)
        assert rate >= 0.90, f"round2 통과율 {rate:.1%}이 90% 미달"

    def test_round2_improves_on_round1(self):
        r1 = json.loads((EVAL_DIR / "_round1_summary.json").read_text(encoding="utf-8"))
        r2 = json.loads((EVAL_DIR / "_round2_summary.json").read_text(encoding="utf-8"))
        assert r2["overall_pass_rate"] >= r1["overall_pass_rate"], \
            f"round2({r2['overall_pass_rate']:.1%}) < round1({r1['overall_pass_rate']:.1%})"

    def test_round1_report_mentions_categories(self):
        text = (EVAL_DIR / "round1_report.md").read_text(encoding="utf-8")
        for cat in ("positive", "edge", "negative", "guardrail"):
            assert cat in text, f"round1_report.md에 '{cat}' 카테고리 언급 없음"

    def test_round2_report_mentions_improvement(self):
        text = (EVAL_DIR / "round2_report.md").read_text(encoding="utf-8")
        assert "Round 1 대비" in text or "개선" in text, \
            "round2_report.md에 round1 대비 개선 내용 없음"

    def test_round2_report_mentions_candidate_fixtures(self):
        text = (EVAL_DIR / "round2_report.md").read_text(encoding="utf-8")
        assert "candidate" in text.lower() or "후보" in text, \
            "round2_report.md에 후보 검색 fixture 언급 없음"


# ---------------------------------------------------------------------------
# README 문서 정책 검증
# ---------------------------------------------------------------------------

class TestReadmePolicy:
    """README.md가 현재 세 흐름 정책과 보안 정책을 설명해야 한다."""

    @pytest.fixture(scope="class")
    def text(self):
        path = ROOT / "README.md"
        assert path.exists(), "README.md 없음"
        return path.read_text(encoding="utf-8")

    def test_mentions_sql_generation_flow(self, text):
        keywords = ["SQL 생성", "쿼리 생성", "query_planner", "비즈니스 요구사항"]
        assert any(k in text for k in keywords), \
            f"README.md에 SQL 생성 흐름 설명 없음 (찾은 키워드: {keywords})"

    def test_mentions_sql_validation_flow(self, text):
        keywords = ["SQL 검증", "SQL 사전 검증", "sql_validator", "직접 SQL"]
        assert any(k in text for k in keywords), \
            f"README.md에 SQL 검증 흐름 설명 없음"

    def test_mentions_operational_diagnosis_flow(self, text):
        keywords = ["운영 성능", "자연어 운영", "후보", "candidate_search"]
        assert any(k in text for k in keywords), \
            f"README.md에 운영 성능 진단 흐름 설명 없음"

    def test_mentions_approver_token(self, text):
        keywords = ["X-Approver-Token", "승인책임자", "approver"]
        assert any(k in text for k in keywords), \
            f"README.md에 승인책임자 토큰 정책 없음"

    def test_mentions_sqlcl_mcp(self, text):
        keywords = ["SQLcl MCP", "sqlcl", "MCP"]
        assert any(k in text for k in keywords), \
            f"README.md에 SQLcl MCP 언급 없음"

    def test_mentions_masking_policy(self, text):
        keywords = ["마스킹", "masking", "개인정보", "PII"]
        assert any(k in text for k in keywords), \
            f"README.md에 마스킹 정책 언급 없음"

    def test_mentions_evaluation_reports(self, text):
        assert "round1_report" in text or "round2_report" in text, \
            "README.md에 평가 리포트 참조 없음"

    def test_mentions_docker_compose(self, text):
        assert "docker compose" in text.lower() or "docker-compose" in text.lower(), \
            "README.md에 Docker Compose 실행 방법 없음"


# ---------------------------------------------------------------------------
# SERVICE 문서 정책 검증
# ---------------------------------------------------------------------------

class TestServicePolicy:
    """SERVICE.md가 현재 정책의 세 흐름과 보안 정책을 설명해야 한다."""

    @pytest.fixture(scope="class")
    def text(self):
        path = ROOT / "SERVICE.md"
        assert path.exists(), "SERVICE.md 없음"
        return path.read_text(encoding="utf-8")

    def test_mentions_three_flows(self, text):
        keywords = ["SQL 생성", "SQL 검증", "운영 성능", "세 흐름", "three"]
        found = [k for k in keywords if k in text]
        assert len(found) >= 2, f"SERVICE.md에 세 흐름 설명 불충분 (찾은 키워드: {found})"

    def test_mentions_approver_role(self, text):
        keywords = ["승인책임자", "approver", "X-Approver-Token"]
        assert any(k in text for k in keywords), \
            f"SERVICE.md에 승인책임자 역할 없음"

    def test_mentions_sqlcl_mcp_boundary(self, text):
        keywords = ["SQLcl MCP", "MCP", "sqlcl"]
        assert any(k in text for k in keywords), \
            f"SERVICE.md에 SQLcl MCP 경계 언급 없음"

    def test_mentions_masking(self, text):
        keywords = ["마스킹", "masking", "개인정보"]
        assert any(k in text for k in keywords), \
            f"SERVICE.md에 마스킹 정책 없음"

    def test_mentions_success_criteria(self, text):
        keywords = ["성공 기준", "통과율", "round1", "round2", "70%", "90%"]
        found = [k for k in keywords if k in text]
        assert len(found) >= 1, f"SERVICE.md에 성공 기준 정보 없음"

    def test_mentions_hitl(self, text):
        keywords = ["HITL", "승인", "interrupt"]
        assert any(k in text for k in keywords), \
            f"SERVICE.md에 HITL 정책 없음"
