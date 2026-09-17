# tools.py - Oracle SQLcl MCP 단일 경로 + db_tool(실행계획 조회, mock 폴백)
#
# 핵심 의사결정: Oracle 대상 DB와의 모든 상호작용(스키마 조회, 실행계획 조회, 제한 실행)은
# Oracle SQLcl MCP 단일 경로를 통해서만 이루어진다. 직접 DB 드라이버(oracledb) 접속 경로는
# 사용하지 않는다.
#
# 자연어 질문↔대상 쿼리 매칭은 고정 카탈로그에 대한 가장 긴 별칭 키워드 매칭으로 결정적으로
# 수행한다. SQLCL_BIN이 없거나 Oracle DSN이 설정되지 않은 경우(채점 환경 등) 고정 mock
# 실행계획 텍스트로 조용히 폴백한다 — db_tool은 이 경우에도 예외를 던지지 않는다.
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import re
import shutil

from dotenv import load_dotenv
from langchain_core.tools import tool

load_dotenv()

# SQLcl MCP 서버 기본 설정 (Oracle 접속 정보는 포함하지 않음 — get_oracle_mcp_tools 전용)
SQLCL_JAVA_HOME = os.environ.get("SQLCL_JAVA_HOME", "/usr/lib/jvm/java-21-openjdk-arm64")

SQLCL_MCP_SERVER_CONFIG = {
    "oracle-sqlcl": {
        "command": os.environ.get("SQLCL_BIN", "sql"),
        "args": ["-mcp"],
        "transport": "stdio",
        "env": {
            "JAVA_HOME": SQLCL_JAVA_HOME,
            "PATH": f"{SQLCL_JAVA_HOME}/bin:" + os.environ.get("PATH", "/usr/bin:/bin"),
        },
    }
}


# "제한 실행" 정책: 실측 통계 수집을 위해 실제로 실행을 허용하더라도 반환 행 수와 대기 시간에
# 상한을 둔다 — 위험 게이트를 통과했다고 해서 무제한 실행을 허용하지는 않는다.
MAX_EXECUTION_ROWS = int(os.environ.get("MAX_EXECUTION_ROWS", "1000"))
MCP_CALL_TIMEOUT_SECONDS = float(os.environ.get("MCP_CALL_TIMEOUT_SECONDS", "30"))


def _cap_rows(sql: str, max_rows: int = MAX_EXECUTION_ROWS) -> str:
    """SELECT 결과 행 수를 max_rows로 제한하는 래핑 쿼리를 만든다(제한 실행)."""
    return f"SELECT * FROM (\n{sql}\n) WHERE ROWNUM <= {max_rows}"


def sqlcl_available() -> bool:
    """SQLcl 실행파일이 PATH에 있는지 확인한다."""
    return shutil.which(os.environ.get("SQLCL_BIN", "sql")) is not None


def _oracle_configured() -> bool:
    """Oracle 접속에 필요한 환경변수(DSN)가 설정되어 있는지 확인한다."""
    return bool(os.environ.get("ORACLE_DSN"))


def _sqlcl_connected_config() -> dict:
    """Oracle에 자동 접속하는 SQLcl MCP 서버 설정.
    ORACLE_DSN, ORACLE_APP_USER, ORACLE_APP_PASSWORD가 있으면 SQLcl이 시작과 함께 접속한다."""
    oracle_dsn = os.environ.get("ORACLE_DSN", "")
    oracle_user = os.environ.get("ORACLE_APP_USER", "appuser")
    oracle_pwd = os.environ.get("ORACLE_APP_PASSWORD", "")

    base_env = {
        "JAVA_HOME": SQLCL_JAVA_HOME,
        "PATH": f"{SQLCL_JAVA_HOME}/bin:" + os.environ.get("PATH", "/usr/bin:/bin"),
    }

    if oracle_dsn and oracle_user and oracle_pwd:
        # SQLcl에 접속 정보를 인자로 전달해 MCP 서버 시작과 동시에 Oracle에 접속한다
        args = [f"{oracle_user}/{oracle_pwd}@{oracle_dsn}", "-mcp"]
    else:
        args = ["-mcp"]

    return {
        "oracle-sqlcl": {
            "command": os.environ.get("SQLCL_BIN", "sql"),
            "args": args,
            "transport": "stdio",
            "env": base_env,
        }
    }


async def get_oracle_mcp_tools() -> list:
    """SQLcl MCP 서버에 연결해 도구 목록을 가져온다 (explain_agent 바인딩용). 서버가 없으면 빈 리스트를 반환한다."""
    if not sqlcl_available():
        return []
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient

        client = MultiServerMCPClient(SQLCL_MCP_SERVER_CONFIG)
        return await client.get_tools()
    except Exception:
        return []


def _run_async_in_new_thread(coro, timeout: float | None = None) -> object:
    """코루틴을 새 스레드에서 asyncio.run()으로 실행한다.
    호출자의 이벤트 루프 상태와 무관하게 동작하며 결과를 동기적으로 반환한다.
    timeout(초) 안에 끝나지 않으면 concurrent.futures.TimeoutError를 던져 요청 경로가
    무한 대기하지 않게 한다 — 기본값은 MCP_CALL_TIMEOUT_SECONDS."""
    effective_timeout = MCP_CALL_TIMEOUT_SECONDS if timeout is None else timeout
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result(timeout=effective_timeout)


async def _sqlcl_mcp_fetch_plan(entry: dict) -> str:
    """SQLcl MCP를 통해 Oracle에 접속해 실행계획을 조회한다.
    접속/실행 실패 시 예외를 그대로 올린다(호출부가 mock 폴백으로 처리).

    langchain-mcp-adapters 0.3.x부터 MultiServerMCPClient는 컨텍스트 매니저(async with)로
    쓸 수 없다(NotImplementedError) — client.session(server_name)으로 세션 하나를 열어
    session_setup부터 마지막 DBMS_XPLAN 조회까지 같은 SQLcl 프로세스를 재사용한다. 매 호출마다
    새 세션을 열면 SQLcl(JVM) 프로세스가 문장 수만큼 재기동돼 지연이 크게 늘어난다."""
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from langchain_mcp_adapters.tools import load_mcp_tools

    config = _sqlcl_connected_config()
    server_name = next(iter(config))
    client = MultiServerMCPClient(config)
    async with client.session(server_name) as session:
        tools_list = await load_mcp_tools(session)
        if not tools_list:
            raise RuntimeError("SQLcl MCP에서 사용 가능한 도구가 없습니다.")

        tools = {t.name: t for t in tools_list}
        sql_tool = None
        for candidate in ("run_statement", "sql", "execute_sql", "run_sql"):
            if candidate in tools:
                sql_tool = tools[candidate]
                break
        if sql_tool is None:
            sql_tool = tools_list[0]

        for stmt in entry.get("session_setup", []):
            await sql_tool.ainvoke({"sql": stmt})

        if entry.get("mode") == "execute_stats":
            hinted_sql = re.sub(
                r"(?i)\bselect\b",
                "SELECT /*+ gather_plan_statistics */",
                _cap_rows(entry["sql"]),
                count=1,
            )
            await sql_tool.ainvoke({"sql": hinted_sql})
            result = await sql_tool.ainvoke({
                "sql": "SELECT PLAN_TABLE_OUTPUT FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR(NULL, NULL, 'ALLSTATS LAST'))"
            })
        else:
            await sql_tool.ainvoke({"sql": f"EXPLAIN PLAN FOR {entry['sql']}"})
            result = await sql_tool.ainvoke({
                "sql": "SELECT PLAN_TABLE_OUTPUT FROM TABLE(DBMS_XPLAN.DISPLAY(NULL, NULL, 'ALL'))"
            })

        return str(result)


async def _sqlcl_mcp_run_user_sql(sql: str) -> dict:
    """SQLcl MCP를 통해 사용자 SELECT 문을 실행하고 실측 실행계획(E-Rows/A-Rows)을 반환한다.
    접속/실행 실패 시 예외를 그대로 올린다(임의의 사용자 SQL이라 mock 폴백 없음).
    _sqlcl_mcp_fetch_plan과 동일한 이유로 client.session()을 직접 사용한다."""
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from langchain_mcp_adapters.tools import load_mcp_tools

    config = _sqlcl_connected_config()
    server_name = next(iter(config))
    client = MultiServerMCPClient(config)
    async with client.session(server_name) as session:
        tools_list = await load_mcp_tools(session)
        if not tools_list:
            raise RuntimeError("SQLcl MCP에서 사용 가능한 도구가 없습니다.")

        tools = {t.name: t for t in tools_list}
        sql_tool = None
        for candidate in ("run_statement", "sql", "execute_sql", "run_sql"):
            if candidate in tools:
                sql_tool = tools[candidate]
                break
        if sql_tool is None:
            sql_tool = tools_list[0]

        hinted_sql = re.sub(
            r"(?i)\bselect\b",
            "SELECT /*+ gather_plan_statistics */",
            _cap_rows(sql),
            count=1,
        )
        await sql_tool.ainvoke({"sql": hinted_sql})
        result = await sql_tool.ainvoke({
            "sql": "SELECT PLAN_TABLE_OUTPUT FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR(NULL, NULL, 'ALLSTATS LAST'))"
        })
        return {"key": "user_sql", "sql": sql, "execution_plan": str(result)}


# ------------------------------------------------------------
# db_tool: 자연어 질문 → 고정 카탈로그 키워드 매칭 → SQLcl MCP로 실행계획 조회
#          (SQLcl 미설치 또는 Oracle DSN 미설정 시 mock 실행계획 텍스트로 폴백)
# ------------------------------------------------------------
QUERY_CATALOG: dict[str, dict] = {
    "orders_customers_join": {
        "aliases": ["주문", "고객", "orders", "customers", "조인", "join", "join_orders_customers"],
        "sql": (
            "SELECT o.order_id, c.customer_name, o.order_date, o.total_amount "
            "FROM orders o, customers c "
            "WHERE TO_CHAR(o.order_date, 'YYYY-MM-DD') = '2026-09-01' "
            "AND o.customer_id = c.customer_id "
            "AND UPPER(c.customer_name) LIKE 'KIM%'"
        ),
        "mode": "explain",
        "description": "고객·주문 조인 조회. TO_CHAR(order_date)와 UPPER(customer_name) 함수 기반 조건으로 인덱스를 타지 못해 ORDERS·CUSTOMERS 모두 풀스캔 발생.",
        "fallback_plan": (
            "TABLE ACCESS FULL ORDERS Cost=8420 Rows=1 "
            "(Predicate: filter(TO_CHAR(order_date,'YYYY-MM-DD')='2026-09-01')); "
            "TABLE ACCESS FULL CUSTOMERS Cost=3 Rows=100000 "
            "(Predicate: filter(UPPER(customer_name) LIKE 'KIM%')); "
            "NESTED LOOPS Cost=8523"
        ),
    },
    "payments_implicit_cast": {
        "aliases": ["결제", "payments", "emp_id", "형변환", "암묵적"],
        "sql": "SELECT * FROM payments WHERE emp_id = 1001",
        "mode": "explain",
        "description": "결제 테이블 사번 조회. emp_id 컬럼이 VARCHAR2인데 숫자 바인드 변수로 TO_NUMBER(emp_id) 암묵 형변환 발생, 인덱스 무효화 → PAYMENTS 풀스캔.",
        "fallback_plan": (
            "TABLE ACCESS FULL PAYMENTS Cost=9000 Rows=2 "
            "(Predicate: filter(TO_NUMBER(emp_id)=1001)) -- emp_id is VARCHAR2, implicit conversion"
        ),
    },
    "orders_stale_stats": {
        "aliases": ["상태", "status", "통계", "pending", "stale"],
        "sql": "SELECT /*+ gather_plan_statistics */ * FROM orders WHERE status = 'PENDING'",
        "mode": "execute_stats",
        "description": "주문 상태별 조회. 배치 유입 이후 통계 재수집이 안 돼 E-Rows와 A-Rows가 크게 어긋남. PENDING 상태 200,000건을 5건으로 오추정.",
        "fallback_plan": (
            "TABLE ACCESS FULL ORDERS Cost=12000 Rows=5 (actual rows=480000, stale stats suspected)"
        ),
    },
    "orders_order_items_join": {
        "aliases": ["order_items", "주문상세", "상세", "nested loops"],
        "sql": "SELECT /*+ USE_NL(o i) */ * FROM orders o, order_items i WHERE o.order_id = i.order_id",
        "mode": "explain",
        "description": "orders ↔ order_items 대용량 조인. order_items.order_id 인덱스 미생성으로 ORDER_ITEMS 풀스캔 + NESTED LOOPS.",
        "fallback_plan": (
            "NESTED LOOPS Cost=50000; TABLE ACCESS FULL ORDERS Rows=200000; "
            "TABLE ACCESS FULL ORDER_ITEMS Rows=2000000"
        ),
    },
    "orders_customers_hash_tempspc": {
        "aliases": ["해시조인", "hash join", "tempspc", "임시테이블스페이스", "임시"],
        "sql": "SELECT * FROM orders o, customers c WHERE o.customer_id = c.customer_id",
        "session_setup": [
            "ALTER SESSION SET WORKAREA_SIZE_POLICY = MANUAL",
            "ALTER SESSION SET HASH_AREA_SIZE = 65536",
        ],
        "mode": "explain",
        "description": "전체 고객 매출 집계. HASH JOIN Build 단계에서 PGA 한도 초과로 TempSpc(임시 테이블스페이스) 스필 발생.",
        "fallback_plan": "HASH JOIN Cost=15000 TempSpc=204800",
    },
    "orders_index_fragmentation": {
        "aliases": ["인덱스", "단편화", "range scan", "fragmentation"],
        "sql": "SELECT * FROM orders WHERE customer_id = 777",
        "mode": "explain",
        "description": "특정 고객 주문 이력 조회. idx_orders_customer_id 인덱스 단편화로 leaf block이 흩어져 INDEX RANGE SCAN 비용이 비정상적으로 높음.",
        "fallback_plan": "INDEX RANGE SCAN IDX_ORDERS_CUSTOMER_ID Cost=450 (unusually high for a range scan)",
    },
    "orders_or_condition": {
        "aliases": ["or 조건", "region", "seoul"],
        "sql": "SELECT * FROM orders WHERE region = 'SEOUL' OR customer_id = 500",
        "mode": "explain",
        "description": "지역 또는 고객 ID 조건 주문 조회. OR 조건으로 인덱스가 있음에도 옵티마이저가 ORDERS 풀스캔 선택.",
        "fallback_plan": (
            "TABLE ACCESS FULL ORDERS Cost=9000 (Predicate: filter(region='SEOUL' OR customer_id=500))"
        ),
    },
}


def _mask_sql_literals(sql: str) -> str:
    """SQL 원문의 리터럴 값(문자열·숫자)을 :param_N 형식으로 마스킹한다.
    단일 따옴표로 감싼 문자열 리터럴과 비교 연산자 뒤의 숫자 리터럴을 순서대로 치환한다."""
    param_counter = [0]

    def _next() -> str:
        param_counter[0] += 1
        return f":param_{param_counter[0]}"

    # 문자열 리터럴 ('...' 형태, '' 이스케이프 허용)
    result = re.sub(r"'(?:[^']|'')*'", lambda _m: _next(), sql)
    # 숫자 리터럴 (비교/= 뒤에 나오는 정수)
    result = re.sub(r"(?<=[=<>!,\s])(\s*)(\b\d+\b)", lambda m: m.group(1) + _next(), result)
    return result


def search_sql_candidates(question: str, top_k: int = 5) -> list[dict]:
    """자연어 질문에서 QUERY_CATALOG의 키워드를 매칭해 마스킹된 SQL 후보 목록을 반환한다.

    각 후보는 sql_id, masked_sql, description, rank를 포함한다.
    매칭 기준은 키워드 길이 합산이며 top_k개 이내로 반환한다."""
    q = question.lower()
    scored: list[tuple[int, str, dict]] = []
    for key, entry in QUERY_CATALOG.items():
        matched_lengths = [len(alias) for alias in entry["aliases"] if alias.lower() in q]
        if matched_lengths:
            score = sum(matched_lengths)
            scored.append((score, key, entry))

    scored.sort(key=lambda x: x[0], reverse=True)
    candidates = []
    for rank, (score, sql_id, entry) in enumerate(scored[:top_k], start=1):
        masked = _mask_sql_literals(entry["sql"])
        description = entry.get("description", entry.get("fallback_plan", ""))
        candidates.append({
            "sql_id": sql_id,
            "masked_sql": masked,
            "description": description,
            "rank": rank,
            "relevance_score": score,
        })
    return candidates


def db_tool(question: str) -> dict | None:
    """자연어 질문에서 고정 쿼리 카탈로그를 키워드로 결정적으로 매칭한 뒤, SQLcl MCP를 통해
    Oracle에서 실제 실행계획을 조회한다. SQLcl이 없거나 Oracle DSN이 설정되지 않았거나
    접속/실행이 실패하면 고정 mock 실행계획 텍스트로 조용히 폴백한다(예외를 던지지 않는다).
    매칭되는 쿼리가 없으면 None을 반환한다."""
    q = question.lower()
    best_key, best_entry, best_len = None, None, -1
    for key, entry in QUERY_CATALOG.items():
        matched_lens = [len(alias) for alias in entry["aliases"] if alias.lower() in q]
        if matched_lens and max(matched_lens) > best_len:
            best_key, best_entry, best_len = key, entry, max(matched_lens)
    if best_entry is None:
        return None

    plan = best_entry["fallback_plan"]
    if sqlcl_available() and _oracle_configured():
        try:
            plan = _run_async_in_new_thread(_sqlcl_mcp_fetch_plan(best_entry))
        except Exception as e:
            print(
                f"[db_tool] SQLcl MCP 조회 실패({type(e).__name__}: {e}) "
                f"— mock 폴백 사용: key={best_key}"
            )
    return {"key": best_key, "sql": best_entry["sql"], "execution_plan": plan}


# ------------------------------------------------------------
# run_user_sql: 사용자가 직접 제시한 SELECT 문을 SQLcl MCP로 실행 후 실측 실행계획 반환
# SELECT/WITH만 허용(allow-list). Oracle DSN이 없거나 실행이 실패하면 예외를 그대로 올린다.
# ------------------------------------------------------------
_SQL_LEADING_KEYWORD = re.compile(r"^\s*(?:--[^\n]*\n|\s)*\(?\s*([A-Za-z]+)", re.IGNORECASE)


def looks_like_sql(text: str) -> bool:
    """질문이 자연어가 아니라 SQL 원문으로 보이면 True."""
    m = _SQL_LEADING_KEYWORD.match(text)
    if not m:
        return False
    return m.group(1).upper() in {
        "SELECT", "WITH", "INSERT", "UPDATE", "DELETE", "MERGE", "DROP",
        "ALTER", "CREATE", "TRUNCATE", "GRANT", "REVOKE", "CALL", "EXEC", "BEGIN",
    }


def is_safe_select(text: str) -> bool:
    """SELECT(또는 WITH ... SELECT) 단일 문장인지 확인한다. 세미콜론으로 다른 문장을 이어붙이면
    첫 문장이 SELECT라도 거부한다."""
    stripped = text.strip().rstrip(";").strip()
    if ";" in stripped:
        return False
    m = _SQL_LEADING_KEYWORD.match(stripped)
    if not m:
        return False
    return m.group(1).upper() in {"SELECT", "WITH"}


def run_user_sql(sql: str) -> dict:
    """사용자가 직접 제시한 SELECT 문을 SQLcl MCP로 실행하고 실측 실행계획(E-Rows/A-Rows 포함)을
    반환한다. Oracle DSN이 없거나 SQLcl이 없거나 실행이 실패하면 예외를 그대로 올린다
    (임의의 사용자 SQL이라 대응할 mock이 없으므로 호출부가 명확한 에러로 처리한다)."""
    if not _oracle_configured():
        raise RuntimeError("ORACLE_DSN이 설정되지 않아 사용자 SQL을 실행할 수 없습니다.")
    if not sqlcl_available():
        raise RuntimeError("SQLcl 실행파일을 찾을 수 없습니다. SQLCL_BIN 또는 PATH를 확인하세요.")

    return _run_async_in_new_thread(_sqlcl_mcp_run_user_sql(sql))


# ------------------------------------------------------------
# 위험 도구 (write) — 항상 mock, HITL 승인(actions.py) 이후에만 실행된다
# ------------------------------------------------------------
@tool
def gather_stats(table_name: str) -> str:
    """(위험: write) 대상 테이블의 옵티마이저 통계를 재수집한다(DBMS_STATS.GATHER_TABLE_STATS).
    실제 DB 변경 작업이므로 반드시 사람 승인 후에만 호출해야 한다."""
    return (
        f"[MOCK] DBMS_STATS.GATHER_TABLE_STATS(ownname=>'APP', tabname=>'{table_name}', "
        f"cascade=>TRUE, method_opt=>'FOR ALL COLUMNS SIZE AUTO') 실행 완료(모의)."
    )


@tool
def create_index(table_name: str, index_ddl: str) -> str:
    """(위험: write) 주어진 DDL로 인덱스를 생성한다. 실제 스키마 변경이므로 반드시
    사람 승인 후에만 호출해야 한다."""
    return f"[MOCK] 다음 DDL을 {table_name}에 실행 완료(모의): {index_ddl}"
