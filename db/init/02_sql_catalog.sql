-- 02_sql_catalog.sql
-- Docker 초기화 2단계: 운영 SQL 후보 카탈로그
--
-- 목적: candidate_search_agent가 자연어 질의로 탐색할 수 있는 운영 SQL 후보를 제공한다.
-- 각 후보는 서로 다른 성능 이슈 시나리오(풀스캔·함수 기반 조건·형변환·통계·조인 등)를 대표한다.
-- 01_setup_schema_and_data.sql이 완료된 뒤 동일한 PDB 세션에서 실행된다.

ALTER SESSION SET CONTAINER = FREEPDB1;
CONNECT appuser/"AppUser_2026!"@FREEPDB1

-- ============================================================
-- 운영 SQL 카탈로그 테이블
-- ============================================================
CREATE TABLE ops_sql_catalog (
  sql_id           VARCHAR2(60)   PRIMARY KEY,
  sql_text         CLOB           NOT NULL,
  scenario_tag     VARCHAR2(40)   NOT NULL,
  description      VARCHAR2(400)  NOT NULL,
  exec_count       NUMBER         DEFAULT 0,
  avg_elapsed_secs NUMBER(10, 3)  DEFAULT 0,
  last_seen_at     DATE           DEFAULT SYSDATE
);

COMMENT ON TABLE  ops_sql_catalog              IS '운영 SQL 후보 카탈로그 — candidate_search_agent RAG 대상';
COMMENT ON COLUMN ops_sql_catalog.sql_id       IS '후보 식별자 (자연어 마스킹 레이블로도 사용)';
COMMENT ON COLUMN ops_sql_catalog.scenario_tag IS '성능 이슈 유형: full_scan | function_based | type_cast | stale_stats | join_nl | join_hash | or_condition | index_frag';

-- ============================================================
-- 운영 SQL 후보 데이터
-- ============================================================

-- 1. 주문-고객 조인 + 함수 기반 조건 (풀스캔 + function-based predicate)
INSERT INTO ops_sql_catalog (sql_id, sql_text, scenario_tag, description, exec_count, avg_elapsed_secs, last_seen_at)
VALUES (
  'orders_customers_join',
  'SELECT o.order_id, c.customer_name, o.order_date, o.total_amount
FROM orders o, customers c
WHERE TO_CHAR(o.order_date, ''YYYY-MM-DD'') = :p_date
  AND o.customer_id = c.customer_id
  AND UPPER(c.customer_name) LIKE :p_name_prefix',
  'function_based',
  '고객·주문 조인 조회. TO_CHAR(order_date)와 UPPER(customer_name) 함수 기반 조건으로 인덱스를 타지 못해 ORDERS·CUSTOMERS 모두 풀스캔 발생. 가장 빈번한 성능 민원 쿼리.',
  4820,
  12.847,
  DATE '2026-09-10'
);

-- 2. 결제 emp_id 암묵적 형변환 (type_cast)
INSERT INTO ops_sql_catalog (sql_id, sql_text, scenario_tag, description, exec_count, avg_elapsed_secs, last_seen_at)
VALUES (
  'payments_implicit_cast',
  'SELECT p.payment_id, p.emp_id, p.amount, p.paid_at
FROM payments p
WHERE p.emp_id = :p_emp_id_num',
  'type_cast',
  '결제 테이블 사번 조회. emp_id 컬럼이 VARCHAR2인데 숫자 바인드 변수를 사용해 TO_NUMBER(emp_id) 암묵 형변환이 발생, idx_payments_empid 인덱스 무효화 → PAYMENTS 풀스캔.',
  3210,
  8.302,
  DATE '2026-09-12'
);

-- 3. PENDING 상태 주문 조회 — stale statistics
INSERT INTO ops_sql_catalog (sql_id, sql_text, scenario_tag, description, exec_count, avg_elapsed_secs, last_seen_at)
VALUES (
  'orders_stale_stats',
  'SELECT o.order_id, o.customer_id, o.total_amount, o.status
FROM orders o
WHERE o.status = :p_status',
  'stale_stats',
  '주문 상태별 조회. 배치 유입 이후 통계 재수집이 안 돼 E-Rows(옵티마이저 추정)와 A-Rows(실제 행 수)가 크게 어긋남. PENDING 상태 200,000건을 5건으로 오추정해 Nested Loops 선택.',
  9870,
  45.120,
  DATE '2026-09-11'
);

-- 4. 주문-주문상세 조인 + 인덱스 누락 (join_nl)
INSERT INTO ops_sql_catalog (sql_id, sql_text, scenario_tag, description, exec_count, avg_elapsed_secs, last_seen_at)
VALUES (
  'orders_order_items_join',
  'SELECT o.order_id, o.total_amount, i.product_id, i.quantity, i.price
FROM orders o
JOIN order_items i ON o.order_id = i.order_id
WHERE o.order_date >= :p_from_date
  AND o.order_date < :p_to_date',
  'join_nl',
  'orders ↔ order_items 대용량 조인. order_items.order_id 인덱스 미생성으로 ORDER_ITEMS 풀스캔 + NESTED LOOPS, 일자 범위가 넓을수록 exponential 비용 증가.',
  1650,
  22.650,
  DATE '2026-09-09'
);

-- 5. 대용량 해시 조인 + 임시 테이블스페이스 스필 (join_hash)
INSERT INTO ops_sql_catalog (sql_id, sql_text, scenario_tag, description, exec_count, avg_elapsed_secs, last_seen_at)
VALUES (
  'orders_customers_hash_tempspc',
  'SELECT c.customer_name, c.region, SUM(o.total_amount) AS total_sales
FROM orders o
JOIN customers c ON o.customer_id = c.customer_id
GROUP BY c.customer_name, c.region',
  'join_hash',
  '전체 고객 매출 집계. HASH JOIN Build 단계에서 PGA 한도 초과로 TempSpc(임시 테이블스페이스) 스필 발생. 대량 디스크 I/O로 응답시간 급등.',
  410,
  35.900,
  DATE '2026-09-08'
);

-- 6. 인덱스 단편화로 비정상 높은 INDEX RANGE SCAN 비용 (index_frag)
INSERT INTO ops_sql_catalog (sql_id, sql_text, scenario_tag, description, exec_count, avg_elapsed_secs, last_seen_at)
VALUES (
  'orders_index_fragmentation',
  'SELECT o.order_id, o.order_date, o.status, o.total_amount
FROM orders o
WHERE o.customer_id = :p_customer_id
ORDER BY o.order_date DESC',
  'index_frag',
  '특정 고객 주문 이력 조회. idx_orders_customer_id 인덱스 단편화(대량 DELETE/INSERT 반복)로 leaf block이 흩어져 INDEX RANGE SCAN 비용이 비정상적으로 높음.',
  6750,
  18.430,
  DATE '2026-09-13'
);

-- 7. OR 조건으로 인한 인덱스 미사용 풀스캔 (or_condition)
INSERT INTO ops_sql_catalog (sql_id, sql_text, scenario_tag, description, exec_count, avg_elapsed_secs, last_seen_at)
VALUES (
  'orders_or_condition',
  'SELECT o.order_id, o.customer_id, o.region, o.status, o.total_amount
FROM orders o
WHERE o.region = :p_region
   OR o.customer_id = :p_customer_id',
  'or_condition',
  '지역 또는 고객 ID 조건 주문 조회. OR 조건으로 idx_orders_region이 있음에도 옵티마이저가 ORDERS 풀스캔 선택. UNION ALL로 재작성하면 각 분기가 독립 인덱스를 사용할 수 있음.',
  2980,
  14.210,
  DATE '2026-09-07'
);

-- 8. 고객 지역별 주문 통계 — 집계 쿼리 full_scan (full_scan)
INSERT INTO ops_sql_catalog (sql_id, sql_text, scenario_tag, description, exec_count, avg_elapsed_secs, last_seen_at)
VALUES (
  'orders_region_summary',
  'SELECT o.region, COUNT(*) AS order_cnt, SUM(o.total_amount) AS total_amt, AVG(o.total_amount) AS avg_amt
FROM orders o
WHERE o.order_date >= :p_from_date
GROUP BY o.region
ORDER BY total_amt DESC',
  'full_scan',
  '지역별 주문 집계 리포트. 전체 ORDERS 풀스캔 후 GROUP BY. 날짜 범위 파라미터가 있지만 order_date 단독 인덱스가 없어 항상 풀스캔. 파티셔닝 또는 함수 기반 인덱스 검토 필요.',
  820,
  28.770,
  DATE '2026-09-06'
);

COMMIT;

EXIT;
