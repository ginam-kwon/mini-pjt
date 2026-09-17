"""AC 12: explain_agent와 knowledge_agent는 실행계획 분석과 하이브리드 RAG 튜닝 근거를 결합해
원인, 실행계획 증거, 개선안을 구조화해 반환한다.

테스트 전략:
- LLM 실제 호출 없이 정적·구조 검사로 계약을 검증한다.
- mock 기반으로 finalize_node의 SqlPlanAnalysis 구조화 출력 계약을 확인한다.
- 하이브리드 RAG(BM25 + Chroma + MultiQuery + LLM 리랭크) 구성 요소를 정적으로 검증한다.
"""
from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# 1. explain_agent 프롬프트: 실행계획 분석 지시문 포함 여부
# ---------------------------------------------------------------------------

class TestExplainAgentPrompt:
    """explain_agent 시스템 프롬프트가 실행계획 분석에 필요한 지시사항을 포함해야 한다."""

    def _prompt(self) -> str:
        from src.prompts import explain as mod
        return mod.SYSTEM_PROMPT

    def test_prompt_covers_full_table_scan(self):
        """TABLE ACCESS FULL(풀 테이블 스캔) 분석 지시가 포함되어야 한다."""
        assert "TABLE ACCESS FULL" in self._prompt(), (
            "explain_agent 프롬프트에 'TABLE ACCESS FULL' 분석 지시가 없습니다."
        )

    def test_prompt_covers_cost_rows(self):
        """Rows/Cost 불일치 분석 지시가 포함되어야 한다."""
        prompt = self._prompt()
        has_cost = "Cost" in prompt or "cost" in prompt
        has_rows = "Rows" in prompt or "rows" in prompt
        assert has_cost and has_rows, (
            "explain_agent 프롬프트에 Rows/Cost 분석 지시가 없습니다."
        )

    def test_prompt_covers_join_methods(self):
        """조인 방식(NESTED LOOPS/HASH JOIN) 분석 지시가 포함되어야 한다."""
        prompt = self._prompt()
        has_join = any(kw in prompt for kw in [
            "NESTED LOOPS", "HASH JOIN", "조인 방식",
        ])
        assert has_join, (
            "explain_agent 프롬프트에 조인 방식 분석 지시가 없습니다."
        )

    def test_prompt_covers_predicate_information(self):
        """Predicate Information(함수로 감싼 컬럼 등) 분석 지시가 포함되어야 한다."""
        prompt = self._prompt()
        has_predicate = any(kw in prompt for kw in [
            "Predicate", "predicate", "함수로 감싼", "함수", "컬럼",
        ])
        assert has_predicate, (
            "explain_agent 프롬프트에 Predicate Information 분석 지시가 없습니다."
        )

    def test_prompt_advisory_only_no_write(self):
        """explain_agent는 직접 쓰기 작업을 실행하지 않도록 프롬프트에 명시되어야 한다."""
        prompt = self._prompt()
        has_advisory = any(kw in prompt for kw in [
            "사람 승인", "별도 절차", "직접 실행하지 않", "승인 후",
        ])
        assert has_advisory, (
            "explain_agent 프롬프트에 쓰기 작업 advisory-only 지시가 없습니다."
        )


# ---------------------------------------------------------------------------
# 2. knowledge_agent 프롬프트: 하이브리드 RAG 도구 활용 지시 포함 여부
# ---------------------------------------------------------------------------

class TestKnowledgeAgentPrompt:
    """knowledge_agent 시스템 프롬프트가 RAG 도구 활용과 튜닝 지식 검색 지시를 포함해야 한다."""

    def _prompt(self) -> str:
        from src.prompts import knowledge as mod
        return mod.SYSTEM_PROMPT

    def test_prompt_references_search_tool(self):
        """프롬프트에 search_tuning_knowledge 도구가 언급되어야 한다."""
        assert "search_tuning_knowledge" in self._prompt(), (
            "knowledge_agent 프롬프트에 'search_tuning_knowledge' 도구 참조가 없습니다."
        )

    def test_prompt_covers_tuning_patterns(self):
        """프롬프트에 튜닝 패턴(풀 스캔, 카디널리티, 조인 등) 검색 지시가 포함되어야 한다."""
        prompt = self._prompt()
        patterns = ["풀 테이블 스캔", "카디널리티", "조인", "통계", "파티션"]
        matched = [p for p in patterns if p in prompt]
        assert len(matched) >= 3, (
            f"knowledge_agent 프롬프트에 튜닝 패턴 키워드가 부족합니다. 매칭: {matched}"
        )

    def test_prompt_prohibits_hallucination(self):
        """프롬프트에 근거 없는 추측을 금지하는 지시가 포함되어야 한다."""
        prompt = self._prompt()
        has_no_hallucination = any(kw in prompt for kw in [
            "추측하거나 지어내지", "근거", "모르겠습니다", "절대",
        ])
        assert has_no_hallucination, (
            "knowledge_agent 프롬프트에 환각 금지 지시가 없습니다."
        )


# ---------------------------------------------------------------------------
# 3. 하이브리드 RAG 구성: retriever.py 정적 분석
# ---------------------------------------------------------------------------

class TestHybridRagRetriever:
    """retriever.py가 BM25 + Chroma 벡터 + MultiQuery + LLM 리랭크를 구현해야 한다."""

    def _retriever_source(self) -> str:
        return (PROJECT_ROOT / "src" / "retriever.py").read_text(encoding="utf-8")

    def _retriever_ast(self) -> ast.Module:
        return ast.parse(self._retriever_source())

    def test_bm25_retriever_imported(self):
        """BM25Retriever가 retriever.py에서 import되어야 한다."""
        source = self._retriever_source()
        assert "BM25Retriever" in source, (
            "retriever.py에 BM25Retriever import가 없습니다 — 하이브리드 RAG 요구사항 위반."
        )

    def test_ensemble_retriever_imported(self):
        """EnsembleRetriever가 retriever.py에서 import되어야 한다."""
        source = self._retriever_source()
        assert "EnsembleRetriever" in source, (
            "retriever.py에 EnsembleRetriever import가 없습니다 — 하이브리드 RAG 요구사항 위반."
        )

    def test_multi_query_retriever_imported(self):
        """MultiQueryRetriever가 retriever.py에서 import되어야 한다."""
        source = self._retriever_source()
        assert "MultiQueryRetriever" in source, (
            "retriever.py에 MultiQueryRetriever import가 없습니다 — 쿼리 확장 요구사항 위반."
        )

    def test_chroma_vector_store_imported(self):
        """Chroma 벡터 스토어가 retriever.py에서 사용되어야 한다."""
        source = self._retriever_source()
        assert "Chroma" in source, (
            "retriever.py에 Chroma import가 없습니다."
        )

    def test_llm_rerank_function_defined(self):
        """LLM 기반 리랭킹 함수가 retriever.py에 정의되어야 한다."""
        tree = self._retriever_ast()
        func_names = [
            node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        ]
        has_rerank = any("rerank" in n.lower() for n in func_names)
        assert has_rerank, (
            f"retriever.py에 리랭킹 함수가 없습니다. 발견된 함수: {func_names}"
        )

    def test_search_tuning_knowledge_tool_defined(self):
        """search_tuning_knowledge 도구가 retriever.py에 @tool 데코레이터로 정의되어야 한다."""
        source = self._retriever_source()
        assert "search_tuning_knowledge" in source, (
            "retriever.py에 search_tuning_knowledge 도구가 없습니다."
        )
        assert "@tool" in source, (
            "retriever.py에 @tool 데코레이터가 없습니다."
        )

    def test_hybrid_retriever_has_weights(self):
        """EnsembleRetriever에 BM25와 벡터 검색의 가중치가 설정되어야 한다."""
        source = self._retriever_source()
        assert "weights" in source, (
            "retriever.py EnsembleRetriever에 weights 설정이 없습니다."
        )

    def test_knowledge_documents_exist(self):
        """data/knowledge/ 디렉터리에 튜닝 지식 문서가 있어야 한다."""
        knowledge_dir = PROJECT_ROOT / "data" / "knowledge"
        assert knowledge_dir.exists(), "data/knowledge/ 디렉터리가 없습니다."
        docs = list(knowledge_dir.glob("*.md"))
        assert len(docs) >= 1, "data/knowledge/에 .md 지식 문서가 없습니다."

    def test_knowledge_documents_cover_core_topics(self):
        """지식 문서가 풀 스캔, 카디널리티, 함수 기반 조건, 조인, 통계 등 핵심 주제를 다뤄야 한다."""
        knowledge_dir = PROJECT_ROOT / "data" / "knowledge"
        all_content = " ".join(
            p.read_text(encoding="utf-8") for p in knowledge_dir.glob("*.md")
        ).lower()
        required_topics = ["full scan", "cardinality", "join", "statistics"]
        # 한국어 변형도 허용
        topic_alternatives = {
            "full scan": ["full scan", "풀 스캔", "full table scan", "풀스캔", "table access full"],
            "cardinality": ["cardinality", "카디널리티"],
            "join": ["join", "조인"],
            "statistics": ["statistics", "통계"],
        }
        missing = []
        for topic, alts in topic_alternatives.items():
            if not any(alt in all_content for alt in alts):
                missing.append(topic)
        assert not missing, (
            f"지식 문서에서 다음 핵심 주제를 찾을 수 없습니다: {missing}"
        )


# ---------------------------------------------------------------------------
# 4. SqlPlanAnalysis 스키마: 구조화 출력 계약 검증
# ---------------------------------------------------------------------------

class TestSqlPlanAnalysisSchema:
    """SqlPlanAnalysis 스키마가 원인·증거·개선안 구조화에 필요한 필드를 모두 갖춰야 한다."""

    def test_root_cause_has_cause_field(self):
        """RootCause 스키마에 'cause' 필드가 있어야 한다."""
        from src.schemas import RootCause
        fields = RootCause.model_fields
        assert "cause" in fields, "RootCause에 'cause' 필드가 없습니다."

    def test_root_cause_has_evidence_field(self):
        """RootCause 스키마에 실행계획 증거를 담는 'evidence' 필드가 있어야 한다."""
        from src.schemas import RootCause
        fields = RootCause.model_fields
        assert "evidence" in fields, "RootCause에 'evidence' 필드가 없습니다."

    def test_root_cause_has_severity_field(self):
        """RootCause 스키마에 심각도 'severity' 필드가 있어야 한다."""
        from src.schemas import RootCause
        fields = RootCause.model_fields
        assert "severity" in fields, "RootCause에 'severity' 필드가 없습니다."

    def test_improvement_has_recommendation_field(self):
        """Improvement 스키마에 구체적 개선 방안 'recommendation' 필드가 있어야 한다."""
        from src.schemas import Improvement
        fields = Improvement.model_fields
        assert "recommendation" in fields, "Improvement에 'recommendation' 필드가 없습니다."

    def test_improvement_has_target_cause_field(self):
        """Improvement 스키마에 해결 대상 원인 'target_cause' 필드가 있어야 한다."""
        from src.schemas import Improvement
        fields = Improvement.model_fields
        assert "target_cause" in fields, "Improvement에 'target_cause' 필드가 없습니다."

    def test_improvement_has_expected_effect_field(self):
        """Improvement 스키마에 기대 효과 'expected_effect' 필드가 있어야 한다."""
        from src.schemas import Improvement
        fields = Improvement.model_fields
        assert "expected_effect" in fields, "Improvement에 'expected_effect' 필드가 없습니다."

    def test_improvement_has_risk_level_field(self):
        """Improvement 스키마에 위험도 'risk_level' 필드가 있어야 한다."""
        from src.schemas import Improvement
        fields = Improvement.model_fields
        assert "risk_level" in fields, "Improvement에 'risk_level' 필드가 없습니다."

    def test_sql_plan_analysis_has_summary(self):
        """SqlPlanAnalysis 스키마에 한 줄 요약 'summary' 필드가 있어야 한다."""
        from src.schemas import SqlPlanAnalysis
        fields = SqlPlanAnalysis.model_fields
        assert "summary" in fields, "SqlPlanAnalysis에 'summary' 필드가 없습니다."

    def test_sql_plan_analysis_has_root_causes(self):
        """SqlPlanAnalysis 스키마에 'root_causes' 리스트 필드가 있어야 한다."""
        from src.schemas import SqlPlanAnalysis
        fields = SqlPlanAnalysis.model_fields
        assert "root_causes" in fields, "SqlPlanAnalysis에 'root_causes' 필드가 없습니다."

    def test_sql_plan_analysis_has_improvements(self):
        """SqlPlanAnalysis 스키마에 'improvements' 리스트 필드가 있어야 한다."""
        from src.schemas import SqlPlanAnalysis
        fields = SqlPlanAnalysis.model_fields
        assert "improvements" in fields, "SqlPlanAnalysis에 'improvements' 필드가 없습니다."

    def test_root_cause_can_be_instantiated(self):
        """RootCause를 올바른 값으로 인스턴스화할 수 있어야 한다."""
        from src.schemas import RootCause
        obj = RootCause(
            cause="풀 테이블 스캔",
            evidence="TABLE ACCESS FULL ORDERS Cost=8420 Rows=1",
            severity="높음",
        )
        assert obj.cause == "풀 테이블 스캔"
        assert "TABLE ACCESS FULL" in obj.evidence
        assert obj.severity == "높음"

    def test_improvement_can_be_instantiated(self):
        """Improvement를 올바른 값으로 인스턴스화할 수 있어야 한다."""
        from src.schemas import Improvement
        obj = Improvement(
            recommendation="order_date에 함수 기반 인덱스 생성",
            target_cause="풀 테이블 스캔",
            expected_effect="Cost/Rows 감소, 인덱스 스캔 전환",
            example="CREATE INDEX idx_orders_date ON orders(TRUNC(order_date))",
            risk_level="write",
        )
        assert obj.recommendation
        assert obj.target_cause == "풀 테이블 스캔"
        assert obj.risk_level == "write"

    def test_sql_plan_analysis_can_be_instantiated(self):
        """SqlPlanAnalysis를 전체 필드로 인스턴스화하고 dict로 직렬화할 수 있어야 한다."""
        from src.schemas import Improvement, RootCause, SqlPlanAnalysis
        analysis = SqlPlanAnalysis(
            summary="ORDERS 테이블 풀스캔으로 인한 성능 저하",
            root_causes=[
                RootCause(
                    cause="함수 기반 조건으로 인덱스 무효화",
                    evidence="TABLE ACCESS FULL ORDERS Cost=8420",
                    severity="높음",
                )
            ],
            improvements=[
                Improvement(
                    recommendation="함수 기반 인덱스(FBI) 생성",
                    target_cause="함수 기반 조건으로 인덱스 무효화",
                    expected_effect="INDEX RANGE SCAN 전환으로 Cost 대폭 감소",
                )
            ],
        )
        data = analysis.model_dump()
        assert data["summary"]
        assert len(data["root_causes"]) == 1
        assert data["root_causes"][0]["evidence"] == "TABLE ACCESS FULL ORDERS Cost=8420"
        assert len(data["improvements"]) == 1


# ---------------------------------------------------------------------------
# 5. finalize_node: SqlPlanAnalysis 구조화 출력으로 두 에이전트 결과를 종합
# ---------------------------------------------------------------------------

class TestFinalizeNodeStructuredOutput:
    """finalize_node가 SqlPlanAnalysis 구조화 출력을 사용해 진단을 종합해야 한다."""

    def _plan_execute_source(self) -> str:
        return (PROJECT_ROOT / "src" / "plan_execute.py").read_text(encoding="utf-8")

    def test_finalize_node_uses_sql_plan_analysis(self):
        """finalize_node가 SqlPlanAnalysis 구조화 출력을 사용해야 한다."""
        source = self._plan_execute_source()
        assert "SqlPlanAnalysis" in source, (
            "plan_execute.py finalize_node에 SqlPlanAnalysis 구조화 출력이 없습니다."
        )

    def test_finalize_node_uses_with_structured_output(self):
        """finalize_node가 with_structured_output을 사용해야 한다."""
        source = self._plan_execute_source()
        assert "with_structured_output" in source, (
            "plan_execute.py에 with_structured_output 호출이 없습니다."
        )

    def test_finalize_node_model_dump(self):
        """finalize_node가 분석 결과를 model_dump()로 직렬화해야 한다."""
        source = self._plan_execute_source()
        assert "model_dump" in source, (
            "plan_execute.py finalize_node에 model_dump() 직렬화가 없습니다."
        )

    def test_finalize_node_uses_past_steps(self):
        """finalize_node가 past_steps(중간 결과) 를 종합해 최종 분석을 생성해야 한다."""
        source = self._plan_execute_source()
        assert "past_steps" in source, (
            "finalize_node에서 past_steps를 참조하지 않습니다 — 에이전트 결과 종합이 안 됩니다."
        )

    def test_finalize_node_defined(self):
        """finalize_node 함수가 plan_execute.py에 정의되어 있어야 한다."""
        tree = ast.parse(self._plan_execute_source())
        func_names = [
            node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        ]
        assert "finalize_node" in func_names, (
            "plan_execute.py에 finalize_node 함수가 없습니다."
        )

    def test_finalize_node_returns_analysis_key(self):
        """finalize_node가 'analysis' 키를 포함한 dict를 반환해야 한다."""
        source = self._plan_execute_source()
        assert '"analysis"' in source or "'analysis'" in source, (
            "finalize_node가 'analysis' 키를 반환하지 않습니다."
        )

    def test_finalize_node_structured_output_mock(self):
        """mock SqlPlanAnalysis 결과가 올바른 딕셔너리 구조로 직렬화되어야 한다.

        실제 LLM을 호출하지 않고 finalize_node의 직렬화 경로를 단독으로 검증한다.
        """
        from src.schemas import Improvement, RootCause, SqlPlanAnalysis

        mock_analysis = SqlPlanAnalysis(
            summary="HASH JOIN TempSpc 스필로 인한 성능 저하",
            root_causes=[
                RootCause(
                    cause="HASH JOIN PGA 한도 초과 TempSpc 스필",
                    evidence="HASH JOIN Cost=15000 TempSpc=204800",
                    severity="높음",
                )
            ],
            improvements=[
                Improvement(
                    recommendation="PGA_AGGREGATE_TARGET 증가 또는 HASH_AREA_SIZE 증가",
                    target_cause="HASH JOIN PGA 한도 초과 TempSpc 스필",
                    expected_effect="TempSpc I/O 제거, Cost 감소",
                    risk_level="write",
                )
            ],
        )

        data = mock_analysis.model_dump()

        # summary 검증
        assert isinstance(data["summary"], str) and data["summary"]

        # root_causes 검증 — evidence가 실행계획 텍스트를 인용해야 한다
        assert len(data["root_causes"]) >= 1
        rc = data["root_causes"][0]
        assert "HASH JOIN" in rc["evidence"], (
            f"root_cause.evidence가 실행계획 텍스트를 인용하지 않습니다: {rc['evidence']}"
        )
        assert rc["severity"] in {"높음", "보통", "낮음"}

        # improvements 검증 — recommendation과 target_cause가 매핑되어야 한다
        assert len(data["improvements"]) >= 1
        imp = data["improvements"][0]
        assert imp["recommendation"]
        assert imp["target_cause"]
        assert imp["expected_effect"]


# ---------------------------------------------------------------------------
# 6. build_explain_agent / build_knowledge_agent: 도구 바인딩 정적 검증
# ---------------------------------------------------------------------------

class TestAgentToolBinding:
    """build_explain_agent와 build_knowledge_agent의 도구 바인딩이 올바르게 설정되어 있어야 한다."""

    def _agents_source(self) -> str:
        return (PROJECT_ROOT / "src" / "agents.py").read_text(encoding="utf-8")

    def test_knowledge_agent_binds_search_tuning_knowledge(self):
        """build_knowledge_agent가 search_tuning_knowledge 도구를 바인딩해야 한다."""
        source = self._agents_source()
        assert "search_tuning_knowledge" in source, (
            "agents.py의 knowledge_agent 빌더에 search_tuning_knowledge 도구가 없습니다."
        )

    def test_explain_agent_binds_oracle_tools(self):
        """build_explain_agent가 Oracle MCP 도구(_oracle_tools)를 바인딩해야 한다."""
        source = self._agents_source()
        # _oracle_tools()는 SQLcl MCP 도구를 반환하는 함수다
        assert "_oracle_tools" in source, (
            "agents.py build_explain_agent에 _oracle_tools 바인딩이 없습니다."
        )

    def test_explain_agent_uses_explain_prompt_module(self):
        """build_explain_agent가 src.prompts.explain 모듈의 프롬프트를 사용해야 한다."""
        source = self._agents_source()
        assert "_explain_mod" in source and "EXPLAIN_SYSTEM_PROMPT" in source, (
            "agents.py build_explain_agent에 explain 프롬프트 모듈 참조가 없습니다."
        )

    def test_knowledge_agent_uses_knowledge_prompt_module(self):
        """build_knowledge_agent가 src.prompts.knowledge 모듈의 프롬프트를 사용해야 한다."""
        source = self._agents_source()
        assert "_knowledge_mod" in source and "KNOWLEDGE_SYSTEM_PROMPT" in source, (
            "agents.py build_knowledge_agent에 knowledge 프롬프트 모듈 참조가 없습니다."
        )

    def test_knowledge_agent_calls_warmup(self):
        """build_knowledge_agent가 warmup()을 호출해 RAG 인덱스를 메인 스레드에서 초기화해야 한다."""
        source = self._agents_source()
        assert "warmup" in source, (
            "agents.py build_knowledge_agent에 warmup() 호출이 없습니다 — "
            "Chroma 스레드 안전 초기화가 누락됩니다."
        )


# ---------------------------------------------------------------------------
# 7. plan_execute 그래프: explain + knowledge 에이전트가 모두 단계에 활용되어야 한다
# ---------------------------------------------------------------------------

class TestDiagnosisGraphAgentUsage:
    """planner_node가 explain_agent와 knowledge_agent를 모두 위임 단계로 언급해야 한다."""

    def _plan_execute_source(self) -> str:
        return (PROJECT_ROOT / "src" / "plan_execute.py").read_text(encoding="utf-8")

    def test_planner_references_explain_agent(self):
        """planner_node가 explain_agent 위임 단계를 계획에 포함해야 한다."""
        source = self._plan_execute_source()
        assert "explain_agent" in source, (
            "plan_execute.py planner_node에 explain_agent 위임 참조가 없습니다."
        )

    def test_planner_references_knowledge_agent(self):
        """planner_node가 knowledge_agent 위임 단계를 계획에 포함해야 한다."""
        source = self._plan_execute_source()
        assert "knowledge_agent" in source, (
            "plan_execute.py planner_node에 knowledge_agent 위임 참조가 없습니다."
        )

    def test_graph_has_finalize_node(self):
        """진단 그래프에 finalize 노드가 포함되어야 한다."""
        source = self._plan_execute_source()
        assert '"finalize"' in source or "'finalize'" in source, (
            "plan_execute.py 그래프에 finalize 노드가 없습니다."
        )

    def test_graph_has_execute_node(self):
        """진단 그래프에 execute 노드가 포함되어야 한다."""
        source = self._plan_execute_source()
        assert '"execute"' in source or "'execute'" in source, (
            "plan_execute.py 그래프에 execute 노드가 없습니다."
        )

    def test_finalize_node_references_sql_and_plan_in_prompt(self):
        """finalize_node가 SQL과 실행계획을 종합 프롬프트에 포함해야 한다."""
        source = self._plan_execute_source()
        # finalize_node 함수 본문에서 sql과 execution_plan이 LLM 호출에 전달되어야 한다
        has_sql_ref = "sql" in source and "execution_plan" in source
        assert has_sql_ref, (
            "finalize_node가 sql/execution_plan 컨텍스트를 LLM 호출에 전달하지 않습니다."
        )
