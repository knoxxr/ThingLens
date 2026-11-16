import pytest
from fastapi.testclient import TestClient

from thinglens.api.app import create_app
from thinglens.auth.users import create_user
from thinglens.scheduler import queue as q


@pytest.fixture
def api(settings, conn, worker, fleet):
    worker.run_until_idle()
    create_user(conn, "admin", "admin-pass-1", "ADMIN")
    create_user(conn, "analyst", "analyst-pass", "ANALYST")
    create_user(conn, "cust-a", "cust-a-pass", "CUSTOMER_VIEWER", [fleet["cust_a"]])
    return TestClient(create_app(settings))


def login(api, user, pw):
    r = api.post("/api/auth/login", json={"login": user, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_login_required_and_bad_password(api, conn):
    assert api.get("/api/entities").status_code == 401
    assert api.post("/api/auth/login", json={"login": "admin", "password": "nope"}).status_code == 401
    assert conn.execute("SELECT action FROM audit_log ORDER BY id DESC").fetchone()[0] == "LOGIN_FAILED"


def test_admin_sees_all_entities(api):
    h = login(api, "admin", "admin-pass-1")
    body = api.get("/api/entities", headers=h).json()
    assert body["total"] == 6
    assert api.get("/api/entities?entity_type=ASSET", headers=h).json()["total"] == 2
    assert api.get("/api/entities?q=PUMP", headers=h).json()["items"][0]["name"] == "PUMP-A"


def test_customer_viewer_scope_includes_sub_customers_only(api, fleet):
    h = login(api, "cust-a", "cust-a-pass")
    names = {e["name"] for e in api.get("/api/entities", headers=h).json()["items"]}
    assert names == {"Line-A", "TS-A1", "PUMP-A"}  # Customer A + 하위 고객 A-Sub, B·테넌트 소유 제외
    assert api.get(f"/api/entities/{fleet['ts_b1']}", headers=h).status_code == 404
    detail = api.get(f"/api/entities/{fleet['line_a']}", headers=h).json()
    assert {r["to_name"] for r in detail["relations_out"]} == {"TS-A1", "PUMP-A"}
    assert api.get("/api/scheduler", headers=h).status_code == 403
    tiers = api.get("/api/catalog/tiers", headers=h).json()
    assert sum(tiers.values()) == 5  # TS-A1 2키 + PUMP-A 3키


def test_entity_detail_contents(api, fleet):
    h = login(api, "analyst", "analyst-pass")
    d = api.get(f"/api/entities/{fleet['pump_a']}", headers=h).json()
    keys = {k["key"]: k for k in d["timeseries_keys"]}
    assert keys["current"]["tier"] == "high" and keys["current"]["interval_s"] == 1.0
    assert d["relations_in"][0]["from_name"] == "Line-A"
    a1 = api.get(f"/api/entities/{fleet['ts_a1']}", headers=h).json()
    assert {"scope": "SERVER_SCOPE", "key": "location", "value": "Line-A"} in [
        {k: a[k] for k in ("scope", "key", "value")} for a in a1["attributes"]]


def test_scheduler_control_requires_admin_and_is_audited(api, conn):
    ha = login(api, "analyst", "analyst-pass")
    st = api.get("/api/scheduler", headers=ha).json()
    assert st["requests_15m"]["count"] > 0 and st["worker_alive"] is True
    assert st["cycles"][0]["status"] == "DONE"
    assert st["rate_max"] == 1000.0 and st["rate_windows"] == []   # 워커 설정 (API 설정 아님)
    assert st["worker_config"]["detail_sync_interval_h"] == 24.0
    assert api.post("/api/scheduler/pause", headers=ha).status_code == 403
    h = login(api, "admin", "admin-pass-1")
    assert api.post("/api/scheduler/pause", headers=h).json() == {"paused": True}
    assert conn.execute("SELECT paused, paused_by FROM scheduler_state").fetchone()[:] == (1, "admin")
    assert api.post("/api/scheduler/resume", headers=h).json() == {"paused": False}
    assert api.post("/api/scheduler/sync-now", headers=h).json() == {"queued": True}
    assert api.post("/api/scheduler/sync-now", headers=h).json() == {"queued": False}  # 중복 방지
    actions = [r["action"] for r in api.get("/api/audit", headers=h).json()]
    assert {"SCHEDULER_PAUSE", "SCHEDULER_RESUME", "SYNC_NOW"} <= set(actions)
    assert api.get("/api/audit", headers=ha).status_code == 403


def test_changes_scoped(api, conn, worker, fleet, tb):
    tb.touch(fleet["ts_a1"], label="lbl-a")
    tb.touch(fleet["ts_b1"], label="lbl-b")
    q.enqueue(conn, "START_CYCLE", {}, q.PRIO_CYCLE, "cycle")
    worker.run_until_idle()
    admin = login(api, "admin", "admin-pass-1")
    assert {c["entity_name"] for c in api.get("/api/changes", headers=admin).json()} == {"TS-A1", "TS-B1"}
    cust = login(api, "cust-a", "cust-a-pass")
    got = api.get("/api/changes", headers=cust).json()
    assert [(c["entity_name"], c["field"], c["after"]) for c in got] == [("TS-A1", "label", "lbl-a")]


def test_user_management(api, fleet):
    h = login(api, "admin", "admin-pass-1")
    r = api.post("/api/users", headers=h, json={"login": "v2", "password": "short", "role": "ANALYST"})
    assert r.status_code == 400
    r = api.post("/api/users", headers=h, json={"login": "v2", "password": "long-enough", "role": "CUSTOMER_VIEWER",
                                                 "customer_ids": [fleet["cust_b"]]})
    assert r.status_code == 200
    hv = login(api, "v2", "long-enough")
    assert {e["name"] for e in api.get("/api/entities", headers=hv).json()["items"]} == {"Line-B", "TS-B1"}
    api.post("/api/auth/logout", headers=hv)
    assert api.get("/api/me", headers=hv).status_code == 401


def test_metrics_and_customers(api, fleet):
    h = login(api, "admin", "admin-pass-1")
    m = api.get("/api/scheduler/metrics?minutes=10", headers=h).json()
    assert len(m) == 10 and sum(b["requests"] for b in m) > 0
    titles = {c["title"]: c["entities"] for c in api.get("/api/customers", headers=h).json()}
    assert titles == {"Customer A": 2, "Customer A-Sub": 1, "Customer B": 2}
    hc = login(api, "cust-a", "cust-a-pass")
    assert {c["title"] for c in api.get("/api/customers", headers=hc).json()} == {"Customer A", "Customer A-Sub"}


def test_serves_web_app_with_spa_fallback(settings, conn, tmp_path):
    web = tmp_path / "dist"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<html>app</html>")
    (web / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("nope")
    settings.web_dir = str(web)
    c = TestClient(create_app(settings))
    assert c.get("/").text == "<html>app</html>"
    assert c.get("/entities/abc").text == "<html>app</html>"     # SPA 경로
    assert c.get("/assets/app.js").text == "console.log(1)"
    assert "nope" not in c.get("/../secret.txt").text            # 디렉터리 탈출 차단
    assert c.get("/api/unknown").status_code == 404


def test_entity_list_counts_and_facets(api):
    h = login(api, "admin", "admin-pass-1")
    items = {e["name"]: e for e in api.get("/api/entities", headers=h).json()["items"]}
    assert items["ORPHAN"]["relation_count"] == 0 and items["PUMP-A"]["key_count"] == 3
    assert items["Line-A"]["relation_count"] == 2
    facets = {(f["entity_type"], f["profile"]): f["n"] for f in api.get("/api/entities-facets", headers=h).json()}
    assert facets[("DEVICE", "TempSensor")] == 3 and facets[("ASSET", "Line")] == 2


def test_relation_change_includes_target_name(api, conn, worker, fleet, tb):
    tb.relations = [r for r in tb.relations if r[2] != fleet["pump_a"]]
    q.enqueue(conn, "START_CYCLE", {"full": True}, q.PRIO_CYCLE, "cycle")
    worker.run_until_idle()
    h = login(api, "admin", "admin-pass-1")
    rel = [c for c in api.get("/api/changes?change_type=RELATION_REMOVED", headers=h).json()]
    assert rel[0]["before"]["to_name"] == "PUMP-A" and rel[0]["entity_name"] == "Line-A"
