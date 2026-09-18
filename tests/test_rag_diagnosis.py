"""AC 12: explain_agent와 knowledge_agent는 실행계획 분석과 하이브리드 RAG 튜닝 근거를 결합해
원인, 실행계획 증거, 개선안을 구조화해 반환한다.

검증 전략 — 실제 LLM/Oracle/Chroma 호출 없이 **동작**을 검증한다.
1) plan_execute 진단 그래프: planner/execute/finalize 노드에 가짜 LLM·supervisor·store를 주입해
   explain_agent 결과(실행계획 증거)와 knowledge_agent 결과(RAG 튜닝 근거)가 하나의
   SqlPlanAnalysis(summary/root_causes/improvements)로 종합되는지 확인한다.
2) retriever: BM25 + Chroma 벡터 + MultiQuery + LLM 리랭크로 이어지는 하이브리드 RAG 배선과
   중복 제거·출처 표기 동작을 확인한다.
3) agents: explain_agent는 SQLcl MCP 도구, knowledge_agent는 search_tuning_knowledge 도구에
   각각 전용 프롬프트 모듈로 묶이는지 확인한다.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.schemas import Improvement, Plan, RootCause, SqlPlanAnalysis  # noqa: E402


# ---------------------------------------------------------------------------
# 공용 테스트 더블
# ---------------------------------------------------------------------------

class _FakeStructured:
    """with_structured_output(...) 이 돌려주는 러너 대역."""

    def __init__(self, schema, result, captured_prompts):
        self.schema = schema
        self._result = result
        self._captured = captured_prompts

    def invoke(self, prompt):
        self._captured.append(prompt)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakeLLM:
    """default_llm() 대역 — 구조화 출력 스키마별 결과를 미리 심어 둔다."""

    def __init__(self, results: dict):
        self.results = results
        self.prompts: list[str] = []
        self.schemas: list = []

    def with_structured_output(self, schema):
        self.schemas.append(schema)
        if schema not in self.results:
            raise AssertionError(f"예상하지 못한 구조화 출력 스키마: {schema}")
        return _FakeStructured(schema, self.results[schema], self.prompts)

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return self.results.get("text", "")


class _StoreItem:
    def __init__(self, value):
        self.value = value


class _FakeStore:
    """langgraph BaseStore 대역 — 네임스페이스/키 기반 get/put만 제공한다."""

    def __init__(self):
        self.data: dict = {}
        self.puts: list[tuple] = []

    def get(self, namespace, key):
        return self.data.get((namespace, key))

    def put(self, namespace, key, value):
        self.puts.append((namespace, key, value))
        self.data[(namespace, key)] = _StoreItem(value)


EXPLAIN_OUTPUT = (
    "[explain_agent] ORDERS 접근 경로가 TABLE ACCESS FULL ORDERS 이고 Cost=8420, Rows=1 로 "
    "카디널리티 오추정이 보입니다. 이어지는 HASH JOIN 단계에서 TempSpc=204800 스필이 발생합니다."
)
KNOWLEDGE_OUTPUT = (
    "[knowledge_agent] [출처: full_scan.md] WHERE 절에서 컬럼을 TRUNC()로 감싸면 일반 인덱스가 "
    "무효화되므로 함수 기반 인덱스(FBI)를 검토한다.\n\n---\n\n"
    "[출처: join_tuning.md] HASH JOIN TempSpc 스필은 PGA 부족 신호다."
)

SAMPLE_ANALYSIS = SqlPlanAnalysis(
    summary="TRUNC(order_date) 조건으로 인덱스가 무효화되어 TABLE ACCESS FULL ORDERS 발생",
    root_causes=[
        RootCause(
            cause="함수 기반 조건으로 인덱스 무효화",
            evidence="TABLE ACCESS FULL ORDERS Cost=8420 Rows=1",
            severity="높음",
        ),
        RootCause(
            cause="HASH JOIN PGA 부족으로 TempSpc 스필",
            evidence="HASH JOIN TempSpc=204800",
            severity="보통",
        ),
    ],
    improvements=[
        Improvement(
            recommendation="TRUNC(order_date) 함수 기반 인덱스 생성",
            target_cause="함수 기반 조건으로 인덱스 무효화",
            expected_effect="INDEX RANGE SCAN 전환으로 Cost 감소",
            example="CREATE INDEX idx_orders_trunc_dt ON orders(TRUNC(order_date))",
            risk_level="write",
        )
    ],
)


# ---------------------------------------------------------------------------
# 1. finalize_node: 실행계획 분석 + RAG 튜닝 근거 → 구조화 진단
# ---------------------------------------------------------------------------

class TestDiagnosisCombinesPlanAndKnowledge:
    """finalize_node가 두 에이전트 결과를 하나의 구조화 진단으로 종합해야 한다."""

    def _run_finalize(self, monkeypatch, store=None):
        import src.plan_execute as pe

        fake_llm = _FakeLLM({SqlPlanAnalysis: SAMPLE_ANALYSIS})
        monkeypatch.setattr(pe, "default_llm", lambda: fake_llm)
        store = store or _FakeStore()
        state = {
            "sql": "SELECT * FROM orders WHERE TRUNC(order_date) = TRUNC(SYSDATE)",
            "execution_plan": "TABLE ACCESS FULL ORDERS Cost=8420 Rows=1\nHASH JOIN TempSpc=204800",
            "question": "왜 느린가요?",
            "plan": [],
            "past_steps": [
                ("explain_agent에게 실행계획 문제 분석 위임", EXPLAIN_OUTPUT),
                ("knowledge_agent에게 관련 튜닝 패턴 검색 위임", KNOWLEDGE_OUTPUT),
            ],
            "from_memory": False,
            "analysis": None,
        }
        result = pe.finalize_node(state, store=store)
        return result, fake_llm, store

    def test_finalize_uses_sql_plan_analysis_schema(self, monkeypatch):
        """종합 단계는 SqlPlanAnalysis 구조화 출력을 사용해야 한다."""
        _, fake_llm, _ = self._run_finalize(monkeypatch)
        assert SqlPlanAnalysis in fake_llm.schemas

    def test_finalize_prompt_includes_explain_agent_output(self, monkeypatch):
        """종합 프롬프트에 explain_agent의 실행계획 분석 결과가 포함되어야 한다."""
        _, fake_llm, _ = self._run_finalize(monkeypatch)
        prompt = fake_llm.prompts[0]
        assert EXPLAIN_OUTPUT in prompt
        assert "explain_agent" in prompt

    def test_finalize_prompt_includes_knowledge_agent_output(self, monkeypatch):
        """종합 프롬프트에 knowledge_agent의 RAG 튜닝 근거가 출처와 함께 포함되어야 한다."""
        _, fake_llm, _ = self._run_finalize(monkeypatch)
        prompt = fake_llm.prompts[0]
        assert KNOWLEDGE_OUTPUT in prompt
        assert "[출처: full_scan.md]" in prompt

    def test_finalize_prompt_includes_sql_and_execution_plan(self, monkeypatch):
        """종합 프롬프트에 원본 SQL과 실행계획 원문이 함께 전달되어야 한다."""
        _, fake_llm, _ = self._run_finalize(monkeypatch)
        prompt = fake_llm.prompts[0]
        assert "TRUNC(order_date)" in prompt
        assert "TABLE ACCESS FULL ORDERS Cost=8420" in prompt

    def test_finalize_returns_structured_analysis_dict(self, monkeypatch):
        """finalize_node는 summary/root_causes/improvements 를 가진 dict를 반환해야 한다."""
        result, _, _ = self._run_finalize(monkeypatch)
        analysis = result["analysis"]
        assert isinstance(analysis, dict)
        assert set(analysis) >= {"summary", "root_causes", "improvements"}
        assert analysis["summary"]

    def test_root_causes_carry_execution_plan_evidence(self, monkeypatch):
        """각 원인은 실행계획 원문을 인용한 evidence와 severity를 가져야 한다."""
        result, _, _ = self._run_finalize(monkeypatch)
        causes = result["analysis"]["root_causes"]
        assert causes, "root_causes가 비어 있습니다."
        for rc in causes:
            assert rc["cause"]
            assert rc["evidence"]
            assert rc["severity"] in {"높음", "보통", "낮음"}
        assert any("TABLE ACCESS FULL" in rc["evidence"] for rc in causes)

    def test_improvements_map_to_root_causes(self, monkeypatch):
        """개선안은 해결 대상 원인·기대 효과·위험도를 갖고 원인 목록과 연결되어야 한다."""
        result, _, _ = self._run_finalize(monkeypatch)
        analysis = result["analysis"]
        cause_names = {rc["cause"] for rc in analysis["root_causes"]}
        assert analysis["improvements"], "improvements가 비어 있습니다."
        for imp in analysis["improvements"]:
            assert imp["recommendation"]
            assert imp["expected_effect"]
            assert imp["risk_level"] in {"read", "write", "destructive"}
            assert imp["target_cause"] in cause_names

    def test_finalize_persists_analysis_by_sql_fingerprint(self, monkeypatch):
        """종합 결과는 SQL 지문 키로 장기 메모리에 저장되어야 한다."""
        import src.plan_execute as pe

        result, _, store = self._run_finalize(monkeypatch)
        sql = "SELECT * FROM orders WHERE TRUNC(order_date) = TRUNC(SYSDATE)"
        namespace, key, value = store.puts[0]
        assert namespace == pe.MEMORY_NAMESPACE
        assert key == pe.sql_fingerprint(sql)
        assert value == result["analysis"]

    def test_finalize_reuses_memory_without_llm_call(self, monkeypatch):
        """from_memory 경로에서는 LLM을 호출하지 않고 저장된 진단을 재사용해야 한다."""
        import src.plan_execute as pe

        cached = SAMPLE_ANALYSIS.model_dump()
        sql = "SELECT 1 FROM dual"
        store = _FakeStore()
        store.data[(pe.MEMORY_NAMESPACE, pe.sql_fingerprint(sql))] = _StoreItem(cached)

        llm = MagicMock()
        monkeypatch.setattr(pe, "default_llm", llm)
        out = pe.finalize_node(
            {
                "sql": sql,
                "execution_plan": "",
                "question": "",
                "plan": [],
                "past_steps": [],
                "from_memory": True,
                "analysis": None,
            },
            store=store,
        )
        assert out["analysis"] == cached
        llm.assert_not_called()


# ---------------------------------------------------------------------------
# 2. planner/execute 노드: 두 에이전트가 진단 단계로 실제 사용되는지
# ---------------------------------------------------------------------------

class TestDiagnosisGraphUsesBothAgents:
    def test_planner_prompt_delegates_to_both_agents(self, monkeypatch):
        """planner는 explain_agent와 knowledge_agent 위임 단계를 계획하도록 지시받아야 한다."""
        import src.plan_execute as pe

        fake_llm = _FakeLLM({Plan: Plan(steps=["explain_agent 위임", "knowledge_agent 위임"])})
        monkeypatch.setattr(pe, "default_llm", lambda: fake_llm)
        out = pe.planner_node(
            {
                "sql": "SELECT * FROM orders",
                "execution_plan": "TABLE ACCESS FULL ORDERS",
                "question": "",
                "plan": [],
                "past_steps": [],
                "from_memory": False,
                "analysis": None,
            },
            store=_FakeStore(),
        )
        prompt = fake_llm.prompts[0]
        assert "explain_agent" in prompt and "knowledge_agent" in prompt
        assert out["plan"] == ["explain_agent 위임", "knowledge_agent 위임"]
        assert out["from_memory"] is False

    def test_execute_node_records_agent_output_in_past_steps(self, monkeypatch):
        """execute 단계 결과(에이전트 응답)가 past_steps에 누적되어 finalize로 전달되어야 한다."""
        import src.plan_execute as pe
        from langchain_core.messages import AIMessage

        captured = {}

        class _FakeSupervisor:
            async def ainvoke(self, payload):
                captured["msg"] = payload["messages"][0].content
                return {"messages": [AIMessage(content=EXPLAIN_OUTPUT)]}

        monkeypatch.setattr(pe, "_supervisor", lambda: _FakeSupervisor())
        import asyncio
        out = asyncio.run(pe.execute_node(
            {
                "sql": "SELECT * FROM orders",
                "execution_plan": "TABLE ACCESS FULL ORDERS Cost=8420",
                "question": "",
                "plan": ["explain_agent에게 실행계획 문제 분석 위임", "knowledge_agent 위임"],
                "past_steps": [],
                "from_memory": False,
                "analysis": None,
            },
            store=_FakeStore(),
        ))
        assert out["past_steps"] == [("explain_agent에게 실행계획 문제 분석 위임", EXPLAIN_OUTPUT)]
        assert out["plan"] == ["knowledge_agent 위임"]
        assert "TABLE ACCESS FULL ORDERS Cost=8420" in captured["msg"]

    def test_graph_builds_with_finalize_route(self, monkeypatch):
        """replan 이후 남은 단계가 없으면 finalize 로 라우팅되어야 한다."""
        import src.plan_execute as pe

        assert pe.route_after_replan({"plan": []}) == "finalize"
        assert pe.route_after_replan({"plan": ["남은 단계"]}) == "execute"


# ---------------------------------------------------------------------------
# 3. 하이브리드 RAG (BM25 + 벡터 + MultiQuery + LLM 리랭크)
# ---------------------------------------------------------------------------

def _install_retriever_import_stubs() -> None:
    """langchain_classic.retrievers 가 이 환경의 scipy/numpy 바이너리 불일치로 import 되지
    않을 수 있다. 실패하는 경우에만 최소 대역을 sys.modules 에 심어 retriever 모듈 자체의
    배선을 테스트할 수 있게 한다(테스트는 어차피 이 심볼들을 monkeypatch 로 교체한다)."""
    try:  # pragma: no cover - 환경에 따라 분기
        import langchain_classic.retrievers  # noqa: F401
        import langchain_classic.retrievers.multi_query  # noqa: F401
        return
    except Exception:
        pass

    base = types.ModuleType("langchain_classic.retrievers")

    class _EnsembleRetriever:  # noqa: D401 - 대역
        def __init__(self, retrievers=None, weights=None, **kwargs):
            self.retrievers = retrievers or []
            self.weights = weights or []

    class _MultiQueryRetriever:  # noqa: D401 - 대역
        def __init__(self, retriever=None, llm=None):
            self.retriever = retriever
            self.llm = llm

        @classmethod
        def from_llm(cls, retriever=None, llm=None, **kwargs):
            return cls(retriever=retriever, llm=llm)

    base.EnsembleRetriever = _EnsembleRetriever
    mq = types.ModuleType("langchain_classic.retrievers.multi_query")
    mq.MultiQueryRetriever = _MultiQueryRetriever
    base.multi_query = mq
    sys.modules["langchain_classic.retrievers"] = base
    sys.modules["langchain_classic.retrievers.multi_query"] = mq


_install_retriever_import_stubs()
import src.retriever as retriever_mod  # noqa: E402
from langchain_core.documents import Document  # noqa: E402


class _CapturedEnsemble:
    def __init__(self, retrievers=None, weights=None, **kwargs):
        self.retrievers = retrievers or []
        self.weights = weights or []


class _CapturedMultiQuery:
    created: list = []

    def __init__(self, retriever=None, llm=None):
        self.retriever = retriever
        self.llm = llm

    @classmethod
    def from_llm(cls, retriever=None, llm=None, **kwargs):
        obj = cls(retriever=retriever, llm=llm)
        cls.created.append(obj)
        return obj


class TestHybridRetrieverWiring:
    """BM25 + Chroma 벡터를 가중 앙상블하고 MultiQuery 로 감싸야 한다."""

    @pytest.fixture
    def wired(self, monkeypatch):
        retriever_mod._hybrid_retriever.cache_clear()
        _CapturedMultiQuery.created = []

        bm25_holder = {}

        class _FakeBM25:
            @classmethod
            def from_documents(cls, docs, preprocess_func=None, **kwargs):
                obj = cls()
                obj.docs = docs
                obj.preprocess_func = preprocess_func
                obj.k = None
                bm25_holder["obj"] = obj
                return obj

        class _FakeVectorDB:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.added = []

            def get(self):
                return {"ids": ["existing"]}

            def add_documents(self, docs):
                self.added.extend(docs)

            def as_retriever(self, search_kwargs=None):
                self.search_kwargs = search_kwargs
                return f"vector_retriever:{search_kwargs}"

        chroma_holder = {}

        def _fake_chroma(**kwargs):
            db = _FakeVectorDB(**kwargs)
            chroma_holder["obj"] = db
            return db

        monkeypatch.setattr(retriever_mod, "Chroma", _fake_chroma)
        monkeypatch.setattr(retriever_mod, "BM25Retriever", _FakeBM25)
        monkeypatch.setattr(retriever_mod, "EnsembleRetriever", _CapturedEnsemble)
        monkeypatch.setattr(retriever_mod, "MultiQueryRetriever", _CapturedMultiQuery)
        monkeypatch.setattr(retriever_mod, "default_embeddings", lambda: "fake-embeddings")
        monkeypatch.setattr(retriever_mod, "default_llm", lambda: "fake-llm")

        built = retriever_mod._hybrid_retriever()
        yield built, bm25_holder["obj"], chroma_holder["obj"]
        retriever_mod._hybrid_retriever.cache_clear()

    def test_returns_multi_query_wrapped_retriever(self, wired):
        built, _, _ = wired
        assert isinstance(built, _CapturedMultiQuery)
        assert built.llm == "fake-llm"

    def test_inner_retriever_is_weighted_ensemble(self, wired):
        built, bm25, _ = wired
        ensemble = built.retriever
        assert isinstance(ensemble, _CapturedEnsemble)
        assert ensemble.weights == [0.3, 0.7], "BM25/벡터 가중치가 설정되지 않았습니다."
        assert bm25 in ensemble.retrievers, "앙상블에 BM25 검색기가 포함되지 않았습니다."
        assert any(
            isinstance(r, str) and r.startswith("vector_retriever") for r in ensemble.retrievers
        ), "앙상블에 Chroma 벡터 검색기가 포함되지 않았습니다."

    def test_bm25_uses_korean_tokenizer(self, wired):
        _, bm25, _ = wired
        assert bm25.preprocess_func is retriever_mod.korean_tokenizer
        assert bm25.k == 3
        assert bm25.docs, "BM25가 지식 문서를 로드하지 않았습니다."

    def test_vector_store_uses_tuning_collection(self, wired):
        _, _, chroma = wired
        assert chroma.kwargs["collection_name"] == retriever_mod.COLLECTION_NAME
        assert chroma.kwargs["embedding_function"] == "fake-embeddings"
        assert chroma.search_kwargs == {"k": 3}

    def test_korean_tokenizer_extracts_content_words(self):
        tokens = retriever_mod.korean_tokenizer("풀 테이블 스캔이 발생하는 이유는 무엇인가요")
        assert tokens, "한국어 토크나이저가 토큰을 만들지 못했습니다."
        assert any("스캔" in t or "테이블" in t for t in tokens)


class TestLlmRerank:
    """LLM 리랭크는 관련도 점수 순으로 문서를 재정렬해야 한다."""

    def _docs(self):
        return [
            Document(page_content="조인 튜닝", metadata={"source": "join.md"}),
            Document(page_content="풀 스캔 원인", metadata={"source": "full_scan.md"}),
            Document(page_content="통계 재수집", metadata={"source": "stats.md"}),
        ]

    def test_reranks_by_score_and_truncates(self, monkeypatch):
        scores = retriever_mod._RelevanceScore(scores=[2, 9, 5])
        monkeypatch.setattr(
            retriever_mod, "default_llm", lambda: _FakeLLM({retriever_mod._RelevanceScore: scores})
        )
        top = retriever_mod._llm_rerank("풀 스캔 원인이 뭔가요", self._docs(), top_k=2)
        assert [d.metadata["source"] for d in top] == ["full_scan.md", "stats.md"]

    def test_empty_docs_short_circuit(self, monkeypatch):
        llm = MagicMock()
        monkeypatch.setattr(retriever_mod, "default_llm", llm)
        assert retriever_mod._llm_rerank("질의", []) == []
        llm.assert_not_called()

    def test_score_length_mismatch_falls_back_to_input_order(self, monkeypatch):
        scores = retriever_mod._RelevanceScore(scores=[7])
        monkeypatch.setattr(
            retriever_mod, "default_llm", lambda: _FakeLLM({retriever_mod._RelevanceScore: scores})
        )
        top = retriever_mod._llm_rerank("질의", self._docs(), top_k=3)
        assert [d.metadata["source"] for d in top] == ["join.md", "full_scan.md", "stats.md"]


class TestSearchTuningKnowledge:
    """search_tuning_knowledge 는 하이브리드 검색 결과를 출처와 함께 돌려줘야 한다."""

    def _wire(self, monkeypatch, candidates, scores):
        class _FakeHybrid:
            def invoke(self, query):
                return candidates

        monkeypatch.setattr(retriever_mod, "_hybrid_retriever", lambda: _FakeHybrid())
        monkeypatch.setattr(
            retriever_mod,
            "default_llm",
            lambda: _FakeLLM({retriever_mod._RelevanceScore: retriever_mod._RelevanceScore(scores=scores)}),
        )

    def test_deduplicates_and_cites_sources(self, monkeypatch):
        docs = [
            Document(page_content="풀 스캔 원인", metadata={"source": "full_scan.md"}),
            Document(page_content="풀 스캔 원인", metadata={"source": "full_scan.md"}),
            Document(page_content="함수 기반 인덱스", metadata={"source": "fbi.md"}),
        ]
        self._wire(monkeypatch, docs, [9, 6])
        out = retriever_mod.search_tuning_knowledge_impl("풀 테이블 스캔 개선", top_k=2)
        assert out.count("[출처: full_scan.md]") == 1, "중복 문서가 제거되지 않았습니다."
        assert "[출처: fbi.md]" in out
        assert out.index("[출처: full_scan.md]") < out.index("[출처: fbi.md]")

    def test_no_hit_returns_explicit_message(self, monkeypatch):
        self._wire(monkeypatch, [], [])
        assert retriever_mod.search_tuning_knowledge_impl("없는 주제") == "관련 지식을 찾지 못했습니다."

    def test_tool_is_exposed_to_knowledge_agent(self, monkeypatch):
        docs = [Document(page_content="HASH JOIN TempSpc 스필", metadata={"source": "join.md"})]
        self._wire(monkeypatch, docs, [8])
        assert retriever_mod.search_tuning_knowledge.name == "search_tuning_knowledge"
        out = retriever_mod.search_tuning_knowledge.invoke({"query": "HASH JOIN 스필"})
        assert "[출처: join.md]" in out

    def test_knowledge_base_documents_cover_core_topics(self):
        docs = retriever_mod.load_documents()
        assert docs, "data/knowledge/ 에 튜닝 지식 문서가 없습니다."
        corpus = " ".join(d.page_content for d in docs).lower()
        alternatives = {
            "full scan": ["full scan", "풀 스캔", "풀스캔", "table access full"],
            "cardinality": ["cardinality", "카디널리티"],
            "join": ["join", "조인"],
            "statistics": ["statistics", "통계"],
        }
        missing = [t for t, alts in alternatives.items() if not any(a in corpus for a in alts)]
        assert not missing, f"지식 문서에 없는 핵심 주제: {missing}"


# ---------------------------------------------------------------------------
# 4. 두 에이전트의 도구·프롬프트 결합
# ---------------------------------------------------------------------------

class TestAgentComposition:
    @pytest.fixture
    def capture_create_agent(self, monkeypatch):
        import src.agents as agents

        calls = []

        def _fake_create_agent(**kwargs):
            calls.append(kwargs)
            return MagicMock(name=kwargs.get("name"))

        monkeypatch.setattr(agents, "create_agent", _fake_create_agent)
        monkeypatch.setattr(agents, "default_llm", lambda: "fake-llm")
        return agents, calls

    def test_explain_agent_binds_sqlcl_mcp_tools(self, capture_create_agent, monkeypatch):
        """explain_agent는 SQLcl MCP 도구와 실행계획 분석 프롬프트로 구성되어야 한다."""
        agents, calls = capture_create_agent
        sentinel = ["mcp-run-sql"]
        monkeypatch.setattr(agents, "_oracle_tools", lambda: sentinel)

        agents.build_explain_agent()
        kwargs = calls[-1]
        assert kwargs["name"] == "explain_agent"
        assert kwargs["tools"] == sentinel
        assert kwargs["system_prompt"] == agents.EXPLAIN_SYSTEM_PROMPT
        assert "TABLE ACCESS FULL" in kwargs["system_prompt"]

    def test_knowledge_agent_binds_rag_tool_after_warmup(self, capture_create_agent, monkeypatch):
        """knowledge_agent는 warmup 후 search_tuning_knowledge RAG 도구로 구성되어야 한다."""
        agents, calls = capture_create_agent
        warmed = []
        monkeypatch.setattr(retriever_mod, "warmup", lambda: warmed.append(True))

        agents.build_knowledge_agent()
        kwargs = calls[-1]
        assert warmed == [True], "RAG 인덱스 warmup 이 호출되지 않았습니다."
        assert kwargs["name"] == "knowledge_agent"
        assert [t.name for t in kwargs["tools"]] == ["search_tuning_knowledge"]
        assert kwargs["system_prompt"] == agents.KNOWLEDGE_SYSTEM_PROMPT
        assert "search_tuning_knowledge" in kwargs["system_prompt"]

    def test_both_agents_registered_for_diagnosis_routing(self):
        """supervisor 라우팅 대상에 두 진단 에이전트가 모두 등록되어 있어야 한다."""
        import src.agents as agents

        assert {"explain_agent", "knowledge_agent"} <= set(agents.AGENT_NAMES)
        from src.prompts.supervisor import SYSTEM_PROMPT
        assert "explain_agent" in SYSTEM_PROMPT
        assert "knowledge_agent" in SYSTEM_PROMPT
