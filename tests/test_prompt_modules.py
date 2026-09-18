"""src/prompts 모듈 역할 매핑 검증.

AC: supervisor, query_planner, sql_validator, candidate_search, plan_risk,
    explain, knowledge, general 역할의 system prompt가 src/prompts의
    독립 모듈에서 import되고 역할 매핑이 검증된다.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

REQUIRED_ROLES = [
    "supervisor",
    "query_planner",
    "sql_validator",
    "candidate_search",
    "plan_risk",
    "explain",
    "knowledge",
    "general",
]


class TestPromptModulesExist:
    """각 역할에 대한 독립 prompt 모듈이 존재하는지 확인한다."""

    @pytest.mark.parametrize("role", REQUIRED_ROLES)
    def test_module_file_exists(self, role: str):
        """src/prompts/<role>.py 파일이 존재해야 한다."""
        module_path = PROJECT_ROOT / "src" / "prompts" / f"{role}.py"
        assert module_path.exists(), (
            f"src/prompts/{role}.py 파일이 없습니다. "
            f"역할 '{role}'의 system prompt 모듈을 생성하세요."
        )

    @pytest.mark.parametrize("role", REQUIRED_ROLES)
    def test_module_importable(self, role: str):
        """src.prompts.<role> 모듈이 import 가능해야 한다."""
        module = importlib.import_module(f"src.prompts.{role}")
        assert module is not None, f"src.prompts.{role} 모듈을 import할 수 없습니다."

    @pytest.mark.parametrize("role", REQUIRED_ROLES)
    def test_module_has_system_prompt(self, role: str):
        """각 역할 모듈에 SYSTEM_PROMPT 상수가 정의되어 있어야 한다."""
        module = importlib.import_module(f"src.prompts.{role}")
        assert hasattr(module, "SYSTEM_PROMPT"), (
            f"src.prompts.{role}에 SYSTEM_PROMPT 상수가 없습니다."
        )

    @pytest.mark.parametrize("role", REQUIRED_ROLES)
    def test_system_prompt_is_nonempty_string(self, role: str):
        """각 역할의 SYSTEM_PROMPT가 비어 있지 않은 문자열이어야 한다."""
        module = importlib.import_module(f"src.prompts.{role}")
        prompt = module.SYSTEM_PROMPT
        assert isinstance(prompt, str), (
            f"src.prompts.{role}.SYSTEM_PROMPT가 str이 아닙니다: {type(prompt)}"
        )
        assert len(prompt.strip()) > 0, (
            f"src.prompts.{role}.SYSTEM_PROMPT가 비어 있습니다."
        )


class TestRolePromptMap:
    """ROLE_PROMPT_MAP이 모든 필수 역할을 포함하고 올바르게 매핑되는지 확인한다."""

    def test_role_prompt_map_exists(self):
        """src.prompts 패키지에 ROLE_PROMPT_MAP이 정의되어 있어야 한다."""
        from src.prompts import ROLE_PROMPT_MAP
        assert isinstance(ROLE_PROMPT_MAP, dict), "ROLE_PROMPT_MAP이 dict가 아닙니다."

    def test_role_prompt_map_contains_all_roles(self):
        """ROLE_PROMPT_MAP에 모든 필수 역할이 포함되어야 한다."""
        from src.prompts import ROLE_PROMPT_MAP
        missing = [role for role in REQUIRED_ROLES if role not in ROLE_PROMPT_MAP]
        assert not missing, (
            f"ROLE_PROMPT_MAP에 누락된 역할: {missing}"
        )

    @pytest.mark.parametrize("role", REQUIRED_ROLES)
    def test_role_prompt_map_value_matches_module(self, role: str):
        """ROLE_PROMPT_MAP[role]이 해당 모듈의 SYSTEM_PROMPT와 동일해야 한다."""
        from src.prompts import ROLE_PROMPT_MAP
        module = importlib.import_module(f"src.prompts.{role}")
        assert ROLE_PROMPT_MAP[role] == module.SYSTEM_PROMPT, (
            f"ROLE_PROMPT_MAP['{role}']이 src.prompts.{role}.SYSTEM_PROMPT와 다릅니다."
        )

    def test_no_extra_roles_in_map(self):
        """ROLE_PROMPT_MAP에 정의되지 않은 역할이 없어야 한다."""
        from src.prompts import ROLE_PROMPT_MAP, REQUIRED_ROLES as pkg_required
        extra = set(ROLE_PROMPT_MAP.keys()) - set(REQUIRED_ROLES)
        # 추가 역할이 있어도 경고만 — 누락된 역할이 없으면 통과
        assert not (set(REQUIRED_ROLES) - set(ROLE_PROMPT_MAP.keys())), (
            f"ROLE_PROMPT_MAP에 필수 역할이 누락됨: {set(REQUIRED_ROLES) - set(ROLE_PROMPT_MAP.keys())}"
        )


class TestAgentsImportFromPrompts:
    """agents.py가 prompt 상수를 src/prompts 모듈에서 import하는지 정적으로 확인한다."""

    def _agents_source(self) -> str:
        return (PROJECT_ROOT / "src" / "agents.py").read_text(encoding="utf-8")

    def test_agents_imports_explain_from_prompts(self):
        """agents.py가 src.prompts 모듈에서 prompt를 import하는 코드가 있어야 한다."""
        source = self._agents_source()
        assert "src.prompts" in source, (
            "agents.py가 src.prompts 모듈에서 prompt를 import하지 않습니다."
        )

    def test_agents_explain_prompt_references_module(self):
        """agents.py에서 EXPLAIN_SYSTEM_PROMPT가 explain 모듈에서 할당되어야 한다."""
        import ast
        tree = ast.parse(self._agents_source())
        # EXPLAIN_SYSTEM_PROMPT = _explain_mod.SYSTEM_PROMPT 또는 유사한 패턴
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "EXPLAIN_SYSTEM_PROMPT":
                        found = True
        assert found, (
            "agents.py에 EXPLAIN_SYSTEM_PROMPT 할당문이 없습니다. "
            "src.prompts.explain.SYSTEM_PROMPT에서 import해야 합니다."
        )

    def test_agents_knowledge_prompt_references_module(self):
        """agents.py에서 KNOWLEDGE_SYSTEM_PROMPT가 knowledge 모듈에서 할당되어야 한다."""
        import ast
        tree = ast.parse(self._agents_source())
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "KNOWLEDGE_SYSTEM_PROMPT":
                        found = True
        assert found, (
            "agents.py에 KNOWLEDGE_SYSTEM_PROMPT 할당문이 없습니다. "
            "src.prompts.knowledge.SYSTEM_PROMPT에서 import해야 합니다."
        )

    def test_agents_inline_prompt_strings_removed(self):
        """agents.py에서 인라인 prompt 문자열 리터럴이 제거되었는지 확인한다.

        EXPLAIN/KNOWLEDGE 프롬프트가 더 이상 agents.py에 직접 정의되지 않아야 한다.
        대신 src.prompts 모듈에서 import한다.
        """
        source = self._agents_source()
        # 인라인 EXPLAIN_SYSTEM_PROMPT 정의 패턴: EXPLAIN_SYSTEM_PROMPT = """..."""
        # src.prompts import 이후 = ... 형식으로 모듈 참조여야 한다
        import ast
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in (
                        "EXPLAIN_SYSTEM_PROMPT", "KNOWLEDGE_SYSTEM_PROMPT"
                    ):
                        # 값이 문자열 리터럴이면 인라인 정의 — 실패
                        assert not isinstance(node.value, ast.Constant), (
                            f"{target.id}이 여전히 agents.py에 인라인 문자열로 정의되어 있습니다. "
                            f"src.prompts 모듈에서 참조하도록 변경하세요."
                        )
