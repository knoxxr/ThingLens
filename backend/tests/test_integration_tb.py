"""실제 테스트 ThingsBoard(deploy/test-env + tools/seed_thingsboard.py 시드)를 대상으로 한 통합 테스트.

실행:
  THINGLENS_IT_TB_URL=http://localhost:8080 pytest -m integration
"""

import os
import time

import pytest

from thinglens.db import connect
from thinglens.scheduler.worker import build_worker

TB_URL = os.environ.get("THINGLENS_IT_TB_URL")
pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not TB_URL, reason="THINGLENS_IT_TB_URL 미설정 (테스트 TB 필요)")]


def test_full_cycle_against_real_tb(tmp_path, settings):
    settings.tb_url = TB_URL
    settings.tb_username = os.environ.get("THINGLENS_IT_TB_USER", "tenant@thingsboard.org")
    settings.tb_password = os.environ.get("THINGLENS_IT_TB_PASSWORD", "tenant")
    settings.rate_max = 10.0
    settings.page_size = 100
    conn = connect(str(tmp_path / "it.db"))
    w = build_worker(conn, settings)
    t0 = time.monotonic()
    w.run_until_idle()
    elapsed = time.monotonic() - t0

    cyc = conn.execute("SELECT * FROM sync_cycle").fetchone()
    assert cyc["status"] == "DONE"
    ent = {r["name"]: r for r in conn.execute("SELECT * FROM entity WHERE deleted_ms IS NULL")}
    for name in ("Plant-1", "Line-A", "Line-B", "TS-A1", "PUMP-A1", "METER-A1", "TS-X1", "OLD-1"):
        assert name in ent, name

    def tier(dev, key):
        r = conn.execute("SELECT tier, interval_s FROM key_profile WHERE entity_id=? AND key=?",
                         (ent[dev]["id"], key)).fetchone()
        return r["tier"] if r else None

    assert tier("PUMP-A1", "current") == "high"
    assert tier("PUMP-A1", "p1_out") == "high"
    assert tier("PUMP-A1", "status") == "low"         # 불규칙 보고
    assert tier("TS-A1", "temperature") == "mid"
    assert tier("TS-A2", "temp") == "mid"             # 이름 변형 키
    assert tier("METER-A1", "energy_kwh") == "low"
    assert tier("OLD-1", "temperature") == "inactive"

    contains = {(r["from_id"], r["to_id"]) for r in conn.execute("SELECT * FROM relation WHERE rel_type='Contains'")}
    assert (ent["Line-A"]["id"], ent["TS-A1"]["id"]) in contains
    assert (ent["Plant-1"]["id"], ent["Line-A"]["id"]) in contains
    assert not [p for p in contains if ent["TS-X1"]["id"] in p]  # 누락 관계 시나리오
    loc = conn.execute("SELECT value_json FROM attribute WHERE entity_id=? AND scope='SERVER_SCOPE' AND key='location'",
                       (ent["TS-A5"]["id"],)).fetchone()[0]
    assert loc == '"Line-B"'                          # 속성 불일치 시나리오 원천 데이터

    # 시계열은 원시값(samples)과 구간 1개짜리 범위 집계(envelope)만 — 요청 로그 카테고리로 확인
    cats = {r[0] for r in conn.execute("SELECT DISTINCT category FROM tb_request_log")}
    assert cats <= {"auth", "entity_list", "relations", "attributes", "keys", "samples", "envelope"}
    n_req = conn.execute("SELECT COUNT(*) FROM tb_request_log").fetchone()[0]
    print(f"\n엔티티 {len(ent)}개, 요청 {n_req}건, {elapsed:.1f}초")


# 시드 키의 정답 (tools/seed_thingsboard.py 시나리오)
SEED_TRUTH = {
    ("TempSensor", "temperature"): ("temperature", "continuous"),
    ("TempSensor", "temp"): ("temperature", "continuous"),
    ("TempSensor", "Temperature_C"): ("temperature", "continuous"),
    ("TempSensor", "humidity"): ("humidity", "continuous"),
    ("TempSensor", "battery"): ("battery", "continuous"),
    ("PumpController", "current"): ("current", "continuous"),
    ("PumpController", "status"): ("status", "binary"),
    ("EnergyMeter", "power_kw"): ("power", "continuous"),
    ("EnergyMeter", "energy_kwh"): ("energy", "counter"),
}
UNCLEAR_TRUTH = {("PumpController", "p1_out"): "pressure"}  # 이름만으로는 불명확한 키


def test_phase2_semantics_and_quality_against_real_tb(tmp_path, settings):
    settings.tb_url = TB_URL
    settings.tb_username = os.environ.get("THINGLENS_IT_TB_USER", "tenant@thingsboard.org")
    settings.tb_password = os.environ.get("THINGLENS_IT_TB_PASSWORD", "tenant")
    settings.rate_max = 10.0
    settings.page_size = 100
    conn = connect(str(tmp_path / "it2.db"))
    w = build_worker(conn, settings)
    t0 = time.monotonic()
    w.run_until_idle()
    elapsed = time.monotonic() - t0
    rows = conn.execute("SELECT e.name, e.profile, a.key, a.data_type, a.semantic, a.status, a.confidence"
                        " FROM key_analysis a JOIN entity e ON e.id=a.entity_id WHERE a.source='TS'").fetchall()
    typed = sem_ok = n = 0
    misses = []
    for r in rows:
        truth = SEED_TRUTH.get((r["profile"], r["key"]))
        if not truth or r["name"] == "OLD-1":
            continue
        n += 1
        typed += r["data_type"] == truth[1]
        ok = r["semantic"] == truth[0] and r["status"] in ("AUTO", "CONFIRMED")
        sem_ok += ok
        if not ok or r["data_type"] != truth[1]:
            misses.append(dict(r))
    print(f"\n키 {n}개: 종류 정확도 {typed / n:.0%}, 의미(자동 판정) 정확도 {sem_ok / n:.0%}, {elapsed:.1f}초")
    for m in misses:
        print("  불일치:", m)
    assert typed / n >= 0.95
    assert sem_ok / n >= 0.85
    unclear = conn.execute("SELECT semantic, status FROM key_analysis WHERE key='p1_out'").fetchall()
    assert all(u["semantic"] == "pressure" and u["status"] == "REVIEW" for u in unclear)  # 후보 제시 + 검토 대기

    def checks(name, key):
        return {r[0] for r in conn.execute(
            "SELECT f.check_type FROM quality_finding f JOIN entity e ON e.id=f.entity_id"
            " WHERE e.name=? AND f.key=? AND f.resolved_ms IS NULL", (name, key))}
    assert "FLATLINE" in checks("TS-A4", "temperature")          # 값 고정 센서
    assert "OUT_OF_RANGE" in checks("TS-B2", "humidity")        # 7일 중 15번뿐인 130% 스파이크
    assert "NO_DATA" in checks("OLD-1", "temperature")          # 40일 전 값 1개
    assert checks("TS-A1", "temperature") == set()              # 정상 센서는 오탐 없음
    agg_reqs = conn.execute("SELECT COUNT(*) FROM tb_request_log WHERE category='envelope'").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM tb_request_log").fetchone()[0]
    print(f"  TB 요청 {total}건 (범위 집계 {agg_reqs}건)")
