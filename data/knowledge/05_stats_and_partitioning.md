# 통계/파티션 관련 성능 저하

- **파티션 프루닝 실패**: 파티션 테이블인데 WHERE 절 조건이 파티션 키에 함수를 씌우거나 파티션 키를
  아예 쓰지 않으면 `Pstart`/`Pstop`이 `KEY`가 아니라 전체 파티션을 훑는 `1..N`으로 나와 파티션 프루닝이
  안 먹힌다. 실행계획의 `Pstart`/`Pstop` 컬럼을 확인한다.
- **Stale 통계로 인한 급격한 계획 변경**: 배치 적재 직후 통계 갱신 전에 쿼리가 실행되면 어제까지 잘 돌던
  쿼리가 갑자기 풀스캔으로 바뀌는 경우가 흔하다.
- **인덱스 단편화(fragmentation)**: 대량 삭제/갱신이 잦은 테이블의 인덱스는 단편화되어
  `INDEX RANGE SCAN` 비용이 서서히 증가할 수 있다 — `ALTER INDEX ... REBUILD`로 재구성한다.

## 개선안
1. 파티션 키를 그대로(가공 없이) WHERE 절에 사용하도록 쿼리를 재작성한다.
2. 배치 적재 파이프라인 마지막 단계에 `DBMS_STATS.GATHER_TABLE_STATS`를 포함시켜 통계 공백을 없앤다.
3. 정기적인 인덱스 재구성 스케줄(`ALTER INDEX ... REBUILD ONLINE`)을 점검한다.
4. `DBMS_STATS.GATHER_TABLE_STATS`의 `stattype=>'auto'`와 자동 통계 수집 작업(`DBMS_AUTO_TASK_ADMIN`)이
   활성화돼 있는지 확인한다.
