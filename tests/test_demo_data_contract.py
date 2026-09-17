"""Docker 초기화 데이터 검증.

AC: Docker 초기화 데이터는 고객·주문·결제 등 업무 스키마와 서로 구분되는 운영 SQL 후보,
    풀스캔·함수 기반 조건·형변환·통계·조인 등 성능 시나리오를 제공한다.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
INIT_DIR = PROJECT_ROOT / "db" / "init"
SCHEMA_SQL = INIT_DIR / "01_setup_schema_and_data.sql"
CATALOG_SQL = INIT_DIR / "02_sql_catalog.sql"

# 필수 성능 시나리오 태그 — ops_sql_catalog에 최소 1건씩 있어야 한다
REQUIRED_SCENARIO_TAGS = [
    "full_scan",
    "function_based",
    "type_cast",
    "stale_stats",
    "join_nl",
    "join_hash",
    "or_condition",
    "index_frag",
]

# 필수 업무 스키마 테이블
REQUIRED_BUSINESS_TABLES = [
    "customers",
    "orders",
    "order_items",
    "payments",
]


# ---------------------------------------------------------------------------
# 초기화 파일 존재 여부
# ---------------------------------------------------------------------------

class TestInitFilesExist:
    """Docker 초기화 SQL 파일이 존재하는지 확인한다."""

    def test_schema_sql_exists(self):
        """01_setup_schema_and_data.sql 파일이 있어야 한다."""
        assert SCHEMA_SQL.exists(), (
            f"{SCHEMA_SQL} 파일이 없습니다. Oracle 스키마·데이터 초기화 파일을 확인하세요."
        )

    def test_catalog_sql_exists(self):
        """02_sql_catalog.sql 파일이 있어야 한다."""
        assert CATALOG_SQL.exists(), (
            f"{CATALOG_SQL} 파일이 없습니다. 운영 SQL 카탈로그 초기화 파일을 생성하세요."
        )

    def test_init_dir_ordering(self):
        """init 디렉터리의 SQL 파일이 번호 순으로 정렬되어 실행 순서가 보장되어야 한다."""
        sql_files = sorted(INIT_DIR.glob("*.sql"))
        assert len(sql_files) >= 2, (
            f"init 디렉터리에 SQL 파일이 2개 이상 있어야 합니다. 현재: {sql_files}"
        )
        names = [f.name for f in sql_files]
        assert names[0].startswith("01_"), f"첫 번째 파일이 01_로 시작해야 합니다: {names[0]}"
        assert names[1].startswith("02_"), f"두 번째 파일이 02_로 시작해야 합니다: {names[1]}"


# ---------------------------------------------------------------------------
# 업무 스키마 테이블 검증
# ---------------------------------------------------------------------------

class TestBusinessSchema:
    """01_setup_schema_and_data.sql에 업무 스키마 테이블이 정의되어 있는지 확인한다."""

    def _schema_source(self) -> str:
        return SCHEMA_SQL.read_text(encoding="utf-8")

    @pytest.mark.parametrize("table", REQUIRED_BUSINESS_TABLES)
    def test_create_table_present(self, table: str):
        """CREATE TABLE <table> 구문이 스키마 파일에 있어야 한다."""
        source = self._schema_source().lower()
        assert f"create table {table}" in source, (
            f"CREATE TABLE {table}이 {SCHEMA_SQL.name}에 없습니다. "
            "업무 스키마 테이블을 추가하세요."
        )

    def test_customers_has_primary_key(self):
        """customers 테이블에 PRIMARY KEY 컬럼이 있어야 한다."""
        source = self._schema_source().lower()
        # customer_id NUMBER PRIMARY KEY 형태 확인
        assert "customer_id" in source and "primary key" in source, (
            "customers 테이블에 PRIMARY KEY 정의가 없습니다."
        )

    def test_orders_references_customers(self):
        """orders 테이블이 customers.customer_id를 REFERENCES하는 FK를 가져야 한다."""
        source = self._schema_source().lower()
        assert "references customers" in source, (
            "orders 테이블이 customers를 참조하는 FK가 없습니다."
        )

    def test_payments_has_emp_id_varchar(self):
        """payments 테이블의 emp_id가 VARCHAR2로 정의되어 type_cast 시나리오를 지원해야 한다."""
        source = self._schema_source()
        assert re.search(r"emp_id\s+VARCHAR2", source, re.IGNORECASE), (
            "payments.emp_id가 VARCHAR2로 정의되어 있지 않습니다. "
            "type_cast 시나리오(암묵적 형변환)를 위해 VARCHAR2 타입이 필요합니다."
        )

    def test_orders_data_volumes_are_large(self):
        """orders에 50000건 이상 데이터가 삽입되어 성능 시나리오 재현에 충분해야 한다."""
        source = self._schema_source()
        # CONNECT BY LEVEL <= N 에서 N 추출
        matches = re.findall(r"connect by level\s*<=\s*(\d+)", source, re.IGNORECASE)
        numbers = [int(m) for m in matches]
        assert any(n >= 50000 for n in numbers), (
            f"orders 대량 데이터 삽입(>=50000)이 없습니다. 현재 LEVEL 상한: {numbers}"
        )

    def test_stale_stats_scenario_present(self):
        """통계 부재(stale stats) 시나리오를 위한 2단계 대량 INSERT가 있어야 한다."""
        source = self._schema_source().lower()
        # 2단계 PENDING 대량 유입 확인
        assert "'pending'" in source and "200000" in source, (
            "stale_stats 시나리오를 위한 PENDING 대량 유입(200000건) INSERT가 없습니다."
        )


# ---------------------------------------------------------------------------
# 운영 SQL 카탈로그 검증
# ---------------------------------------------------------------------------

class TestOperationalSqlCatalog:
    """02_sql_catalog.sql이 올바른 카탈로그 구조와 시나리오를 포함하는지 확인한다."""

    def _catalog_source(self) -> str:
        return CATALOG_SQL.read_text(encoding="utf-8")

    def test_ops_sql_catalog_table_created(self):
        """CREATE TABLE ops_sql_catalog 구문이 있어야 한다."""
        source = self._catalog_source().lower()
        assert "create table ops_sql_catalog" in source, (
            "02_sql_catalog.sql에 CREATE TABLE ops_sql_catalog이 없습니다."
        )

    def test_table_has_sql_id_column(self):
        """ops_sql_catalog에 sql_id PRIMARY KEY 컬럼이 있어야 한다."""
        source = self._catalog_source().lower()
        assert "sql_id" in source and "primary key" in source, (
            "ops_sql_catalog 테이블에 sql_id PRIMARY KEY가 없습니다."
        )

    def test_table_has_scenario_tag_column(self):
        """ops_sql_catalog에 scenario_tag 컬럼이 있어야 한다."""
        source = self._catalog_source().lower()
        assert "scenario_tag" in source, (
            "ops_sql_catalog에 scenario_tag 컬럼이 없습니다. "
            "성능 시나리오 유형을 분류하는 컬럼이 필요합니다."
        )

    def test_table_has_sql_text_column(self):
        """ops_sql_catalog에 sql_text 컬럼이 있어야 한다."""
        source = self._catalog_source().lower()
        assert "sql_text" in source, (
            "ops_sql_catalog에 sql_text 컬럼이 없습니다."
        )

    def test_table_has_performance_metrics(self):
        """ops_sql_catalog에 exec_count와 avg_elapsed_secs 성능 지표 컬럼이 있어야 한다."""
        source = self._catalog_source().lower()
        assert "exec_count" in source, (
            "ops_sql_catalog에 exec_count 컬럼이 없습니다."
        )
        assert "avg_elapsed_secs" in source, (
            "ops_sql_catalog에 avg_elapsed_secs 컬럼이 없습니다."
        )

    @pytest.mark.parametrize("tag", REQUIRED_SCENARIO_TAGS)
    def test_scenario_tag_present(self, tag: str):
        """각 필수 성능 시나리오 태그가 카탈로그 데이터에 1건 이상 있어야 한다."""
        source = self._catalog_source()
        assert f"'{tag}'" in source, (
            f"scenario_tag='{tag}'인 운영 SQL 후보가 02_sql_catalog.sql에 없습니다. "
            f"해당 성능 이슈 시나리오를 대표하는 SQL 후보를 추가하세요."
        )

    def test_minimum_candidate_count(self):
        """운영 SQL 후보가 최소 7개 이상 INSERT되어야 한다 (서로 구분되는 시나리오)."""
        source = self._catalog_source()
        inserts = re.findall(r"INSERT INTO ops_sql_catalog", source, re.IGNORECASE)
        assert len(inserts) >= 7, (
            f"ops_sql_catalog INSERT가 {len(inserts)}건뿐입니다. 최소 7건이 필요합니다."
        )

    def test_candidates_have_distinct_sql_ids(self):
        """각 SQL 후보의 sql_id가 서로 달라야 한다."""
        source = self._catalog_source()
        # sql_id 값 추출: 'orders_customers_join' 형태
        sql_ids = re.findall(r"'(orders_\w+|payments_\w+)'", source)
        assert len(sql_ids) == len(set(sql_ids)), (
            f"ops_sql_catalog에 중복 sql_id가 있습니다: {[x for x in sql_ids if sql_ids.count(x) > 1]}"
        )

    def test_candidates_use_bind_variables(self):
        """운영 SQL 후보가 리터럴 대신 바인드 변수(:p_xxx)를 사용해야 한다 (마스킹·재사용 표준)."""
        source = self._catalog_source()
        bind_vars = re.findall(r":p_\w+", source)
        assert len(bind_vars) >= 5, (
            f"바인드 변수(:p_xxx)가 {len(bind_vars)}개뿐입니다. "
            "운영 SQL 후보는 바인드 변수 형태로 저장되어야 합니다."
        )

    def test_commit_present(self):
        """INSERT 후 COMMIT이 있어야 한다."""
        source = self._catalog_source()
        assert re.search(r"\bCOMMIT\b", source, re.IGNORECASE), (
            "02_sql_catalog.sql에 COMMIT이 없습니다."
        )


# ---------------------------------------------------------------------------
# 시나리오 커버리지 교차 검증
# ---------------------------------------------------------------------------

class TestScenarioCoverage:
    """업무 스키마 + 운영 SQL 카탈로그 조합으로 필수 성능 시나리오가 재현 가능한지 확인한다."""

    def test_function_based_scenario_uses_business_tables(self):
        """function_based 시나리오 SQL이 orders 또는 customers 테이블을 참조해야 한다."""
        source = CATALOG_SQL.read_text(encoding="utf-8")
        # function_based INSERT 블록 추출
        fb_block = re.search(
            r"'function_based'.*?(?=INSERT INTO|EXIT;|$)", source, re.DOTALL
        )
        assert fb_block, "function_based 시나리오 INSERT를 찾을 수 없습니다."
        block_text = fb_block.group(0).lower()
        assert "orders" in block_text or "customers" in block_text, (
            "function_based 시나리오가 orders/customers 업무 테이블을 참조하지 않습니다."
        )

    def test_type_cast_scenario_uses_payments(self):
        """type_cast 시나리오 SQL이 payments 테이블을 참조해야 한다."""
        source = CATALOG_SQL.read_text(encoding="utf-8")
        tc_block = re.search(
            r"'type_cast'.*?(?=INSERT INTO|EXIT;|$)", source, re.DOTALL
        )
        assert tc_block, "type_cast 시나리오 INSERT를 찾을 수 없습니다."
        assert "payments" in tc_block.group(0).lower(), (
            "type_cast 시나리오가 payments 업무 테이블을 참조하지 않습니다."
        )

    def test_join_scenarios_reference_multiple_tables(self):
        """join_nl 또는 join_hash 시나리오 INSERT 블록이 2개 이상의 업무 테이블을 참조해야 한다."""
        source = CATALOG_SQL.read_text(encoding="utf-8")
        # INSERT 블록을 단위로 분리해 join 시나리오가 있는 블록 전체를 검사한다
        insert_blocks = re.split(r"INSERT INTO ops_sql_catalog", source, flags=re.IGNORECASE)
        join_blocks = [
            b for b in insert_blocks
            if re.search(r"'join_(?:nl|hash)'", b)
        ]
        assert join_blocks, "join_nl 또는 join_hash 시나리오를 찾을 수 없습니다."
        for block in join_blocks:
            tables_referenced = re.findall(
                r"\b(orders|customers|order_items|payments)\b", block.lower()
            )
            assert len(set(tables_referenced)) >= 2, (
                f"조인 시나리오 INSERT 블록이 단일 테이블만 참조합니다: {tables_referenced}"
            )

    def test_all_candidates_have_description(self):
        """모든 SQL 후보에 비어 있지 않은 description이 있어야 한다."""
        source = CATALOG_SQL.read_text(encoding="utf-8")
        # INSERT 블록을 단위로 분리해 각 블록에서 고한국어/영문 description 유무 확인
        insert_blocks = re.split(r"INSERT INTO ops_sql_catalog", source, flags=re.IGNORECASE)
        # 첫 번째 원소는 CREATE TABLE 부분 — 제외
        data_blocks = [b for b in insert_blocks[1:] if "VALUES" in b.upper()]
        assert len(data_blocks) >= 7, (
            f"데이터 INSERT 블록이 {len(data_blocks)}개뿐입니다. 최소 7건 필요."
        )
        for block in data_blocks:
            # VALUES 절 내 작은따옴표 문자열 목록 — 4번째가 description
            str_vals = re.findall(r"'((?:[^']|'')*)'", block)
            # sql_id(1), sql_text(2), scenario_tag(3), description(4) — 인덱스 3
            assert len(str_vals) >= 4, (
                f"INSERT 블록에서 문자열 값이 충분하지 않습니다: {str_vals[:4]}"
            )
            desc = str_vals[3].replace("''", "'").strip()
            assert len(desc) > 10, (
                f"description이 너무 짧습니다: '{desc[:30]}'"
            )
