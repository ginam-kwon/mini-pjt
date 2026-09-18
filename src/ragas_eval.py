# ragas_eval.py - RAGAS 4지표(positive/edge 케이스 전용) 계산
#
# ragas 0.4.3의 신규 metrics.collections API는 "modern"(instructor 기반) LLM/임베딩 어댑터만
# 받는다. 우리는 Bedrock을 쓰므로: LLM은 anthropic SDK의 AnthropicBedrock 클라이언트로
# ragas.llms.llm_factory(provider="anthropic")에 연결하고, 임베딩은 BedrockEmbeddings를
# BaseRagasEmbedding으로 감싸는 얇은 어댑터를 직접 구현한다.
#
# 호환성 메모: ragas 0.4.3은 import 시점에 langchain_community.chat_models.vertexai 를
# 참조하는데, 이 프로젝트가 쓰는 langchain-community(0.4.2, deprecated)에는 그 모듈이 없다.
# 실제로 VertexAI를 쓰지 않으므로, ragas import 전에 더미 모듈을 등록해 우회한다
# (설치된 패키지를 건드리지 않는 국소적 호환성 shim).
from __future__ import annotations

import sys
import types

if "langchain_community.chat_models.vertexai" not in sys.modules:
    _stub = types.ModuleType("langchain_community.chat_models.vertexai")

    class _ChatVertexAIStub:  # pragma: no cover - 실제로 인스턴스화되지 않는 자리표시자
        pass

    _stub.ChatVertexAI = _ChatVertexAIStub
    sys.modules["langchain_community.chat_models.vertexai"] = _stub

from functools import lru_cache

from ragas.embeddings.base import BaseRagasEmbedding
from ragas.llms import llm_factory
from ragas.llms.base import InstructorBaseRagasLLM, InstructorLLM
from ragas.metrics.collections import AnswerRelevancy, ContextPrecisionWithoutReference, ContextRecall, Faithfulness

from src.common import FALLBACK_MODEL_IDS, REGION, _is_throttling_error, default_embeddings

# 호환성 메모 2: ragas의 InstructorLLM._map_provider_params()는 anthropic 어댑터에도
# temperature/top_p를 그대로 kwargs로 넘기는데, 이 환경의 anthropic SDK(messages.create)는
# 그 두 파라미터를 받지 않아 "unexpected keyword argument 'temperature'"로 매번 실패한다.
# 실제로 지원하는 max_tokens만 넘어가도록 메서드 자체를 국소적으로 교체한다.
def _map_provider_params_max_tokens_only(self) -> dict:
    # 기본값 1024는 faithfulness의 NLI 판정 목록(statements 여러 개 + verdict)처럼 긴 JSON
    # 출력에서 잘려("EOF while parsing a list") InstructorRetryException을 유발했다 — 여유있게
    # 높인다. 4096으로 올렸던 첫 시도도 라우팅 수정 이후 답변이 더 길고 구체적으로 진단하게
    # 되면서(원인·근거·개선안이 여러 개) NLI 문장 분해 결과가 더 길어져 다시 잘렸다 — 8192로
    # 더 올린다.
    args = self.model_args
    max_tokens = args.get("max_tokens") if isinstance(args, dict) else getattr(args, "max_tokens", None)
    return {"max_tokens": max_tokens or 8192}


InstructorLLM._map_provider_params = _map_provider_params_max_tokens_only

# anthropic SDK(AnthropicBedrock)로만 붙일 수 있으므로 Claude 계열만 후보로 쓴다 (Nova는 별도 SDK 필요 — 범위 밖).
_CLAUDE_FALLBACK_IDS = [m for m in FALLBACK_MODEL_IDS if "claude" in m]

RAGAS_THRESHOLDS = {
    "faithfulness": 0.7,
    "answer_relevancy": 0.7,
    "context_precision": 0.6,
    "context_recall": 0.6,
}


class _BedrockRagasEmbedding(BaseRagasEmbedding):
    """BedrockEmbeddings(Titan)를 ragas의 modern BaseRagasEmbedding 인터페이스로 감싼다."""

    def __init__(self):
        super().__init__()
        self._emb = default_embeddings()

    def embed_text(self, text: str, **kwargs) -> list[float]:
        return self._emb.embed_query(text)

    async def aembed_text(self, text: str, **kwargs) -> list[float]:
        return self._emb.embed_query(text)


class _FallbackInstructorLLM(InstructorBaseRagasLLM):
    """RAGAS의 InstructorBaseRagasLLM을 구현하되, 쓰로틀링을 만나면 _CLAUDE_FALLBACK_IDS
    순서로 다음 모델로 즉시 넘어가 같은 요청을 재시도한다 (src.common.MultiModelChatBedrockConverse와
    동일한 전략, ragas의 instructor 기반 LLM 인터페이스용)."""

    def __init__(self):
        from anthropic import AsyncAnthropicBedrock

        # ragas의 Metric.score()는 내부적으로 항상 asyncio.run(self.ascore(...))를 거쳐
        # agenerate()를 호출한다 — 동기 클라이언트(AnthropicBedrock)를 쓰면
        # "Cannot use agenerate() with a synchronous client" 로 매번 실패한다.
        # max_retries=0: 같은 모델을 여러 번 재시도하지 않고 쓰로틀링이면 바로 다음 후보로 넘어간다.
        self._client = AsyncAnthropicBedrock(aws_region=REGION, max_retries=0)
        self._llms: dict[str, object] = {}

    def _llm_for(self, model_id: str):
        # 참고: llm_factory(..., temperature=0)을 주면 instructor의 anthropic 어댑터가
        # "AsyncMessages.create() got an unexpected keyword argument 'temperature'"로 실패한다
        # (0.4.3 기준 anthropic 어댑터 버그로 보임) — temperature 인자 없이 기본값으로 호출한다.
        if model_id not in self._llms:
            self._llms[model_id] = llm_factory(model_id, provider="anthropic", client=self._client)
        return self._llms[model_id]

    def generate(self, prompt: str, response_model):
        last_err = None
        for model_id in _CLAUDE_FALLBACK_IDS:
            try:
                return self._llm_for(model_id).generate(prompt, response_model)
            except Exception as e:
                if _is_throttling_error(e):
                    last_err = e
                    print(f"[fallback] ragas llm {model_id} 쓰로틀링 — 다음 모델로 재시도")
                    continue
                raise
        raise last_err

    async def agenerate(self, prompt: str, response_model):
        last_err = None
        for model_id in _CLAUDE_FALLBACK_IDS:
            try:
                return await self._llm_for(model_id).agenerate(prompt, response_model)
            except Exception as e:
                if _is_throttling_error(e):
                    last_err = e
                    print(f"[fallback] ragas llm {model_id} 쓰로틀링 — 다음 모델로 재시도")
                    continue
                raise
        raise last_err


@lru_cache(maxsize=1)
def _ragas_llm():
    return _FallbackInstructorLLM()


@lru_cache(maxsize=1)
def _metrics():
    llm = _ragas_llm()
    embeddings = _BedrockRagasEmbedding()
    return {
        "faithfulness": Faithfulness(llm=llm),
        "answer_relevancy": AnswerRelevancy(llm=llm, embeddings=embeddings),
        "context_precision": ContextPrecisionWithoutReference(llm=llm),
        "context_recall": ContextRecall(llm=llm),
    }


def score_item(question: str, answer: str, contexts: list[str], reference: str) -> dict:
    """positive/edge 케이스 1건에 대해 RAGAS 4지표를 계산한다. 실패하면 None 값으로 채우되,
    (쓰로틀링 등으로) 조용히 사라지지 않도록 원인은 stderr에 남긴다."""
    m = _metrics()
    scores: dict[str, float | None] = {}
    try:
        scores["faithfulness"] = float(m["faithfulness"].score(
            user_input=question, response=answer, retrieved_contexts=contexts
        ).value)
    except Exception as e:
        print(f"[ragas] faithfulness 실패: {type(e).__name__}: {e}", file=sys.stderr)
        scores["faithfulness"] = None
    try:
        scores["answer_relevancy"] = float(m["answer_relevancy"].score(
            user_input=question, response=answer
        ).value)
    except Exception as e:
        print(f"[ragas] answer_relevancy 실패: {type(e).__name__}: {e}", file=sys.stderr)
        scores["answer_relevancy"] = None
    try:
        scores["context_precision"] = float(m["context_precision"].score(
            user_input=question, response=answer, retrieved_contexts=contexts
        ).value)
    except Exception as e:
        print(f"[ragas] context_precision 실패: {type(e).__name__}: {e}", file=sys.stderr)
        scores["context_precision"] = None
    try:
        scores["context_recall"] = float(m["context_recall"].score(
            user_input=question, retrieved_contexts=contexts, reference=reference or answer
        ).value)
    except Exception as e:
        print(f"[ragas] context_recall 실패: {type(e).__name__}: {e}", file=sys.stderr)
        scores["context_recall"] = None
    return scores


def average_scores(all_scores: list[dict]) -> dict:
    """positive/edge 케이스들의 RAGAS 점수 평균을 낸다 (None은 제외)."""
    keys = ["faithfulness", "answer_relevancy", "context_precision", "context_recall"]
    out = {}
    for k in keys:
        values = [s[k] for s in all_scores if s.get(k) is not None]
        out[k] = round(sum(values) / len(values), 3) if values else None
    return out
