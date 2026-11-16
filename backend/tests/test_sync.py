import json

from thinglens.db import connect
from thinglens.scheduler import queue as q
from thinglens.scheduler.worker import build_worker

from fake_tb import FakeTb, build_fleet


def changes(conn, **filt):
    sql, args = "SELECT * FROM metadata_change WHERE 1=1", []
    for k, v in filt.items():
        sql += f" AND {k}=?"
        args.append(v)
    return [dict(r) for r in conn.execute(sql, args)]


def drain(worker, rounds=50):
    """서킷이 열리면 대기 시간이 지난 것으로 간주하고 끝까지 처리."""
    for _ in range(rounds):
        worker.run_until_idle()
        if not worker.client.limiter.circuit_open:
            if not worker.conn.execute("SELECT 1 FROM sync_task WHERE status='PENDING'").fetchone():
                return
        worker.client.limiter.open_until = 0
        worker.conn.execute("UPDATE sync_task SET next_run_ms=0 WHERE status='PENDING'")


def new_cycle(conn, worker):
    q.enqueue(conn, "START_CYCLE", {}, q.PRIO_CYCLE, "cycle")
    drain(worker)


def test_first_cycle_loads_everything_without_change_noise(conn, worker, fleet, tb):
    worker.run_until_idle()
    cyc = conn.execute("SELECT * FROM sync_cycle").fetchall()
    assert len(cyc) == 1 and cyc[0]["status"] == "DONE" and cyc[0]["entities_seen"] == 6
    ents = {r["name"]: r for r in conn.execute("SELECT * FROM entity")}
    assert set(ents) == {"Line-A", "Line-B", "TS-A1", "PUMP-A", "TS-B1", "ORPHAN"}
    assert ents["TS-A1"]["owner_id"] == fleet["cust_a"] and ents["TS-A1"]["owner_name"] == "Customer A"
    assert ents["ORPHAN"]["owner_id"] is None
    assert ents["TS-A1"]["profile"] == "TempSensor"
    # 하위 고객 계층
    assert conn.execute("SELECT parent_id FROM customer WHERE id=?", (fleet["cust_a_sub"],)).fetchone()[0] == fleet["cust_a"]
    # 관계·속성·키
    assert conn.execute("SELECT COUNT(*) FROM relation").fetchone()[0] == 3
    attrs = {(r["scope"], r["key"]): json.loads(r["value_json"])
             for r in conn.execute("SELECT * FROM attribute WHERE entity_id=?", (fleet["ts_a1"],))}
    assert attrs == {("SERVER_SCOPE", "location"): "Line-A", ("SHARED_SCOPE", "interval"): 60,
                     ("CLIENT_SCOPE", "fw"): "1.0"}
    # 보고 주기 등급
    tiers = {(r["entity_id"], r["key"]): r["tier"] for r in conn.execute("SELECT * FROM key_profile")}
    assert tiers[(fleet["pump_a"], "current")] == "high"
    assert tiers[(fleet["pump_a"], "dead")] == "inactive"
    assert tiers[(fleet["ts_a1"], "temperature")] == "mid"
    assert tiers[(fleet["ts_a1"], "battery")] == "low"
    assert tiers[(fleet["orphan"], "temperature")] == "low"
    # 최초 적재는 기준선: 변경 이력 없음
    assert changes(conn) == []
    # 읽기 전용: POST 는 로그인뿐 (CE 목록은 GET)
    assert {p for m, p in tb.requests if m == "POST"} == {"/api/auth/login"}


def test_second_cycle_records_changes(conn, worker, fleet, tb):
    worker.run_until_idle()
    tb.touch(fleet["ts_a1"], name="TS-A1-renamed", attrs={"SERVER_SCOPE": {"location": "Line-B"},
                                                         "SHARED_SCOPE": {}, "CLIENT_SCOPE": {"fw": "2.0"}})
    tb.entities[fleet["ts_a1"]]["ts"]["humidity"] = 60
    tb.relations = [r for r in tb.relations if r[2] != fleet["pump_a"]]
    tb.touch(fleet["line_a"])
    new_dev = tb.add_entity("DEVICE", "NEW-1", "TempSensor", owner=fleet["cust_b"], ts={"temperature": 60})
    del tb.entities[fleet["orphan"]]
    new_cycle(conn, worker)

    got = {(c["change_type"], c["field"]) for c in changes(conn, cycle_id=2)}
    assert ("ENTITY_UPDATED", "name") in got
    assert ("ATTRIBUTE_CHANGED", "SERVER_SCOPE.location") in got
    assert ("ATTRIBUTE_REMOVED", "SHARED_SCOPE.interval") in got
    assert ("TS_KEY_ADDED", "humidity") in got
    assert ("RELATION_REMOVED", "Contains") in got
    assert ("ENTITY_ADDED", None) in got
    assert ("ENTITY_REMOVED", None) in got
    # CLIENT 속성 값 변화(fw 1.0→2.0)는 기록하지 않음
    assert ("ATTRIBUTE_CHANGED", "CLIENT_SCOPE.fw") not in got
    # 새 엔티티의 세부(키 등)는 ENTITY_ADDED 외에 기록하지 않음
    assert not [c for c in changes(conn, cycle_id=2) if c["entity_id"] == new_dev and c["change_type"] != "ENTITY_ADDED"]
    removed = conn.execute("SELECT deleted_ms FROM entity WHERE id=?", (fleet["orphan"],)).fetchone()[0]
    assert removed is not None
    # 새 키도 주기 측정됨
    assert conn.execute("SELECT tier FROM key_profile WHERE entity_id=? AND key='humidity'",
                        (fleet["ts_a1"],)).fetchone()[0] == "mid"
    cyc = conn.execute("SELECT * FROM sync_cycle WHERE id=2").fetchone()
    assert cyc["status"] == "DONE" and cyc["changes"] == len(changes(conn, cycle_id=2))


def test_unchanged_entities_skip_detail_requests(conn, worker, fleet, tb):
    worker.run_until_idle()
    before = len(tb.requests)
    new_cycle(conn, worker)
    second = tb.requests[before:]
    # 변경 없음 + 상세 재확인 주기(24h) 이전 → 목록 조회만
    assert not any("/relations/" in p or "/values/attributes/" in p for _, p in second)
    assert changes(conn) == []


def test_failed_listing_never_marks_entities_deleted(conn, worker, fleet, tb):
    worker.run_until_idle()
    tb.fail["/api/tenant/deviceInfos"] = [500] * 30
    new_cycle(conn, worker)
    cyc = conn.execute("SELECT * FROM sync_cycle WHERE id=2").fetchone()
    assert cyc["status"] == "PARTIAL"
    assert conn.execute("SELECT COUNT(*) FROM entity WHERE deleted_ms IS NOT NULL").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM sync_task WHERE kind='SYNC_ENTITY_LIST' AND status='FAILED'").fetchone()


def test_transient_errors_retried_and_token_refreshed(conn, settings, fleet, tb):
    tb.fail["/api/relations/info"] = [503, 429]
    w = build_worker(conn, settings, transport=tb.transport(), sleep=lambda s: None)
    w.run_until_idle()
    assert conn.execute("SELECT status FROM sync_cycle").fetchone()[0] == "DONE"
    assert conn.execute("SELECT COUNT(*) FROM relation").fetchone()[0] == 3
    assert w.client.limiter.slowdowns >= 1
    tb.token_gen += 1  # 서버 측 토큰 만료
    new_cycle(conn, w)
    assert conn.execute("SELECT status FROM sync_cycle WHERE id=2").fetchone()[0] == "DONE"


def test_pause_stops_processing(conn, worker):
    conn.execute("UPDATE scheduler_state SET paused=1 WHERE id=1")
    assert worker.step() == "paused"
    assert conn.execute("SELECT COUNT(*) FROM sync_task").fetchone()[0] == 0
    conn.execute("UPDATE scheduler_state SET paused=0 WHERE id=1")
    assert worker.step() != "paused"


def test_circuit_breaker_defers_tasks(conn, settings, fleet, tb):
    tb.fail["/api/"] = [503] * 50  # TB 전체 장애 (로그인 포함)
    w = build_worker(conn, settings, transport=tb.transport(), sleep=lambda s: None)
    for _ in range(10):
        if w.step() == "circuit_open":
            break
    assert w.client.limiter.circuit_open
    st = conn.execute("SELECT circuit_open_until_ms FROM scheduler_state").fetchone()[0]
    assert st is not None
    assert w.step() == "circuit_open"


def test_request_log_and_heartbeat(conn, worker):
    worker.run_until_idle()
    n = conn.execute("SELECT COUNT(*) FROM tb_request_log").fetchone()[0]
    assert n > 10
    st = conn.execute("SELECT * FROM scheduler_state").fetchone()
    assert st["last_heartbeat_ms"] and st["current_rate"] > 0


def test_pe_edition_owner_and_endpoints(tmp_path, settings):
    tb = FakeTb("PE")
    ids = build_fleet(tb)
    conn = connect(str(tmp_path / "pe.db"))
    w = build_worker(conn, settings, transport=tb.transport(), sleep=lambda s: None)
    w.run_until_idle()
    row = conn.execute("SELECT owner_id, owner_name FROM entity WHERE id=?", (ids["pump_a"],)).fetchone()
    assert tuple(row) == (ids["cust_a_sub"], "Customer A-Sub")
    assert conn.execute("SELECT owner_id FROM entity WHERE id=?", (ids["orphan"],)).fetchone()[0] is None
    assert ("GET", "/api/deviceInfos/all") in tb.requests


def test_restart_resumes_pending_work(conn, settings, fleet, tb):
    w = build_worker(conn, settings, transport=tb.transport(), sleep=lambda s: None)
    for _ in range(4):
        w.step()  # 주기 중간에 '종료'
    conn.execute("UPDATE sync_task SET status='RUNNING' WHERE status='PENDING' AND id = (SELECT MIN(id) FROM sync_task WHERE status='PENDING')")
    w2 = build_worker(conn, settings, transport=tb.transport(), sleep=lambda s: None)  # 재시작
    w2.run_until_idle()
    assert conn.execute("SELECT status FROM sync_cycle").fetchone()[0] == "DONE"
    assert conn.execute("SELECT COUNT(*) FROM entity").fetchone()[0] == 6


def test_attribute_and_parent_relation_changes_need_full_or_stale_detail(conn, worker, fleet, tb):
    """TB는 속성 변경이나 부모 쪽 관계 변경 시 엔티티 버전을 올리지 않는다 (실제 TB 4.3.1에서 확인)."""
    worker.run_until_idle()
    tb.entities[fleet["ts_a1"]]["attrs"]["SERVER_SCOPE"]["location"] = "Line-X"   # 버전 변화 없음
    tb.relations = [r for r in tb.relations if r[2] != fleet["ts_b1"]]              # Line-B 쪽 관계 삭제
    new_cycle(conn, worker)                                                         # 일반 주기
    assert changes(conn, cycle_id=2) == []                                          # 24시간 재확인 전이므로 미감지
    q.enqueue(conn, "START_CYCLE", {"full": True}, q.PRIO_CYCLE, "cycle")           # 수동 '지금 동기화'
    drain(worker)
    got = {(c["change_type"], c["field"]) for c in changes(conn, cycle_id=3)}
    assert got == {("ATTRIBUTE_CHANGED", "SERVER_SCOPE.location"), ("RELATION_REMOVED", "Contains")}
    assert conn.execute("SELECT full FROM sync_cycle WHERE id=3").fetchone()[0] == 1
