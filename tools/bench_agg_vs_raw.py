#!/usr/bin/env python3
"""Cassandra 수집 방식 실측: TB 서버 집계(agg) vs 원시값(agg=NONE) 수집 (계획서 §3.6).

같은 키·같은 구간을 여러 방식으로 조회하고 응답 시간과 (가능하면) Cassandra 읽기 횟수를 비교한다.
읽기 전용이지만 운영 TB에서는 피크 시간을 피하고 짧은 구간으로만 실행할 것.

Cassandra 읽기 횟수는 `nodetool tablestats` 의 ts_kv_cf "Local read count" 증가량으로 잰다.
  --cassandra-container 이름 : docker exec 로 nodetool 실행 (테스트 환경)
  --nodetool "명령"          : 임의의 nodetool 실행 명령 (예: "ssh cass01 nodetool")

사용 예
  python3 bench_agg_vs_raw.py --url http://localhost:8080 --username tenant@thingsboard.org \
      --device PUMP-A1 --key current --hours 6 --cassandra-container tl-cassandra
"""

import argparse
import getpass
import os
import re
import shlex
import statistics
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tb_diagnose as tbd  # noqa: E402


def read_count(nodetool_cmd, keyspace):
    if not nodetool_cmd:
        return None
    out = subprocess.run(nodetool_cmd + ["tablestats", "%s.ts_kv_cf" % keyspace],
                         capture_output=True, text=True, timeout=60).stdout
    m = re.search(r"Local read count:\s*(\d+)", out)
    return int(m.group(1)) if m else None


def find_device(client, name):
    body = tbd._entity_query_body("DEVICE", 0, 10)
    body["keyFilters"] = [{
        "key": {"type": "ENTITY_FIELD", "key": "name"}, "valueType": "STRING",
        "predicate": {"type": "STRING", "operation": "EQUAL",
                      "value": {"defaultValue": name}, "ignoreCase": False}}]
    data = client.query("/api/entitiesQuery/find", body)
    rows = data.get("data", [])
    if not rows:
        raise SystemExit("디바이스를 찾을 수 없습니다: %s" % name)
    return rows[0]["entityId"]["id"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url", required=True)
    ap.add_argument("--username", required=True)
    ap.add_argument("--device", required=True, help="디바이스 이름")
    ap.add_argument("--key", required=True, help="텔레메트리 키")
    ap.add_argument("--hours", type=float, default=6, help="조회 구간(시간)")
    ap.add_argument("--repeat", type=int, default=5, help="방식별 반복 횟수")
    ap.add_argument("--cassandra-container", help="Cassandra docker 컨테이너 이름")
    ap.add_argument("--nodetool", help="nodetool 실행 명령 (컨테이너 대신)")
    ap.add_argument("--keyspace", default="thingsboard")
    args = ap.parse_args(argv)

    nodetool = None
    if args.cassandra_container:
        nodetool = ["docker", "exec", args.cassandra_container, "nodetool"]
    elif args.nodetool:
        nodetool = shlex.split(args.nodetool)

    password = os.environ.get("TB_PASSWORD") or getpass.getpass("ThingsBoard 비밀번호: ")
    client = tbd.TbClient(args.url, args.username, password, tbd.AdaptiveRateLimiter(5))
    client.login()
    dev_id = find_device(client, args.device)
    end = int(time.time() * 1000)
    start = end - int(args.hours * 3_600_000)
    span_min = int(args.hours * 60)

    methods = [
        ("서버 집계 1분 (AVG, %d구간)" % span_min, {"agg": "AVG", "interval": 60_000, "limit": 100_000}),
        ("서버 집계 1시간 (AVG, %d구간)" % max(1, int(args.hours)), {"agg": "AVG", "interval": 3_600_000, "limit": 100_000}),
        ("원시값 (agg=NONE, limit 50,000)", {"agg": "NONE", "limit": 50_000, "orderBy": "ASC"}),
    ]
    path = "/api/plugins/telemetry/DEVICE/%s/values/timeseries" % dev_id
    results = []
    for label, extra in methods:
        params = {"keys": args.key, "startTs": start, "endTs": end}
        params.update(extra)
        try:
            client.get(path, params=params)  # 워밍업 (JIT/커넥션 안정화)
        except tbd.TbError as e:
            # TB는 집계 구간 수가 ts_max_intervals(기본 700)를 넘으면 거부한다 — 구간 1개 = DB 조회 1회
            results.append((label + " — TB 거부: %s" % str(e)[:60], None, None, 0, 0, None))
            continue
        rc0 = read_count(nodetool, args.keyspace)
        times, points, nbytes = [], 0, 0
        for _ in range(args.repeat):
            t0 = time.monotonic()
            data = client.get(path, params=params) or {}
            times.append((time.monotonic() - t0) * 1000)
            pts = data.get(args.key, [])
            points = len(pts)
            nbytes = len(str(pts))
        rc1 = read_count(nodetool, args.keyspace)
        reads = (rc1 - rc0) / args.repeat if rc0 is not None and rc1 is not None else None
        results.append((label, statistics.median(times), max(times), points, nbytes, reads))

    print("\n대상: %s / %s, 구간 %.1f시간, 방식별 %d회 반복\n" % (args.device, args.key, args.hours, args.repeat))
    print("| 방식 | 응답 중앙값 ms | 최대 ms | 반환 점 수 | 응답 크기(대략) | Cassandra 읽기/요청 |")
    print("|---|---|---|---|---|---|")
    for label, med, mx, pts, nb, reads in results:
        if med is None:
            print("| %s | - | - | - | - | - |" % label)
            continue
        print("| %s | %.0f | %.0f | %s | %s KB | %s |" % (
            label, med, mx, f"{pts:,}", f"{nb // 1024:,}", "-" if reads is None else "%.0f" % reads))
    return 0


if __name__ == "__main__":
    sys.exit(main())
