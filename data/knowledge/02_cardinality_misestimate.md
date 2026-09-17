# 카디널리티 오추정과 통계 노후화

실행계획의 `Rows`(예상 행 수)와 실제 `Statistics` 섹션의 `rows processed`/`consistent gets`가
크게 어긋나면 옵티마이저가 잘못된 통계로 실행 계획을 세웠다는 신호다.

- **원인 1 — 통계 정보가 오래됐다.** 데이터가 많이 바뀌었는데 통계를 갱신하지 않으면 옵티마이저가
  과거 데이터 분포로 판단해 조인 순서/방식을 잘못 고른다.
- **원인 2 — 히스토그램 부재.** 데이터 분포가 균일하지 않은 컬럼(예: 특정 값에 쏠린 상태 코드)에
  히스토그램이 없으면 균등 분포로 가정해 카디널리티를 잘못 추정한다.
- **원인 3 — 바인드 변수 피킹(Bind Peeking) 부작용.** 첫 실행 시 바인드 값 기준으로 계획이 캐시되어
  이후 다른 분포의 값에도 같은(부적합한) 계획이 재사용된다.

## 개선안
1. `EXEC DBMS_STATS.GATHER_TABLE_STATS(ownname=>'APP', tabname=>'ORDERS', cascade=>TRUE, method_opt=>'FOR ALL COLUMNS SIZE AUTO');`
2. 쏠린 분포 컬럼에는 `method_opt=>'FOR COLUMNS SIZE 254 status_code'`로 히스토그램을 명시적으로 생성한다.
3. SQL에 `/*+ CARDINALITY(t 100) */` 같은 힌트는 임시방편이며 근본 해결(통계 최신화)을 우선한다.
4. 바인드 변수 문제가 의심되면 `ADAPTIVE_CURSOR_SHARING`(11g+) 또는 `NO_BIND_AWARE` 힌트, 문제 SQL만
   리터럴로 재작성하는 것도 검토한다.
