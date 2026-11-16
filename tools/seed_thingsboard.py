#!/usr/bin/env python3
"""테스트용 ThingsBoard에 '정답이 알려진' 시드 데이터를 적재한다.

⚠ 쓰기 작업을 수행하므로 **테스트 TB 전용**이다. 기본적으로 localhost 대상만 허용한다.

구성
  Site "Plant-1" ─Contains→ Line-A, Line-B (에셋)
  Line-A ─Contains→ TS-A1..A5, PUMP-A1, METER-A1
  Line-B ─Contains→ TS-B1..B3, PUMP-B1
  TS-X1, OLD-1 : 관계 없음

심어 둔 시나리오 (seed_truth.json 으로 저장, 이후 분석 기능의 정답셋)
  - name_variants      : 같은 온도인데 키 이름이 temperature / temp / Temperature_C
  - high_freq          : 펌프 current·p1_out 1초 주기 (최근 6시간)
  - lagged_pair        : p1_out 은 current 를 5초 늦게 따라감
  - event_driven       : 펌프 status 는 불규칙(변화 시) 보고
  - counter            : 전력량 energy_kwh 는 누적 카운터
  - stuck_sensor       : TS-A4 온도가 최근 3일 22.0 고정
  - broken_correlation : TS-A3 은 최근 2일 형제들과 상관이 깨짐
  - redundant_pair     : TS-A5 는 TS-A1 과 거의 동일 (r≈0.99)
  - attribute_mismatch : TS-A5 location 속성이 부모(Line-A)와 다름 (Line-B)
  - out_of_range       : TS-B2 humidity 에 130% 스파이크
  - missing_relation   : TS-X1 은 Line-B 패턴인데 관계 없음
  - inactive_key       : OLD-1 은 40일 전 값 1개뿐

사용
  python3 seed_thingsboard.py --url http://localhost:8080 --username tenant@thingsboard.org
"""

import argparse
import getpass
import json
import math
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

MIN_MS = 60_000
HOUR_MS = 3_600_000
DAY_MS = 86_400_000


class Tb:
    def __init__(self, url, username, password):
        self.url = url.rstrip("/")
        self.token = None
        r = self.call("POST", "/api/auth/login", {"username": username, "password": password}, auth=False)
        self.token = r["token"]

    def call(self, method, path, body=None, params=None, auth=True, ok404=False):
        url = self.url + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if auth:
            headers["X-Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                if e.code == 404 and ok404:
                    return None
                if e.code in (429, 500, 502, 503) and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError("%s %s → HTTP %s %s" % (method, path, e.code, e.read()[:300]))

    def ensure_entity(self, kind, name, profile):
        """kind: device|asset. 같은 이름이 있으면 재사용 (재실행 가능)."""
        if kind == "device":
            found = self.call("GET", "/api/tenant/devices", params={"deviceName": name}, ok404=True)
            if found:
                return found["id"]["id"]
            return self.call("POST", "/api/device", {"name": name, "type": profile})["id"]["id"]
        found = self.call("GET", "/api/tenant/assets", params={"assetName": name}, ok404=True)
        if found:
            return found["id"]["id"]
        return self.call("POST", "/api/asset", {"name": name, "type": profile})["id"]["id"]

    def relate(self, from_type, from_id, to_type, to_id, rtype="Contains"):
        self.call("POST", "/api/relation", {
            "from": {"entityType": from_type, "id": from_id},
            "to": {"entityType": to_type, "id": to_id},
            "type": rtype, "typeGroup": "COMMON"})

    def attributes(self, etype, eid, scope, values):
        self.call("POST", "/api/plugins/telemetry/%s/%s/attributes/%s" % (etype, eid, scope), values)

    def telemetry(self, etype, eid, rows, batch=1000):
        for i in range(0, len(rows), batch):
            self.call("POST", "/api/plugins/telemetry/%s/%s/timeseries/ANY" % (etype, eid), rows[i:i + batch])


# --------------------------------------------------------------------------- #
# 신호 생성
# --------------------------------------------------------------------------- #
def random_walk(rng, n, step=0.05, revert=0.01):
    x, out = 0.0, []
    for _ in range(n):
        x += rng.gauss(0, step) - revert * x
        out.append(x)
    return out


def build_dataset(now_ms, days, seed=42):
    rng = random.Random(seed)
    start = now_ms - days * DAY_MS
    minutes = [start + i * MIN_MS for i in range(days * 1440)]
    n = len(minutes)
    daily = [3.0 * math.sin(2 * math.pi * ((t / DAY_MS) % 1.0) - math.pi / 2) for t in minutes]
    walk = {"A": random_walk(rng, n, 0.08), "B": random_walk(rng, n, 0.08)}
    hwalk = {"A": random_walk(rng, n, 0.15), "B": random_walk(rng, n, 0.15)}

    def temp_series(line, offset, noise=0.15):
        return [22 + offset + daily[i] + walk[line][i] * 4 + rng.gauss(0, noise) for i in range(n)]

    def hum_series(line, offset):
        return [45 + offset - daily[i] * 1.5 + hwalk[line][i] * 6 + rng.gauss(0, 0.5) for i in range(n)]

    dev = {}  # name -> {"profile", "parent", "rows": {ts: {k: v}}, "attrs": {...}}

    def add(name, profile, parent, location):
        dev[name] = {"profile": profile, "parent": parent, "rows": {},
                     "attrs": {"location": location, "installDate": "2025-03-01"}}

    def put(name, key, ts_list, values, fmt=lambda v: round(v, 3)):
        rows = dev[name]["rows"]
        for t, v in zip(ts_list, values):
            rows.setdefault(t, {})[key] = fmt(v)

    hours = minutes[::60]
    # --- Line-A 온도 센서 (키 이름 변형) ---
    for name, tkey, off in (("TS-A1", "temperature", 0.0), ("TS-A2", "temp", 0.4),
                            ("TS-A3", "Temperature_C", -0.3), ("TS-A4", "temperature", 0.2)):
        add(name, "TempSensor", "Line-A", "Line-A")
        t = temp_series("A", off)
        if name == "TS-A3":  # 최근 2일 상관 붕괴: 독립적인 움직임으로 교체
            own = random_walk(rng, n, 0.12)
            cut = n - 2 * 1440
            t = t[:cut] + [22 + off + own[i] * 6 + rng.gauss(0, 0.3) for i in range(cut, n)]
        if name == "TS-A4":  # 최근 3일 값 고정
            cut = n - 3 * 1440
            t = t[:cut] + [22.0] * (n - cut)
        put(name, tkey, minutes, t)
        put(name, "humidity", minutes, hum_series("A", rng.uniform(-2, 2)))
        put(name, "battery", hours, [100 - i * 0.05 for i in range(len(hours))], lambda v: round(v, 1))
    # TS-A5: TS-A1 과 거의 동일 (중복 센서), location 불일치
    add("TS-A5", "TempSensor", "Line-A", "Line-B")
    a1 = [dev["TS-A1"]["rows"][t]["temperature"] for t in minutes]
    put("TS-A5", "temperature", minutes, [v + rng.gauss(0, 0.05) for v in a1])
    put("TS-A5", "humidity", minutes, hum_series("A", 1.0))

    # --- Line-B 온도 센서 ---
    for name, off in (("TS-B1", 0.0), ("TS-B2", 0.5), ("TS-B3", -0.5)):
        add(name, "TempSensor", "Line-B", "Line-B")
        put(name, "temperature", minutes, temp_series("B", off))
        h = hum_series("B", off)
        if name == "TS-B2":
            for i in rng.sample(range(n), 15):
                h[i] = 130.0
        put(name, "humidity", minutes, h)
        put(name, "battery", hours, [98 - i * 0.04 for i in range(len(hours))], lambda v: round(v, 1))

    # --- 관계 없는 센서 (Line-B 패턴) ---
    add("TS-X1", "TempSensor", None, "")
    put("TS-X1", "temperature", minutes, temp_series("B", 0.2))
    put("TS-X1", "humidity", minutes, hum_series("B", 0.0))

    # --- 펌프: 1초 주기 최근 6시간, p1_out 은 current 를 5초 지연 추종 ---
    sec_start = now_ms - 6 * HOUR_MS
    seconds = [sec_start + i * 1000 for i in range(6 * 3600)]
    for name, parent in (("PUMP-A1", "Line-A"), ("PUMP-B1", "Line-B")):
        add(name, "PumpController", parent, parent)
        drive = random_walk(rng, len(seconds) + 5, 0.03, 0.002)
        current = [12 + drive[i + 5] * 8 + rng.gauss(0, 0.05) for i in range(len(seconds))]
        pressure = [3.0 + 0.4 * (drive[i] * 8) + rng.gauss(0, 0.02) for i in range(len(seconds))]
        put(name, "current", seconds, current)
        put(name, "p1_out", seconds, pressure)
        # status: 7일간 불규칙 변화 시 보고
        t, st, ts_list, vals = start, "RUN", [], []
        while t < now_ms:
            ts_list.append(t)
            vals.append(st)
            st = "STOP" if st == "RUN" else "RUN"
            t += int(rng.expovariate(1 / (3 * HOUR_MS))) + MIN_MS
        put(name, "status", ts_list, vals, lambda v: v)

    # --- 전력량계: 15분 주기 누적 카운터 ---
    add("METER-A1", "EnergyMeter", "Line-A", "Line-A")
    q = minutes[::15]
    power = [5 + 2 * math.sin(2 * math.pi * ((t / DAY_MS) % 1.0)) + rng.gauss(0, 0.2) for t in q]
    energy, acc = [], 1000.0
    for p in power:
        acc += p * 0.25
        energy.append(acc)
    put("METER-A1", "power_kw", q, power)
    put("METER-A1", "energy_kwh", q, energy)

    # --- 비활성 키 ---
    add("OLD-1", "TempSensor", None, "")
    put("OLD-1", "temperature", [now_ms - 40 * DAY_MS], [21.0])

    truth = {
        "generated_at_ms": now_ms,
        "days": days,
        "hierarchy": {"Plant-1": ["Line-A", "Line-B"],
                      "Line-A": [d for d, v in dev.items() if v["parent"] == "Line-A"],
                      "Line-B": [d for d, v in dev.items() if v["parent"] == "Line-B"]},
        "scenarios": {
            "name_variants": {"semantic": "temperature",
                              "keys": {"TS-A1": "temperature", "TS-A2": "temp", "TS-A3": "Temperature_C"}},
            "high_freq": {"devices": ["PUMP-A1", "PUMP-B1"], "keys": ["current", "p1_out"], "interval_s": 1},
            "lagged_pair": {"device": "PUMP-A1", "leader": "current", "follower": "p1_out", "lag_s": 5},
            "event_driven": {"devices": ["PUMP-A1", "PUMP-B1"], "key": "status"},
            "counter": {"device": "METER-A1", "key": "energy_kwh"},
            "stuck_sensor": {"device": "TS-A4", "key": "temperature", "since_days_ago": 3, "value": 22.0},
            "broken_correlation": {"device": "TS-A3", "key": "Temperature_C", "since_days_ago": 2,
                                   "siblings": ["TS-A1", "TS-A2", "TS-A5"]},
            "redundant_pair": {"devices": ["TS-A1", "TS-A5"], "key": "temperature"},
            "attribute_mismatch": {"device": "TS-A5", "attribute": "location", "value": "Line-B",
                                   "parent": "Line-A"},
            "out_of_range": {"device": "TS-B2", "key": "humidity", "value": 130.0, "count": 15},
            "missing_relation": {"device": "TS-X1", "expected_parent": "Line-B"},
            "orphans": ["TS-X1", "OLD-1"],
            "inactive_key": {"device": "OLD-1", "key": "temperature"},
        },
    }
    return dev, truth


def main(argv=None):
    ap = argparse.ArgumentParser(description="테스트 TB 시드 데이터 적재 (쓰기 작업 — 테스트 TB 전용)")
    ap.add_argument("--url", default="http://localhost:8080")
    ap.add_argument("--username", default="tenant@thingsboard.org")
    ap.add_argument("--days", type=int, default=7, help="1분 주기 데이터 기간(일)")
    ap.add_argument("--truth-out", default="seed_truth.json")
    ap.add_argument("--allow-remote", action="store_true",
                    help="localhost 가 아닌 TB에 적재 허용 (반드시 테스트 TB일 때만)")
    args = ap.parse_args(argv)

    host = urllib.parse.urlparse(args.url).hostname
    if host not in ("localhost", "127.0.0.1", "::1") and not args.allow_remote:
        ap.error("안전장치: localhost 가 아닌 TB(%s)에는 --allow-remote 없이 쓰지 않습니다. 운영 TB에 실행하지 마세요." % host)

    password = os.environ.get("TB_PASSWORD") or getpass.getpass("테스트 TB 비밀번호: ")
    tb = Tb(args.url, args.username, password)
    now_ms = int(time.time() // 60) * MIN_MS
    dev, truth = build_dataset(now_ms, args.days)

    print("에셋·관계 생성")
    ids = {}
    ids["Plant-1"] = ("ASSET", tb.ensure_entity("asset", "Plant-1", "Site"))
    for line in ("Line-A", "Line-B"):
        ids[line] = ("ASSET", tb.ensure_entity("asset", line, "Line"))
        tb.relate("ASSET", ids["Plant-1"][1], "ASSET", ids[line][1])
        tb.attributes("ASSET", ids[line][1], "SERVER_SCOPE", {"location": line, "site": "Plant-1"})

    total = 0
    for name, d in dev.items():
        eid = tb.ensure_entity("device", name, d["profile"])
        ids[name] = ("DEVICE", eid)
        if d["parent"]:
            tb.relate("ASSET", ids[d["parent"]][1], "DEVICE", eid)
        attrs = {k: v for k, v in d["attrs"].items() if v}
        if attrs:
            tb.attributes("DEVICE", eid, "SERVER_SCOPE", attrs)
        rows = [{"ts": t, "values": vals} for t, vals in sorted(d["rows"].items())]
        tb.telemetry("DEVICE", eid, rows)
        npts = sum(len(r["values"]) for r in rows)
        total += npts
        print("  %-9s %-15s 값 %7d개" % (name, d["profile"], npts))

    truth["entity_ids"] = {k: v[1] for k, v in ids.items()}
    with open(args.truth_out, "w", encoding="utf-8") as f:
        json.dump(truth, f, ensure_ascii=False, indent=2)
    print("완료: 디바이스 %d개, 텔레메트리 값 %d개, 정답 파일 %s" % (len(dev), total, args.truth_out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
