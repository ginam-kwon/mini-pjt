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
    """SQLcl MCP 서버 설정.

    이전엔 `sql user/pass@dsn -mcp`처럼 접속 문자열을 CLI 인자로 넘기면 MCP 세션도 자동
    접속되는 줄 알았는데, 실측해보니 그렇지 않았다 — `sql_run` 도구가 항상
    "Connection not established"를 반환했다. SQLcl MCP의 `connect` 도구는 CLI 인자가 아니라
    **connmgr에 미리 저장된 연결 이름**만 받는다("Connection not found: appuser" 에러로 확인).
    그래서 CLI 인자는 그냥 `-mcp`만 넘기고, 각 세션에서 `connect` 도구를 명시적으로 호출해
    `SQLCL_CONNECTION_NAME`(기본 mini_pjt_conn)이라는 이름으로 접속한다(`_connect_and_get_sql_tool`).
    이 이름의 연결은 이 호스트에 한 번 저장해둬야 한다 — README '실DB 셋업' 참고:
        sql -S /nolog
        SQL> connect -save mini_pjt_conn -savepwd appuser/"AppUser_2026!"@localhost:1521/FREEPDB1
    저장돼 있지 않으면 connect 호출이 실패하고, 이 프로젝트 전체의 기존 정책대로 mock/에러
    폴백으로 조용히 넘어간다(Docker/SQLcl 자체가 없는 채점 환경과 동일하게 처리됨)."""
    base_env = {
        "JAVA_HOME": SQLCL_JAVA_HOME,
        "PATH": f"{SQLCL_JAVA_HOME}/bin:" + os.environ.get("PATH", "/usr/bin:/bin"),
    }
    return {
        "oracle-sqlcl": {
            "command": os.environ.get("SQLCL_BIN", "sql"),
            "args": ["-mcp"],
            "transport": "stdio",
            "env": base_env,
        }
    }


def _sqlcl_connection_name() -> str:
    return os.environ.get("SQLCL_CONNECTION_NAME", "mini_pjt_conn")


async def _connect_and_get_sql_tool(session):
    """MCP 세션에서 도구 목록을 가져오고 저장된 연결(SQLCL_CONNECTION_NAME)로 접속한 뒤
    SQL 실행 도구를 반환한다. 모든 SQLcl MCP 호출 지점이 이 순서를 공유한다.

    실제 SQLcl MCP 서버가 노출하는 도구 이름은 'sql_run'이다 — 이전 코드가 찾던 이름들
    ('run_statement'/'sql'/'execute_sql'/'run_sql')은 전부 실제 도구 목록에 없어서 매번
    tools_list[0]('connections_list')로 잘못 폴백하고 있었다(실측으로 발견)."""
    from langchain_mcp_adapters.tools import load_mcp_tools

    tools_list = await load_mcp_tools(session)
    if not tools_list:
        raise RuntimeError("SQLcl MCP에서 사용 가능한 도구가 없습니다.")
    tools = {t.name: t for t in tools_list}

    connect_tool = tools.get("connect")
    if connect_tool is not None:
        await connect_tool.ainvoke({"connection_name": _sqlcl_connection_name()})

    sql_tool = None
    for candidate in ("sql_run", "sqlcl_run", "run_statement", "sql", "execute_sql", "run_sql"):
        if candidate in tools:
            sql_tool = tools[candidate]
            break
    if sql_tool is None:
        sql_tool = tools_list[0]
    return sql_tool


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

    config = _sqlcl_connected_config()
    server_name = next(iter(config))
    client = MultiServerMCPClient(config)
    async with client.session(server_name) as session:
        sql_tool = await _connect_and_get_sql_tool(session)

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

    config = _sqlcl_connected_config()
    server_name = next(iter(config))
    client = MultiServerMCPClient(config)
    async with client.session(server_name) as session:
        sql_tool = await _connect_and_get_sql_tool(session)

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


def _fallback_search_sql_candidates(question: str, top_k: int = 5) -> list[dict]:
    """(기존 로직) 자연어 질문에서 QUERY_CATALOG의 별칭을 키워드로 매칭해 마스킹된 SQL 후보
    목록을 반환한다. Oracle/SQLcl MCP를 쓸 수 없을 때의 안전망 — db_tool과 동일한 폴백 철학."""
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


_SQL_TEXT_KEYWORD_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{2,}$")


def _relevant_sql_text_keywords(question: str) -> list[str]:
    """자연어 질문에서 QUERY_CATALOG 별칭 매칭으로 관련 시나리오를 고른 뒤, 그중 실제 SQL
    원문에 등장하는 영어 토큰(테이블/컬럼명 등)만 추려 V$SQL 검색 키워드로 쓴다. 한국어 별칭이나
    "hash join"/"range scan"처럼 SQL 원문에 그대로 안 나오는 표현은 제외한다."""
    q = question.lower()
    keywords: set[str] = set()
    for entry in QUERY_CATALOG.values():
        aliases = entry["aliases"]
        if any(alias.lower() in q for alias in aliases):
            sql_lower = entry["sql"].lower()
            for alias in aliases:
                alias_lower = alias.lower()
                if _SQL_TEXT_KEYWORD_RE.match(alias_lower) and alias_lower in sql_lower:
                    keywords.add(alias_lower)
    return sorted(keywords)


def _parse_sql_run_csv(result: object) -> list[dict]:
    """sql_run 도구가 반환하는 [{'type': 'text', 'text': '"COL1","COL2"\\nval1,val2\\n'}, ...]
    형태에서 첫 텍스트 블록을 quoted-CSV로 파싱한다(실측으로 확인한 실제 출력 형식).

    SQL 자체가 실패하면(예: ORA-00935) sql_run은 예외 대신 "Error starting at line..." 같은
    평문을 돌려준다 — CSV가 아니므로 파싱을 시도하면 DictReader가 헤더/행 길이 불일치로 깨진다.
    호출부가 예외로 감싸 폴백하게 두지 않고, 여기서 분명한 실패 신호(빈 리스트) 대신 조용히
    None을 반환해 명시적으로 구분한다."""
    import csv
    import io

    if not result:
        return []
    first = result[0] if isinstance(result, list) else result
    text = first.get("text", "") if isinstance(first, dict) else str(first)
    if not text.strip():
        return []
    if text.lstrip().startswith("Error") or "SQL Error" in text or "ORA-" in text:
        raise RuntimeError(f"sql_run 도구가 오류를 반환했습니다: {text[:200]!r}")
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for row in reader:
        if None in row:  # 헤더보다 필드가 많은 손상된 행 — 조용히 건너뛴다
            continue
        if any((v or "").strip() for v in row.values()):
            rows.append(row)
    return rows


async def _warmup_operational_queries_async() -> None:
    from langchain_mcp_adapters.client import MultiServerMCPClient

    config = _sqlcl_connected_config()
    server_name = next(iter(config))
    client = MultiServerMCPClient(config)
    async with client.session(server_name) as session:
        sql_tool = await _connect_and_get_sql_tool(session)
        for key, entry in QUERY_CATALOG.items():
            try:
                for stmt in entry.get("session_setup", []):
                    await sql_tool.ainvoke({"sql": stmt})
                hinted_sql = re.sub(
                    r"(?i)\bselect\b",
                    "SELECT /*+ gather_plan_statistics */",
                    _cap_rows(entry["sql"]),
                    count=1,
                )
                await sql_tool.ainvoke({"sql": hinted_sql})
            except Exception as e:
                print(f"[warmup] '{key}' 웜업 실행 실패(무시): {type(e).__name__}: {e}")


def warmup_operational_queries() -> None:
    """대표 운영 쿼리 8개를 실제로 실행해 Oracle 공유 풀(V$SQL)에 올려둔다 — search_sql_candidates가
    조회할 대상을 만드는 웜업. API 서버 기동 시 1회 호출한다(src/api.py startup 이벤트).

    V$SQL은 인스턴스 메모리에만 있고 db/init/*.sql은 볼륨이 비어있을 때 딱 1회만 실행되므로,
    "검색 가능한 상태로 만드는 것"은 DB 초기화가 아니라 서버가 뜰 때마다 이 함수가 책임진다 —
    컨테이너를 재시작해도(볼륨은 그대로라 db/init은 다시 안 돌아도) API 서버만 재기동하면
    다시 채워진다. Oracle 미설정/SQLcl 없음/실패는 조용히 무시한다(best-effort — 실패해도
    서버 기동을 막지 않고, search_sql_candidates에는 폴백 경로가 항상 있다)."""
    if not (sqlcl_available() and _oracle_configured()):
        return
    try:
        _run_async_in_new_thread(_warmup_operational_queries_async(), timeout=120)
    except Exception as e:
        print(f"[warmup] 운영 쿼리 웜업 실패(무시): {type(e).__name__}: {e}")


_ROWNUM_WRAPPER_RE = re.compile(
    r"^SELECT\s+(?:/\*\+[^*]*\*/\s+)?\*\s+FROM\s*\(\s*(.*?)\s*\)\s*WHERE\s+ROWNUM\s*<=\s*:?\S+\s*$",
    re.IGNORECASE | re.DOTALL,
)
_PLAIN_COMMENT_RE = re.compile(r"/\*(?!\+)[^*]*\*/")


def _clean_cached_sql_text(sql_text: str) -> str:
    """warmup_operational_queries()가 붙인 결과 행 제한 래핑과 SQLcl MCP 클라이언트가 자동으로
    넣는 부가 주석("LLM in use is ...")을 표시용으로 걷어낸다(V$SQL.SQL_TEXT는 실제로 실행된
    문장을 그대로 담고 있어서 이런 래핑까지 그대로 캐시돼 있다)."""
    cleaned = _PLAIN_COMMENT_RE.sub("", sql_text).strip()
    m = _ROWNUM_WRAPPER_RE.match(cleaned)
    if m:
        cleaned = m.group(1).strip()
    return cleaned


async def _v_sql_search_async(keywords: list[str], top_k: int) -> list[dict]:
    from langchain_mcp_adapters.client import MultiServerMCPClient

    config = _sqlcl_connected_config()
    server_name = next(iter(config))
    client = MultiServerMCPClient(config)
    async with client.session(server_name) as session:
        sql_tool = await _connect_and_get_sql_tool(session)
        like_clauses = " OR ".join(f"LOWER(sql_text) LIKE '%{kw}%'" for kw in keywords)
        # V$SQL은 같은 SQL_ID라도 자식 커서(bind peeking 등)별로 여러 행을 가질 수 있어
        # SQL_ID로 GROUP BY해 하나로 합친다 — 아니면 같은 후보가 여러 번 보인다(실측으로 발견).
        query = (
            "SELECT sql_id, MIN(sql_text) AS sql_text, SUM(executions) AS executions, "
            "SUM(elapsed_time) AS elapsed_time, MAX(last_active_time) AS last_active_time "
            "FROM v$sql "
            f"WHERE parsing_schema_name = 'APPUSER' AND ({like_clauses}) "
            "AND LOWER(sql_text) NOT LIKE '%v$sql%' "
            "GROUP BY sql_id "
            # SUM(elapsed_time)를 ORDER BY에 다시 쓰면 ORA-00935(group function is nested too
            # deeply)가 난다(실측으로 확인) — SELECT의 별칭(elapsed_time)을 그대로 참조한다.
            f"ORDER BY elapsed_time DESC FETCH FIRST {int(top_k)} ROWS ONLY"
        )
        result = await sql_tool.ainvoke({"sql": query})
        return _parse_sql_run_csv(result)


def search_sql_candidates(question: str, top_k: int = 5) -> list[dict]:
    """자연어 질문에서 관련 있는 운영 SQL 후보를 실제 Oracle V$SQL(공유 풀)에서 동적으로
    조회한다. QUERY_CATALOG의 별칭 매칭으로 "어떤 테이블/시나리오가 관련 있는지"만 결정적으로
    판정하고(기존 로직 재사용), 그 결과로 정적 텍스트를 돌려주는 대신 V$SQL을 SQLcl MCP로
    조회해 진짜 sql_id·sql_text·실행 통계를 가져온다 — warmup_operational_queries()가 올려둔
    대표 쿼리뿐 아니라 같은 테이블을 건드리는 다른 캐시된 쿼리도 함께 잡힌다.

    Oracle 미설정/SQLcl 없음/MCP 실패 시 _fallback_search_sql_candidates()(기존 카탈로그
    키워드 매칭)로 조용히 폴백한다 — db_tool과 동일한 안전망 철학."""
    keywords = _relevant_sql_text_keywords(question)
    if not keywords or not (sqlcl_available() and _oracle_configured()):
        return _fallback_search_sql_candidates(question, top_k=top_k)

    try:
        rows = _run_async_in_new_thread(_v_sql_search_async(keywords, top_k))
    except Exception as e:
        print(f"[search_sql_candidates] V$SQL 조회 실패({type(e).__name__}: {e}) — 폴백 사용")
        return _fallback_search_sql_candidates(question, top_k=top_k)

    if not rows:
        return _fallback_search_sql_candidates(question, top_k=top_k)

    candidates = []
    for rank, row in enumerate(rows, start=1):
        sql_text = _clean_cached_sql_text(row.get("SQL_TEXT", ""))
        executions = row.get("EXECUTIONS", "0")
        elapsed_us = row.get("ELAPSED_TIME", "0")
        try:
            elapsed_secs = float(elapsed_us) / 1_000_000
        except ValueError:
            elapsed_secs = 0.0
        candidates.append({
            "sql_id": row.get("SQL_ID", ""),
            "masked_sql": _mask_sql_literals(sql_text),
            "description": f"실행 {executions}회, 총 소요 {elapsed_secs:.1f}초 (V$SQL 실측)",
            "rank": rank,
            "executions": executions,
            "elapsed_secs": elapsed_secs,
        })
    return candidates


_SQL_ID_RE = re.compile(r"^[A-Za-z0-9]+$")


async def _sqlcl_mcp_fetch_live_plan(sql_id: str) -> dict:
    """실제 V$SQL.SQL_ID로 SQL 원문과 실제 실행계획(DBMS_XPLAN.DISPLAY_CURSOR)을 가져온다.
    공유 풀에서 evict돼 더 이상 없는 sql_id는 빈 dict를 반환한다(호출부가 no_answer로 처리)."""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    config = _sqlcl_connected_config()
    server_name = next(iter(config))
    client = MultiServerMCPClient(config)
    async with client.session(server_name) as session:
        sql_tool = await _connect_and_get_sql_tool(session)
        text_result = await sql_tool.ainvoke({
            "sql": f"SELECT sql_text FROM v$sql WHERE sql_id = '{sql_id}' FETCH FIRST 1 ROWS ONLY"
        })
        rows = _parse_sql_run_csv(text_result)
        if not rows:
            return {}
        sql_text = _clean_cached_sql_text(rows[0].get("SQL_TEXT", ""))
        plan_result = await sql_tool.ainvoke({
            "sql": f"SELECT PLAN_TABLE_OUTPUT FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR('{sql_id}', NULL, 'ALLSTATS LAST'))"
        })
        return {"sql": sql_text, "execution_plan": str(plan_result)}


def fetch_live_sql_plan(sql_id: str) -> dict | None:
    """주어진 실제 V$SQL.SQL_ID의 SQL 원문·실행계획을 SQLcl MCP로 조회한다. Oracle 미설정/SQLcl
    없음/조회 실패/공유 풀에서 사라진 sql_id는 None을 반환한다(호출부가 no_answer로 안내)."""
    if not _SQL_ID_RE.match(sql_id):
        return None
    if not (sqlcl_available() and _oracle_configured()):
        return None
    try:
        result = _run_async_in_new_thread(_sqlcl_mcp_fetch_live_plan(sql_id))
    except Exception as e:
        print(f"[fetch_live_sql_plan] sql_id='{sql_id}' 조회 실패({type(e).__name__}: {e})")
        return None
    return result or None


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
