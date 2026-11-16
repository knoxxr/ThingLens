"""Phase 3: 관계 구조 분석 + 엔티티 360°."""

import pytest
from fastapi.testclient import TestClient

from thinglens import topology as tm
from thinglens.api.app import create_app
from thinglens.auth.users import create_user
from thinglens.db import connect
from thinglens.scheduler.worker import build_worker

from fake_tb import FakeTb, build_fleet


def ent(eid, et="DEVICE", owner=None, profile="P"):
    return {"id": eid, "entity_type": et, "name": eid, "profile": profile, "owner_id": owner, "owner_name": owner}


@pytest.fixture
def sample():
    nodes = {n["id"]: n for n in [
        ent("Site", "ASSET", "c1"), ent("Line", "ASSET", "c1"), ent("Empty", "ASSET"),
        ent("D1", owner="c1"), ent("D2", owner="c1sub"), ent("D3", owner="c2"), ent("D4"), ent("D5", owner="c1"),
        ent("D6", owner="c1"), ent("X1", "ASSET"), ent("X2", "ASSET"),
        *[ent(f"L{i}", "ASSET") for i in range(9)],
    ]}
    edges = [("Site", "Line", "Contains"), ("Line", "D1", "Contains"), ("Line", "D2", "Contains"),
             ("Line", "D3", "Contains"), ("Site", "D5", "Manages"), ("Site", "D6", "Contains"),
             ("Line", "D6", "Contains"), ("X1", "X2", "Contains"), ("X2", "X1", "Contains")]
    edges += [(f"L{i}", f"L{i + 1}", "Contains") for i in range(8)]  # 깊이 8 사슬
    return tm.Topology(nodes, edges, {"Contains"})


def test_topology_issues(sample):
    issues = tm.analyze(sample, customer_parent={"c1sub": "c1"},
                        groups_of={"D1": {"G"}, "D2": {"G"}, "D3": set(), "D6": {"G"}}, max_depth=6)
    got = {(i.type, i.entity_id) for i in issues}
    assert ("NO_RELATION", "D4") in got
    assert ("OUTSIDE_HIERARCHY", "D5") in got                   # Manages 관계만 있음
    assert ("MULTI_PARENT", "D6") in got
    assert {("CYCLE", "X1"), ("CYCLE", "X2")} <= got
    assert ("EMPTY_ASSET", "Empty") in got
    assert ("TOO_DEEP", "L7") in got and ("TOO_DEEP", "L8") in got and ("TOO_DEEP", "L6") not in got
    assert ("OWNER_MISMATCH", "D3") in got                      # c1 아래에 c2 소유
    assert ("OWNER_MISMATCH", "D2") not in got                  # 하위 고객은 정상
    assert ("GROUP_MISMATCH", "D3") in got                      # 형제 3/4가 그룹 G
    sev = {(i.type, i.entity_id): i.severity for i in issues}
    assert sev[("CYCLE", "X1")] == "critical" and sev[("OWNER_MISMATCH", "D3")] == "warning"
    assert {i.type for i in issues if i.entity_id == "D1"} == set()  # 정상 디바이스는 이슈 없음


def test_topology_navigation_and_stats(sample):
    assert sample.path_to_root("D1") == ["Site", "Line"]
    assert sample.descendants("Site") == {"Line", "D1", "D2", "D3", "D6"}
    assert "Site" in sample.roots and "X1" not in sample.roots   # 순환은 루트 없음
    st = tm.stats(sample)
    assert st["relation_types"] == {"Contains": 16, "Manages": 1}
    assert st["max_depth"] == 8 and st["components"] == 3
    assert sample.path_to_root("X1") == ["X2"]                    # 순환에서도 멈춤


@pytest.fixture
def api3(settings, conn, worker, fleet):
    worker.run_until_idle()
    create_user(conn, "admin", "admin-pass-1", "ADMIN")
    create_user(conn, "cust-a", "cust-a-pass", "CUSTOMER_VIEWER", [fleet["cust_a"]])
    return TestClient(create_app(settings))


def hdr(api, u, pw):
    return {"Authorization": "Bearer " + api.post("/api/auth/login", json={"login": u, "password": pw}).json()["token"]}


def test_topology_api_and_scope(api3, fleet):
    h = hdr(api3, "admin", "admin-pass-1")
    t = api3.get("/api/topology", headers=h).json()
    assert len(t["nodes"]) == 6 and len(t["edges"]) == 3
    types = {(i["type"], i["entity_name"]) for i in t["issues"]}
    assert ("NO_RELATION", "ORPHAN") in types
    assert ("OWNER_MISMATCH", "PUMP-A") not in types              # A-Sub 는 A 의 하위 고객
    assert t["stats"]["roots"] == 2
    tree = api3.get("/api/topology/tree", headers=h).json()
    assert {r["name"] for r in tree["roots"]} == {"Line-A", "Line-B"}
    assert [o["name"] for o in tree["outside"]] == ["ORPHAN"]
    c = hdr(api3, "cust-a", "cust-a-pass")
    tc = api3.get("/api/topology", headers=c).json()
    assert {n["name"] for n in tc["nodes"]} == {"Line-A", "TS-A1", "PUMP-A"}


def test_entity_overview_360(api3, fleet):
    h = hdr(api3, "admin", "admin-pass-1")
    o = api3.get(f"/api/entities/{fleet['ts_a1']}/overview", headers=h).json()
    assert [p["name"] for p in o["path"]] == ["Line-A"]
    assert [s["name"] for s in o["siblings"]] == ["PUMP-A"]
    assert o["summary"][0].startswith("Line-A 아래의")
    line = api3.get(f"/api/entities/{fleet['line_a']}/overview", headers=h).json()
    assert line["descendants"] == 2 and line["children_by_profile"] == {"DEVICE:TempSensor": 1, "DEVICE:Pump": 1}
    orphan = api3.get(f"/api/entities/{fleet['orphan']}/overview", headers=h).json()
    assert orphan["issues"][0]["type"] == "NO_RELATION" and "계층에도 속하지 않은" in orphan["summary"][0]
    c = hdr(api3, "cust-a", "cust-a-pass")
    assert api3.get(f"/api/entities/{fleet['ts_b1']}/overview", headers=c).status_code == 404


def test_pe_groups_sync_and_group_mismatch(tmp_path, settings):
    tb = FakeTb("PE")
    ids = build_fleet(tb)
    extra = [tb.add_entity("DEVICE", f"TS-A{i}", "TempSensor", ts={"temperature": 60}) for i in (2, 3, 4)]
    for e in extra:
        tb.relate(ids["line_a"], e)
    tb.add_group("DEVICE", "All", list(tb.entities))
    tb.add_group("DEVICE", "LineA-sensors", [ids["ts_a1"], ids["pump_a"], extra[0], extra[1]])  # extra[2] 만 빠짐
    conn = connect(str(tmp_path / "pe3.db"))
    settings.db_path = str(tmp_path / "pe3.db")
    build_worker(conn, settings, transport=tb.transport(), sleep=lambda s: None).run_until_idle()
    assert conn.execute("SELECT COUNT(*) FROM entity_group").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM entity_group_member").fetchone()[0] == 4  # 'All' 은 구성원 미수집
    create_user(conn, "admin", "admin-pass-1", "ADMIN")
    api = TestClient(create_app(settings))
    h = hdr(api, "admin", "admin-pass-1")
    t = api.get("/api/topology", headers=h).json()
    gm = [i for i in t["issues"] if i["type"] == "GROUP_MISMATCH"]
    assert [i["entity_name"] for i in gm] == ["TS-A4"]
    o = api.get(f"/api/entities/{ids['ts_a1']}/overview", headers=h).json()
    assert o["groups"] == ["LineA-sensors"]


def test_ce_groups_skipped_quickly(conn, worker, tb):
    worker.run_until_idle()
    group_reqs = [p for _, p in tb.requests if "entityGroup" in p]
    assert group_reqs == ["/api/entityGroups/DEVICE"]  # CE: 404 한 번 후 중단
