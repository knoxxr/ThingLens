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
from ..scheduler.planner import classify_tier, estimate_interval
from ..tb.adapter import DAY_MS, EntityRec, TbAdapter
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
# 키 보고 주기 측정 (계획서 §3.5 ①)
# --------------------------------------------------------------------------- #
def profile_keys(ctx: Ctx, p: dict) -> None:
    conn, a, s = ctx.conn, ctx.adapter, ctx.settings
    ent = conn.execute("SELECT * FROM entity WHERE id=?", (p["entity_id"],)).fetchone()
    if ent is None or ent["deleted_ms"] is not None:
        return
    keys = [r["key"] for r in conn.execute("SELECT key FROM ts_key WHERE entity_id=? ORDER BY key", (ent["id"],))]
    results: dict[str, dict] = {}
    pending = keys
    for lookback_days in (1, 30):  # Cassandra 파티션 스캔을 줄이려 짧은 구간부터
        if not pending:
            break
        short = []
        for i in range(0, len(pending), 20):
            batch = pending[i:i + 20]
            end = now_ms()
            data = a.ts_raw(ent["entity_type"], ent["id"], batch, end - lookback_days * DAY_MS, end, s.sample_points)
            for k in batch:
                pts = data.get(k) or []
                info = estimate_interval([pt["ts"] for pt in pts if "ts" in pt])
                results[k] = {"info": info, "points": len(pts),
                              "last_ts": max((pt["ts"] for pt in pts if "ts" in pt), default=None),
                              "value_kind": _value_kind(pts)}
                if info is None:
                    short.append(k)
        pending = short
    t = now_ms()
    with Tx(conn):
        for k, r in results.items():
            info = r["info"]
            conn.execute(
                "INSERT INTO key_profile (entity_id, key, interval_s, irregular, tier, points, last_ts, value_kind,"
                " profiled_ms) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(entity_id, key) DO UPDATE SET"
                " interval_s=excluded.interval_s, irregular=excluded.irregular, tier=excluded.tier,"
                " points=excluded.points, last_ts=excluded.last_ts, value_kind=excluded.value_kind,"
                " profiled_ms=excluded.profiled_ms",
                (ent["id"], k, info["interval_s"] if info else None, int(info["irregular"]) if info else None,
                 classify_tier(info), r["points"], r["last_ts"], r["value_kind"], t))
        conn.execute("UPDATE entity SET profile_synced_ms=? WHERE id=?", (t, ent["id"]))


def _value_kind(points) -> str | None:
    kinds: dict[str, int] = {}
    for pt in points:
        v = pt.get("value")
        if isinstance(v, bool):
            k = "boolean"
        elif isinstance(v, (int, float)):
            k = "number"
        elif isinstance(v, str):
            sv = v.strip()
            if sv.lower() in ("true", "false"):
                k = "boolean"
            elif sv[:1] in ("{", "["):
                k = "json"
            else:
                try:
                    float(sv)
                    k = "number"
                except ValueError:
                    k = "string"
        else:
            continue
        kinds[k] = kinds.get(k, 0) + 1
    return max(kinds, key=kinds.get) if kinds else None


HANDLERS = {
    "START_CYCLE": start_cycle,
    "SYNC_CUSTOMERS": sync_customers,
    "SYNC_ENTITY_LIST": sync_entity_list,
    "SYNC_ENTITY_DETAIL": sync_entity_detail,
    "PROFILE_KEYS": profile_keys,
    "FINISH_CYCLE": finish_cycle,
}
