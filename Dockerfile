FROM python:3.11-slim

WORKDIR /app

# SQLcl MCP 연동(Oracle 대상 DB 접근 단일 경로, README '실DB 셋업' 참고)에 필요한 OpenJDK 21 +
# SQLcl. 없어도 API는 기동하지만(db_tool/candidate 조회가 mock으로 폴백) 실DB 경로를 쓰려면
# 필수다. openjdk-21-jdk-headless(경량, JRE+컴파일러만)면 SQLcl 실행에 충분하다.
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends openjdk-21-jdk-headless curl unzip \
    && curl -fsSL -o /tmp/sqlcl.zip https://download.oracle.com/otn_software/java/sqldeveloper/sqlcl-latest.zip \
    && unzip -q /tmp/sqlcl.zip -d /opt \
    && ln -s /opt/sqlcl/bin/sql /usr/local/bin/sql \
    && rm -f /tmp/sqlcl.zip \
    && apt-get purge -y -qq curl unzip \
    && apt-get autoremove -y -qq \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x docker-entrypoint.sh

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# docker-entrypoint.sh는 SQLcl connmgr에 연결을 저장한 뒤 uvicorn으로 API를 기동한다.
ENTRYPOINT ["./docker-entrypoint.sh"]
