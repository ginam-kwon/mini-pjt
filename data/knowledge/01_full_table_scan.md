# 풀 테이블 스캔 (TABLE ACCESS FULL)

Oracle 실행계획에서 `TABLE ACCESS FULL`이 큰 테이블에 나타나고 `Rows` 예상치가 실제 처리량보다
훨씬 작으면(예: 예상 1건인데 physical reads 수십만) 다음을 의심한다.

- **원인 1 — 사용 가능한 인덱스가 없다.** WHERE 절 컬럼에 인덱스가 없으면 옵티마이저는 풀스캔을 선택한다.
- **원인 2 — 컬럼에 함수를 씌워 인덱스를 못 쓴다.** `TO_CHAR(order_date, 'YYYY-MM-DD') = '2026-09-01'`,
  `UPPER(name) LIKE 'KIM%'` 처럼 컬럼을 함수로 감싸면 일반 B-Tree 인덱스는 사용되지 않는다(비-sargable).
- **원인 3 — 옵티마이저가 풀스캔이 더 싸다고(잘못) 판단.** 통계정보(통계)가 오래되어 카디널리티를
  잘못 추정했거나, 실제로 테이블이 작아 풀스캔이 합리적인 경우도 있다.

## 개선안
1. WHERE 절 컬럼에 인덱스를 만든다 (일반 인덱스 또는 복합 인덱스).
2. 함수로 컬럼을 감싸는 대신 **범위 조건**으로 재작성한다.
   `TO_CHAR(order_date,'YYYY-MM-DD')='2026-09-01'` → `order_date >= DATE '2026-09-01' AND order_date < DATE '2026-09-02'`
3. 함수 사용이 불가피하면 **함수 기반 인덱스(Function-Based Index)** 를 생성한다.
   `CREATE INDEX idx_cust_name_upper ON customers (UPPER(customer_name));`
4. `DBMS_STATS.GATHER_TABLE_STATS`로 통계를 최신화해 옵티마이저 판단을 개선한다.
