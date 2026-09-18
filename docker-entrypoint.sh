#!/usr/bin/env sh
# docker-entrypoint.sh - 컨테이너 기동 시 SQLcl connmgr에 Oracle 연결을 1회 저장한 뒤 API를 띄운다.
#
# SQLcl MCP의 connect 도구는 접속 문자열을 바로 받지 않고 connmgr에 미리 저장된 연결 이름만
# 받는다(README '실DB 셋업' 참고). 로컬 개발에서는 호스트에서 한 번 `sql -S /nolog`로 저장하면
# 되지만, docker-compose 배포에서는 오라클 컨테이너 호스트명(oracle-db)과 자격증명이 컴포즈
# 네트워크에서만 확정되는 런타임 값이라 이미지 빌드 시점에 구울 수 없다 — 그래서 컨테이너가 뜰
# 때마다(재시작해도 매번) 이 스크립트가 connmgr에 다시 저장한다. SQLcl이 없거나 저장이 실패해도
# API 기동 자체는 막지 않는다 — db_tool/candidate 조회가 조용히 mock 폴백으로 넘어간다.
set -eu

CONN_NAME="${SQLCL_CONNECTION_NAME:-mini_pjt_conn}"

if command -v sql >/dev/null 2>&1 && [ -n "${ORACLE_DSN:-}" ]; then
  printf 'connect -save %s -savepwd %s/"%s"@%s\nexit\n' \
    "$CONN_NAME" "${ORACLE_APP_USER:-appuser}" "${ORACLE_APP_PASSWORD:-}" "$ORACLE_DSN" \
    | sql -S /nolog \
    && echo "[entrypoint] SQLcl 연결 '$CONN_NAME' 저장 완료" \
    || echo "[entrypoint] SQLcl 연결 저장 실패 — db_tool은 mock으로 폴백한다(계속 진행)"
else
  echo "[entrypoint] SQLcl 없음 또는 ORACLE_DSN 미설정 — SQLcl MCP 없이 기동(mock 폴백)"
fi

exec uvicorn src.api:app --host 0.0.0.0 --port "${PORT:-8000}"
