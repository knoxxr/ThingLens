"""Phase 2: 데이터 종류·의미 추론, 품질 점검, 카탈로그·품질 API."""

import math

import pytest
from fastapi.testclient import TestClient

from thinglens.api.app import create_app
from thinglens.auth.users import create_user
from thinglens.db import now_ms
from thinglens.quality.checks import run_checks, score
from thinglens.scheduler import queue as q
from thinglens.scheduler.worker import build_worker
from thinglens.semantics.infer import (BINARY, CATEG, CONT, COUNTER, JSON_, TEXT, TIMESTAMP, compute_stats,
                                       infer_data_type, infer_semantic, tokenize)

from fake_tb import FakeTb

DAY = 86_400_000
NOW = 1_800_000_000_000


def pts(values, step=60_000, start=0):
    return [(start + i * step, v) for i, v in enumerate(values)]


# -- 순수 함수 -------------------------------------------------------------------
@pytest.mark.parametrize("key,tokens", [
    ("Temperature_C", ["temperature", "c"]), ("p1_out", ["p", "1", "out"]), ("lastActivityTime", ["last", "activity", "time"]),
    ("PM2.5", ["pm", "2", "5"]), ("온도_센서1", ["온도", "센서", "1"]), ("CO2ppm", ["co", "2", "ppm"]),
])
def test_tokenize(key, tokens):
    assert tokenize(key) == tokens


@pytest.mark.parametrize("values,expected", [
    (["true", "false"] * 10, BINARY),
    (["0", "1", "1", "0"] * 5, BINARY),
    ([str(1000 + i * 1.5) for i in range(30)], CONT),  # 30분짜리 창 하나 — 카운터로 단정하지 않음
    ([str(20 + math.sin(i)) for i in range(30)], CONT),
    ([str(1_794_000_000_000 + i * 60_000) for i in range(30)], TIMESTAMP),
    (['{"a": 1}'] * 10, JSON_),
    ([f"SN-{i:05d}" for i in range(30)], TEXT),
    (["AUTO", "MANUAL", "OFF"] * 10, CATEG),
    (["1", "2"], None),  # 판단 불가 (점 부족)
])
def test_data_types(values, expected):
    p = pts(values)
    assert infer_data_type(p, compute_stats(p)) == expected


def test_counter_needs_consistent_long_term_rate():
    hour = 3_600_000
    energy = [pts([str(1000 + (DAY * 7 - 7 * DAY) / hour + i / 60) for i in range(60)], start=0),
              pts([str(1000 + 6 * 24 + i / 60) for i in range(60)], start=6 * DAY),
              pts([str(1000 + 7 * 24 + i / 60) for i in range(60)], start=7 * DAY)]
    flat = [p for w in energy for p in w]
    assert infer_data_type(flat, compute_stats(flat), energy) == COUNTER
    # 느린 파형: 각 창 안에서는 증가하지만 장기 평균 증가율은 거의 0
    wave = [pts([str(45 + i * 0.04) for i in range(60)], start=0),
            pts([str(44 + i * 0.04) for i in range(60)], start=6 * DAY),
            pts([str(46 + i * 0.04) for i in range(60)], start=7 * DAY)]
    flat = [p for w in wave for p in w]
    assert infer_data_type(flat, compute_stats(flat), wave) == CONT


def test_semantic_name_unit_range_and_conflicts():
    temp = pts([str(22 + math.sin(i / 5)) for i in range(60)])
    s = infer_semantic("Temperature_C", CONT, compute_stats(temp))
    assert (s.semantic, s.unit, s.unit_source, s.status) == ("temperature", "°C", "suffix", "AUTO")
    # 이름은 습도인데 값이 물리 범위(0~100) 밖 → 감점되어 검토 대기
    weird = pts([str(400 + i) for i in range(60)])
    assert infer_semantic("humidity", CONT, compute_stats(weird)).status == "REVIEW"
    # 단위 접미사가 다른 계열이면 감점
    assert infer_semantic("temperature_kw", CONT, compute_stats(temp)).confidence < s.confidence
    # 사용자 확정이 항상 우선
    r = infer_semantic("ch2", CONT, compute_stats(temp), rule={"semantic": "pressure", "unit": "kPa", "created_by": "kim"})
    assert (r.semantic, r.unit, r.status, r.confidence) == ("pressure", "kPa", "CONFIRMED", 1.0)


def _checks(windows, **kw):
    base = dict(now_ms=NOW, interval_s=60.0, irregular=False, tier="mid", data_type=CONT, semantic="temperature",
                semantic_conf=0.9, envelope=None, first_seen_ms=NOW - 30 * DAY)
    base.update(kw)
    return {f.check: f for f in run_checks(windows=windows, **base)}


def test_quality_checks():
    healthy = [pts([str(20 + math.sin(i / 3)) for i in range(60)], start=NOW - 3_600_000)]
    assert _checks(healthy) == {}
    # 값 고정: 최근 창이 모두 같고 이전 창은 변동
    flat = [pts(["22.0"] * 60, start=NOW - 3_600_000), pts([str(20 + i % 5) for i in range(60)], start=NOW - DAY)]
    assert _checks(flat)["FLATLINE"].severity == "critical"
    # 보고 중단: 2일 전이 마지막
    stale = [pts([str(20 + i % 3) for i in range(60)], start=NOW - 2 * DAY - 3_600_000)]
    assert _checks(stale)["STALE"].severity == "critical"
    # 물리 범위 이탈: 샘플은 정상이지만 7일 범위의 최댓값이 130%
    env = {"start": NOW - 7 * DAY, "end": NOW, "min": 20.0, "max": 130.0, "count": 10080}
    f = _checks(healthy, semantic="humidity", envelope=env)
    assert f["OUT_OF_RANGE"].detail["max"] == 130.0
    assert "OUT_OF_RANGE" not in _checks(healthy, semantic="humidity", semantic_conf=0.4, envelope=env)  # 의미 불확실
    # 수신률 저하
    low = {"start": NOW - DAY, "end": NOW, "min": 1, "max": 2, "count": 500}
    assert _checks(healthy, envelope=low)["LOW_RECEIVE_RATE"].detail["rate"] < 0.5
    # 미래 타임스탬프, 숫자·문자 혼재, 튐
    fut = [healthy[0] + [(NOW + DAY, "21")]]
    assert "FUTURE_TS" in _checks(fut)
    mixed = [pts([str(i) if i % 3 else "N/A" for i in range(60)], start=NOW - 3_600_000)]
    assert "MIXED_TYPE" in _checks(mixed)
    spiky = [pts([str(20 + (i % 3) * 0.1) for i in range(59)] + ["900"], start=NOW - 3_600_000)]
    assert "SPIKE" in _checks(spiky)
    # 데이터 없음
    assert _checks([[]], tier="inactive")["NO_DATA"].severity == "critical"
    assert score(list(_checks(flat).values())) == 50


# -- 가짜 TB 로 전체 흐름 ------------------------------------------------------------
def build_phase2_fleet(tb: FakeTb) -> dict:
    now = tb.now_ms
    c = tb.add_customer("Customer A")
    sine = lambda t: f"{22 + 3 * math.sin(t / 3_600_000):.3f}"  # noqa: E731
    spike_at = now - 3 * DAY - 7 * 60_000  # 7일 범위 안, 최근·과거 샘플 창 밖

    def hum(t):
        return "130.0" if abs(t - spike_at) < 30_000 else f"{45 + 5 * math.sin(t / 7_200_000):.2f}"

    ids = {
        "S1": tb.add_entity("DEVICE", "S1", "TempSensor", owner=c, attrs={"SERVER_SCOPE": {"location": "Line-A"},
                                                                          "CLIENT_SCOPE": {"fw": "1.2.0"}},
                            ts={"temperature": {"interval": 60, "gen": sine}, "humidity": {"interval": 60, "gen": hum},
                                "t1": {"interval": 60, "gen": sine}, "battery": {"interval": 3600, "gen": lambda t: "97.5"}}),
        "S2": tb.add_entity("DEVICE", "S2", "TempSensor",
                            ts={"temperature": {"interval": 60, "gen": lambda t: "22.0" if t > now - DAY else sine(t)}}),
        "S3": tb.add_entity("DEVICE", "S3", "TempSensor",
                            ts={"temperature": {"interval": 60, "gen": sine, "until": now - 2 * DAY}}),
        "P1": tb.add_entity("DEVICE", "P1", "PumpController",
                            ts={"current": {"interval": 1, "gen": lambda t: f"{12 + math.sin(t / 5000):.3f}"},
                                "status": {"interval": 3600, "gen": lambda t: "RUN" if (t // 3_600_000) % 2 else "STOP"}}),
        "M1": tb.add_entity("DEVICE", "M1", "EnergyMeter",
                            ts={"energy_kwh": {"interval": 900, "gen": lambda t: f"{t / 3_600_000:.3f}"}}),
        "G1": tb.add_entity("DEVICE", "G1", "Gateway", ts={"ch2": {"interval": 60, "gen": lambda t: f"{(t // 60000) % 997}"}}),
    }
    ids["cust"] = c
    return ids


@pytest.fixture
def p2(conn, settings):
    tb = FakeTb("CE")
    ids = build_phase2_fleet(tb)
    w = build_worker(conn, settings, transport=tb.transport(), sleep=lambda s: None)
    w.run_until_idle()
    return tb, ids, w


def analysis(conn, eid, key):
    return conn.execute("SELECT * FROM key_analysis WHERE entity_id=? AND source='TS' AND key=?", (eid, key)).fetchone()


def open_checks(conn, eid, key=None):
    sql, args = "SELECT check_type FROM quality_finding WHERE entity_id=? AND resolved_ms IS NULL", [eid]
    if key:
        sql += " AND key=?"
        args.append(key)
    return {r[0] for r in conn.execute(sql, args)}


def test_semantics_and_types_end_to_end(conn, p2):
    tb, ids, _ = p2
    expect = {("S1", "temperature"): ("temperature", CONT, "AUTO"), ("S1", "humidity"): ("humidity", CONT, "AUTO"),
              ("S1", "battery"): ("battery", CONT, "AUTO"), ("P1", "current"): ("current", CONT, "AUTO"),
              ("P1", "status"): ("status", BINARY, "AUTO"), ("M1", "energy_kwh"): ("energy", COUNTER, "AUTO"),
              ("S1", "t1"): ("temperature", CONT, "REVIEW"), ("G1", "ch2"): ("unknown", CONT, "REVIEW")}
    for (dev, key), (sem, dt, status) in expect.items():
        a = analysis(conn, ids[dev], key)
        assert (a["semantic"], a["data_type"], a["status"]) == (sem, dt, status), (dev, key, dict(a))
    roles = {(r["scope"], r["key"]): r["role"] for r in conn.execute(
        "SELECT scope, key, role FROM key_analysis WHERE entity_id=? AND source='ATTR'", (ids["S1"],))}
    assert roles == {("SERVER_SCOPE", "location"): "location", ("CLIENT_SCOPE", "fw"): "firmware"}


def test_quality_findings_end_to_end(conn, p2):
    tb, ids, _ = p2
    assert "OUT_OF_RANGE" in open_checks(conn, ids["S1"], "humidity")   # 7일 범위로만 잡히는 드문 스파이크
    assert open_checks(conn, ids["S1"], "temperature") == set()
    assert "FLATLINE" in open_checks(conn, ids["S2"], "temperature")
    assert "STALE" in open_checks(conn, ids["S3"], "temperature")
    assert open_checks(conn, ids["P1"]) == set()
    s1 = conn.execute("SELECT score FROM quality_score WHERE entity_id=? AND key=''", (ids["S1"],)).fetchone()[0]
    assert open_checks(conn, ids["S1"], "battery") == {"CONSTANT"}            # 항상 97.5 → 참고(info)
    assert s1 == round((100 + 50 + 100 + 95) / 4)


def test_envelopes_are_single_interval_and_bounded(conn, p2):
    tb, ids, _ = p2
    assert tb.agg_requests, "범위 집계 요청이 있어야 함"
    assert {a for a, _ in tb.agg_requests} == {"MIN", "MAX", "COUNT"}
    env = conn.execute("SELECT * FROM key_envelope WHERE entity_id=? AND key='current'", (ids["P1"],)).fetchone()
    assert env["end_ts"] - env["start_ts"] <= 10_000 * 1000          # 1초 키: 원시값 1만 개 이내 구간
    env = conn.execute("SELECT * FROM key_envelope WHERE entity_id=? AND key='temperature'", (ids["S1"],)).fetchone()
    assert env["end_ts"] - env["start_ts"] == 10_000 * 60_000 or env["end_ts"] - env["start_ts"] == 7 * DAY
    # 문자열 키(status)는 범위 요청 대상 아님
    assert conn.execute("SELECT 1 FROM key_envelope WHERE key='status'").fetchone() is None


def test_daily_quality_check_resolves_findings(conn, p2):
    tb, ids, w = p2
    tb.entities[ids["S2"]]["ts"]["temperature"] = {"interval": 60, "gen": lambda t: f"{22 + math.sin(t / 3e6):.3f}"}
    conn.execute("UPDATE entity SET quality_checked_ms=?", (now_ms() - 2 * DAY,))
    q.enqueue(conn, "START_CYCLE", {}, q.PRIO_CYCLE, "cycle")
    w.run_until_idle()
    assert conn.execute("SELECT COUNT(*) FROM sync_task WHERE kind='QUALITY_CHECK' AND status='DONE'").fetchone()[0] >= 5
    assert "FLATLINE" not in open_checks(conn, ids["S2"], "temperature")
    resolved = conn.execute("SELECT resolved_ms FROM quality_finding WHERE entity_id=? AND check_type='FLATLINE'",
                            (ids["S2"],)).fetchone()[0]
    assert resolved is not None


# -- API -------------------------------------------------------------------------
@pytest.fixture
def api2(settings, conn, p2):
    create_user(conn, "analyst", "analyst-pass", "ANALYST")
    create_user(conn, "cust", "cust-a-pass", "CUSTOMER_VIEWER", [p2[1]["cust"]])
    return TestClient(create_app(settings))


def hdr(api, u, pw):
    return {"Authorization": "Bearer " + api.post("/api/auth/login", json={"login": u, "password": pw}).json()["token"]}


def test_catalog_review_and_confirm(api2, conn, p2):
    tb, ids, _ = p2
    h = hdr(api2, "analyst", "analyst-pass")
    keys = api2.get("/api/catalog/keys", headers=h).json()
    assert keys[0]["status"] == "REVIEW"  # 검토 대기가 먼저
    review = {(k["profile"], k["key"]) for k in keys if k["status"] == "REVIEW"}
    assert {("TempSensor", "t1"), ("Gateway", "ch2")} <= review
    temp = [k for k in keys if (k["profile"], k["key"]) == ("TempSensor", "temperature")][0]
    assert temp["entities"] == 3 and temp["semantic"] == "temperature"
    # t1 을 온도로, ch2 를 압력(kPa)으로 확정
    assert api2.post("/api/catalog/rules", headers=h, json={"profile": "TempSensor", "key": "t1",
                                                             "semantic": "temperature"}).json()["reanalyzed_entities"] == 1
    api2.post("/api/catalog/rules", headers=h, json={"key": "ch2", "semantic": "pressure", "unit": "kPa"})
    a = analysis(conn, ids["G1"], "ch2")
    assert (a["semantic"], a["unit"], a["status"]) == ("pressure", "kPa", "CONFIRMED")
    groups = {g["semantic"]: g for g in api2.get("/api/catalog/semantic-groups", headers=h).json()}
    assert {k["key"] for k in groups["temperature"]["keys"]} == {"temperature", "t1"}  # 이름이 달라도 같은 의미
    assert api2.post("/api/catalog/rules", headers=h, json={"key": "x", "semantic": "nope"}).status_code == 400
    rid = api2.get("/api/catalog/rules", headers=h).json()[0]["id"]
    api2.delete(f"/api/catalog/rules/{rid}", headers=h)
    assert analysis(conn, ids["G1"], "ch2")["status"] == "REVIEW"
    cust = hdr(api2, "cust", "cust-a-pass")
    assert api2.post("/api/catalog/rules", headers=cust, json={"key": "t1", "semantic": "temperature"}).status_code == 403


def test_quality_api_scoped(api2, p2):
    tb, ids, _ = p2
    h = hdr(api2, "analyst", "analyst-pass")
    summ = api2.get("/api/quality/summary", headers=h).json()
    assert summ["entities_scored"] == 6 and summ["worst"][0]["score"] <= 75
    checks = {c["check"] for c in summ["open_by_check"]}
    assert {"OUT_OF_RANGE", "FLATLINE", "STALE"} <= checks
    crit = api2.get("/api/quality/findings?severity=critical", headers=h).json()
    assert {f["entity_name"] for f in crit} >= {"S1", "S2", "S3"}
    cust = hdr(api2, "cust", "cust-a-pass")
    assert {f["entity_name"] for f in api2.get("/api/quality/findings", headers=cust).json()} == {"S1"}
    d = api2.get(f"/api/entities/{ids['S1']}", headers=h).json()
    hum = [k for k in d["timeseries_keys"] if k["key"] == "humidity"][0]
    assert hum["semantic"] == "humidity" and hum["quality_score"] == 50 and hum["findings"][0]["check"] == "OUT_OF_RANGE"
    assert d["attribute_roles"]["SERVER_SCOPE.location"]["role"] == "location"
