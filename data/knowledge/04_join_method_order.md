# 조인 방식과 순서 (NESTED LOOPS / HASH JOIN / MERGE JOIN)

- **NESTED LOOPS**: 외부 테이블 행 수가 적고 내부 테이블에 조인 컬럼 인덱스가 있을 때 효율적이다.
  외부 테이블 예상 행 수가 잘못 추정돼(과소추정) 대량 데이터에 NESTED LOOPS가 선택되면 반복 접근 비용이
  기하급수적으로 커진다. 실행계획에서 NESTED LOOPS 아래 양쪽 다 `TABLE ACCESS FULL`이면 특히 위험 신호다.
- **HASH JOIN**: 대량 데이터를 조인할 때 유리하지만 작은 쪽 테이블이 메모리(PGA)에 다 안 들어가면
  `TempSpc`(임시 테이블스페이스 스필)가 발생해 느려진다. 실행계획의 `TempSpc` 컬럼 값을 확인한다.
- **MERGE JOIN**: 양쪽이 이미 정렬돼 있거나 정렬 비용이 싸면 유리하다.

## 개선안
1. 통계를 최신화해 옵티마이저가 올바른 조인 방식을 고르게 한다(카디널리티 오추정이 근본 원인인 경우 많음).
2. 대량 조인에서 NESTED LOOPS가 잘못 선택됐다면 `/*+ USE_HASH(a b) */` 힌트로 임시 검증 후,
   근본적으로는 통계/인덱스를 고쳐 옵티마이저가 스스로 HASH JOIN을 고르게 한다.
3. HASH JOIN에서 TempSpc 스필이 보이면 `PGA_AGGREGATE_TARGET`/`WORK_AREA_SIZE_POLICY` 조정이나
   조인 순서 재배치(작은 테이블을 build 쪽으로)를 검토한다.
4. 조인 컬럼에 인덱스가 없어 NESTED LOOPS의 내부 테이블 접근이 풀스캔이 되고 있다면 인덱스를 추가한다.
