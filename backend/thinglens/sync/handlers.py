"""메타데이터 동기화 작업 핸들러 + 변경 이력 기록 ([I] 계획서 §13).

작업 흐름 (한 '동기화 주기'):
  START_CYCLE → SYNC_CUSTOMERS, SYNC_ENTITY_LIST(DEVICE/ASSET) → 엔티티별 SYNC_ENTITY_DETAIL
              → (키가 바뀌었거나 오래되면) PROFILE_KEYS → FINISH_CYCLE (삭제 판정)

변경 감지 지연 (저부하를 위한 절충)
  - 이름·라벨·프로파일·소유자, 추가·삭제 : 매 주기(기본 6시간) — 엔티티 목록만으로 확인
  - 관계·속성·텔레메트리 키           : 엔티티 버전이 바뀌었거나 상세 재확인 주기(기본 24시간) 경과 시
    (TB는 속성 변경·다른 엔티티의 관계 변경 시 버전을 올리지 않으므로)
  - 수동 '지금 동기화'(full)           : 모든 엔티티 상세를 즉시 재확인

변경 이력 규칙
  - 완료된 주기가 하나도 없으면(최초 적재) 변경으로 기록하지 않는다 (기준선)
  - 새로 생긴 엔티티는 ENTITY_ADDED 하나만 기록 (그 엔티티의 관계·속성·키는 기록하지 않음)
  - 디바이스 접속 상태(active)·CLIENT 속성 값처럼 수시로 바뀌는 값은 기록하지 않음
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..config import Settings
from ..db import Tx, dumps, now_ms
from ..scheduler import queue as q
from ..scheduler.planner import estimate_interval
from ..tb.adapter import DAY_MS, EntityRec, TbAdapter
from . import analysis as an
from ..tb.client import TbError

ENTITY_TYPES = ("DEVICE", "ASSET")
TRACKED_ENTITY_FIELDS = ("name", "label", "profile", "owner_id")
VALUE_TRACKED_SCOPES = ("SERVER_SCOPE", "SHARED_SCOPE")


@dataclass
class Ctx:
    conn: sqlite3.Connection
    adapter: TbAdapter
    settings: Settings


# --------------------------------------------------------------------------- #
# 공통
# --------------------------------------------------------------------------- #
def has_baseline(conn) -> bool:
    return conn.execute("SELECT 1 FROM sync_cycle WHERE status IN ('DONE','PARTIAL') LIMIT 1").fetchone() is not None


def record_change(conn, cycle_id, entity_id, entity_type, change_type, field=None, before=None, after=None):
    conn.execute(
        "INSERT INTO metadata_change (cycle_id, ts_ms, entity_id, entity_type, change_type, field, before_json, after_json)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (cycle_id, now_ms(), entity_id, entity_type, change_type, field,
         None if before is None else dumps(before), None if after is None else dumps(after)))


def _task_pending_for_cycle(conn, kinds, cycle_id) -> int:
    marks = ",".join("?" * len(kinds))
    return conn.execute(
        f"SELECT COUNT(*) FROM sync_task WHERE status IN ('PENDING','RUNNING') AND kind IN ({marks})"
        " AND json_extract(payload, '$.cycle_id') = ?", (*kinds, cycle_id)).fetchone()[0]


# --------------------------------------------------------------------------- #
# 주기 시작·종료
# --------------------------------------------------------------------------- #
def start_cycle(ctx: Ctx, p: dict) -> None:
    conn = ctx.conn
    full = bool(p.get("full"))
    cur = conn.execute("INSERT INTO sync_cycle (started_ms, status, full) VALUES (?, 'RUNNING', ?)",
                       (now_ms(), int(full)))
    cid = cur.lastrowid
    q.enqueue(conn, "SYNC_CUSTOMERS", {"cycle_id": cid}, q.PRIO_ENTITY_LIST, f"customers:{cid}")
    q.enqueue(conn, "SYNC_GROUPS", {"cycle_id": cid}, q.PRIO_ENTITY_DETAIL, f"groups:{cid}")
    for et in ENTITY_TYPES:
        q.enqueue(conn, "SYNC_ENTITY_LIST", {"cycle_id": cid, "entity_type": et, "full": full},
                  q.PRIO_ENTITY_LIST, f"list:{et}:{cid}")
    q.enqueue(conn, "FINISH_CYCLE", {"cycle_id": cid}, q.PRIO_FINISH, f"finish:{cid}")


def finish_cycle(ctx: Ctx, p: dict) -> str | None:
    """목록·상세 작업이 남아 있으면 'WAIT' 반환 (워커가 잠시 뒤 재시도)."""
    conn, cid = ctx.conn, p["cycle_id"]
    if _task_pending_for_cycle(conn, ("SYNC_ENTITY_LIST", "SYNC_ENTITY_DETAIL", "SYNC_CUSTOMERS"), cid):
        return "WAIT"
    cycle = conn.execute("SELECT * FROM sync_cycle WHERE id=?", (cid,)).fetchone()
    failed_list = conn.execute(
        "SELECT COUNT(*) FROM sync_task WHERE status='FAILED' AND kind='SYNC_ENTITY_LIST'"
        " AND json_extract(payload, '$.cycle_id') = ?", (cid,)).fetchone()[0]
    baseline = has_baseline(conn)
    with Tx(conn):
        status = "DONE"
        if failed_list:
            status = "PARTIAL"  # 목록을 다 못 읽었으면 삭제 판정을 하지 않음 (오탐 방지)
        else:
            gone = conn.execute("SELECT id, entity_type, name FROM entity WHERE deleted_ms IS NULL"
                                " AND (last_seen_ms IS NULL OR last_seen_ms < ?)", (cycle["started_ms"],)).fetchall()
            for e in gone:
                conn.execute("UPDATE entity SET deleted_ms=? WHERE id=?", (now_ms(), e["id"]))
                conn.execute("DELETE FROM relation WHERE from_id=? OR to_id=?", (e["id"], e["id"]))
                if baseline:
                    record_change(conn, cid, e["id"], e["entity_type"], "ENTITY_REMOVED", before={"name": e["name"]})
        due = now_ms() - int(ctx.settings.quality_interval_h * 3_600_000)
        for r in conn.execute("SELECT e.id FROM entity e WHERE e.deleted_ms IS NULL AND e.profile_synced_ms IS NOT NULL"
                              " AND (e.quality_checked_ms IS NULL OR e.quality_checked_ms < ?)", (due,)).fetchall():
            q.enqueue(conn, "QUALITY_CHECK", {"entity_id": r["id"]}, q.PRIO_QUALITY, f"quality:{r['id']}")
        n_changes = conn.execute("SELECT COUNT(*) FROM metadata_change WHERE cycle_id=?", (cid,)).fetchone()[0]
        conn.execute("UPDATE sync_cycle SET status=?, finished_ms=?, changes=? WHERE id=?",
                     (status, now_ms(), n_changes, cid))
    return None


# --------------------------------------------------------------------------- #
# 고객·엔티티 목록
# --------------------------------------------------------------------------- #
def sync_customers(ctx: Ctx, p: dict) -> None:
    t = now_ms()
    for c in ctx.adapter.iter_customers():
        ctx.conn.execute(
            "INSERT INTO customer (id, title, parent_id, last_seen_ms) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET title=excluded.title, parent_id=excluded.parent_id,"
            " last_seen_ms=excluded.last_seen_ms", (c["id"], c["title"], c["parent_id"], t))


def sync_groups(ctx: Ctx, p: dict) -> None:
    """[PE] 디바이스·에셋 그룹과 구성원. CE면 그룹 API가 없어 바로 종료."""
    conn, a = ctx.conn, ctx.adapter
    t = now_ms()
    for gt in ("DEVICE", "ASSET"):
        groups = a.entity_groups(gt)
        if groups is None:
            return
        members = {g["id"]: a.group_members(g["id"]) for g in groups if (g["name"] or "") != "All"}
        with Tx(conn):
            for g in groups:
                conn.execute("INSERT INTO entity_group (id, group_type, name, owner_id, last_seen_ms) VALUES (?,?,?,?,?)"
                             " ON CONFLICT(id) DO UPDATE SET name=excluded.name, owner_id=excluded.owner_id,"
                             " last_seen_ms=excluded.last_seen_ms", (g["id"], gt, g["name"], g["owner_id"], t))
            for gid, ids in members.items():
                conn.execute("DELETE FROM entity_group_member WHERE group_id=?", (gid,))
                conn.executemany("INSERT OR IGNORE INTO entity_group_member (group_id, entity_id) VALUES (?, ?)",
                                 [(gid, i) for i in ids])
            conn.execute("DELETE FROM entity_group_member WHERE group_id IN (SELECT id FROM entity_group"
                         " WHERE group_type=? AND last_seen_ms < ?)", (gt, t))
            conn.execute("DELETE FROM entity_group WHERE group_type=? AND last_seen_ms < ?", (gt, t))


def sync_entity_list(ctx: Ctx, p: dict) -> None:
    conn, cid, s = ctx.conn, p["cycle_id"], ctx.settings
    baseline = has_baseline(conn)
    detail_stale_ms = now_ms() - int(s.detail_sync_interval_h * 3_600_000)
    seen = 0
    for rec in ctx.adapter.iter_entities(p["entity_type"]):
        seen += 1
        need_detail = _upsert_entity(conn, cid, rec, baseline, detail_stale_ms)
        if need_detail or p.get("full"):
            q.enqueue(conn, "SYNC_ENTITY_DETAIL", {"cycle_id": cid, "entity_id": rec.id}, q.PRIO_ENTITY_DETAIL,
                      f"detail:{rec.id}")
    conn.execute("UPDATE sync_cycle SET entities_seen = entities_seen + ? WHERE id=?", (seen, cid))


def _upsert_entity(conn, cid, rec: EntityRec, baseline: bool, detail_stale_ms: int) -> bool:
    t = now_ms()
    old = conn.execute("SELECT * FROM entity WHERE id=?", (rec.id,)).fetchone()
    values = (rec.entity_type, rec.name, rec.label, rec.profile, rec.owner_id, rec.owner_name,
              None if rec.active is None else int(rec.active), rec.tb_version, rec.created_time)
    with Tx(conn):
        if old is None or old["deleted_ms"] is not None:
            if old is None:
                conn.execute(
                    "INSERT INTO entity (entity_type, name, label, profile, owner_id, owner_name, active, tb_version,"
                    " created_time, id, first_seen_ms, last_seen_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (*values, rec.id, t, t))
            else:
                conn.execute(
                    "UPDATE entity SET entity_type=?, name=?, label=?, profile=?, owner_id=?, owner_name=?, active=?,"
                    " tb_version=?, created_time=?, last_seen_ms=?, deleted_ms=NULL, detail_synced_ms=NULL WHERE id=?",
                    (*values, t, rec.id))
            if baseline:
                record_change(conn, cid, rec.id, rec.entity_type, "ENTITY_ADDED",
                              after={"name": rec.name, "profile": rec.profile, "owner_id": rec.owner_id})
            return True
        if baseline:
            new = {"name": rec.name, "label": rec.label, "profile": rec.profile, "owner_id": rec.owner_id}
            for f in TRACKED_ENTITY_FIELDS:
                if (old[f] or None) != (new[f] or None):
                    record_change(conn, cid, rec.id, rec.entity_type, "ENTITY_UPDATED", f, old[f], new[f])
        conn.execute(
            "UPDATE entity SET entity_type=?, name=?, label=?, profile=?, owner_id=?, owner_name=?, active=?,"
            " tb_version=?, created_time=?, last_seen_ms=? WHERE id=?", (*values, t, rec.id))
    version_changed = rec.tb_version is not None and rec.tb_version != old["tb_version"]
    stale = old["detail_synced_ms"] is None or old["detail_synced_ms"] < detail_stale_ms
    return version_changed or stale


# --------------------------------------------------------------------------- #
# 엔티티 상세: 관계·속성·텔레메트리 키
# --------------------------------------------------------------------------- #
def sync_entity_detail(ctx: Ctx, p: dict) -> None:
    conn, cid, a = ctx.conn, p.get("cycle_id"), ctx.adapter
    ent = conn.execute("SELECT * FROM entity WHERE id=?", (p["entity_id"],)).fetchone()
    if ent is None or ent["deleted_ms"] is not None:
        return
    et, eid = ent["entity_type"], ent["id"]
    try:
        rels = a.relations_from(et, eid)
        scopes = ("SERVER_SCOPE", "SHARED_SCOPE", "CLIENT_SCOPE") if et == "DEVICE" else ("SERVER_SCOPE",)
        attrs = {sc: a.attributes(et, eid, sc) for sc in scopes}
        keys = a.ts_keys(et, eid)
    except TbError as e:
        if e.status == 404:
            return  # 목록 조회 후 삭제됨 — 다음 주기 FINISH 에서 처리
        raise
    track = has_baseline(conn) and ent["detail_synced_ms"] is not None
    keys_added = False
    with Tx(conn):
        _diff_relations(conn, cid, ent, rels, track)
        for sc, rows in attrs.items():
            _diff_attributes(conn, cid, ent, sc, rows, track)
        keys_added = _diff_ts_keys(conn, cid, ent, keys, track)
        conn.execute("UPDATE entity SET detail_synced_ms=? WHERE id=?", (now_ms(), eid))
    stale_ms = now_ms() - int(ctx.settings.profile_interval_d * DAY_MS)
    if keys and (keys_added or ent["profile_synced_ms"] is None or ent["profile_synced_ms"] < stale_ms):
        q.enqueue(conn, "PROFILE_KEYS", {"entity_id": eid}, q.PRIO_PROFILE, f"profile:{eid}")
    an.analyze_entity(conn, eid)  # 속성 역할 갱신 (로컬 계산)


def _diff_relations(conn, cid, ent, rels, track):
    eid = ent["id"]
    old = {(r["to_id"], r["rel_type"], r["type_group"]): dict(r)
           for r in conn.execute("SELECT * FROM relation WHERE from_id=?", (eid,))}
    new = {(r["to_id"], r["rel_type"], r["type_group"]): r for r in rels}
    if track:
        for k in new.keys() - old.keys():
            r = new[k]
            record_change(conn, cid, eid, ent["entity_type"], "RELATION_ADDED", r["rel_type"],
                          after={"to_id": r["to_id"], "to_type": r["to_type"]})
        for k in old.keys() - new.keys():
            r = old[k]
            record_change(conn, cid, eid, ent["entity_type"], "RELATION_REMOVED", r["rel_type"],
                          before={"to_id": r["to_id"], "to_type": r["to_type"]})
    conn.execute("DELETE FROM relation WHERE from_id=?", (eid,))
    conn.executemany(
        "INSERT OR REPLACE INTO relation (from_id, from_type, to_id, to_type, rel_type, type_group)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [(r["from_id"], r["from_type"], r["to_id"], r["to_type"], r["rel_type"], r["type_group"]) for r in rels])


def _diff_attributes(conn, cid, ent, scope, rows, track):
    eid = ent["id"]
    old = {r["key"]: r["value_json"] for r in
           conn.execute("SELECT key, value_json FROM attribute WHERE entity_id=? AND scope=?", (eid, scope))}
    new = {r["key"]: dumps(r["value"]) for r in rows}
    if track:
        for k in new.keys() - old.keys():
            record_change(conn, cid, eid, ent["entity_type"], "ATTRIBUTE_ADDED", f"{scope}.{k}",
                          after=rows_value(rows, k))
        for k in old.keys() - new.keys():
            record_change(conn, cid, eid, ent["entity_type"], "ATTRIBUTE_REMOVED", f"{scope}.{k}",
                          before=_loads(old[k]))
        if scope in VALUE_TRACKED_SCOPES:
            for k in new.keys() & old.keys():
                if new[k] != old[k]:
                    record_change(conn, cid, eid, ent["entity_type"], "ATTRIBUTE_CHANGED", f"{scope}.{k}",
                                  _loads(old[k]), rows_value(rows, k))
    conn.execute("DELETE FROM attribute WHERE entity_id=? AND scope=?", (eid, scope))
    conn.executemany("INSERT INTO attribute (entity_id, scope, key, value_json, last_update_ts) VALUES (?,?,?,?,?)",
                     [(eid, scope, r["key"], dumps(r["value"]), r["last_update_ts"]) for r in rows])


def rows_value(rows, key):
    for r in rows:
        if r["key"] == key:
            return r["value"]
    return None


def _loads(s):
    import json
    return None if s is None else json.loads(s)


def _diff_ts_keys(conn, cid, ent, keys, track) -> bool:
    eid = ent["id"]
    old = {r["key"] for r in conn.execute("SELECT key FROM ts_key WHERE entity_id=?", (eid,))}
    new = set(keys)
    added, removed = new - old, old - new
    if track:
        for k in sorted(added):
            record_change(conn, cid, eid, ent["entity_type"], "TS_KEY_ADDED", k)
        for k in sorted(removed):
            record_change(conn, cid, eid, ent["entity_type"], "TS_KEY_REMOVED", k)
    t = now_ms()
    conn.executemany("INSERT INTO ts_key (entity_id, key, first_seen_ms) VALUES (?, ?, ?)",
                     [(eid, k, t) for k in added])
    if removed:
        conn.executemany("DELETE FROM ts_key WHERE entity_id=? AND key=?", [(eid, k) for k in removed])
        conn.executemany("DELETE FROM key_profile WHERE entity_id=? AND key=?", [(eid, k) for k in removed])
    return bool(added)


# --------------------------------------------------------------------------- #
# 키 분석 (주 1회·키 변경 시) / 품질 점검 (매일) — 계획서 §3.5, §5.0, §13 [B]
# --------------------------------------------------------------------------- #
def profile_keys(ctx: Ctx, p: dict) -> None:
    """최근·1일 전·7일 전 샘플 + 7일 범위(MIN/MAX/COUNT, 구간 1개) → 보고 주기·종류·의미·품질."""
    conn, a, s = ctx.conn, ctx.adapter, ctx.settings
    ent = conn.execute("SELECT * FROM entity WHERE id=?", (p["entity_id"],)).fetchone()
    if ent is None or ent["deleted_ms"] is not None:
        return
    keys = [r["key"] for r in conn.execute("SELECT key FROM ts_key WHERE entity_id=? ORDER BY key", (ent["id"],))]
    if not keys:
        return
    latest = an.fetch_latest(a, ent, keys, s.sample_points)
    intervals = {k: (estimate_interval([t for t, _ in pts]) or {}).get("interval_s") for k, pts in latest.items()}
    older = an.fetch_older_windows(a, ent, {k: iv for k, iv in intervals.items() if iv}, s.sample_points)
    numeric = an._numeric_keys(latest)
    env = an.fetch_envelopes(a, ent, {k: intervals[k] for k in numeric if intervals[k]}, 7 * DAY_MS)
    an.save_profile_and_samples(conn, ent, latest, older, env, "profile")
    t = now_ms()
    conn.execute("UPDATE entity SET profile_synced_ms=?, quality_checked_ms=? WHERE id=?", (t, t, ent["id"]))
    an.analyze_entity(conn, ent["id"])


def quality_check(ctx: Ctx, p: dict) -> None:
    """최근 샘플 + 24시간 범위만 새로 받아 품질 재점검."""
    conn, a, s = ctx.conn, ctx.adapter, ctx.settings
    ent = conn.execute("SELECT * FROM entity WHERE id=?", (p["entity_id"],)).fetchone()
    if ent is None or ent["deleted_ms"] is not None:
        return
    prof = {r["key"]: r["interval_s"] for r in conn.execute(
        "SELECT k.key, p.interval_s FROM ts_key k LEFT JOIN key_profile p ON p.entity_id=k.entity_id AND p.key=k.key"
        " WHERE k.entity_id=?", (ent["id"],))}
    if not prof:
        return
    latest = an.fetch_latest(a, ent, list(prof), s.sample_points)
    numeric = an._numeric_keys(latest)
    env = an.fetch_envelopes(a, ent, {k: prof[k] for k in numeric if prof.get(k)}, DAY_MS)
    an.save_profile_and_samples(conn, ent, latest, None, env, "daily")
    conn.execute("UPDATE entity SET quality_checked_ms=? WHERE id=?", (now_ms(), ent["id"]))
    an.analyze_entity(conn, ent["id"])


def analyze_all(ctx: Ctx, p: dict) -> None:
    """TB 요청 없이 전체 재분석 (사용자가 의미를 확정했을 때 등)."""
    an.analyze_all(ctx.conn)


HANDLERS = {
    "START_CYCLE": start_cycle,
    "SYNC_CUSTOMERS": sync_customers,
    "SYNC_GROUPS": sync_groups,
    "SYNC_ENTITY_LIST": sync_entity_list,
    "SYNC_ENTITY_DETAIL": sync_entity_detail,
    "PROFILE_KEYS": profile_keys,
    "QUALITY_CHECK": quality_check,
    "ANALYZE_ALL": analyze_all,
    "FINISH_CYCLE": finish_cycle,
}
