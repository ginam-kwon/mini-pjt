-- 01_setup_schema_and_data.sql
-- gvenzl/oracle-free 컨테이너가 최초 기동 시 1회만 SYSDBA로 실행하는 초기화 스크립트.
-- (볼륨이 비어있을 때만 실행됨 — 재실행하려면 `docker compose down -v`로 볼륨을 지우고 다시 올려야 한다)
--
-- 목적: mock V$SQL 픽스처가 흉내내던 7가지 Oracle 성능 이슈 패턴을 실제 테이블/데이터/통계로
-- 재현한다. db_tool(src/tools.py)은 이 스키마에 대해 실제 EXPLAIN PLAN/DBMS_XPLAN을 실행해
-- 진짜 실행계획을 가져온다.

ALTER SESSION SET CONTAINER = FREEPDB1;

-- 애플리케이션 전용 스키마 (고정 개발용 비밀번호 — 로컬 데모 컨테이너 전용, 운영 배포에는 사용 금지)
CREATE USER appuser IDENTIFIED BY "AppUser_2026!"
  DEFAULT TABLESPACE users
  QUOTA UNLIMITED ON users;
GRANT CREATE SESSION, CREATE TABLE, CREATE SEQUENCE, CREATE PROCEDURE TO appuser;
-- DBMS_XPLAN.DISPLAY_CURSOR(...'ALLSTATS LAST')가 내부적으로 V$SESSION/V$SQL 등 동적 성능 뷰를
-- 조회한다. 이게 없으면 예외 없이 그냥 "User has no SELECT privilege on V$SESSION" 텍스트 한 줄을
-- "실행계획"인 것처럼 반환해버려서(db_tool이 잡아낼 수 있는 예외가 아니다) 문제를 늦게 발견했다
-- (orders_stale_stats 카탈로그 항목 + 사용자가 직접 준 SELECT를 실행/분석하는 경로 둘 다 필요).
GRANT SELECT_CATALOG_ROLE TO appuser;

CONNECT appuser/"AppUser_2026!"@FREEPDB1

-- ============================================================
-- 스키마
-- ============================================================
CREATE TABLE customers (
  customer_id   NUMBER PRIMARY KEY,
  customer_name VARCHAR2(100) NOT NULL,
  region        VARCHAR2(20)  NOT NULL
);

CREATE TABLE orders (
  order_id     NUMBER PRIMARY KEY,
  customer_id  NUMBER NOT NULL REFERENCES customers(customer_id),
  order_date   DATE NOT NULL,
  total_amount NUMBER(12,2) NOT NULL,
  status       VARCHAR2(20) NOT NULL,
  region       VARCHAR2(20) NOT NULL
);
-- 조인 컬럼엔 정상적인 인덱스가 있다 — 문제는 함수로 감싼 predicate 쪽임을 보여주기 위함
CREATE INDEX idx_orders_customer_id ON orders(customer_id);
-- region에도 인덱스가 있지만 OR 조건 선택도가 낮아(약 20%) 옵티마이저가 그래도 풀스캔을 택함을
-- 보여주기 위한 용도다 (orders_or_condition 시나리오) — "인덱스가 있는데 왜 안 쓰지?" 케이스
CREATE INDEX idx_orders_region ON orders(region);

CREATE TABLE order_items (
  item_id    NUMBER PRIMARY KEY,
  order_id   NUMBER NOT NULL,
  product_id NUMBER NOT NULL,
  quantity   NUMBER NOT NULL,
  price      NUMBER(10,2) NOT NULL
);
-- 의도적으로 order_id에 인덱스를 만들지 않는다 (조인 방식/순서 문제 재현용, knowledge/04)

CREATE TABLE payments (
  payment_id NUMBER PRIMARY KEY,
  emp_id     VARCHAR2(10) NOT NULL,   -- 사번을 문자열로 저장 (앞자리 0 유지 등 실무 관행)
  amount     NUMBER(12,2) NOT NULL,
  paid_at    DATE NOT NULL
);
CREATE INDEX idx_payments_empid ON payments(emp_id);

-- ============================================================
-- 1단계 적재: 기준 데이터 (이후 이 시점 통계를 "정상" 기준선으로 고정한다)
-- ============================================================
INSERT INTO customers (customer_id, customer_name, region)
SELECT LEVEL,
       CASE WHEN LEVEL = 1 THEN 'Kim Minsu' ELSE 'customer_' || LPAD(LEVEL, 6, '0') END,
       CASE MOD(LEVEL, 5)
         WHEN 0 THEN 'SEOUL' WHEN 1 THEN 'BUSAN' WHEN 2 THEN 'INCHEON'
         WHEN 3 THEN 'DAEGU' ELSE 'DAEJEON' END
FROM DUAL CONNECT BY LEVEL <= 20000;

INSERT INTO orders (order_id, customer_id, order_date, total_amount, status, region)
SELECT LEVEL,
       MOD(LEVEL, 20000) + 1,
       DATE '2026-01-01' + MOD(LEVEL, 250),
       ROUND(DBMS_RANDOM.VALUE(1000, 500000), 2),
       CASE MOD(LEVEL, 10) WHEN 0 THEN 'PENDING' WHEN 1 THEN 'CANCELLED' ELSE 'COMPLETE' END,
       CASE MOD(LEVEL, 5)
         WHEN 0 THEN 'SEOUL' WHEN 1 THEN 'BUSAN' WHEN 2 THEN 'INCHEON'
         WHEN 3 THEN 'DAEGU' ELSE 'DAEJEON' END
FROM DUAL CONNECT BY LEVEL <= 50000;

-- customer_id=1('Kim Minsu')의 주문 하나를 2026-09-01로 고정 (질의 예시와 매칭시키기 위함)
UPDATE orders SET order_date = DATE '2026-09-01'
WHERE order_id = (SELECT MIN(order_id) FROM orders WHERE customer_id = 1);

INSERT INTO order_items (item_id, order_id, product_id, quantity, price)
SELECT LEVEL,
       MOD(LEVEL, 250000) + 1,
       MOD(LEVEL, 500) + 1,
       MOD(LEVEL, 5) + 1,
       ROUND(DBMS_RANDOM.VALUE(1000, 50000), 2)
FROM DUAL CONNECT BY LEVEL <= 300000;

INSERT INTO payments (payment_id, emp_id, amount, paid_at)
SELECT LEVEL,
       TO_CHAR(1000 + MOD(LEVEL, 5000), 'FM0000'),
       ROUND(DBMS_RANDOM.VALUE(10000, 900000), 2),
       DATE '2026-01-01' + MOD(LEVEL, 200)
FROM DUAL CONNECT BY LEVEL <= 20000;

COMMIT;

-- 기준선 통계 고정: 이 시점 이후 ORDERS 테이블 통계는 의도적으로 재수집하지 않는다
-- (아래 2단계에서 대량 유입되는 PENDING 주문에 대해 stale stats 시나리오를 재현하기 위함)
EXEC DBMS_STATS.GATHER_TABLE_STATS(ownname => 'APPUSER', tabname => 'CUSTOMERS', cascade => TRUE);
EXEC DBMS_STATS.GATHER_TABLE_STATS(ownname => 'APPUSER', tabname => 'ORDER_ITEMS', cascade => TRUE);
EXEC DBMS_STATS.GATHER_TABLE_STATS(ownname => 'APPUSER', tabname => 'PAYMENTS', cascade => TRUE);
EXEC DBMS_STATS.GATHER_TABLE_STATS(ownname => 'APPUSER', tabname => 'ORDERS', cascade => TRUE);

-- ============================================================
-- 2단계: 대량 PENDING 주문 배치 유입 (orders_stale_stats 시나리오)
-- 통계 재수집 없이 실제 행 수만 크게 늘려 옵티마이저 추정치(E-Rows)와
-- 실제 실행 결과(A-Rows)가 크게 어긋나게 만든다.
-- ============================================================
INSERT INTO orders (order_id, customer_id, order_date, total_amount, status, region)
SELECT 50000 + LEVEL,
       MOD(LEVEL, 20000) + 1,
       DATE '2026-08-01' + MOD(LEVEL, 30),
       ROUND(DBMS_RANDOM.VALUE(1000, 500000), 2),
       'PENDING',
       CASE MOD(LEVEL, 5)
         WHEN 0 THEN 'SEOUL' WHEN 1 THEN 'BUSAN' WHEN 2 THEN 'INCHEON'
         WHEN 3 THEN 'DAEGU' ELSE 'DAEJEON' END
FROM DUAL CONNECT BY LEVEL <= 200000;
COMMIT;
-- <- 여기서 의도적으로 DBMS_STATS를 다시 돌리지 않는다 (stale stats 유지)

-- ============================================================
-- 3단계: 인덱스 단편화 재현 (orders_index_fragmentation 시나리오)
-- idx_orders_customer_id 대상으로 대량 delete/insert를 반복해 leaf block을 흩어놓은 뒤,
-- "인덱스 통계만" 재수집한다 (테이블 통계는 여전히 stale 상태로 남겨둔다).
-- ============================================================
DELETE FROM orders WHERE MOD(order_id, 7) = 0;
COMMIT;

INSERT INTO orders (order_id, customer_id, order_date, total_amount, status, region)
SELECT 300000 + LEVEL,
       MOD(LEVEL, 20000) + 1,
       DATE '2026-09-01' + MOD(LEVEL, 10),
       ROUND(DBMS_RANDOM.VALUE(1000, 500000), 2),
       CASE MOD(LEVEL, 10) WHEN 0 THEN 'PENDING' ELSE 'COMPLETE' END,
       CASE MOD(LEVEL, 5)
         WHEN 0 THEN 'SEOUL' WHEN 1 THEN 'BUSAN' WHEN 2 THEN 'INCHEON'
         WHEN 3 THEN 'DAEGU' ELSE 'DAEJEON' END
FROM DUAL CONNECT BY LEVEL <= 40000;
COMMIT;

EXEC DBMS_STATS.GATHER_INDEX_STATS(ownname => 'APPUSER', indname => 'IDX_ORDERS_CUSTOMER_ID');

EXIT;
