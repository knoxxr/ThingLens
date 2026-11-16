#!/usr/bin/env bash
# 테스트용 ThingsBoard(CE) + PostgreSQL + Cassandra 기동. 최초 1회는 스키마 설치(데모 데이터 포함).
set -euo pipefail
cd "$(dirname "$0")"
TB_PORT="${TB_PORT:-8080}"

docker compose up -d --wait postgres cassandra

installed=$(docker compose exec -T postgres psql -U tb -d thingsboard -tAc \
  "select count(*) from information_schema.tables where table_name='tb_user'" 2>/dev/null || echo 0)
if [ "${installed// /}" != "1" ]; then
  echo "ThingsBoard 스키마 설치 (최초 1회, 1~3분 소요)..."
  docker compose run --rm -e INSTALL_TB=true -e LOAD_DEMO=true tb
fi

docker compose up -d tb
echo -n "ThingsBoard 기동 대기"
for _ in $(seq 1 90); do
  if curl -s -o /dev/null -w '%{http_code}' "http://localhost:${TB_PORT}/login" | grep -q 200; then
    echo " 완료: http://localhost:${TB_PORT}  (tenant@thingsboard.org / tenant)"
    exit 0
  fi
  echo -n "."; sleep 5
done
echo " 시간 초과 — 'docker compose logs tb' 확인"; exit 1
