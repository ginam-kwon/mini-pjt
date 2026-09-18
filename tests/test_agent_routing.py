"""AC 9: SQL 검증·후보 탐색·쿼리 생성·실행계획 분석·튜닝 지식·범위 밖 요청 라우팅 검증.

각 담당 에이전트 빌더 함수가 정의되고, supervisor 구성이 모든 에이전트를 포함하며,
supervisor가 직접 답하지 않도록 prompt가 설정되어 있는지 정적으로 확인한다.
"""
from __future__ import annotations
import asyncio

import ast
import importlib
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

REQUIRED_AGENT_BUILDERS = [
    "build_explain_agent",
    "build_knowledge_agent",
    "build_query_planner_agent",
    "build_sql_validator_agent",
    "build_candidate_search_agent",
    "build_general_agent",
]

REQUIRED_AGENT_NAMES = [
    "explain_agent",
    "knowledge_agent",
    "query_planner_agent",
    "sql_validator_agent",
    "candidate_search_agent",
    "general_agent",
]

# 각 에이전트가 처리해야 할 요청 유형 (라우팅 매핑 문서화)
ROUTING_MAP = {
    "query_planner_agent": "비즈니스 요구사항에서 SELECT SQL을 설계",
    "sql_validator_agent": "사용자가 직접 입력한 SQL을 검증",
    "candidate_search_agent": "자연어로 운영 중인 시스템의 SQL 후보를 탐색",
    "explain_agent": "SQL 실행계획의 연산자·비용·조건 문제를 분석",
    "knowledge_agent": "Oracle SQL 튜닝 지식·개선 패턴을 조회",
    "general_agent": "위 범주에 해당하지 않는 모든 요청",
}


def _agents_source() -> str:
    return (PROJECT_ROOT / "src" / "agents.py").read_text(encoding="utf-8")


def _agents_ast() -> ast.Module:
    return ast.parse(_agents_source())


class TestAgentBuildersExist:
    """각 담당 에이전트 빌더 함수가 agents.py에 정의되어 있는지 확인한다."""

    @pytest.mark.parametrize("builder", REQUIRED_AGENT_BUILDERS)
    def test_builder_function_defined(self, builder: str):
        """agents.py에 빌더 함수가 정의되어 있어야 한다."""
        tree = _agents_ast()
        func_names = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        ]
        assert builder in func_names, (
            f"agents.py에 {builder} 함수가 없습니다."
        )

    @pytest.mark.parametrize("builder", REQUIRED_AGENT_BUILDERS)
    def test_builder_importable(self, builder: str):
        """각 빌더 함수가 src.agents에서 import 가능해야 한다."""
        import src.agents as agents_mod
        assert hasattr(agents_mod, builder), (
            f"src.agents에 {builder}를 찾을 수 없습니다."
        )

    @pytest.mark.parametrize("builder", REQUIRED_AGENT_BUILDERS)
    def test_builder_is_callable(self, builder: str):
        """각 빌더 함수가 callable이어야 한다."""
        import src.agents as agents_mod
        func = getattr(agents_mod, builder)
        assert callable(func), f"{builder}이 callable이 아닙니다."


class TestAgentNamesInSupervisor:
    """build_supervisor가 모든 담당 에이전트를 포함하도록 정의되어 있는지 확인한다."""

    def test_supervisor_function_exists(self):
        """build_supervisor 함수가 agents.py에 있어야 한다."""
        tree = _agents_ast()
        func_names = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        ]
        assert "build_supervisor" in func_names

    def test_supervisor_calls_all_builders(self):
        """build_supervisor 함수 본문에서 모든 에이전트 빌더가 호출되어야 한다."""
        tree = _agents_ast()
        supervisor_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "build_supervisor":
                supervisor_func = node
                break
        assert supervisor_func is not None

        called_funcs = set()
        for node in ast.walk(supervisor_func):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    called_funcs.add(node.func.id)

        missing = [b for b in REQUIRED_AGENT_BUILDERS if b not in called_funcs]
        assert not missing, (
            f"build_supervisor에서 호출되지 않는 빌더: {missing}"
        )

    @pytest.mark.parametrize("agent_name", REQUIRED_AGENT_NAMES)
    def test_agent_name_in_supervisor_scope(self, agent_name: str):
        """각 에이전트 이름이 agents.py에서 참조되어야 한다."""
        source = _agents_source()
        assert agent_name in source, (
            f"agents.py에서 '{agent_name}'을 찾을 수 없습니다."
        )

    def test_agent_names_constant_defined(self):
        """AGENT_NAMES 상수가 agents.py에 정의되어 있어야 한다."""
        import src.agents as agents_mod
        assert hasattr(agents_mod, "AGENT_NAMES"), (
            "src.agents에 AGENT_NAMES 상수가 없습니다."
        )

    def test_agent_names_contains_all(self):
        """AGENT_NAMES가 모든 필수 에이전트를 포함해야 한다."""
        import src.agents as agents_mod
        missing = [n for n in REQUIRED_AGENT_NAMES if n not in agents_mod.AGENT_NAMES]
        assert not missing, (
            f"AGENT_NAMES에 누락된 에이전트: {missing}"
        )


class TestSupervisorNoDirectResponse:
    """Supervisor prompt가 직접 사용자 응답을 금지하고 에이전트 라우팅을 강제하는지 확인한다."""

    def test_supervisor_prompt_defined(self):
        """Supervisor SYSTEM_PROMPT는 독립 프롬프트 모듈에 정의되어야 한다."""
        from src.prompts.supervisor import SYSTEM_PROMPT
        assert SYSTEM_PROMPT

    def test_supervisor_prompt_forbids_direct_response(self):
        """Supervisor SYSTEM_PROMPT가 직접 응답을 명시적으로 금지해야 한다."""
        from src.prompts.supervisor import SYSTEM_PROMPT
        prompt = SYSTEM_PROMPT
        # 직접 응답 금지 표현이 있는지 확인
        has_no_direct = any(phrase in prompt for phrase in [
            "직접 답하지 않",
            "직접 사용자",
            "직접 답변",
            "직접 응답",
            "반드시 에이전트",
        ])
        assert has_no_direct, (
            "Supervisor SYSTEM_PROMPT에 직접 응답 금지 표현이 없습니다."
        )

    def test_supervisor_prompt_covers_general_agent(self):
        """Supervisor SYSTEM_PROMPT에 general_agent 라우팅 규칙이 포함되어야 한다."""
        from src.prompts.supervisor import SYSTEM_PROMPT
        prompt = SYSTEM_PROMPT
        assert "general_agent" in prompt, (
            "Supervisor SYSTEM_PROMPT에 general_agent 라우팅이 없습니다."
        )

    @pytest.mark.parametrize("agent_name", REQUIRED_AGENT_NAMES)
    def test_supervisor_prompt_mentions_each_agent(self, agent_name: str):
        """Supervisor SYSTEM_PROMPT에 각 에이전트가 언급되어야 한다."""
        from src.prompts.supervisor import SYSTEM_PROMPT
        prompt = SYSTEM_PROMPT
        assert agent_name in prompt, (
                f"Supervisor SYSTEM_PROMPT에 '{agent_name}'이 언급되지 않았습니다."
        )


class TestPromptImportsFromModules:
    """agents.py가 각 에이전트의 system prompt를 src/prompts 모듈에서 import하는지 확인한다."""

    def test_all_prompt_constants_imported_from_prompts(self):
        """agents.py가 src.prompts 모듈에서 prompt 모듈을 import해야 한다."""
        source = _agents_source()
        assert "src.prompts" in source, (
            "agents.py가 src.prompts에서 import하지 않습니다."
        )

    @pytest.mark.parametrize("role_mod", [
        "_explain_mod",
        "_knowledge_mod",
        "_query_planner_mod",
        "_sql_validator_mod",
        "_candidate_search_mod",
        "_general_mod",
    ])
    def test_prompt_module_alias_present(self, role_mod: str):
        """각 역할 prompt 모듈 alias가 agents.py에 있어야 한다."""
        source = _agents_source()
        assert role_mod in source, (
            f"agents.py에서 {role_mod} alias를 찾을 수 없습니다."
        )

    @pytest.mark.parametrize("prompt_const", [
        "EXPLAIN_SYSTEM_PROMPT",
        "KNOWLEDGE_SYSTEM_PROMPT",
        "QUERY_PLANNER_SYSTEM_PROMPT",
        "SQL_VALIDATOR_SYSTEM_PROMPT",
        "CANDIDATE_SEARCH_SYSTEM_PROMPT",
        "GENERAL_SYSTEM_PROMPT",
    ])
    def test_prompt_constant_assigned_from_module(self, prompt_const: str):
        """각 에이전트 system prompt 상수가 모듈 참조로 할당되어야 한다."""
        tree = _agents_ast()
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == prompt_const:
                        # 값이 문자열 리터럴이면 인라인 정의 — 실패
                        assert not isinstance(node.value, ast.Constant), (
                            f"{prompt_const}이 인라인 문자열로 정의되어 있습니다. "
                            f"src.prompts 모듈에서 참조하세요."
                        )
                        found = True
        assert found, f"agents.py에 {prompt_const} 할당문이 없습니다."


class TestRoutingCoverage:
    """라우팅 매핑이 6가지 요청 유형을 모두 커버하는지 확인한다."""

    def test_routing_map_completeness(self):
        """ROUTING_MAP이 모든 필수 에이전트를 커버해야 한다."""
        missing = [n for n in REQUIRED_AGENT_NAMES if n not in ROUTING_MAP]
        assert not missing, f"ROUTING_MAP에 누락된 에이전트: {missing}"

    def test_supervisor_prompt_covers_all_routing_types(self):
        """Supervisor SYSTEM_PROMPT가 6가지 요청 유형의 라우팅을 모두 명시해야 한다."""
        from src.prompts.supervisor import SYSTEM_PROMPT
        prompt = SYSTEM_PROMPT
        for agent_name in REQUIRED_AGENT_NAMES:
            assert agent_name in prompt, (
                f"Supervisor SYSTEM_PROMPT에 {agent_name} 라우팅이 없습니다."
            )


class TestOutOfScopeReachesSupervisor:
    """행동 검증(정적 검사 아님): db_tool이 매칭되지 못한 요청은 knowledge_agent로 직행하지
    않고 반드시 supervisor를 거친다 — supervisor만이 knowledge_agent/general_agent 중
    어디로 보낼지 실제로 판단할 수 있는 지점이기 때문이다."""

    def test_run_query_catalog_miss_invokes_supervisor_not_knowledge_directly(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        from langchain_core.messages import AIMessage

        import src.pipeline as pipeline

        pipeline._supervisor.cache_clear()
        fake_supervisor = MagicMock()
        fake_supervisor.ainvoke = AsyncMock(return_value={
            "messages": [AIMessage(content="이 질문은 범위 밖입니다.")]
        })
        monkeypatch.setattr(pipeline, "build_supervisor", lambda: fake_supervisor)
        monkeypatch.setattr(pipeline, "input_guard", lambda q: (False, "통과"))
        monkeypatch.setattr(pipeline, "db_tool", lambda q: None)

        result = asyncio.run(pipeline.run_query(question="오늘 저녁 뭐 먹을지 추천해줘"))

        assert result["status"] == "ok"
        fake_supervisor.ainvoke.assert_called_once()
        pipeline._supervisor.cache_clear()
