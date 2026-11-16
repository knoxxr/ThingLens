"""REST API (Phase 1): 로그인, 스케줄러 상태·제어, 엔티티·변경 이력 조회 (고객 범위 적용)."""


import json
import os
import statistics
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..auth import users
from ..config import Settings
from ..db import connect, loads, now_ms
from ..quality.checks import CHECK_KO
from ..scheduler import queue as q
from ..semantics.dictionary import ATTRIBUTE_ROLES, SEMANTICS
from ..semantics.infer import DATA_TYPE_KO
from ..sync.analysis import analyze_entity


class LoginReq(BaseModel):
    login: str
    password: str


class RuleReq(BaseModel):
    profile: str | None = None      # None = 모든 프로파일의 같은 키 이름
    key: str
    semantic: str                   # SEMANTICS 의 id 또는 'unknown'(의미 없음 확정)
    unit: str | None = None


class UserCreateReq(BaseModel):
    login: str
    password: str
    role: str
    customer_ids: list[str] = []


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="ThingLens", version="0.1.0")

    def db():
        conn = connect(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    Db = Annotated[object, Depends(db)]

    def current_user(conn: Db, authorization: Annotated[str | None, Header()] = None):
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "로그인이 필요합니다")
        u = users.user_for_token(conn, authorization[7:])
        if u is None:
            raise HTTPException(401, "세션이 만료되었습니다")
        return u

    User = Annotated[object, Depends(current_user)]

    def require(user, *roles):
        if user["role"] not in roles:
            raise HTTPException(403, "권한이 없습니다")

    def scope_sql(conn, user, alias="e"):
        scope = users.customer_scope(conn, user)
        if scope is None:
            return "1=1", []
        if not scope:
            return "0=1", []
        return f"{alias}.owner_id IN ({','.join('?' * len(scope))})", sorted(scope)

    # -- 인증 ---------------------------------------------------------------
    @app.post("/api/auth/login")
    def login(req: LoginReq, conn: Db):
        token = users.login(conn, req.login, req.password, settings.session_ttl_h)
        if token is None:
            raise HTTPException(401, "아이디 또는 비밀번호가 올바르지 않습니다")
        u = conn.execute("SELECT * FROM app_user WHERE login=?", (req.login,)).fetchone()
        return {"token": token, "user": _user_out(u)}

    @app.post("/api/auth/logout")
    def logout(conn: Db, user: User, authorization: Annotated[str, Header()]):
        users.logout(conn, authorization[7:])
        return {"ok": True}

    @app.get("/api/me")
    def me(user: User):
        return _user_out(user)

    # -- 사용자 관리 (ADMIN) ------------------------------------------------------
    @app.get("/api/users")
    def list_users(conn: Db, user: User):
        require(user, "ADMIN")
        return [_user_out(u) for u in conn.execute("SELECT * FROM app_user ORDER BY id")]

    @app.post("/api/users")
    def add_user(req: UserCreateReq, conn: Db, user: User):
        require(user, "ADMIN")
        try:
            uid = users.create_user(conn, req.login, req.password, req.role, req.customer_ids)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception:
            raise HTTPException(409, "이미 존재하는 아이디입니다")
        users.audit(conn, user["id"], user["login"], "USER_CREATE", req.login,
                    {"role": req.role, "customer_ids": req.customer_ids})
        return _user_out(conn.execute("SELECT * FROM app_user WHERE id=?", (uid,)).fetchone())

    @app.get("/api/audit")
    def audit_log(conn: Db, user: User, limit: int = Query(100, le=1000)):
        require(user, "ADMIN")
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r, detail=loads(r["detail_json"])) for r in rows]

    # -- 스케줄러 -----------------------------------------------------------------
    @app.get("/api/scheduler")
    def scheduler_status(conn: Db, user: User):
        require(user, "ADMIN", "ANALYST")
        st = dict(conn.execute("SELECT * FROM scheduler_state WHERE id=1").fetchone())
        hb = st.get("last_heartbeat_ms")
        st["worker_alive"] = bool(hb and now_ms() - hb < 60_000)
        since = now_ms() - 15 * 60_000
        reqs = conn.execute("SELECT latency_ms, status FROM tb_request_log WHERE ts_ms >= ?", (since,)).fetchall()
        lat = sorted(r["latency_ms"] for r in reqs)
        st["requests_15m"] = {
            "count": len(reqs),
            "per_sec": round(len(reqs) / 900, 3),
            "p50_ms": round(statistics.median(lat)) if lat else None,
            "p95_ms": round(lat[max(0, int(len(lat) * 0.95) - 1)]) if lat else None,
            "load_errors": sum(1 for r in reqs if r["status"] is None or r["status"] == 429 or r["status"] >= 500),
        }
        st["queue"] = q.counts(conn)
        st["pending"] = q.pending_count(conn)
        st["cycles"] = [dict(r) for r in conn.execute("SELECT * FROM sync_cycle ORDER BY id DESC LIMIT 5")]
        cfg = loads(st.pop("worker_config", None)) or {}   # 워커가 실제 적용 중인 설정
        st["worker_config"] = cfg
        st["rate_max"] = cfg.get("rate_max")
        st["rate_windows"] = cfg.get("rate_windows", [])
        return st

    def _set_paused(conn, user, paused: bool):
        conn.execute("UPDATE scheduler_state SET paused=?, paused_by=?, paused_ms=? WHERE id=1",
                     (int(paused), user["login"] if paused else None, now_ms() if paused else None))
        users.audit(conn, user["id"], user["login"], "SCHEDULER_PAUSE" if paused else "SCHEDULER_RESUME")

    @app.post("/api/scheduler/pause")
    def pause(conn: Db, user: User):
        require(user, "ADMIN")
        _set_paused(conn, user, True)
        return {"paused": True}

    @app.post("/api/scheduler/resume")
    def resume(conn: Db, user: User):
        require(user, "ADMIN")
        _set_paused(conn, user, False)
        return {"paused": False}

    @app.post("/api/scheduler/sync-now")
    def sync_now(conn: Db, user: User, full: bool = True):
        """full=True(기본): 관계·속성·키까지 모든 엔티티를 즉시 재확인."""
        require(user, "ADMIN")
        if conn.execute("SELECT 1 FROM sync_cycle WHERE status='RUNNING'").fetchone():
            raise HTTPException(409, "이미 동기화 주기가 진행 중입니다")
        tid = q.enqueue(conn, "START_CYCLE", {"full": full}, q.PRIO_CYCLE, "cycle")
        users.audit(conn, user["id"], user["login"], "SYNC_NOW", detail={"full": full})
        return {"queued": tid is not None}

    # -- 엔티티 ------------------------------------------------------------------
    @app.get("/api/entities")
    def list_entities(conn: Db, user: User, entity_type: str | None = None, q_: str | None = Query(None, alias="q"),
                      profile: str | None = None, limit: int = Query(100, le=1000), offset: int = 0):
        where, args = scope_sql(conn, user)
        sql = f"SELECT e.* FROM entity e WHERE e.deleted_ms IS NULL AND {where}"
        if entity_type:
            sql += " AND e.entity_type=?"
            args.append(entity_type)
        if profile:
            sql += " AND e.profile=?"
            args.append(profile)
        if q_:
            sql += " AND (e.name LIKE ? OR e.label LIKE ?)"
            args += [f"%{q_}%", f"%{q_}%"]
        total = conn.execute(sql.replace("SELECT e.*", "SELECT COUNT(*)"), args).fetchone()[0]
        sql = sql.replace("SELECT e.*", "SELECT e.*, (SELECT COUNT(*) FROM ts_key k WHERE k.entity_id=e.id) AS key_count,"
                          " (SELECT COUNT(*) FROM relation r WHERE r.from_id=e.id OR r.to_id=e.id) AS relation_count")
        rows = conn.execute(sql + " ORDER BY e.entity_type, e.name LIMIT ? OFFSET ?", [*args, limit, offset])
        return {"total": total, "items": [_entity_out(r) | {"key_count": r["key_count"],
                                                            "relation_count": r["relation_count"]} for r in rows]}

    @app.get("/api/entities-facets")
    def entity_facets(conn: Db, user: User):
        """필터용: 유형·프로파일별 엔티티 수 (범위 적용)."""
        where, args = scope_sql(conn, user)
        rows = conn.execute(f"SELECT e.entity_type, e.profile, COUNT(*) n FROM entity e WHERE e.deleted_ms IS NULL"
                            f" AND {where} GROUP BY 1, 2 ORDER BY 1, 3 DESC", args)
        return [dict(r) for r in rows]

    @app.get("/api/entities/{entity_id}")
    def entity_detail(entity_id: str, conn: Db, user: User):
        where, args = scope_sql(conn, user)
        e = conn.execute(f"SELECT e.* FROM entity e WHERE e.id=? AND {where}", [entity_id, *args]).fetchone()
        if e is None:
            raise HTTPException(404, "엔티티를 찾을 수 없습니다")
        rel_sql = (
            "SELECT r.*, e.name AS other_name, e.entity_type AS other_type FROM relation r JOIN entity e ON e.id = r.{o}"
            f" WHERE r.{{s}}=? AND e.deleted_ms IS NULL AND {where}")
        rel_from = conn.execute(rel_sql.format(o="to_id", s="from_id"), [entity_id, *args]).fetchall()
        rel_to = conn.execute(rel_sql.format(o="from_id", s="to_id"), [entity_id, *args]).fetchall()
        attrs = conn.execute("SELECT scope, key, value_json, last_update_ts FROM attribute WHERE entity_id=?"
                             " ORDER BY scope, key", (entity_id,)).fetchall()
        keys = conn.execute(
            "SELECT k.key, k.first_seen_ms, p.interval_s, p.irregular, p.tier, p.last_ts, p.value_kind, p.profiled_ms"
            " FROM ts_key k LEFT JOIN key_profile p ON p.entity_id=k.entity_id AND p.key=k.key"
            " WHERE k.entity_id=? ORDER BY k.key", (entity_id,)).fetchall()
        return {
            **_entity_out(e),
            "relations_out": [{"type": r["rel_type"], "to_id": r["to_id"], "to_name": r["other_name"],
                               "to_type": r["other_type"]} for r in rel_from],
            "relations_in": [{"type": r["rel_type"], "from_id": r["from_id"], "from_name": r["other_name"],
                              "from_type": r["other_type"]} for r in rel_to],
            "attributes": [{"scope": a["scope"], "key": a["key"], "value": loads(a["value_json"]),
                            "last_update_ts": a["last_update_ts"]} for a in attrs],
            "timeseries_keys": [dict(k) | _key_extra(conn, entity_id, k["key"]) for k in keys],
            "attribute_roles": {f'{r["scope"]}.{r["key"]}': {"role": r["role"], "dynamic": bool(r["dynamic"])}
                                for r in conn.execute("SELECT scope, key, role, dynamic FROM key_analysis"
                                                      " WHERE entity_id=? AND source='ATTR'", (entity_id,))},
            "quality_score": (conn.execute("SELECT score FROM quality_score WHERE entity_id=? AND key=''",
                                           (entity_id,)).fetchone() or [None])[0],
        }

    @app.get("/api/changes")
    def changes(conn: Db, user: User, entity_id: str | None = None, change_type: str | None = None,
                since_ms: int | None = None, limit: int = Query(200, le=2000)):
        where, args = scope_sql(conn, user)
        sql = ("SELECT c.*, e.name AS entity_name FROM metadata_change c LEFT JOIN entity e ON e.id = c.entity_id"
               f" WHERE {where}")
        if entity_id:
            sql += " AND c.entity_id=?"
            args.append(entity_id)
        if change_type:
            sql += " AND c.change_type=?"
            args.append(change_type)
        if since_ms:
            sql += " AND c.ts_ms >= ?"
            args.append(since_ms)
        rows = conn.execute(sql + " ORDER BY c.id DESC LIMIT ?", [*args, limit]).fetchall()
        out = [{**{k: r[k] for k in ("id", "cycle_id", "ts_ms", "entity_id", "entity_type", "entity_name",
                                     "change_type", "field")},
                "before": loads(r["before_json"]), "after": loads(r["after_json"])} for r in rows]
        # 관계 변경: 대상 엔티티 이름을 붙여 사람이 읽을 수 있게 (삭제된 엔티티 포함)
        ids = {v["to_id"] for c in out for v in (c["before"], c["after"]) if isinstance(v, dict) and "to_id" in v}
        if ids:
            names = {r["id"]: r["name"] for r in conn.execute(
                f"SELECT id, name FROM entity WHERE id IN ({','.join('?' * len(ids))})", list(ids))}
            for c in out:
                for v in (c["before"], c["after"]):
                    if isinstance(v, dict) and "to_id" in v:
                        v["to_name"] = names.get(v["to_id"])
        return out

    @app.get("/api/scheduler/metrics")
    def scheduler_metrics(conn: Db, user: User, minutes: int = Query(60, ge=5, le=1440)):
        """분 단위 TB 요청 수·지연·부하 오류 (스케줄러 화면 그래프용)."""
        require(user, "ADMIN", "ANALYST")
        end = now_ms() // 60_000 * 60_000 + 60_000
        start = end - minutes * 60_000
        buckets: dict[int, list] = {}
        for r in conn.execute("SELECT ts_ms, latency_ms, status FROM tb_request_log WHERE ts_ms >= ?", (start,)):
            buckets.setdefault((r["ts_ms"] - start) // 60_000, []).append(r)
        out = []
        for i in range(minutes):
            rows = buckets.get(i, [])
            lat = sorted(r["latency_ms"] for r in rows)
            out.append({
                "ts_ms": start + i * 60_000,
                "requests": len(rows),
                "p95_ms": round(lat[max(0, int(len(lat) * 0.95) - 1)]) if lat else None,
                "load_errors": sum(1 for r in rows if r["status"] is None or r["status"] == 429
                                   or r["status"] >= 500),
            })
        return out

    @app.get("/api/customers")
    def customers(conn: Db, user: User):
        scope = users.customer_scope(conn, user)
        rows = conn.execute("SELECT c.*, (SELECT COUNT(*) FROM entity e WHERE e.owner_id=c.id AND e.deleted_ms IS NULL)"
                            " AS entities FROM customer c ORDER BY title").fetchall()
        return [dict(r) for r in rows if scope is None or r["id"] in scope]

    # -- 데이터 카탈로그 (Phase 2) ---------------------------------------------------
    @app.get("/api/catalog/meta")
    def catalog_meta(user: User):
        return {"semantics": {k: {"ko": v["ko"], "default_unit": v["default_unit"]} for k, v in SEMANTICS.items()},
                "roles": {k: v["ko"] for k, v in ATTRIBUTE_ROLES.items()},
                "data_types": {k or "": v for k, v in DATA_TYPE_KO.items()},
                "checks": CHECK_KO}

    @app.get("/api/catalog/keys")
    def catalog_keys(conn: Db, user: User, status: str | None = None, semantic: str | None = None,
                     q_: str | None = Query(None, alias="q")):
        """텔레메트리 키를 (프로파일, 키 이름) 단위로 묶어 보여준다 — 검토·확정도 이 단위."""
        where, args = scope_sql(conn, user)
        rows = conn.execute(
            "SELECT e.profile, a.key, a.data_type, a.semantic, a.unit, a.unit_source, a.confidence, a.status,"
            " a.evidence_json, a.alternatives_json, p.tier, p.interval_s, s.score"
            " FROM key_analysis a JOIN entity e ON e.id=a.entity_id"
            " LEFT JOIN key_profile p ON p.entity_id=a.entity_id AND p.key=a.key"
            " LEFT JOIN quality_score s ON s.entity_id=a.entity_id AND s.key=a.key"
            f" WHERE a.source='TS' AND e.deleted_ms IS NULL AND {where}", args).fetchall()
        groups: dict = {}
        for r in rows:
            g = groups.setdefault((r["profile"], r["key"]), {"profile": r["profile"], "key": r["key"], "entities": 0,
                                                               "_sem": {}, "_dt": {}, "_tier": {}, "_conf": [],
                                                               "_scores": [], "_iv": [], "status": "AUTO",
                                                               "unit": None, "evidence": None, "alternatives": []})
            g["entities"] += 1
            for f, v in (("_sem", r["semantic"]), ("_dt", r["data_type"]), ("_tier", r["tier"])):
                g[f][v] = g[f].get(v, 0) + 1
            g["_conf"].append(r["confidence"] or 0)
            if r["score"] is not None:
                g["_scores"].append(r["score"])
            if r["interval_s"]:
                g["_iv"].append(r["interval_s"])
            rank = {"CONFIRMED": 3, "REVIEW": 2, "AUTO": 1}
            if r["status"] == "CONFIRMED" or (rank[r["status"]] > rank[g["status"]] and g["status"] != "CONFIRMED"):
                g["status"] = r["status"]
            if g["evidence"] is None or r["status"] == "REVIEW":
                g["evidence"], g["alternatives"], g["unit"] = loads(r["evidence_json"]), loads(r["alternatives_json"]), r["unit"]
                g["unit_source"] = r["unit_source"]
        out = []
        for g in groups.values():
            sem = max(g["_sem"], key=g["_sem"].get)
            item = {k: v for k, v in g.items() if not k.startswith("_")} | {
                "semantic": sem, "semantic_split": len(g["_sem"]) > 1,
                "data_type": max(g["_dt"], key=lambda k: g["_dt"][k]),
                "tiers": g["_tier"], "confidence": round(min(g["_conf"]), 2),
                "interval_s": min(g["_iv"]) if g["_iv"] else None,
                "quality_avg": round(sum(g["_scores"]) / len(g["_scores"])) if g["_scores"] else None}
            if status and item["status"] != status:
                continue
            if semantic and item["semantic"] != semantic:
                continue
            if q_ and q_.lower() not in item["key"].lower():
                continue
            out.append(item)
        out.sort(key=lambda i: ({"REVIEW": 0, "AUTO": 1, "CONFIRMED": 2}[i["status"]], i["semantic"], i["key"]))
        return out

    @app.get("/api/catalog/semantic-groups")
    def semantic_groups(conn: Db, user: User):
        """같은 의미의 키 묶음 — 이름이 달라도 같은 의미면 한 그룹 (계획서 §5.0 (4))."""
        where, args = scope_sql(conn, user)
        rows = conn.execute(
            "SELECT a.semantic, a.key, a.unit, COUNT(*) n FROM key_analysis a JOIN entity e ON e.id=a.entity_id"
            f" WHERE a.source='TS' AND e.deleted_ms IS NULL AND {where} AND a.status IN ('AUTO','CONFIRMED')"
            " GROUP BY 1, 2, 3 ORDER BY 1, 4 DESC", args).fetchall()
        out: dict = {}
        for r in rows:
            g = out.setdefault(r["semantic"], {"semantic": r["semantic"],
                                               "ko": SEMANTICS.get(r["semantic"], {}).get("ko", "모름"),
                                               "keys": [], "entities": 0, "units": set()})
            g["keys"].append({"key": r["key"], "entities": r["n"], "unit": r["unit"]})
            g["entities"] += r["n"]
            if r["unit"]:
                g["units"].add(r["unit"])
        return sorted(({**g, "units": sorted(g["units"])} for g in out.values()), key=lambda g: -g["entities"])

    @app.get("/api/catalog/attributes")
    def catalog_attributes(conn: Db, user: User):
        where, args = scope_sql(conn, user)
        rows = conn.execute(
            "SELECT a.scope, a.key, a.role, SUM(a.dynamic) dyn, COUNT(*) n FROM key_analysis a"
            f" JOIN entity e ON e.id=a.entity_id WHERE a.source='ATTR' AND e.deleted_ms IS NULL AND {where}"
            " GROUP BY 1, 2, 3 ORDER BY 3, 5 DESC", args).fetchall()
        return [{"scope": r["scope"], "key": r["key"], "role": r["role"], "entities": r["n"],
                 "dynamic": r["dyn"] > 0} for r in rows]

    @app.get("/api/catalog/rules")
    def list_rules(conn: Db, user: User):
        return [dict(r) for r in conn.execute("SELECT * FROM semantic_rule ORDER BY created_ms DESC")]

    def _reanalyze(conn, profile, key):
        sql = "SELECT DISTINCT e.id FROM entity e JOIN ts_key k ON k.entity_id=e.id WHERE k.key=? AND e.deleted_ms IS NULL"
        args = [key]
        if profile is not None:
            sql += " AND e.profile IS ?"
            args.append(profile)
        ids = [r[0] for r in conn.execute(sql, args)]
        for eid in ids:
            analyze_entity(conn, eid)
        return len(ids)

    @app.post("/api/catalog/rules")
    def add_rule(req: RuleReq, conn: Db, user: User):
        """의미 확정 (사용자 피드백). 같은 (프로파일, 키)에 다시 저장하면 덮어쓴다. 해당 엔티티만 즉시 재분석."""
        require(user, "ADMIN", "ANALYST")
        if req.semantic != "unknown" and req.semantic not in SEMANTICS:
            raise HTTPException(400, f"알 수 없는 의미: {req.semantic}")
        conn.execute("DELETE FROM semantic_rule WHERE key=? AND profile IS ?", (req.key, req.profile))
        conn.execute("INSERT INTO semantic_rule (profile, key, semantic, unit, created_by, created_ms)"
                     " VALUES (?,?,?,?,?,?)", (req.profile, req.key, req.semantic, req.unit, user["login"], now_ms()))
        n = _reanalyze(conn, req.profile, req.key)
        users.audit(conn, user["id"], user["login"], "SEMANTIC_CONFIRM", f"{req.profile or '*'}/{req.key}",
                    {"semantic": req.semantic, "unit": req.unit})
        return {"reanalyzed_entities": n}

    @app.delete("/api/catalog/rules/{rule_id}")
    def delete_rule(rule_id: int, conn: Db, user: User):
        require(user, "ADMIN", "ANALYST")
        r = conn.execute("SELECT * FROM semantic_rule WHERE id=?", (rule_id,)).fetchone()
        if r is None:
            raise HTTPException(404, "확정 기록이 없습니다")
        conn.execute("DELETE FROM semantic_rule WHERE id=?", (rule_id,))
        n = _reanalyze(conn, r["profile"], r["key"])
        users.audit(conn, user["id"], user["login"], "SEMANTIC_UNCONFIRM", f"{r['profile'] or '*'}/{r['key']}")
        return {"reanalyzed_entities": n}

    # -- 데이터 품질 (Phase 2 [B]) -------------------------------------------------
    @app.get("/api/quality/summary")
    def quality_summary(conn: Db, user: User):
        where, args = scope_sql(conn, user)
        scores = conn.execute(
            "SELECT e.id, e.name, e.entity_type, e.profile, s.score FROM quality_score s JOIN entity e ON e.id=s.entity_id"
            f" WHERE s.key='' AND e.deleted_ms IS NULL AND {where} ORDER BY s.score, e.name", args).fetchall()
        buckets = {"90~100": 0, "70~89": 0, "50~69": 0, "0~49": 0}
        for r in scores:
            s = r["score"]
            buckets["90~100" if s >= 90 else "70~89" if s >= 70 else "50~69" if s >= 50 else "0~49"] += 1
        by_check = conn.execute(
            "SELECT f.check_type, f.severity, COUNT(*) n FROM quality_finding f JOIN entity e ON e.id=f.entity_id"
            f" WHERE f.resolved_ms IS NULL AND e.deleted_ms IS NULL AND {where} GROUP BY 1, 2 ORDER BY 3 DESC",
            args).fetchall()
        return {"entities_scored": len(scores),
                "average": round(sum(r["score"] for r in scores) / len(scores)) if scores else None,
                "buckets": buckets,
                "open_by_check": [{"check": r["check_type"], "check_ko": CHECK_KO.get(r["check_type"]),
                                   "severity": r["severity"], "count": r["n"]} for r in by_check],
                "worst": [dict(r) for r in scores[:10]]}

    @app.get("/api/quality/findings")
    def quality_findings(conn: Db, user: User, severity: str | None = None, check: str | None = None,
                         entity_id: str | None = None, include_resolved: bool = False,
                         limit: int = Query(500, le=5000)):
        where, args = scope_sql(conn, user)
        sql = ("SELECT f.*, e.name AS entity_name, e.profile FROM quality_finding f JOIN entity e ON e.id=f.entity_id"
               f" WHERE e.deleted_ms IS NULL AND {where}")
        if not include_resolved:
            sql += " AND f.resolved_ms IS NULL"
        for col, val in (("f.severity", severity), ("f.check_type", check), ("f.entity_id", entity_id)):
            if val:
                sql += f" AND {col}=?"
                args.append(val)
        sql += (" ORDER BY CASE f.severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,"
                " f.last_detected_ms DESC LIMIT ?")
        rows = conn.execute(sql, [*args, limit]).fetchall()
        return [{k: r[k] for k in ("id", "entity_id", "entity_name", "profile", "key", "check_type", "severity",
                                   "message", "first_detected_ms", "last_detected_ms", "resolved_ms")}
                | {"check_ko": CHECK_KO.get(r["check_type"]), "detail": loads(r["detail_json"])} for r in rows]

    @app.get("/api/catalog/tiers")
    def tiers(conn: Db, user: User):
        where, args = scope_sql(conn, user)
        rows = conn.execute(
            "SELECT COALESCE(p.tier, 'unprofiled') tier, COUNT(*) n FROM ts_key k JOIN entity e ON e.id=k.entity_id"
            " LEFT JOIN key_profile p ON p.entity_id=k.entity_id AND p.key=k.key"
            f" WHERE e.deleted_ms IS NULL AND {where} GROUP BY 1", args)
        return {r["tier"]: r["n"] for r in rows}

    _mount_web(app, settings)
    return app


def _mount_web(app: FastAPI, settings: Settings) -> None:
    """빌드된 웹 화면(frontend/dist)을 같은 서버에서 제공 — 사내망 단일 배포용. /api 외 경로는 index.html."""
    web_dir = settings.web_dir
    if not web_dir or not os.path.isfile(os.path.join(web_dir, "index.html")):
        return
    root = os.path.realpath(web_dir)

    @app.get("/{path:path}", include_in_schema=False)
    def web(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, "not found")
        target = os.path.realpath(os.path.join(root, path))
        if path and target.startswith(root + os.sep) and os.path.isfile(target):
            return FileResponse(target)
        return FileResponse(os.path.join(root, "index.html"))


def _key_extra(conn, entity_id, key) -> dict:
    a = conn.execute("SELECT data_type, semantic, unit, unit_source, confidence, status, evidence_json"
                     " FROM key_analysis WHERE entity_id=? AND source='TS' AND key=?", (entity_id, key)).fetchone()
    s = conn.execute("SELECT score FROM quality_score WHERE entity_id=? AND key=?", (entity_id, key)).fetchone()
    f = conn.execute("SELECT check_type, severity, message FROM quality_finding WHERE entity_id=? AND key=?"
                     " AND resolved_ms IS NULL", (entity_id, key)).fetchall()
    return {
        "data_type": a["data_type"] if a else None, "semantic": a["semantic"] if a else None,
        "unit": a["unit"] if a else None, "unit_source": a["unit_source"] if a else None,
        "confidence": a["confidence"] if a else None, "semantic_status": a["status"] if a else None,
        "evidence": loads(a["evidence_json"]) if a else [],
        "quality_score": s["score"] if s else None,
        "findings": [{"check": r["check_type"], "check_ko": CHECK_KO.get(r["check_type"]), "severity": r["severity"],
                      "message": r["message"]} for r in f],
    }


def _user_out(u) -> dict:
    return {"id": u["id"], "login": u["login"], "role": u["role"],
            "customer_ids": json.loads(u["customer_ids"] or "[]"), "active": bool(u["active"])}


def _entity_out(r) -> dict:
    return {k: r[k] for k in ("id", "entity_type", "name", "label", "profile", "owner_id", "owner_name",
                              "created_time", "first_seen_ms", "last_seen_ms", "detail_synced_ms",
                              "profile_synced_ms")} | {"active": None if r["active"] is None else bool(r["active"])}
