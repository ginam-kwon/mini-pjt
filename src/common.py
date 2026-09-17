# common.py - 공용 LLM/임베딩 클라이언트와 상수
#
# 채점기 관례(day1~day7)를 따라 모델 생성은 항상 함수 안에서 하고, llm=None 을
# 받아 주입 가능하게 둔다. 모듈 최상단에서 실제 Bedrock 클라이언트를 만들지 않는다.
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from langchain_aws import BedrockEmbeddings, ChatBedrockConverse
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatResult

load_dotenv()  # 이 프로젝트 로컬 .env (Oracle 접속 정보 등)
# load_dotenv()는 가장 가까운 .env를 찾으면 탐색을 멈추므로, 위 호출이 로컬 .env를 먼저
# 찾아버리면 AWS 자격증명이 든 상위 공유 .env(sds-ax-practice/.env)까지 못 올라간다.
# override=False(기본값)라 이미 설정된 키는 덮어쓰지 않으므로, 부족한 키(AWS_*)만 보충된다.
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
EMBED_MODEL_ID = "amazon.titan-embed-text-v2:0"
REGION = "us-east-1"

# 쓰로틀링(계정 일일 토큰 한도 등)에 걸리면 이 순서대로 다음 모델로 즉시 넘어가 같은 요청을
# 재시도한다. us./global. 접두사가 다른 항목은 같은 모델이라도 별도 인퍼런스 프로파일이라
# 쿼터가 분리되어 있을 가능성이 높다 — 그래서 같은 모델의 접두사 변형도 앞쪽에 넣어둔다.
# Claude 계열을 Nova보다 앞에 둔 이유: 이 프로젝트는 구조화 출력(with_structured_output)과
# 도구 호출(bind_tools)에 크게 의존하는데 Claude 쪽이 더 검증되어 있다. Nova는 최후 수단이다.
# (실측: Nova를 1순위로 바꿔봤더니 RAG 리랭커의 with_structured_output 호출에서
# `OutputParserException: Unknown tool type: 'RelevanceScore'`로 깨짐 — 쓰로틀링이 아닌
# 오류라 폴백 체인이 넘어가지 않고 그대로 실패해 원복함.)
FALLBACK_MODEL_IDS: list[str] = [
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "us.anthropic.claude-sonnet-4-6",
    "global.anthropic.claude-sonnet-4-6",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    "us.amazon.nova-pro-v1:0",
    "us.amazon.nova-2-lite-v1:0",
    "global.amazon.nova-2-lite-v1:0",
    "us.amazon.nova-lite-v1:0",
]


def _is_throttling_error(e: Exception) -> bool:
    """botocore ThrottlingException(계정 일일 토큰 한도 등)인지 확인한다. 이 경우에만
    다음 모델로 넘어간다 — 그 외 오류(잘못된 프롬프트 등)는 모델을 바꿔도 소용없으므로 그대로 올린다.

    e.response는 호출 경로에 따라 모양이 다르다 — botocore 예외는 dict 스타일
    (.get("Error", {})...), anthropic SDK(AsyncAnthropicBedrock 등)의 APIStatusError는
    httpx.Response 객체(.status_code)를 준다. 둘 다 안전하게 다룬다."""
    response = getattr(e, "response", None)
    if isinstance(response, dict) and response.get("Error", {}).get("Code") == "ThrottlingException":
        return True
    status_code = getattr(response, "status_code", None) or getattr(e, "status_code", None)
    if status_code == 429:
        return True
    if "ReadTimeoutError" in type(e).__name__ or "ConnectTimeoutError" in type(e).__name__:
        # 쓰로틀링으로 처리 지연이 길어지면 우리가 건 30초 timeout에 먼저 걸릴 수 있다 —
        # 같은 모델을 그대로 재시도해봐야 소용없으니 다음 모델로 넘어간다.
        return True
    return "ThrottlingException" in str(e) or "Too many tokens" in str(e) or "rate_limit" in str(e).lower()


class MultiModelChatBedrockConverse(ChatBedrockConverse):
    """ChatBedrockConverse와 동일하게 동작하되, 쓰로틀링을 만나면 FALLBACK_MODEL_IDS 순서로
    즉시 다음 모델로 바꿔 같은 요청을 재시도한다. BaseChatModel을 그대로 상속하므로
    bind_tools/with_structured_output/create_agent(model=...) 등 기존 코드 어디에도
    변경 없이 그대로 꽂힌다 — 공유 캐시 인스턴스(self)를 직접 mutate하지 않고 매 시도마다
    model_copy(update=...)로 임시 복사본을 만들어 호출하므로 동시 요청에도 안전하다."""

    def _candidate_model_ids(self) -> list[str]:
        primary = self.model_id
        rest = [m for m in FALLBACK_MODEL_IDS if m != primary]
        return [primary, *rest]

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs,
    ) -> ChatResult:
        last_err: Exception | None = None
        for model_id in self._candidate_model_ids():
            client = self if model_id == self.model_id else self.model_copy(update={"model_id": model_id})
            try:
                return ChatBedrockConverse._generate(client, messages, stop=stop, run_manager=run_manager, **kwargs)
            except Exception as e:
                if _is_throttling_error(e):
                    last_err = e
                    print(f"[fallback] {model_id} 쓰로틀링 — 다음 모델로 재시도")
                    continue
                raise
        raise last_err


@lru_cache(maxsize=1)
def default_llm(temperature: float = 0) -> ChatBedrockConverse:
    # temperature=0 + timeout=30: seed.yaml의 DeterminismPolicy. max_retries=0 —
    # 모델 하나당 같은 요청을 여러 번 재시도(지수 백오프)하지 않고 쓰로틀링이면 바로 다음
    # 모델로 넘어간다("즉시 재시도"). 재시도 자체는 FALLBACK_MODEL_IDS 순회가 대신한다.
    # 다만 쓰로틀링으로 모델이 바뀌면 그 요청 하나는 다른 모델이 답한 것이므로 완전한
    # 결정성은 깨질 수 있다 — README의 트라이앤에러 회고에 트레이드오프로 기록.
    return MultiModelChatBedrockConverse(
        model=MODEL_ID, region_name=REGION, temperature=temperature, max_retries=0, timeout=30
    )


@lru_cache(maxsize=1)
def default_embeddings() -> BedrockEmbeddings:
    return BedrockEmbeddings(model_id=EMBED_MODEL_ID, region_name=REGION)


def get_text(message) -> str:
    """ChatBedrockConverse는 content를 블록 리스트로 주기도 하므로 텍스트만 모아 반환한다."""
    content = message.content
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content if isinstance(block, dict))
    return content


def last_nonempty_text(messages) -> str:
    """create_supervisor는 핸드오프 직후 마지막 메시지가 빈 종료 턴(content=[])이거나
    'Transferring back to supervisor' 같은 핸드오프 안내 메시지일 수 있다. 뒤에서부터
    도구 호출이 아니고(tool_calls 없음) 핸드오프 표식이 아닌 AIMessage 중 텍스트가 있는
    것을 찾아 반환한다 (ToolMessage/HumanMessage는 최종 답변이 아니므로 건너뛴다)."""
    from langchain_core.messages import AIMessage

    for m in reversed(messages):
        if not isinstance(m, AIMessage):
            continue
        if getattr(m, "tool_calls", None):
            continue
        if m.response_metadata.get("__is_handoff_back"):
            continue
        text = get_text(m)
        if text:
            return text
    return ""
