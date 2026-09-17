# SERVICE · Oracle SQL Copilot — SQL 생성·검증·운영 성능 진단 Agent

## 0. 세 흐름 (Service Flows)

이 서비스는 승인책임자(`X-Approver-Token`)만 사용할 수 있는 세 가지 흐름을 제공한다:

| 흐름 | 설명 |
|---|---|
| **SQL 생성** | 비즈니스 요구사항을 입력하면 `query_planner_agent`가 Plan-Execute 패턴으로 SELECT 초안을 생성하고 SQLcl MCP로 스키마를 확인한다 |
| **SQL 검증** | 사용자가 입력한 SELECT를 `sql_validator_agent`가 accept/revise/reject로 판정하고 실행계획 위험도를 평가한다 |
| **운영 성능 진단** | 자연어 운영 요청에 대해 `candidate_search_agent`가 마스킹된 SQL 후보 목록을 반환하고, 사용자가 선택한 후보를 `explain_agent`·`knowledge_agent`가 RAG 근거와 함께 진단한다 |

### 승인책임자 (Approver) 역할

- 세 흐름 접근: 유효한 `X-Approver-Token` 헤더 필수. 누락 → 401, 불일치 → 403.
- 승인된 요청: 사용자 식별자, 시각, 작업, 결과를 감사 기록으로 남긴다. 원문 토큰은 저장하지 않는다.
- 권한 검증 실패: SQLcl MCP 호출과 SQLite 쓰기를 전혀 수행하지 않는다.

### SQLcl MCP 경계

Oracle 대상 DB의 모든 상호작용(스키마 조회, SQL 후보 탐색, 실행계획 조회, 제한 실행)은 **SQLcl MCP** 단일 경로를 사용한다. 직접 DB 드라이버 접속 경로는 사용하지 않는다.

### 마스킹 정책

이메일·전화번호·사번·비밀번호·SQL 리터럴 등 민감정보는 UI·API·LLM·trace·로그·SQLite 저장 전에 마스킹한다. 운영 SQL 후보는 구조를 보여주되 식별값과 리터럴을 마스킹한다.

### API 요청 유형과 응답 계약

기존 제출 계약인 `POST /query {"question": str}`는 유지하고, 세 흐름의 전용 진입점을 추가했다.
모든 요청 유형은 동일한 응답 계약 `{"status", "answer", "contexts", "trace"}`를 따른다.

| 요청 유형 | 경로 | 요청 본문 |
|---|---|---|
| 진단 질문 / SELECT 직접 입력 | `POST /query` | `{"question": str}` |
| SQL 생성 | `POST /generate` | `{"requirement": str}` |
| SQL 검증 | `POST /validate` | `{"sql": str}` |
| 후보 탐색 | `POST /candidates` | `{"question": str}` |
| 후보 선택 후 진단 | `POST /candidates/diagnose` | `{"sql_id": str}` |

## 1. 사용자·문제·가치

- 누구를 위한 서비스인가: Oracle DB를 다루는 DBA 및 백엔드 개발자(승인책임자 역할 보유)
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

- **도구**: SQLcl MCP 도구(스키마 조회, 운영 SQL 후보 탐색, `EXPLAIN PLAN`/`DBMS_XPLAN` 실행계획 조회,
  위험 평가를 통과한 SELECT의 제한 실행) — Oracle 대상 DB 접근은 이 단일 경계만 사용하고 직접 DB 드라이버
  접속 경로는 사용하지 않는다. 그 밖에 `search_tuning_knowledge`(하이브리드 RAG로 튜닝 지식베이스 검색),
  `gather_stats`/`create_index`(위험 쓰기 도구 — advisory 정책상 자동 실행하지 않으며 HITL 승인 게이트 유지)
- **서브 에이전트**: Supervisor가 explain_agent(실행계획 분석)·knowledge_agent(지식베이스 검색)에 위임하고
  최종 종합은 Supervisor 본인이 구조화 출력으로 작성한다
- **데이터**: Docker(`docker-compose.yml`, gvenzl/oracle-free)로 띄우는 실제 Oracle Database Free 인스턴스에
  대표 성능 문제 패턴 7종(풀스캔 유발 함수형 predicate, 암묵적 형변환, stale stats, 조인 방식 문제, 해시조인
  TEMP 스필, 인덱스 단편화, 비선택적 OR 조건)을 실데이터로 재현해 적재한다(`db/init/01_setup_schema_and_data.sql`).
  Oracle 튜닝 이론 지식베이스 문서 5종(`src/knowledge/*.md`, 풀스캔/카디널리티/비-sargable/조인/통계 원인·개선 패턴)
- **연동**: Oracle 공식 MCP(SQLcl 내장 MCP 서버 `sql -mcp`)를 클라이언트로 소비하는 확장 경로를 실제로
  설치·구성했다(OpenJDK 21 + SQLcl, `src/tools.py`의 `get_oracle_mcp_tools()`가 `connect`/`sql_run`/
  `schema_information` 등 9개 MCP 도구를 정상적으로 가져온다 — explain_agent가 자동으로 사용 가능).
  진단·생성·검증·후보 탐색 파이프라인의 대상 DB 접근은 모두 이 MCP 경계를 지난다. 로컬 FileTracer와
  Langfuse 콜백으로 트레이싱하며, 관측 도구 실패가 API 요청 자체를 실패시키지 않는다.

## 4. 서비스 정책 (가드레일 요약)

- 개선안은 조언(advisory)만 제공하며 인덱스 생성이나 통계 재수집을 자동으로 실행하지 않는다 — 모든 위험 작업은
  사람 승인(`interrupt()` 기반 HITL) 게이트를 통과해야 한다(fail-closed)
- Oracle 외 DB(PostgreSQL, MySQL 등)는 지원 범위가 아니다
- 외부 LLM에 보내기 전 SQL 리터럴에 섞인 개인정보/자격증명(사번, 전화번호, 이메일, AWS 키, 접속 비밀번호)을
  레드액션(마스킹)하며, 프롬프트 인젝션 방어 가드레일이 입력/도구 출력/RAG 검색 결과 지점에 적용된다
- 사용자는 단일 SELECT/WITH 조회문을 직접 입력할 수 있다. UPDATE·DELETE가 포함된 입력은 어떤 DB/MCP 호출보다
  먼저 코드 기반 가드레일에서 차단하고 "SELECT 조회문만 입력 가능"을 안내한다
- 실행계획 원문은 사용자에게 입력받지 않는다 — 허용된 SQL에 대해 시스템이 SQLcl MCP로 매번 새로 조회하며,
  위험 평가를 통과한 경우에만 제한 실행한다(붙여넣은 실행계획을 신뢰하지 않음)

## 5. 성공 기준

- `evaluation/test_queries.csv`(20문항 내외, positive/negative/edge/guardrail 4개 카테고리) 기준
  규칙기반 통과율이 1차(round1) 70% 이상, 2차(round2, 개선 후) 90% 이상
- positive/edge 케이스에 대해 RAGAS 4지표(faithfulness/answer_relevancy ≥ 0.7, context_precision/context_recall
  ≥ 0.6)를 충족
- 개선안 응답에 자동 실행/적용으로 오인될 수 있는 문구가 0건이며, 위험 도구는 실제로 HITL 승인 없이는 실행되지 않음
- 12개 구현 역량(LCEL 구조화 출력, ReAct, RAG, 다중 도구, MCP 연동, 가드레일, HITL, 미들웨어, Multi-Agent
  Supervisor, Plan-Execute+장기메모리, Observability, RAGAS/LLM-as-Judge 평가)이 각각 명명된
  모듈로 구현·검증됨(README.md 매핑표 참고)
