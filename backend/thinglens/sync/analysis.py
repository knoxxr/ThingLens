"""Phase 2: 샘플·범위 수집(저부하) + 데이터 종류·의미 추론 + 품질 점검 저장.

TB 요청은 fetch_* 함수에서만 일어나고, analyze_* 는 저장된 데이터로만 계산한다.

수집 규칙 (Phase 0·2 실측 근거: docs/phase0/cassandra-benchmark.md)
  - 샘플: 원시값(agg=NONE), 최근 창 + (1일 전, 7일 전) 창, 키당 최대 N개
  - 범위(envelope): MIN·MAX·COUNT 를 **구간 1개짜리** 집계로 요청 (구간이 1개면 Cassandra 조회도 적음)
    요청당 읽는 원시값이 키당 약 1만 개를 넘지 않도록 창 길이를 제한 (1초 키 ≈ 2.8시간, 1분 키 ≈ 7일)
"""

from __future__ import annotations

import json
from collections import defaultdict

from ..db import Tx, dumps, loads, now_ms
from ..quality.checks import CHECK_KO, run_checks, score
from ..scheduler.planner import classify_tier, estimate_interval
from ..semantics.dictionary import SEMANTICS, UNKNOWN
from ..semantics.infer import CATEG, CONT, classify_attribute, compute_stats, infer_data_type, infer_semantic
from ..tb.adapter import DAY_MS

HOUR_MS = 3_600_000
OLDER_WINDOW_OFFSETS = (DAY_MS, 7 * DAY_MS)
ENVELOPE_MAX_POINTS = 10_000
KEYS_PER_SAMPLE_REQ = 20
KEYS_PER_AGG_REQ = 5


# --------------------------------------------------------------------------- #
# TB 조회
# --------------------------------------------------------------------------- #
def fetch_latest(adapter, ent, keys, n) -> dict[str, list]:
    """키별 최근 원시값 n개. 1일 → 부족하면 30일 (Cassandra 파티션 스캔을 줄이려 짧은 구간부터)."""
    out: dict[str, list] = {k: [] for k in keys}
    pending = list(keys)
    for days in (1, 30):
        short = []
        for i in range(0, len(pending), KEYS_PER_SAMPLE_REQ):
            batch = pending[i:i + KEYS_PER_SAMPLE_REQ]
            end = now_ms()
            data = adapter.ts_raw(ent["entity_type"], ent["id"], batch, end - days * DAY_MS, end, n)
            for k in batch:
                pts = [(p["ts"], p.get("value")) for p in (data.get(k) or []) if "ts" in p]
                out[k] = pts
                if len(pts) < 3:
                    short.append(k)
        pending = short
        if not pending:
            break
    return out


def fetch_older_windows(adapter, ent, key_interval: dict[str, float], n) -> dict[str, list[list]]:
    """1일 전·7일 전 창 (값 고정 판단·통계용). 주기가 비슷한 키끼리 묶어 요청."""
    out: dict[str, list[list]] = {k: [] for k in key_interval}
    groups: dict[int, list[str]] = defaultdict(list)
    for k, iv in key_interval.items():
        span = int(min(7 * DAY_MS, max(HOUR_MS, iv * 1000 * n * 1.5)))
        groups[span].append(k)
    for off in OLDER_WINDOW_OFFSETS:
        for span, ks in groups.items():
            for i in range(0, len(ks), KEYS_PER_SAMPLE_REQ):
                batch = ks[i:i + KEYS_PER_SAMPLE_REQ]
                end = now_ms() - off
                data = adapter.ts_raw(ent["entity_type"], ent["id"], batch, end - span, end, n)
                for k in batch:
                    out[k].append([(p["ts"], p.get("value")) for p in (data.get(k) or []) if "ts" in p])
    return out


def envelope_span(interval_s: float | None, max_span_ms: int) -> int:
    if not interval_s:
        return max_span_ms
    return int(min(max_span_ms, max(HOUR_MS, ENVELOPE_MAX_POINTS * interval_s * 1000)))


def fetch_envelopes(adapter, ent, key_interval: dict[str, float | None], max_span_ms: int) -> dict[str, dict]:
    """숫자 키의 MIN·MAX·COUNT — 구간 1개짜리 집계."""
    out: dict[str, dict] = {}
    groups: dict[int, list[str]] = defaultdict(list)
    for k, iv in key_interval.items():
        groups[envelope_span(iv, max_span_ms)].append(k)
    end = now_ms()
    for span, ks in groups.items():
        for i in range(0, len(ks), KEYS_PER_AGG_REQ):
            batch = ks[i:i + KEYS_PER_AGG_REQ]
            for k in batch:
                out[k] = {"start": end - span, "end": end, "min": None, "max": None, "count": None}
            for agg in ("MIN", "MAX", "COUNT"):
                data = adapter.ts_agg(ent["entity_type"], ent["id"], batch, end - span, end, agg)
                for k in batch:
                    pts = data.get(k) or []
                    if pts:
                        try:
                            v = float(pts[0].get("value"))
                        except (TypeError, ValueError):
                            continue
                        out[k][{"MIN": "min", "MAX": "max", "COUNT": "count"}[agg]] = int(v) if agg == "COUNT" else v
            for k in batch:
                if out[k]["count"] is None and out[k]["min"] is None:
                    out[k]["count"] = 0
    return out


def _numeric_keys(windows_by_key: dict[str, list]) -> list[str]:
    st = {k: compute_stats(v) for k, v in windows_by_key.items()}
    return [k for k, s in st.items() if s.n >= 3 and s.numeric_ratio >= 0.95]


# --------------------------------------------------------------------------- #
# 저장
# --------------------------------------------------------------------------- #
def save_profile_and_samples(conn, ent, latest, older, envelopes, kind):
    t = now_ms()
    with Tx(conn):
        for k, pts in latest.items():
            info = estimate_interval([ts for ts, _ in pts])
            conn.execute(
                "INSERT INTO key_profile (entity_id, key, interval_s, irregular, tier, points, last_ts, value_kind,"
                " profiled_ms) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(entity_id, key) DO UPDATE SET"
                " interval_s=excluded.interval_s, irregular=excluded.irregular, tier=excluded.tier,"
                " points=excluded.points, last_ts=excluded.last_ts, profiled_ms=excluded.profiled_ms",
                (ent["id"], k, info["interval_s"] if info else None, int(info["irregular"]) if info else None,
                 classify_tier(info), len(pts), max((ts for ts, _ in pts), default=None), None, t))
            if older is None:  # 일일 점검: 최근 창만 교체
                row = conn.execute("SELECT windows_json FROM key_sample WHERE entity_id=? AND key=?",
                                   (ent["id"], k)).fetchone()
                wins = loads(row["windows_json"]) if row else [[]]
                wins[0] = pts
            else:
                wins = [pts] + older.get(k, [])
            conn.execute("INSERT INTO key_sample (entity_id, key, sampled_ms, windows_json) VALUES (?,?,?,?)"
                         " ON CONFLICT(entity_id, key) DO UPDATE SET sampled_ms=excluded.sampled_ms,"
                         " windows_json=excluded.windows_json", (ent["id"], k, t, json.dumps(wins)))
        for k, e in envelopes.items():
            conn.execute("INSERT INTO key_envelope (entity_id, key, kind, start_ts, end_ts, min, max, count, computed_ms)"
                         " VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(entity_id, key, kind) DO UPDATE SET"
                         " start_ts=excluded.start_ts, end_ts=excluded.end_ts, min=excluded.min, max=excluded.max,"
                         " count=excluded.count, computed_ms=excluded.computed_ms",
                         (ent["id"], k, kind, e["start"], e["end"], e["min"], e["max"], e["count"], t))


# --------------------------------------------------------------------------- #
# 분석 (로컬 계산)
# --------------------------------------------------------------------------- #
def find_rule(conn, profile, key):
    r = conn.execute("SELECT * FROM semantic_rule WHERE key=? AND profile IS ?", (key, profile)).fetchone()
    if r is None:
        r = conn.execute("SELECT * FROM semantic_rule WHERE key=? AND profile IS NULL", (key,)).fetchone()
    return dict(r) if r else None


def analyze_entity(conn, entity_id: str) -> None:
    ent = conn.execute("SELECT * FROM entity WHERE id=?", (entity_id,)).fetchone()
    if ent is None or ent["deleted_ms"] is not None:
        return
    t = now_ms()
    key_scores: list[int] = []
    with Tx(conn):
        # 텔레메트리 키 ---------------------------------------------------------
        keys = conn.execute(
            "SELECT k.key, k.first_seen_ms, p.interval_s, p.irregular, p.tier, s.windows_json FROM ts_key k"
            " LEFT JOIN key_profile p ON p.entity_id=k.entity_id AND p.key=k.key"
            " LEFT JOIN key_sample s ON s.entity_id=k.entity_id AND s.key=k.key WHERE k.entity_id=?",
            (entity_id,)).fetchall()
        for k in keys:
            if k["windows_json"] is None:
                continue  # 아직 샘플 없음
            windows = [[(ts, v) for ts, v in w] for w in loads(k["windows_json"])]
            pts = [p for w in windows for p in w]
            st = compute_stats(pts)
            dt = infer_data_type(pts, st, windows)
            sem = infer_semantic(k["key"], dt, st, ent["profile"], find_rule(conn, ent["profile"], k["key"]))
            if dt == CATEG and st.numeric_ratio >= 0.95 and sem.semantic != UNKNOWN and \
                    CONT in SEMANTICS.get(sem.semantic, {}).get("types", []):
                dt = CONT  # 정수 값 측정치
            env = _latest_envelope(conn, entity_id, k["key"])
            findings = run_checks(now_ms=t, windows=windows, interval_s=k["interval_s"],
                                  irregular=bool(k["irregular"]), tier=k["tier"], data_type=dt,
                                  semantic=sem.semantic, semantic_conf=sem.confidence, envelope=env,
                                  first_seen_ms=k["first_seen_ms"])
            conn.execute(
                "INSERT INTO key_analysis (entity_id, source, scope, key, data_type, stats_json, semantic, unit,"
                " unit_source, confidence, status, evidence_json, alternatives_json, analyzed_ms)"
                " VALUES (?, 'TS', '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(entity_id, source, scope, key) DO UPDATE SET data_type=excluded.data_type,"
                " stats_json=excluded.stats_json, semantic=excluded.semantic, unit=excluded.unit,"
                " unit_source=excluded.unit_source, confidence=excluded.confidence, status=excluded.status,"
                " evidence_json=excluded.evidence_json, alternatives_json=excluded.alternatives_json,"
                " analyzed_ms=excluded.analyzed_ms",
                (entity_id, k["key"], dt, dumps(st.as_dict()), sem.semantic, sem.unit, sem.unit_source,
                 sem.confidence, sem.status, dumps(sem.evidence), dumps(sem.alternatives), t))
            _reconcile_findings(conn, entity_id, k["key"], findings, t)
            sc = score(findings)
            key_scores.append(sc)
            conn.execute("INSERT INTO quality_score (entity_id, key, score, computed_ms) VALUES (?,?,?,?)"
                         " ON CONFLICT(entity_id, key) DO UPDATE SET score=excluded.score,"
                         " computed_ms=excluded.computed_ms", (entity_id, k["key"], sc, t))
        # 사라진 키 정리
        conn.execute("DELETE FROM key_analysis WHERE entity_id=? AND source='TS' AND key NOT IN"
                     " (SELECT key FROM ts_key WHERE entity_id=?)", (entity_id, entity_id))
        conn.execute("UPDATE quality_finding SET resolved_ms=? WHERE entity_id=? AND resolved_ms IS NULL AND key NOT IN"
                     " (SELECT key FROM ts_key WHERE entity_id=?)", (t, entity_id, entity_id))
        conn.execute("DELETE FROM quality_score WHERE entity_id=? AND key != '' AND key NOT IN"
                     " (SELECT key FROM ts_key WHERE entity_id=?)", (entity_id, entity_id))
        if key_scores:
            conn.execute("INSERT INTO quality_score (entity_id, key, score, computed_ms) VALUES (?, '', ?, ?)"
                         " ON CONFLICT(entity_id, key) DO UPDATE SET score=excluded.score,"
                         " computed_ms=excluded.computed_ms",
                         (entity_id, round(sum(key_scores) / len(key_scores)), t))

        # 속성 ----------------------------------------------------------------
        changes = {r["field"]: r["n"] for r in conn.execute(
            "SELECT field, COUNT(*) n FROM metadata_change WHERE entity_id=? AND change_type='ATTRIBUTE_CHANGED'"
            " GROUP BY field", (entity_id,))}
        conn.execute("DELETE FROM key_analysis WHERE entity_id=? AND source='ATTR'", (entity_id,))
        for a in conn.execute("SELECT scope, key, value_json FROM attribute WHERE entity_id=?", (entity_id,)).fetchall():
            info = classify_attribute(a["key"], a["scope"], loads(a["value_json"]),
                                      changes.get(f'{a["scope"]}.{a["key"]}', 0))
            conn.execute(
                "INSERT INTO key_analysis (entity_id, source, scope, key, data_type, role, dynamic, status,"
                " evidence_json, analyzed_ms) VALUES (?, 'ATTR', ?, ?, ?, ?, ?, 'AUTO', ?, ?)",
                (entity_id, a["scope"], a["key"], info["value_type"], info["role"], int(info["dynamic"]),
                 dumps([info["role_reason"]]), t))


def _latest_envelope(conn, entity_id, key):
    rows = conn.execute("SELECT * FROM key_envelope WHERE entity_id=? AND key=? ORDER BY computed_ms DESC",
                        (entity_id, key)).fetchall()
    if not rows:
        return None
    r = rows[0]
    return {"start": r["start_ts"], "end": r["end_ts"], "min": r["min"], "max": r["max"], "count": r["count"]}


def _reconcile_findings(conn, entity_id, key, findings, t):
    open_rows = {r["check_type"]: r["id"] for r in conn.execute(
        "SELECT id, check_type FROM quality_finding WHERE entity_id=? AND key=? AND resolved_ms IS NULL",
        (entity_id, key))}
    seen = set()
    for f in findings:
        seen.add(f.check)
        if f.check in open_rows:
            conn.execute("UPDATE quality_finding SET severity=?, message=?, detail_json=?, last_detected_ms=?"
                         " WHERE id=?", (f.severity, f.message, dumps(f.detail), t, open_rows[f.check]))
        else:
            conn.execute("INSERT INTO quality_finding (entity_id, key, check_type, severity, message, detail_json,"
                         " first_detected_ms, last_detected_ms) VALUES (?,?,?,?,?,?,?,?)",
                         (entity_id, key, f.check, f.severity, f.message, dumps(f.detail), t, t))
    for check, fid in open_rows.items():
        if check not in seen:
            conn.execute("UPDATE quality_finding SET resolved_ms=? WHERE id=?", (t, fid))


def analyze_all(conn) -> int:
    ids = [r["id"] for r in conn.execute("SELECT id FROM entity WHERE deleted_ms IS NULL")]
    for eid in ids:
        analyze_entity(conn, eid)
    return len(ids)


_ = CHECK_KO  # 표시용 이름은 API에서 사용
