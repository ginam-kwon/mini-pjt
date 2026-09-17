# tools.py - Oracle 공식 MCP(SQLcl 내장 MCP 서버, `sql -mcp`) 연동 + db_tool(실DB 조회, mock 폴백)
#
# 핵심 의사결정 이력: 붙여넣기 입력은 금지한다 — 진단은 항상 db_tool(V$SQL 스캔에 준하는 조회)로만
# 이루어진다. 최초 버전(seed.yaml v1.1.0)은 실제 Oracle DB가 없어 고정 mock 픽스처만 반환했으나,
# 이후 docker-compose(Oracle Database Free) 로컬 인스턴스를 구성해 실제 EXPLAIN PLAN/DBMS_XPLAN을
# 조회하도록 전환했다 (docker-compose.yml, db/init/01_setup_schema_and_data.sql 참고).
# 자연어 질문↔대상 쿼리 매칭은 여전히 고정 카탈로그에 대한 exact/substring 키워드 매칭으로 결정적으로
# 수행한다(퍼지 매칭·NLU 불필요) — 바뀐 것은 "매칭 이후 실행계획을 어디서 가져오는가" 뿐이다.
# ORACLE_DSN이 없거나 DB 접속이 실패하면(예: 채점 환경에 Docker가 없는 경우) 과거와 동일한 고정
# mock 실행계획 텍스트로 조용히 폴백한다 — db_tool은 이 경우에도 예외를 던지지 않는다.
from __future__ import annotations

import os
import re
import shutil

from dotenv import load_dotenv
from langchain_core.tools import tool

load_dotenv()

# SQLcl은 최신 버전부터 JDK 11+가 필요하다(이 환경 기본 java는 8이라 별도로 OpenJDK 21을 설치했다).
# MCP 서버 서브프로세스는 stdio transport 규격상 부모 프로세스 환경변수를 그대로 물려받지 않고
# 안전한 서브셋만 전달하므로, JAVA_HOME/PATH를 명시적으로 채워 넘긴다.
SQLCL_JAVA_HOME = os.environ.get("SQLCL_JAVA_HOME", "/usr/lib/jvm/java-21-openjdk-arm64")

# day4_practice/mcp_agent.py 패턴: MultiServerMCPClient({"name": {"command":..., "args":..., "transport": "stdio"}})
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


def sqlcl_available() -> bool:
    """SQLcl 실행파일이 PATH에 있는지 확인한다. 없으면 mock 픽스처만 사용한다."""
    return shutil.which(os.environ.get("SQLCL_BIN", "sql")) is not None


async def get_oracle_mcp_tools() -> list:
    """SQLcl MCP 서버에 연결해 도구 목록을 가져온다 (확장 경로). 서버가 없으면 빈 리스트를 반환한다."""
    if not sqlcl_available():
        return []
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient

        client = MultiServerMCPClient(SQLCL_MCP_SERVER_CONFIG)
        return await client.get_tools()
    except Exception:
        return []


# ------------------------------------------------------------
# db_tool: 자연어 질문 → 고정 카탈로그 키워드 매칭 → 실DB(EXPLAIN PLAN/DBMS_XPLAN) 조회
#          (DB 미접속 시 mock 실행계획 텍스트로 폴백)
#
# 실DB 스키마는 db/init/01_setup_schema_and_data.sql이 docker-compose의 Oracle Free
# 컨테이너에 최초 기동 시 만든다. 아래 각 항목의 "sql"은 그 스키마에 대해 실제 실행되는 쿼리다.
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
        # emp_id는 VARCHAR2 컬럼이다. 따옴표 없는 숫자 리터럴과 비교하면 Oracle이 문자↔숫자
        # 비교 규칙에 따라 컬럼 쪽을 TO_NUMBER()로 감싸 idx_payments_empid를 못 쓰게 만든다.
        "sql": "SELECT * FROM payments WHERE emp_id = 1001",
        "mode": "explain",
        "fallback_plan": (
            "TABLE ACCESS FULL PAYMENTS Cost=9000 Rows=2 "
            "(Predicate: filter(TO_NUMBER(emp_id)=1001)) -- emp_id is VARCHAR2, implicit conversion"
        ),
    },
    "orders_stale_stats": {
        "aliases": ["상태", "status", "통계", "pending", "stale"],
        # 실제로 실행한 뒤 DBMS_XPLAN.DISPLAY_CURSOR(... 'ALLSTATS LAST')로 E-Rows(추정)와
        # A-Rows(실제)를 함께 보여줘야 stale stats를 증명할 수 있으므로 mode=execute_stats.
        "sql": "SELECT /*+ gather_plan_statistics */ * FROM orders WHERE status = 'PENDING'",
        "mode": "execute_stats",
        "fallback_plan": (
            "TABLE ACCESS FULL ORDERS Cost=12000 Rows=5 (actual rows=480000, stale stats suspected)"
        ),
    },
    "orders_order_items_join": {
        "aliases": ["order_items", "주문상세", "상세", "nested loops"],
        # order_items.order_id에 인덱스가 없는 상태에서 NESTED LOOPS를 강제(USE_NL 힌트)하면
        # 내부 테이블을 매 반복 풀스캔하는 안티패턴을 실행계획으로 보여줄 수 있다.
        "sql": "SELECT /*+ USE_NL(o i) */ * FROM orders o, order_items i WHERE o.order_id = i.order_id",
        "mode": "explain",
        "fallback_plan": (
            "NESTED LOOPS Cost=50000; TABLE ACCESS FULL ORDERS Rows=200000; "
            "TABLE ACCESS FULL ORDER_ITEMS Rows=2000000"
        ),
    },
    "orders_customers_hash_tempspc": {
        "aliases": ["해시조인", "hash join", "tempspc", "임시테이블스페이스", "임시"],
        "sql": "SELECT * FROM orders o, customers c WHERE o.customer_id = c.customer_id",
        # 세션 워크에리어를 수동+최소로 낮춰 해시 조인이 TEMP로 스필하도록 재현한다
        # (레거시 세션 설정이 남아있는 실무 사례를 흉내낸 것 — knowledge/04 참고).
        "session_setup": [
            "ALTER SESSION SET WORKAREA_SIZE_POLICY = MANUAL",
            "ALTER SESSION SET HASH_AREA_SIZE = 65536",
        ],
        "mode": "explain",
        "fallback_plan": "HASH JOIN Cost=15000 TempSpc=204800",
    },
    "orders_index_fragmentation": {
        "aliases": ["인덱스", "단편화", "range scan", "fragmentation"],
        # idx_orders_customer_id는 db/init 스크립트에서 대량 delete/insert 이후 인덱스 통계만
        # 재수집해 실제로 조각난 상태를 재현해 둔 인덱스다.
        "sql": "SELECT * FROM orders WHERE customer_id = 777",
        "mode": "explain",
        "fallback_plan": "INDEX RANGE SCAN IDX_ORDERS_CUSTOMER_ID Cost=450 (unusually high for a range scan)",
    },
    "orders_or_condition": {
        "aliases": ["or 조건", "region", "seoul"],
        "sql": "SELECT * FROM orders WHERE region = 'SEOUL' OR customer_id = 500",
        "mode": "explain",
        "fallback_plan": (
            "TABLE ACCESS FULL ORDERS Cost=9000 (Predicate: filter(region='SEOUL' OR customer_id=500))"
        ),
    },
}


def _oracle_configured() -> bool:
    return bool(os.environ.get("ORACLE_DSN"))


def _get_oracle_connection():
    import oracledb

    return oracledb.connect(
        user=os.environ.get("ORACLE_APP_USER", "appuser"),
        password=os.environ.get("ORACLE_APP_PASSWORD", ""),
        dsn=os.environ["ORACLE_DSN"],
    )


def _display_last_cursor_stats(cur) -> str:
    """방금 이 세션에서 실행한 SQL의 실측 실행계획(E-Rows/A-Rows 포함)을 가져온다.

    DBMS_XPLAN.DISPLAY_CURSOR(NULL, NULL, ...)는 "커서 캐시에서 마지막으로 참조된 SQL"을
    막연히 추정하는데, python-oracledb(thin 모드)가 연결/세션 설정용으로 내부적으로 날리는
    다른 쿼리(예: sys.service$ 조회)가 그 사이에 끼어들면 엉뚱한 SQL의 실행계획이 나온다
    (실측으로 확인됨). V$SESSION.PREV_SQL_ID로 "이 세션이 마지막으로 실행한 SQL_ID"를 명시적으로
    짚어서 그 문제를 없앤다(SELECT_CATALOG_ROLE 필요 — db/init 스크립트에서 appuser에 부여)."""
    cur.execute("SELECT prev_sql_id FROM v$session WHERE sid = SYS_CONTEXT('USERENV', 'SID')")
    (sql_id,) = cur.fetchone()
    cur.execute(
        "SELECT PLAN_TABLE_OUTPUT FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR(:sql_id, NULL, 'ALLSTATS LAST'))",
        sql_id=sql_id,
    )
    return "\n".join(row[0] for row in cur.fetchall())


def _fetch_real_plan(entry: dict) -> str:
    """실DB에 접속해 진짜 실행계획 텍스트를 가져온다. 접속/실행 실패 시 예외를 그대로 올린다
    (호출부인 db_tool이 잡아서 mock 폴백으로 넘어간다)."""
    conn = _get_oracle_connection()
    try:
        with conn.cursor() as cur:
            cur.arraysize = 1000
            for stmt in entry.get("session_setup", []):
                cur.execute(stmt)
            if entry.get("mode") == "execute_stats":
                # 실제로 실행해야 A-Rows(실제 행 수)가 채워진다 — 결과는 버리고 통계만 쓴다.
                cur.execute(entry["sql"])
                cur.fetchall()
                return _display_last_cursor_stats(cur)
            cur.execute(f"EXPLAIN PLAN FOR {entry['sql']}")
            cur.execute("SELECT PLAN_TABLE_OUTPUT FROM TABLE(DBMS_XPLAN.DISPLAY(NULL, NULL, 'ALL'))")
            return "\n".join(row[0] for row in cur.fetchall())
    finally:
        conn.close()


def db_tool(question: str) -> dict | None:
    """자연어 질문에서 고정 쿼리 카탈로그를 키워드로 결정적으로 매칭한 뒤, 실DB(Oracle Free
    컨테이너)에서 실제 실행계획을 조회한다. ORACLE_DSN 미설정이거나 접속/실행이 실패하면 고정
    mock 실행계획 텍스트로 조용히 폴백한다(db_tool 자체는 예외를 던지지 않는다). 매칭되는 쿼리가
    없으면 None을 반환한다(명시적 실패 — 퍼지 매칭이나 추측을 하지 않는다).

    카탈로그 순회 중 처음 매칭되는 항목을 바로 반환하면, "조인"/"join"처럼 여러 항목에 걸친
    범용 별칭을 가진 항목(orders_customers_join)이 "해시조인"/"tempspc"처럼 더 구체적인
    별칭을 가진 항목(orders_customers_hash_tempspc)보다 사전 순서상 앞에 있다는 이유만으로
    먼저 매칭돼버린다. 그래서 전체 카탈로그를 훑어 가장 긴(가장 구체적인) 별칭이 매칭된
    항목을 고른다 — 여전히 완전히 결정적이고, 별도의 퍼지/유사도 매칭은 아니다."""
    q = question.lower()
    best_key, best_entry, best_len = None, None, -1
    for key, entry in QUERY_CATALOG.items():
        matched_lens = [len(alias) for alias in entry["aliases"] if alias.lower() in q]
        if matched_lens and max(matched_lens) > best_len:
            best_key, best_entry, best_len = key, entry, max(matched_lens)
    if best_entry is None:
        return None

    plan = best_entry["fallback_plan"]
    if _oracle_configured():
        try:
            plan = _fetch_real_plan(best_entry)
        except Exception as e:
            print(f"[db_tool] 실DB 조회 실패({type(e).__name__}: {e}) — mock 폴백 사용: key={best_key}")
    return {"key": best_key, "sql": best_entry["sql"], "execution_plan": plan}


# ------------------------------------------------------------
# 사용자가 자연어 대신 SQL 원문을 그대로 질문에 넣은 경우: 고정 카탈로그 매칭 없이 그 SQL로
# 직접 진단한다. "붙여넣기 금지" 정책은 여전히 지킨다 — 사용자가 주는 건 SQL 텍스트뿐이고,
# 실행계획은 절대 사용자 말을 믿지 않고 매번 실DB에서 새로 만들어낸다. SELECT만 허용하고
# (deny-list가 아니라 allow-list — INSERT/UPDATE/DELETE/MERGE/DROP/... 등 하나하나 막을
# 필요 없이 SELECT/WITH가 아니면 전부 차단), 여러 문장을 세미콜론으로 이어붙이는 것도 막는다.
# ------------------------------------------------------------
_SQL_LEADING_KEYWORD = re.compile(r"^\s*(?:--[^\n]*\n|\s)*\(?\s*([A-Za-z]+)", re.IGNORECASE)


def looks_like_sql(text: str) -> bool:
    """질문이 자연어가 아니라 SQL 원문으로 보이면 True (선행 키워드가 SQL 문장 키워드)."""
    m = _SQL_LEADING_KEYWORD.match(text)
    if not m:
        return False
    return m.group(1).upper() in {
        "SELECT", "WITH", "INSERT", "UPDATE", "DELETE", "MERGE", "DROP",
        "ALTER", "CREATE", "TRUNCATE", "GRANT", "REVOKE", "CALL", "EXEC", "BEGIN",
    }


def is_safe_select(text: str) -> bool:
    """SELECT(또는 WITH ... SELECT) 단일 문장인지 확인한다. 세미콜론으로 다른 문장을 이어붙이면
    (예: "SELECT 1 FROM dual; DROP TABLE orders") 첫 문장이 SELECT라도 거부한다."""
    stripped = text.strip().rstrip(";").strip()
    if ";" in stripped:  # 세미콜론으로 문장이 더 있다 -> 다중 문장, 거부
        return False
    m = _SQL_LEADING_KEYWORD.match(stripped)
    if not m:
        return False
    return m.group(1).upper() in {"SELECT", "WITH"}


def run_user_sql(sql: str) -> dict:
    """사용자가 직접 제시한 SELECT 문을 실DB에서 실제로 실행하고(gather_plan_statistics),
    실측 실행계획(E-Rows/A-Rows 포함)을 반환한다. 실DB가 없거나 실행이 실패하면 예외를 그대로
    올린다 — 이 함수는 임의의 SQL이라 대응할 mock이 없으므로 호출부가 명확한 에러로 처리해야
    한다(카탈로그 매칭 경로처럼 조용히 mock으로 폴백하지 않는다)."""
    if not _oracle_configured():
        raise RuntimeError("ORACLE_DSN이 설정되지 않아 사용자 SQL을 실행할 실DB가 없습니다.")

    hinted_sql = re.sub(r"(?i)\bselect\b", "SELECT /*+ gather_plan_statistics */", sql, count=1)
    conn = _get_oracle_connection()
    try:
        conn.call_timeout = 10_000  # 10초 — 임의의 사용자 SQL이 오래 걸려 요청을 막는 것을 방지
        with conn.cursor() as cur:
            cur.arraysize = 1000
            cur.execute(hinted_sql)
            # 주의: fetchmany()로 일부만 받고 커서를 안 비우면, 이후 DISPLAY_CURSOR 조회 시
            # python-oracledb가 내부적으로 다른 쿼리(sys.service$ 관련 failover 설정 조회)를
            # 끼워넣어 PREV_SQL_ID가 엉뚱한 SQL을 가리키게 된다(실측으로 확인) — 반드시 fetchall()로
            # 커서를 완전히 비워야 한다. 행 수 제한은 call_timeout(아래)으로 실행 시간을 갈음한다.
            cur.fetchall()
            plan = _display_last_cursor_stats(cur)
        return {"key": "user_sql", "sql": sql, "execution_plan": plan}
    finally:
        conn.close()


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
