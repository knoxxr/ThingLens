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

    # 서버 집계 요청이 없어야 함 (원시값만) — 요청 로그 카테고리로 확인
    cats = {r[0] for r in conn.execute("SELECT DISTINCT category FROM tb_request_log")}
    assert cats <= {"auth", "entity_list", "relations", "attributes", "keys", "samples"}
    n_req = conn.execute("SELECT COUNT(*) FROM tb_request_log").fetchone()[0]
    print(f"\n엔티티 {len(ent)}개, 요청 {n_req}건, {elapsed:.1f}초")
