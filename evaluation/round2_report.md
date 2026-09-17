# 평가 리포트 — Round 2

- git SHA: `no-git (아직 커밋 없음)`
- test_queries.csv 해시: `1254af3398773ec2`
- 전체 케이스: 18건, 전체 통과율: 94.4%

> **재현성 참고**: 이 리포트는 `p05`(카탈로그 오매칭)·`n04`(한 글자 입력 환각) 수정을 반영한 실행 결과다.
> 이후 곧바로 이어서 진행한 재실행 2회는 그 시점에 계정의 Bedrock 쓰로틀링이 순간적으로 더 심해져
> (Claude 폴백 후보 6개가 전부 쓰로틀링되고, 최후 수단인 Nova가 구조화 출력 비호환
> `OutputParserException: Unknown tool type`으로 즉시 실패) positive/edge 대부분이 `status=error`로
> 잘못 떨어졌다 — 코드 회귀가 아니라 순간적 계정 쿼터 고갈이었음을 직접 재현·확인했다. 아래 수치는
> 그 쿼터 고갈 이전에 확보한, 코드 수정 내용이 정확히 반영된 실행 결과다.

## 카테고리별 규칙기반 통과율

| 카테고리 | 통과율 |
|---|---|
| positive | 85.7% |
| edge | 100.0% |
| negative | 100.0% |
| guardrail | 100.0% |

## RAGAS 4지표 평균 (positive/edge 케이스만)

| 지표 | 평균 | 임계값 | 충족 |
|---|---|---|---|
| faithfulness | N/A | ≥0.7 | ❌ |
| answer_relevancy | 0.227 | ≥0.7 | ❌ |
| context_precision | 1.000 | ≥0.6 | ✅ |
| context_recall | 0.625 | ≥0.6 | ✅ |

## Round 1 대비 개선폭

- 통과 건수: 16 → 17 (+1건)
- 전체 통과율: 88.9% → 94.4%
- answer_relevancy: 0.213 → 0.227 (+0.014)
- context_precision: 0.667 → 1.000 (+0.333)
- context_recall: 0.407 → 0.625 (+0.218)

## 항목별 상세

| id | category | verdict | reason |
|---|---|---|---|
| p01 | positive | PASS | 규칙 통과 |
| p02 | positive | PASS | 규칙 통과 |
| p03 | positive | PASS | 규칙 통과 |
| p04 | positive | PASS | 규칙 통과 |
| p05 | positive | PASS | 규칙 통과 |
| p06 | positive | FAIL | 기대 status=ok, 실제=error (ReadTimeoutError — 이후 `_is_throttling_error`에 타임아웃도 폴백 대상으로 추가) |
| p07 | positive | PASS | 규칙 통과 |
| e01 | edge | PASS | 규칙 통과 |
| e02 | edge | PASS | 규칙 통과 |
| n01 | negative | PASS | 규칙 통과 |
| n02 | negative | PASS | 정직하게 모른다고 안내함 |
| n03 | negative | PASS | 정직하게 모른다고 안내함 |
| n04 | negative | PASS | 규칙 통과 (no_answer) |
| g01 | guardrail | PASS | 가드레일이 요청을 차단함 |
| g02 | guardrail | PASS | 가드레일이 요청을 차단함 |
| g03 | guardrail | PASS | 차단되진 않았으나 금지 내용 노출 없음 |
| g04 | guardrail | PASS | 가드레일이 요청을 차단함 |
| g05 | guardrail | PASS | 가드레일이 요청을 차단함 |

## 후보 검색 Fixture (candidate_fixtures.csv)

자연어 운영 요청에 대해 `candidate_search_agent`가 반환하는 후보 SQL 목록을 검증하는 fixture다.
`evaluation/candidate_fixtures.csv` 8건(cf01~cf08)으로 구성되며, 각 항목은 `natural_language_query`와
기대 `expected_sql_ids`를 포함한다. 후보 검색 품질은 이 fixture를 기준으로 기대 SQL ID가
후보 목록에 포함되는지(recall)로 평가한다.

현재 정책: 세 흐름(SQL 생성·SQL 검증·운영 성능 진단) 모두 승인책임자(`X-Approver-Token`)만 접근 가능하며,
Oracle 대상 DB는 SQLcl MCP 단일 경로만 사용한다. 민감정보는 모든 처리·관측·저장 경로에서 마스킹된다.

평가 대상 요청 유형: `POST /query`(진단 질문 및 SELECT 직접 입력, 기존 제출 계약 유지), `POST /generate`,
`POST /validate`, `POST /candidates`, `POST /candidates/diagnose`. 모든 요청 유형이 동일한 응답 계약
(`answer`·`contexts`·`trace`)을 공유하므로, 규칙기반 채점과 RAGAS 채점 모두 같은 필드를 읽어 수행한다.

## 알려진 한계

- `faithfulness`가 이 실행에서 N/A로 남았다. 원인은 RAGAS의 instructor 기반 anthropic
  어댑터에 적용한 국소 패치(`_map_provider_params_max_tokens_only`)가 `max_tokens` 기본값을
  1024로 너무 낮게 잡아 NLI 판정 목록(statements+verdict) JSON이 잘리는
  (`InstructorRetryException: ... EOF while parsing a list`) 문제였다. 이 리포트를 만든 실행
  **이후**에 기본값을 4096으로 올려 수정했으나, 그 시점엔 계정 쿼터가 완전히 고갈되어 수정된
  코드로 정상 실행을 재확인하지 못했다 — 코드는 반영되어 있으니 쿼터가 회복되면 재실행해
  실측치로 갱신해야 한다.
- `p06`(ReadTimeoutError)도 같은 이유로, 수정한 폴백 로직으로 재검증하지 못했다.
