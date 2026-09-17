# rag.py - Oracle 튜닝 지식베이스에 대한 하이브리드 RAG (day2_practice 패턴 응용)
#
# day2_practice 는 BAAI/bge-reranker-v2-m3 CrossEncoder로 리랭킹하지만, 이 프로젝트는
# 대용량 모델 다운로드 없이 이미 쓰는 Bedrock LLM으로 구조화 출력 기반 리랭킹을 한다
# (핵심 의사결정 2, README 참고).
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from kiwipiepy import Kiwi
from langchain_chroma import Chroma
from langchain_classic.retrievers import EnsembleRetriever
from langchain_classic.retrievers.multi_query import MultiQueryRetriever
from langchain_community.retrievers import BM25Retriever
from langchain_core.documents import Document
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from src.common import default_embeddings, default_llm

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "data" / "knowledge"
PERSIST_DIR = str(Path(__file__).resolve().parent.parent / "chroma_db")
COLLECTION_NAME = "oracle_tuning_kb"

_kiwi = Kiwi()
_KEEP_TAGS = {"NNG", "NNP", "NNB", "NR", "NP", "VV", "VA", "SL", "SN"}


def korean_tokenizer(text: str) -> list[str]:
    return [token.form for token in _kiwi.tokenize(text) if token.tag in _KEEP_TAGS]


def load_documents() -> list[Document]:
    docs = []
    for path in sorted(KNOWLEDGE_DIR.glob("*.md")):
        docs.append(Document(page_content=path.read_text(encoding="utf-8"), metadata={"source": path.name}))
    return docs


@lru_cache(maxsize=1)
def _hybrid_retriever():
    docs = load_documents()

    vectordb = Chroma(
        collection_name=COLLECTION_NAME,
        embedding_function=default_embeddings(),
        persist_directory=PERSIST_DIR,
    )
    if not vectordb.get()["ids"]:
        vectordb.add_documents(docs)
    vector_retriever = vectordb.as_retriever(search_kwargs={"k": 3})

    bm25 = BM25Retriever.from_documents(docs, preprocess_func=korean_tokenizer)
    bm25.k = 3

    ensemble = EnsembleRetriever(retrievers=[bm25, vector_retriever], weights=[0.3, 0.7])
    return MultiQueryRetriever.from_llm(retriever=ensemble, llm=default_llm())


class _RelevanceScore(BaseModel):
    scores: list[int] = Field(description="입력된 문서 순서와 동일한 순서로, 각 문서의 질의 관련도 0~10 점수")


def _llm_rerank(query: str, docs: list[Document], top_k: int = 3) -> list[Document]:
    if not docs:
        return []
    listing = "\n\n".join(f"[{i}] ({d.metadata.get('source')})\n{d.page_content}" for i, d in enumerate(docs))
    prompt = (
        "다음 질의에 대해 각 문서가 얼마나 관련 있는지 0~10점으로 채점하세요.\n"
        f"질의: {query}\n\n문서 목록:\n{listing}"
    )
    result = default_llm().with_structured_output(_RelevanceScore).invoke(prompt)
    scores = result.scores if len(result.scores) == len(docs) else [0] * len(docs)
    ranked = sorted(zip(docs, scores), key=lambda pair: pair[1], reverse=True)
    return [d for d, _ in ranked[:top_k]]


def search_tuning_knowledge_impl(query: str, top_k: int = 3) -> str:
    """하이브리드 검색(BM25+벡터) → 쿼리 확장(MultiQuery) → LLM 리랭크 순으로 문서를 찾는다."""
    candidates = _hybrid_retriever().invoke(query)
    # 중복 제거 (MultiQueryRetriever가 같은 문서를 여러 번 반환할 수 있음)
    seen = set()
    unique = []
    for d in candidates:
        key = d.metadata.get("source")
        if key not in seen:
            seen.add(key)
            unique.append(d)
    top = _llm_rerank(query, unique, top_k=top_k)
    if not top:
        return "관련 지식을 찾지 못했습니다."
    return "\n\n---\n\n".join(f"[출처: {d.metadata.get('source')}]\n{d.page_content}" for d in top)


@tool
def search_tuning_knowledge(query: str) -> str:
    """Oracle SQL 튜닝 지식베이스에서 질의와 관련된 원인/개선 패턴 문서를 검색한다.
    풀 테이블 스캔, 카디널리티 오추정, 조인 방식, 통계/파티션 등 키워드로 검색하세요."""
    return search_tuning_knowledge_impl(query)


def warmup() -> None:
    """Chroma의 PersistentClient(Rust 바인딩)는 워커 스레드에서 처음 생성되면 불안정하다
    (LangGraph ToolNode는 도구를 ThreadPoolExecutor 안에서 실행한다). 에이전트를 만들기 전
    메인 스레드에서 한 번 미리 호출해 lru_cache에 채워 두면 이후 어느 스레드에서 호출되든
    이미 만들어진 클라이언트를 재사용하게 되어 문제가 없다."""
    _hybrid_retriever()


if __name__ == "__main__":
    print(search_tuning_knowledge_impl("풀 테이블 스캔이 왜 발생하고 어떻게 고치나요?"))
