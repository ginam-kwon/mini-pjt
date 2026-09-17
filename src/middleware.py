# middleware.py - AgentMiddleware 4종 (day5 패턴 응용)
#
# 원칙: 차단(InputGuard) -> 정제(Masking) -> 검증(OutputCheck) -> 기록(Logging).
# 마스킹이 로깅보다 뒤에 있으면 로그에 원본이 남으므로 순서 자체가 설계다.
from __future__ import annotations

import datetime
import json

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage

from src.common import get_text
from src.guardrails import (
    has_pii,
    input_guard,
    is_off_topic,
    mask_pii,
)

LOG_PATH = "agent_log.jsonl"


class InputGuardMiddleware(AgentMiddleware):
    """모델 호출 전 입력을 검사해, 인젝션/범위이탈이면 모델 호출 없이 즉시 종료한다."""

    def before_model(self, state, runtime):
        last_human = next(
            (m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), None
        )
        if not last_human:
            return None

        text = get_text(last_human)
        blocked, reason = input_guard(text)
        if blocked:
            return {
                "jump_to": "end",
                "messages": [AIMessage(content=f"요청을 처리할 수 없습니다. 사유: {reason}")],
            }
        return None


class MaskingMiddleware(AgentMiddleware):
    """모델 호출 전 사용자 입력에 섞인 개인정보/자격증명을 마스킹한다."""

    def before_model(self, state, runtime):
        last_human = next(
            (m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), None
        )
        if not last_human:
            return None
        text = get_text(last_human)
        masked = mask_pii(text)
        if masked != text:
            return {"messages": [HumanMessage(content=masked, id=last_human.id)]}
        return None


class OutputCheckMiddleware(AgentMiddleware):
    """모델 응답에서 민감정보 노출/범위이탈을 검사해 정제한다. 도구 호출 응답은 건드리지 않는다."""

    def after_model(self, state, runtime):
        last = state["messages"][-1]
        if not isinstance(last, AIMessage) or getattr(last, "tool_calls", None):
            return None

        text = get_text(last)
        if not text:
            return None

        if has_pii(text):
            return {"messages": [AIMessage(content=mask_pii(text), id=last.id)]}
        if is_off_topic(text):
            return {"messages": [AIMessage(
                content="죄송합니다. 그 주제는 이 Agent의 담당 범위(Oracle SQL 성능 진단)가 아닙니다.",
                id=last.id,
            )]}
        return None


class LoggingMiddleware(AgentMiddleware):
    """모델 호출 전후를 agent_log.jsonl 에 기록한다."""

    def _write(self, record: dict) -> None:
        record["time"] = datetime.datetime.now().isoformat()
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def before_model(self, state, runtime):
        last = state["messages"][-1] if state["messages"] else None
        if last:
            self._write({"event": "before_model", "message_type": type(last).__name__, "preview": get_text(last)[:120]})
        return None

    def after_model(self, state, runtime):
        last = state["messages"][-1]
        record = {"event": "after_model", "message_type": type(last).__name__}
        if getattr(last, "tool_calls", None):
            record["tool_calls"] = [t["name"] for t in last.tool_calls]
        if getattr(last, "usage_metadata", None):
            record["total_tokens"] = last.usage_metadata.get("total_tokens", 0)
        self._write(record)
        return None


# 실행 순서 그대로 리스트 순서에 반영한다: 차단 -> 정제 -> (모델/도구 실행) -> 검증 -> 기록
MIDDLEWARE_ORDER = [
    InputGuardMiddleware(),
    MaskingMiddleware(),
    OutputCheckMiddleware(),
    LoggingMiddleware(),
]
