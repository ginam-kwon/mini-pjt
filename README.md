# 미니 PJT: Oracle SQL 실행계획 성능진단 Agent

## 무엇을 푸나

DBA/백엔드 담당자가 반복적으로 하는 업무 — "느린 SQL을 받아 실행계획을 분석하고 성능 저하 원인과 개선안을 제시" — 를
자연어 질문 하나로 자동화한다. 사용자는 SQL이나 실행계획을 직접 붙여넣지 않고, 질문만 하면 `db_tool`이 대상 쿼리를
찾아 실행계획을 조회하고, Multi-Agent Supervisor가 원인 진단과 개선안을 근거와 함께 제시한다.

## 활용한 패턴 (Day 1~7)

| # | 패턴 | Day | 위치 |
|---|---|---|---|
| 1 | LCEL 구조화 출력 | Day 1 | `src/plan_execute.py`(finalize_node), `src/schemas.py` |
| 2 | ReAct | Day 3 | `src/agents.py`의 `create_agent` (도구 호출 루프) |
| 3 | RAG(하이브리드+리랭크+쿼리확장) | Day 2 | `src/retriever.py` |
| 4 | 다중 도구 | Day 4 | `src/tools.py`(db_tool/gather_stats/create_index) |
| 5 | MCP 서버 연동 | Day 4 | `src/tools.py`(SQLcl `sql -mcp`, 확장 경로) |
| 6 | 가드레일 | Day 5 | `src/guardrails.py`, `src/middleware.py` |
| 7 | HITL | Day 5 | `src/actions.py` (interrupt/Command, 승인/수정/거절) |
| 8 | 미들웨어 | Day 5 | `src/middleware.py` (4종 + MIDDLEWARE_ORDER) |
| 9 | Multi-Agent Supervisor | Day 6 | `src/agents.py`(create_supervisor) |
| 10 | Plan-Execute·장기메모리 | Day 7 | `src/plan_execute.py`(InMemoryStore) |
| 11 | Observability | Day 7 | `src/tracing.py`(FileTracer → trace.jsonl + 응답 trace 필드 + Langfuse 콜백 병행) |
| 12 | 평가(RAGAS·LLM-as-Judge) | Day 7 | `run_eval.py`, `src/ragas_eval.py`, `evaluation/test_queries.csv` |

공식 요건(§5)은 1·3·11·12 4개만 필수이나, 12개 전부를 통합하기로 확정했다(권장되지 않았으나 승인됨 — seed.yaml 참고).

## 아키텍처

```
POST /query {"question": "..."}
        │
   입력검증(빈값/과대입력/비문자열) → InputGuard(차단, 규칙+LLM) → src/guardrails.py
        │
   db_tool(question) 로 mock V$SQL 픽스처에서 SQL/실행계획을 키워드 매칭으로 결정적 조회  src/tools.py
        │ (매칭 실패 시 knowledge_agent로 순수 지식 질문 처리)
        │
   Plan-Execute 그래프  src/plan_execute.py
     planner → execute(supervisor 위임) → replan → finalize
                       │
              create_supervisor([explain_agent, knowledge_agent])   src/agents.py
                ├─ explain_agent : db_tool이 조회한 SQL/실행계획(데이터로만 취급)을 분석
                │    · gather_stats/create_index는 write 위험도구 → POST /actions/apply 를 거쳐
                │      항상 needs_approval() → interrupt() 승인 후 실행  src/actions.py
                └─ knowledge_agent : 하이브리드 RAG(BM25+Chroma+MultiQuery+LLM 리랭크)  src/retriever.py
              장기 메모리(InMemoryStore): SQL 지문(fingerprint)별 과거 진단 재사용
        │
   최종 LCEL 구조화 출력 SqlPlanAnalysis(summary, root_causes[], improvements[])
        │
   OutputCheck(검증) → Logging(기록: agent_log.jsonl, trace.jsonl)
        │
   응답 {"answer": str, "contexts": [{"doc_id","text"}], "trace": [{"step","input","output"}]}
```

## 실DB 셋업 (Oracle Database Free, Docker)

`db_tool`은 이제 mock 텍스트가 아니라 **실제 Oracle DB**에 `EXPLAIN PLAN`/`DBMS_XPLAN`을 실행해 진짜
실행계획을 조회한다. 로컬에 Oracle 클라이언트를 설치할 필요 없이 Docker 하나로 끝난다.

```bash
cd /home/ubuntu/edu/AX/sds-ax-practice/mini-pjt
cp .env.example .env   # 필요하면 비밀번호/포트 수정 (기본값 그대로도 동작함)

docker compose up -d   # gvenzl/oracle-free 컨테이너 기동
# 최초 기동 시 db/init/01_setup_schema_and_data.sql이 1회 자동 실행되어
# customers/orders/order_items/payments 스키마와 7가지 성능 이슈 패턴용 데이터를 적재한다
# (수 분 소요). 아래로 진행 상황 확인:
docker compose logs -f oracle-db

# 헬스체크로 준비 완료 확인 (healthy가 뜰 때까지)
docker inspect -f '{{.State.Health.Status}}' sql-tuning-oracle
```

컨테이너를 완전히 초기화(볼륨 삭제 후 데이터 재적재)하려면 `docker compose down -v && docker compose up -d`.

`ORACLE_DSN`이 설정되지 않았거나 DB 접속/실행이 실패하면(예: Docker가 없는 채점 환경) `db_tool`은
과거와 동일한 고정 mock 실행계획 텍스트로 조용히 폴백한다(예외를 던지지 않음) — `src/tools.py`의
`fallback_plan` 참고. 즉 Docker 없이도 이 프로젝트는 그대로 동작하지만, **기본/권장 경로는 실DB
조회**다.

인덱스 생성·통계 재수집(`gather_stats`/`create_index`)은 실DB가 연결된 뒤에도 여전히 mock으로
남아 있다 — SERVICE.md 정책(개선안은 advisory만 제공, 자동 실행 금지)에 따른 의도된 설계다.

## Langfuse 셋업 (Observability, 셀프호스팅)

`src/tracing.py`의 자체 `FileTracer`(trace.jsonl 기록)는 그대로 유지하고, 그 위에 Langfuse 콜백을
**병행**으로 붙였다 — LLM 호출/도구 호출/Multi-Agent Supervisor의 위임 흐름을 웹 UI에서 트레이스
단위로 들여다볼 수 있다. Oracle DB와 마찬가지로 `docker-compose.yml`에 이미 정의되어 있어 별도
계정 없이 로컬에서 완전히 자체 운영된다(postgres/clickhouse/redis/minio + langfuse-web/worker).

```bash
cd /home/ubuntu/edu/AX/sds-ax-practice/mini-pjt
docker compose up -d langfuse-postgres langfuse-clickhouse langfuse-redis langfuse-minio langfuse-worker langfuse-web
# 헬스체크
curl -s http://localhost:3000/api/public/health   # {"status":"OK",...}
```

최초 기동 시 `LANGFUSE_INIT_*`(.env) 값으로 조직/프로젝트/관리자 계정/API 키가 **자동 생성**된다 —
UI에서 별도로 회원가입·프로젝트 생성을 할 필요가 없다. 브라우저로 http://localhost:3000 을 열어
`LANGFUSE_INIT_USER_EMAIL` / `LANGFUSE_INIT_USER_PASSWORD`(.env)로 로그인하면 바로 트레이스를 볼 수
있다. `src/tracing.py`의 `langfuse_callbacks()`는 `.env`의 `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`가
`LANGFUSE_INIT_PROJECT_PUBLIC_KEY`/`SECRET_KEY`와 동일한 값이어야 트레이스를 이 프로젝트로 보낸다.

Langfuse 컨테이너가 없거나 키가 비어 있어도 `langfuse_callbacks()`는 예외를 던지지 않고 빈 리스트를
반환한다 — 관측 도구 하나가 죽었다고 진단 API(`POST /query`) 자체가 500을 내면 안 되기 때문
(FileTracer는 Langfuse와 무관하게 항상 동작).

주의: `minio/minio` 이미지는 Docker Hub 정책 변경으로 인증 없이 pull이 막혀 있어
`quay.io/minio/minio` 미러를 쓴다. 또한 postgres(5432)/clickhouse/redis/minio는 호스트 포트를
노출하지 않는다 — 이 컨테이너들은 langfuse-web/worker가 컴포즈 내부 네트워크로만 접속하면 되고,
샌드박스에 이미 다른 postgres(5432)가 떠 있어 충돌을 피하기 위함이다.

## 실행 방법

```bash
# 의존성 (저장소 루트 requirements.txt 는 이미 설치돼 있다는 전제 + 이 미니프로젝트 전용 추가분)
/home/ubuntu/edu/AX/sds-ax-practice/.venv/bin/pip install -r requirements-add.txt

# API 서버 (위 "실DB 셋업"으로 Oracle 컨테이너를 먼저 띄워둔 상태 권장)
cd /home/ubuntu/edu/AX/sds-ax-practice/mini-pjt
/home/ubuntu/edu/AX/sds-ax-practice/.venv/bin/python -m uvicorn src.agent:app --reload

# 진단 질문 (자연어 하나만 — SQL/실행계획을 직접 붙여넣지 않는다)
curl -s localhost:8000/query -X POST -H 'content-type: application/json' \
  -d '{"question": "주문과 고객을 조인하는 쿼리가 느린데 원인과 개선안을 알려줘"}' | python -m json.tool

# 위험 개선안 적용 (HITL) — 승인 대기 응답을 받는다
curl -s localhost:8000/actions/apply -X POST -H 'content-type: application/json' -d '{
  "tool": "create_index", "args": {"table_name": "ORDERS", "index_ddl": "CREATE INDEX idx_orders_date ON orders(order_date)"}
}'
# -> {"status": "awaiting_approval", "approval_id": "...", ...}
curl -s localhost:8000/approve/<approval_id> -X POST -H 'content-type: application/json' -d '{"decision": "approve"}'

# 평가 (1차 → 개선 → 2차)
/home/ubuntu/edu/AX/sds-ax-practice/.venv/bin/python run_eval.py --round 1
# ... round1_report.md의 실패 케이스를 보고 프롬프트/top_k/가드레일 정규식을 조정 ...
/home/ubuntu/edu/AX/sds-ax-practice/.venv/bin/python run_eval.py --round 2
```

## RAGAS 평가 결과

`evaluation/round2_report.md`(2차, 최종) 기준 — positive/edge 케이스 한정 평균:

- context_recall: 0.625 (임계값 ≥0.6 충족 ✅)
- context_precision: 1.000 (임계값 ≥0.6 충족 ✅)
- faithfulness: N/A — RAGAS instructor 어댑터의 `max_tokens` 절단 버그(트라이앤에러 회고 참고)를
  round2 리포트 확정 직후 수정했으나, 그 시점에 계정 쿼터가 고갈돼 수정 코드로 재검증하지 못한
  상태다(임계값 ≥0.7, 미충족 표기)
- answer_relevancy: 0.227 (임계값 ≥0.7, 미충족)

임계값: faithfulness/answer_relevancy ≥ 0.7, context_precision/context_recall ≥ 0.6.
negative/guardrail 케이스는 RAGAS로 채점하지 않는다 — 정답이 "거부"인 케이스에 faithfulness 같은 지표를
적용하는 것은 무의미하기 때문이며, 이 케이스들은 `expected_traits`/`forbidden` 컬럼 기준 규칙기반으로만
판정한다(자세한 근거는 `seed.yaml`의 `평가 척도 적합성` 원칙 참고).

## 인-아웃 세트 통과율 (자체 평가)

- 1차 (round1): `evaluation/round1_report.md` 참고, 목표 ≥ 70%
- 2차 (round2, 개선 후): `evaluation/round2_report.md` 참고, 목표 ≥ 90%
- 개선폭: round2_report.md의 "Round 1 대비 개선폭" 섹션에 카테고리별/RAGAS 지표별 수치로 기록됨

## 트라이앤에러 회고

- **시도했지만 실패/보류한 접근**:
  - `analyze_pasted_plan`(사용자가 SQL/실행계획을 직접 붙여넣는 폴백)을 초기에 구현했으나, "붙여넣기 금지 —
    반드시 실시간 DB 조회로만 진단" 정책을 확정하면서 전면 폐기하고 `db_tool`(mock V$SQL 픽스처 키워드 매칭)로
    교체했다.
  - RAGAS 연동 시 `ragas` 0.4.3이 import 시점에 `langchain_community.chat_models.vertexai`를 참조하는데
    이 프로젝트의 `langchain-community`(0.4.2, deprecated)에는 그 모듈이 없어 즉시 ImportError가 났다.
    실제로 VertexAI를 쓰지 않으므로 더미 모듈을 `sys.modules`에 등록하는 국소적 호환성 shim으로 우회했다
    (`src/ragas_eval.py` 상단 주석 참고).
  - RAGAS의 신규 `metrics.collections` API가 LangChain 모델을 직접 받지 못하고 "modern"(instructor 기반)
    어댑터만 받아, `anthropic` SDK의 `AnthropicBedrock` 클라이언트로 우회 연결했다. 임베딩도 동일한 이유로
    `BedrockEmbeddings`를 `BaseRagasEmbedding`으로 감싸는 얇은 어댑터를 직접 작성했다.
  - 배치 모드(일간 V$SQL 스캔)는 공식 요건이 아니고(§4-2 API 계약에 없음) service.md에서 스스로 얹은 확장
    목표라, 실DB 연동·모델 폴백·RAGAS 안정화 등 필수 요건에 시간을 우선 배분하며 설계만 하고 구현은
    보류했다(seed.yaml의 명시적 선택지였던 batch 확장 범위 밖 처리).
- **최종 채택한 접근과 이유**: `run_eval.py` 참고 — 규칙기반(전 카테고리) + RAGAS(positive/edge만) 이원화,
  `temperature=0`/`timeout=30s`/1회 실행으로 게이트 재현성을 담보.
- **결정 번복: mock 전용 → 실DB 연동**: 최초엔 "이 샌드박스와 채점 환경 모두 실제 Oracle DB가 없다"는 전제로
  `db_tool`을 고정 mock 픽스처 전용으로 설계했다(seed.yaml v1.1.0). 이후 사용자 요청으로 이 전제를 뒤집어
  `docker-compose.yml`(gvenzl/oracle-free)로 로컬 Oracle Database Free 인스턴스를 실제로 구성했고,
  `db_tool`은 이제 그 위에서 진짜 `EXPLAIN PLAN`/`DBMS_XPLAN`을 실행해 7가지 성능 이슈 패턴을 실데이터로
  재현한다(`db/init/01_setup_schema_and_data.sql`). `ORACLE_DSN` 미설정/접속 실패 시에는 과거의 고정 mock
  텍스트로 조용히 폴백해 Docker가 없는 환경(예: 채점 서버)에서도 깨지지 않는다 — "실DB 우선, mock은
  안전망"으로 정책이 바뀐 것이며 이전 결정을 숨기지 않고 여기 기록한다. 자연어 질문→쿼리 매칭 방식(고정
  카탈로그 키워드 매칭)과 붙여넣기 금지 정책 자체는 그대로 유지된다.
  또한 SQLcl(OpenJDK 21 필요, 이 환경 기본 java는 8이라 별도 설치)을 설치해 `sql -mcp` 기반 Oracle 공식
  MCP 서버 연동도 실제로 동작하도록 구성했다 — `get_oracle_mcp_tools()`가 `connect`/`sql_run`/
  `schema_information` 등 9개 도구를 정상적으로 가져오며, explain_agent가 자동으로 사용할 수 있다. 다만
  진단 파이프라인의 기본 경로는 여전히 결정적인 `db_tool` 직접 접속이고, MCP는 확장/탐색 경로로만 둔다
  (진단 결과의 재현성을 LLM 기반 MCP 호출의 비결정성에 의존시키지 않기 위함).
- **모델 폴백 관련 트라이앤에러**: 실습 계정의 Bedrock 일일 토큰 쿼터가 반복적으로 고갈되어(`ThrottlingException`),
  `src/common.py`에 `MultiModelChatBedrockConverse`(쓰로틀링 시 `FALLBACK_MODEL_IDS` 순서로 즉시 다음 모델로
  재시도)를 추가했다. 과정에서 실제로 겪은 문제들:
  - `_is_throttling_error`가 처음엔 botocore 예외(`e.response`가 dict)만 인식했는데, RAGAS 쪽 anthropic SDK
    (`AsyncAnthropicBedrock`) 예외는 `e.response`가 httpx `Response` 객체라 `.get()` 호출 시
    `AttributeError`로 깨졌다 — dict/객체 두 형태를 모두 안전하게 처리하도록 수정.
  - `ReadTimeoutError`(우리가 건 30초 timeout)는 `ThrottlingException`이 아니라서 폴백 대상으로 인식되지
    않아, 쿼터가 고갈된 상황에서 응답이 늦어지면 다음 모델로 넘어가지 못하고 그대로 실패했다 —
    `_is_throttling_error`에 타임아웃류 예외도 포함하도록 수정.
  - 폴백 후보 마지막 단계인 Nova(`us.amazon.nova-*`)는 이 프로젝트가 전반적으로 의존하는
    `with_structured_output`/도구 호출 방식과 호환되지 않아(`OutputParserException: Unknown tool type`),
    Claude 계열이 전부 쓰로틀링된 극단적 상황에서는 Nova로 넘어가도 즉시 실패한다 — 실측으로 확인했고,
    현재는 "최후 수단으로 시도는 하되 실패해도 감수" 수준으로만 두고 있다(구조화 출력 없는 별도
    경로를 Nova용으로 새로 만드는 것은 범위 밖으로 보류).
  - RAGAS instructor 어댑터의 `max_tokens` 기본값을 1024로 좁게 잡았다가 `faithfulness` 지표의 NLI
    판정 JSON(문장이 많을 때)이 중간에 잘려(`InstructorRetryException: EOF while parsing a list`)
    항상 `N/A`로 떨어지는 문제를 겪었다 — 4096으로 올려 해결.
- **남은 한계**:
  - 로컬 샌드박스에서 Docker 컨테이너 최초 기동 시 알 수 없는 원인(호스트 네트워크 재구성 추정)으로
    컨테이너가 재시작되어 초기화 스크립트가 중간에 끊기는 현상을 겪었다 — `docker compose down -v && up -d`로
    볼륨을 비우고 재기동하면 해결되지만, 채점 환경에서도 동일 증상이 재현될 가능성을 배제할 수 없다. 이
    때문에 `db_tool`의 mock 폴백 경로(예외를 던지지 않고 조용히 대체)를 안전망으로 유지했다.
  - 실제 Oracle DB 프로비저닝은 여전히 "1회성 로컬 Docker 컨테이너" 수준이며, 운영급 배포(백업, 고가용성,
    실제 자격증명 관리)는 범위 밖이다 — `.env`의 개발용 고정 비밀번호는 로컬 데모 전용이다.
  - 위험 쓰기 도구(`gather_stats`/`create_index`)는 실DB 연동 이후에도 의도적으로 mock으로 남겨뒀다
    (SERVICE.md의 advisory-only 정책 유지 — 실DB에 실제 DDL/통계 재수집을 자동 실행하지 않는다).

## 핵심 코드 위치

- `src/agent.py` — FastAPI 진입점 (`POST /query`, `POST /actions/apply`, `POST /approve/{id}`)
- `src/pipeline.py` — 입력검증·가드레일·db_tool 조회·진단/지식 라우팅 오케스트레이션
- `src/tools.py` — db_tool(mock V$SQL) + SQLcl MCP 확장 경로 + 위험 도구(gather_stats/create_index)
- `src/retriever.py` — 하이브리드 RAG (BM25 + Chroma + MultiQuery + LLM 리랭크)
- `src/agents.py` — explain_agent/knowledge_agent + Supervisor 조립
- `src/plan_execute.py` — Plan-Execute 그래프 + 장기 메모리
- `src/actions.py` — HITL 승인 그래프
- `src/ragas_eval.py` — RAGAS 4지표 (Bedrock 연동 어댑터)
- `run_eval.py` — 평가 실행기 (규칙기반 + RAGAS, round1/round2 리포트 생성)
