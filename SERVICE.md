# SERVICE · Oracle SQL 실행계획 성능진단 Agent

## 1. 사용자·문제·가치

- 누구를 위한 서비스인가: Oracle DB를 다루는 DBA 및 백엔드 개발자
- 어떤 문제를 푸는가: SQL 성능 저하가 발생해도 실행계획을 직접 읽고 원인(풀스캔, 인덱스 미사용, 조인 문제, 통계 stale 등)을
  판별하는 데 시간이 걸리고 매번 DBA에게 물어봐야 한다
- 지금은 어떻게 해결하나: 개발자가 직접 EXPLAIN PLAN을 실행해 결과를 눈으로 해석하거나 경험 있는 DBA/선임에게 물어본다
- 이걸로 뭐가 좋아지나: 자연어로 질문 하나만 던지면 대상 쿼리의 실행계획을 자동으로 조회해 원인과 개선안을 근거와 함께
  즉시 받을 수 있다(반복적인 "이 쿼리 왜 느려요?" 문의를 대체)

## 2. 서비스 확장 관점

- 사내 기여: DBA/선임 개발자에게 반복적으로 몰리는 쿼리 튜닝 문의를 줄인다
- 규모: Oracle DB를 운영하는 팀 전체가 챗봇으로 즉시 질의 가능. 일간 자동 스캔(배치) 확장은 시간이 허락하는 대로
  선택적으로 추가한다(README.md 트라이앤에러 회고에 진행 상황 기록)
- 대체제와 차별점: Oracle SQL Tuning Advisor 등 공식 도구는 별도 라이선스(Diagnostic Pack)가 필요하지만
  본 서비스는 라이선스 비용 없이 동작하고, 룰+LLM 하이브리드 진단과 튜닝 지식베이스(RAG)를 결합해 일반 도구가
  주지 못하는 맥락 있는 설명을 자연어로 제공한다

## 3. 사용 예상 도구·데이터

- **도구**: `db_tool`(질문에서 대상 쿼리를 키워드 매칭으로 결정적으로 찾은 뒤, 실제 Oracle DB에
  `EXPLAIN PLAN`/`DBMS_XPLAN`을 실행해 진짜 실행계획을 조회. DB 미접속 시에만 고정 mock 텍스트로 폴백),
  `search_tuning_knowledge`(하이브리드 RAG로 튜닝 지식베이스 검색), `gather_stats`/`create_index`(위험 쓰기
  도구, 실DB 연동 이후에도 항상 mock — advisory 정책상 자동 실행하지 않으며 HITL 승인 게이트는 그대로 유지)
- **서브 에이전트**: Supervisor가 explain_agent(실행계획 분석)·knowledge_agent(지식베이스 검색)에 위임하고
  최종 종합은 Supervisor 본인이 구조화 출력으로 작성한다
- **데이터**: Docker(`docker-compose.yml`, gvenzl/oracle-free)로 띄우는 실제 Oracle Database Free 인스턴스에
  대표 성능 문제 패턴 7종(풀스캔 유발 함수형 predicate, 암묵적 형변환, stale stats, 조인 방식 문제, 해시조인
  TEMP 스필, 인덱스 단편화, 비선택적 OR 조건)을 실데이터로 재현해 적재한다(`db/init/01_setup_schema_and_data.sql`).
  Oracle 튜닝 이론 지식베이스 문서 5종(`src/knowledge/*.md`, 풀스캔/카디널리티/비-sargable/조인/통계 원인·개선 패턴)
- **연동**: Oracle 공식 MCP(SQLcl 내장 MCP 서버 `sql -mcp`)를 클라이언트로 소비하는 확장 경로를 실제로
  설치·구성했다(OpenJDK 21 + SQLcl, `src/tools.py`의 `get_oracle_mcp_tools()`가 `connect`/`sql_run`/
  `schema_information` 등 9개 MCP 도구를 정상적으로 가져온다 — explain_agent가 자동으로 사용 가능).
  다만 진단 파이프라인의 기본 경로는 여전히 결정적 키워드 매칭 + 직접 DB 접속(`db_tool`)이며, MCP는
  탐색적 확장 경로로 유지한다(진단 로직에 LLM 기반 비결정성을 들이지 않기 위함). 로컬 FileTracer로
  트레이싱한다.

## 4. 서비스 정책 (가드레일 요약)

- 개선안은 조언(advisory)만 제공하며 인덱스 생성이나 통계 재수집을 자동으로 실행하지 않는다 — 모든 위험 작업은
  사람 승인(`interrupt()` 기반 HITL) 게이트를 통과해야 한다(fail-closed)
- Oracle 외 DB(PostgreSQL, MySQL 등)는 지원 범위가 아니다
- 외부 LLM에 보내기 전 SQL 리터럴에 섞인 개인정보/자격증명(사번, 전화번호, 이메일, AWS 키, 접속 비밀번호)을
  레드액션(마스킹)하며, 프롬프트 인젝션 방어 가드레일이 입력/도구 출력/RAG 검색 결과 지점에 적용된다
- 사용자가 SQL/실행계획 텍스트를 직접 붙여넣는 입력 경로는 없다 — 진단은 반드시 `db_tool`을 통한 조회로만
  이루어진다(붙여넣기 우회 금지)

## 5. 성공 기준

- `evaluation/test_queries.csv`(20문항 내외, positive/negative/edge/guardrail 4개 카테고리) 기준
  규칙기반 통과율이 1차(round1) 70% 이상, 2차(round2, 개선 후) 90% 이상
- positive/edge 케이스에 대해 RAGAS 4지표(faithfulness/answer_relevancy ≥ 0.7, context_precision/context_recall
  ≥ 0.6)를 충족
- 개선안 응답에 자동 실행/적용으로 오인될 수 있는 문구가 0건이며, 위험 도구는 실제로 HITL 승인 없이는 실행되지 않음
- 12개 구현 역량(LCEL 구조화 출력, ReAct, RAG, 다중 도구, MCP 연동, 가드레일, HITL, 미들웨어, Multi-Agent
  Supervisor, Plan-Execute+장기메모리, Observability, RAGAS/LLM-as-Judge 평가)이 각각 명명된
  모듈로 구현·검증됨(README.md 매핑표 참고)
