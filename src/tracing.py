# tracing.py - Observability: 로컬 트레이서(day7 local_tracer.py 패턴) + 선택적 LangSmith
from __future__ import annotations

import datetime
import json
import os
import time
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

TRACE_PATH = "trace.jsonl"


def _mask(text: str) -> str:
    """trace/log 기록 전 PII를 마스킹한다. guardrails 순환참조 없이 lazy import로 처리."""
    try:
        from src.guardrails import mask_pii
        return mask_pii(str(text))
    except Exception:
        return str(text)


class FileTracer(BaseCallbackHandler):
    """모든 모델/도구 호출을 trace.jsonl 에 JSONL로 기록하고, 동시에 이번 요청의 이벤트를
    메모리에도 쌓아 공식 API 응답의 trace 필드(요청 단위 JSON 배열)를 채울 수 있게 한다."""

    def __init__(self, path: str = TRACE_PATH):
        self.path = path
        self._starts: dict[UUID, float] = {}
        self.events: list[dict] = []

    def _record(self, record: dict) -> None:
        record["ts"] = datetime.datetime.now().isoformat()
        self.events.append(record)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        self._starts[run_id] = time.time()
        self._record({"event": "chat_model_start", "run_id": str(run_id)})

    def on_llm_end(self, response, *, run_id, **kwargs):
        latency = time.time() - self._starts.pop(run_id, time.time())
        usage = {}
        try:
            usage = response.generations[0][0].message.usage_metadata or {}
        except Exception:
            pass
        self._record({"event": "llm_end", "run_id": str(run_id), "latency_s": round(latency, 3), "usage": usage})

    def on_tool_start(self, serialized, input_str, *, run_id, **kwargs):
        self._starts[run_id] = time.time()
        self._record({"event": "tool_start", "run_id": str(run_id), "tool": serialized.get("name"), "input": _mask(input_str)[:200]})

    def on_tool_end(self, output, *, run_id, **kwargs):
        latency = time.time() - self._starts.pop(run_id, time.time())
        self._record({"event": "tool_end", "run_id": str(run_id), "latency_s": round(latency, 3), "output": _mask(output)[:200]})

    def on_tool_error(self, error, *, run_id, **kwargs):
        self._record({"event": "tool_error", "run_id": str(run_id), "error": str(error)})

    def api_trace(self) -> list[dict]:
        """공식 API 응답의 trace 필드 형식({step, input, output})으로 변환한다."""
        steps: list[dict] = []
        for e in self.events:
            ev = e.get("event")
            if ev == "chat_model_start":
                steps.append({"step": "llm_call", "input": None, "output": None})
            elif ev == "llm_end":
                if steps and steps[-1]["step"] == "llm_call" and steps[-1]["output"] is None:
                    steps[-1]["output"] = {"latency_s": e.get("latency_s"), "usage": e.get("usage")}
                else:
                    steps.append({"step": "llm_call", "input": None, "output": {"latency_s": e.get("latency_s"), "usage": e.get("usage")}})
            elif ev == "tool_start":
                steps.append({"step": f"tool:{e.get('tool')}", "input": e.get("input"), "output": None})
            elif ev == "tool_end":
                target = next((s for s in reversed(steps) if s["step"].startswith("tool:") and s["output"] is None), None)
                if target:
                    target["output"] = e.get("output")
                else:
                    steps.append({"step": "tool_end", "input": None, "output": e.get("output")})
            elif ev == "tool_error":
                steps.append({"step": "tool_error", "input": None, "output": e.get("error")})
        return steps


def new_tracer(path: str = TRACE_PATH) -> FileTracer:
    """요청 하나를 추적할 새 FileTracer 인스턴스를 만든다 (호출부가 .events/.api_trace()로 읽는다)."""
    if os.environ.get("LANGCHAIN_API_KEY"):
        os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
        os.environ.setdefault("LANGCHAIN_PROJECT", "oracle-sql-perf-agent")
    return FileTracer(path)


def tracing_callbacks() -> list:
    """호출부가 개별 트레이서 인스턴스를 다시 읽을 필요가 없을 때 쓰는 간단한 콜백 리스트."""
    return [new_tracer()]


def langfuse_callbacks() -> list:
    """LANGFUSE_PUBLIC_KEY/SECRET_KEY가 설정되어 있으면 Langfuse 콜백 핸들러를 FileTracer와
    병행으로 추가한다 (docker-compose의 langfuse-web/worker, README "Langfuse 셋업" 참고).
    미설정이거나 SDK/서버 접속이 실패해도 예외를 던지지 않고 조용히 빈 리스트를 반환한다 —
    관측 도구 하나가 죽었다고 진단 API 자체가 500을 내면 안 되기 때문."""
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        return []
    try:
        from langfuse.langchain import CallbackHandler

        return [CallbackHandler()]
    except Exception:
        return []


def observability_callbacks(tracer: FileTracer) -> list:
    """요청 하나에 붙일 콜백 목록: FileTracer는 항상 유지하고 Langfuse는 부가로만 더한다.
    Langfuse 조회/생성이 어떤 이유로 예외를 던져도 여기서 삼켜서, 관측 도구 실패가
    API 요청 자체를 실패시키지 않게 한다(요청 응답의 trace 배열은 FileTracer가 계속 채운다)."""
    callbacks: list = [tracer]
    try:
        callbacks.extend(langfuse_callbacks())
    except Exception:
        pass
    return callbacks
