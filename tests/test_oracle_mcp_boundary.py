"""MCP 경계 검증: Oracle 대상 DB 접근이 SQLcl MCP만 사용하는지 확인한다.

AC: Oracle 대상 DB를 조회하거나 실행계획·제한 실행을 수행하는 서비스 경로는
SQLcl MCP 도구만 사용하고 직접 DB 드라이버 접속을 사용하지 않는다.
"""
from __future__ import annotations

import ast
import asyncio
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# 프로젝트 루트를 sys.path에 추가
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

TOOLS_PATH = PROJECT_ROOT / "src" / "tools.py"


# ---------------------------------------------------------------------------
# 정적 분석: oracledb 직접 접속 코드 부재 확인
# ---------------------------------------------------------------------------

class TestNoDirectOracleDriverInSource:
    """tools.py 소스코드에서 oracledb 직접 접속 코드가 없는지 정적으로 확인한다."""

    def _parsed_tools(self) -> ast.Module:
        return ast.parse(TOOLS_PATH.read_text(encoding="utf-8"))

    def test_no_oracledb_connect_call(self):
        """tools.py에서 oracledb.connect() 직접 호출이 없어야 한다."""
        tree = self._parsed_tools()
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "connect"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "oracledb"
                ):
                    violations.append(f"line {node.lineno}")
        assert not violations, (
            f"oracledb.connect() 직접 호출이 발견됨: {violations}. "
            "Oracle 접근은 SQLcl MCP를 통해서만 이루어져야 합니다."
        )

    def test_no_import_oracledb_at_module_level(self):
        """tools.py에서 oracledb를 import하지 않는다."""
        tree = self._parsed_tools()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "oracledb", (
                        f"직접 oracledb import 발견 (line {node.lineno}). "
                        "직접 DB 드라이버 접속은 금지됩니다."
                    )
            elif isinstance(node, ast.ImportFrom):
                assert node.module != "oracledb", (
                    f"'from oracledb ...' import 발견 (line {node.lineno}). "
                    "직접 DB 드라이버 접속은 금지됩니다."
                )

    def test_no_get_oracle_connection_function(self):
        """tools.py에 _get_oracle_connection 함수가 없어야 한다 (직접 접속 함수 제거 확인)."""
        tree = self._parsed_tools()
        func_names = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        assert "_get_oracle_connection" not in func_names, (
            "_get_oracle_connection 함수가 아직 존재합니다. 직접 DB 드라이버 접속 코드를 제거하세요."
        )

    def test_sqlcl_mcp_fetch_plan_is_async(self):
        """_sqlcl_mcp_fetch_plan이 async 함수로 정의되어 있어야 한다."""
        tree = self._parsed_tools()
        async_funcs = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
        }
        assert "_sqlcl_mcp_fetch_plan" in async_funcs, (
            "_sqlcl_mcp_fetch_plan이 async 함수로 정의되어 있지 않습니다."
        )

    def test_sqlcl_mcp_run_user_sql_is_async(self):
        """_sqlcl_mcp_run_user_sql이 async 함수로 정의되어 있어야 한다."""
        tree = self._parsed_tools()
        async_funcs = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
        }
        assert "_sqlcl_mcp_run_user_sql" in async_funcs, (
            "_sqlcl_mcp_run_user_sql이 async 함수로 정의되어 있지 않습니다."
        )

    def test_sqlcl_mcp_config_present(self):
        """SQLCL_MCP_SERVER_CONFIG 딕셔너리가 tools.py에 정의되어 있어야 한다."""
        source = TOOLS_PATH.read_text(encoding="utf-8")
        assert "SQLCL_MCP_SERVER_CONFIG" in source, (
            "SQLCL_MCP_SERVER_CONFIG이 tools.py에 없습니다."
        )
        assert "oracle-sqlcl" in source, (
            "'oracle-sqlcl' 키가 SQLCL_MCP_SERVER_CONFIG에 없습니다."
        )

    def test_mcp_import_present(self):
        """langchain_mcp_adapters 사용 코드가 tools.py에 있어야 한다."""
        source = TOOLS_PATH.read_text(encoding="utf-8")
        assert "langchain_mcp_adapters" in source, (
            "tools.py에서 langchain_mcp_adapters를 사용하는 코드가 없습니다. "
            "Oracle 접근 경로가 SQLcl MCP가 아닌 것으로 보입니다."
        )

    def test_multiserver_mcp_client_used(self):
        """MultiServerMCPClient가 tools.py에서 사용되어야 한다."""
        source = TOOLS_PATH.read_text(encoding="utf-8")
        assert "MultiServerMCPClient" in source, (
            "MultiServerMCPClient가 tools.py에서 사용되지 않습니다."
        )


# ---------------------------------------------------------------------------
# 런타임 검증: MCP 경로 사용 확인 (mock 기반)
# ---------------------------------------------------------------------------

class TestDbToolMCPPath:
    """db_tool이 SQLcl MCP 경로를 통해 Oracle에 접근하는지 검증한다."""

    def test_db_tool_calls_mcp_fetch_plan_when_available(self):
        """sqlcl_available()이 True이고 Oracle DSN이 설정되어 있으면 _sqlcl_mcp_fetch_plan을 호출한다."""
        from src import tools

        mock_plan = "TABLE ACCESS FULL ORDERS Cost=8420 Rows=1"

        with (
            patch.object(tools, "sqlcl_available", return_value=True),
            patch.object(tools, "_oracle_configured", return_value=True),
            patch.object(
                tools,
                "_run_async_in_new_thread",
                return_value=mock_plan,
            ) as mock_runner,
        ):
            result = tools.db_tool("주문 고객 조인 성능 문제")

        assert result is not None
        assert result["execution_plan"] == mock_plan
        mock_runner.assert_called_once()

    def test_db_tool_falls_back_to_mock_when_sqlcl_unavailable(self):
        """sqlcl_available()이 False이면 MCP를 호출하지 않고 mock 실행계획을 반환한다."""
        from src import tools

        with (
            patch.object(tools, "sqlcl_available", return_value=False),
            patch.object(tools, "_oracle_configured", return_value=True),
            patch.object(tools, "_run_async_in_new_thread") as mock_runner,
        ):
            result = tools.db_tool("주문 고객 조인 성능 문제")

        assert result is not None
        mock_runner.assert_not_called()
        # mock 폴백 실행계획이 반환된다
        assert "fallback" in result["execution_plan"].lower() or len(result["execution_plan"]) > 0

    def test_db_tool_falls_back_to_mock_when_oracle_not_configured(self):
        """ORACLE_DSN이 없으면 MCP를 호출하지 않고 mock 실행계획을 반환한다."""
        from src import tools

        with (
            patch.object(tools, "sqlcl_available", return_value=True),
            patch.object(tools, "_oracle_configured", return_value=False),
            patch.object(tools, "_run_async_in_new_thread") as mock_runner,
        ):
            result = tools.db_tool("주문 고객 조인 성능 문제")

        assert result is not None
        mock_runner.assert_not_called()

    def test_db_tool_falls_back_to_mock_on_mcp_error(self):
        """MCP 호출이 실패하면 mock 실행계획으로 폴백한다(예외를 올리지 않는다)."""
        from src import tools

        with (
            patch.object(tools, "sqlcl_available", return_value=True),
            patch.object(tools, "_oracle_configured", return_value=True),
            patch.object(
                tools,
                "_run_async_in_new_thread",
                side_effect=RuntimeError("SQLcl MCP connection failed"),
            ),
        ):
            result = tools.db_tool("주문 고객 조인 성능 문제")

        assert result is not None
        assert "execution_plan" in result

    def test_db_tool_returns_none_for_no_match(self):
        """카탈로그에 매칭되는 키워드가 없으면 None을 반환한다."""
        from src import tools

        result = tools.db_tool("날씨가 어때요")
        assert result is None


class TestRunUserSqlMCPPath:
    """run_user_sql이 SQLcl MCP 경로를 통해 Oracle에 접근하는지 검증한다."""

    def test_run_user_sql_calls_mcp_when_available(self):
        """sqlcl_available()이 True이고 Oracle DSN이 설정되어 있으면 _sqlcl_mcp_run_user_sql을 통해 실행된다."""
        from src import tools

        sql = "SELECT 1 FROM dual"
        mock_result = {"key": "user_sql", "sql": sql, "execution_plan": "TABLE ACCESS DUAL"}

        with (
            patch.object(tools, "_oracle_configured", return_value=True),
            patch.object(tools, "sqlcl_available", return_value=True),
            patch.object(
                tools,
                "_run_async_in_new_thread",
                return_value=mock_result,
            ) as mock_runner,
        ):
            result = tools.run_user_sql(sql)

        assert result == mock_result
        mock_runner.assert_called_once()

    def test_run_user_sql_raises_when_oracle_not_configured(self):
        """ORACLE_DSN이 없으면 RuntimeError를 올린다."""
        from src import tools

        with patch.object(tools, "_oracle_configured", return_value=False):
            with pytest.raises(RuntimeError, match="ORACLE_DSN"):
                tools.run_user_sql("SELECT 1 FROM dual")

    def test_run_user_sql_raises_when_sqlcl_unavailable(self):
        """SQLcl 실행파일이 없으면 RuntimeError를 올린다."""
        from src import tools

        with (
            patch.object(tools, "_oracle_configured", return_value=True),
            patch.object(tools, "sqlcl_available", return_value=False),
        ):
            with pytest.raises(RuntimeError, match="SQLcl"):
                tools.run_user_sql("SELECT 1 FROM dual")


# ---------------------------------------------------------------------------
# SQLcl MCP 설정 검증
# ---------------------------------------------------------------------------

class TestSqlclMcpConfig:
    """_sqlcl_connected_config가 Oracle 접속 정보를 올바르게 포함하는지 확인한다."""

    def test_config_includes_connection_when_dsn_set(self):
        """ORACLE_DSN, ORACLE_APP_USER, ORACLE_APP_PASSWORD가 모두 있으면 args에 접속 정보가 포함된다."""
        from src import tools

        with (
            patch.dict(
                "os.environ",
                {
                    "ORACLE_DSN": "localhost:1521/FREEPDB1",
                    "ORACLE_APP_USER": "appuser",
                    "ORACLE_APP_PASSWORD": "secret",
                },
            )
        ):
            config = tools._sqlcl_connected_config()

        oracle_cfg = config["oracle-sqlcl"]
        assert oracle_cfg["transport"] == "stdio"
        assert "-mcp" in oracle_cfg["args"]
        # 접속 문자열이 args에 포함되어야 한다
        joined = " ".join(oracle_cfg["args"])
        assert "appuser" in joined
        assert "localhost:1521/FREEPDB1" in joined

    def test_config_no_connection_when_dsn_missing(self):
        """ORACLE_DSN이 없으면 접속 정보 없이 '-mcp' arg만 포함한다."""
        import os
        from src import tools

        env = {k: v for k, v in os.environ.items() if k not in ("ORACLE_DSN",)}
        env.pop("ORACLE_DSN", None)

        with patch.dict("os.environ", env, clear=True):
            config = tools._sqlcl_connected_config()

        oracle_cfg = config["oracle-sqlcl"]
        assert oracle_cfg["args"] == ["-mcp"]


# ---------------------------------------------------------------------------
# 경계 확장 검증: src/ 전체 서비스 경로와 의존성 선언
# ---------------------------------------------------------------------------

DIRECT_DRIVER_MODULES = {"oracledb", "cx_Oracle"}


def _src_modules() -> list[Path]:
    """src/ 아래의 모든 파이썬 서비스 모듈 경로."""
    return sorted(
        p for p in (PROJECT_ROOT / "src").rglob("*.py")
        if "__pycache__" not in p.parts
    )


class TestNoDirectDriverAcrossServicePaths:
    """tools.py뿐 아니라 src/ 전체 서비스 경로에서 직접 DB 드라이버가 없어야 한다."""

    def test_src_modules_discovered(self):
        """스캔 대상 모듈이 실제로 존재해야 한다(빈 스캔으로 통과하는 것을 막는다)."""
        modules = _src_modules()
        assert len(modules) >= 5, f"src/ 모듈 스캔 결과가 비정상적으로 적음: {modules}"
        assert TOOLS_PATH in modules

    def test_no_direct_driver_import_in_any_src_module(self):
        """src/ 어느 모듈도 oracledb/cx_Oracle을 import하지 않는다."""
        violations = []
        for path in _src_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root = alias.name.split(".")[0]
                        if root in DIRECT_DRIVER_MODULES:
                            violations.append(f"{path.name}:{node.lineno} import {alias.name}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    root = node.module.split(".")[0]
                    if root in DIRECT_DRIVER_MODULES:
                        violations.append(f"{path.name}:{node.lineno} from {node.module}")
        assert not violations, (
            f"직접 DB 드라이버 import 발견: {violations}. "
            "Oracle 접근은 SQLcl MCP 단일 경로만 사용해야 합니다."
        )

    def test_no_direct_driver_connect_call_in_any_src_module(self):
        """src/ 어느 모듈도 oracledb.connect()/cx_Oracle.connect()를 호출하지 않는다."""
        violations = []
        for path in _src_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr in ("connect", "create_pool")
                    and isinstance(func.value, ast.Name)
                    and func.value.id in DIRECT_DRIVER_MODULES
                ):
                    violations.append(f"{path.name}:{node.lineno} {func.value.id}.{func.attr}()")
        assert not violations, (
            f"직접 DB 드라이버 접속 호출 발견: {violations}. "
            "실행계획 조회와 제한 실행은 SQLcl MCP를 통해야 합니다."
        )


class TestDependencyManifestHasNoDirectDriver:
    """직접 DB 드라이버는 의존성으로도 선언하지 않는다."""

    def test_requirements_does_not_declare_direct_driver(self):
        req = PROJECT_ROOT / "requirements.txt"
        declared = []
        for raw in req.read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            name = re.split(r"[<>=!\[;]", line)[0].strip()
            if name in DIRECT_DRIVER_MODULES:
                declared.append(name)
        assert not declared, (
            f"requirements.txt가 직접 DB 드라이버를 선언함: {declared}. "
            "Oracle 접근 경로는 SQLcl MCP 단일 경로여야 합니다."
        )

    def test_requirements_declares_mcp_adapter(self):
        """SQLcl MCP 소비에 필요한 어댑터 의존성은 선언되어 있어야 한다."""
        req_text = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
        assert "langchain-mcp-adapters" in req_text, (
            "langchain-mcp-adapters 의존성이 없습니다. SQLcl MCP 경로를 사용할 수 없습니다."
        )
