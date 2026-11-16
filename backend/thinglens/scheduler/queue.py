"""SQLite 기반 우선순위 작업 큐 (계획서 §3.3 ③④). 재시작 시 RUNNING 작업은 PENDING 으로 되돌려 이어서 수행."""

from __future__ import annotations

import sqlite3

from ..db import Tx, dumps, loads, now_ms

# 우선순위: 숫자가 작을수록 먼저
PRIO_CYCLE = 5
PRIO_ENTITY_LIST = 10
PRIO_ENTITY_DETAIL = 20
PRIO_ANALYZE = 25
PRIO_PROFILE = 30
PRIO_QUALITY = 35
PRIO_TS_INCREMENTAL = 40
PRIO_BACKFILL = 50
PRIO_FINISH = 90

MAX_ATTEMPTS = 5


def enqueue(conn: sqlite3.Connection, kind: str, payload: dict, priority: int,
            dedupe_key: str | None = None, run_at_ms: int | None = None) -> int | None:
    """dedupe_key 가 같은 작업이 이미 대기·실행 중이면 무시하고 None."""
    t = now_ms()
    try:
        cur = conn.execute(
            "INSERT INTO sync_task (kind, priority, payload, dedupe_key, status, next_run_ms, created_ms, updated_ms)"
            " VALUES (?, ?, ?, ?, 'PENDING', ?, ?, ?)",
            (kind, priority, dumps(payload), dedupe_key, run_at_ms or t, t, t))
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def claim_next(conn: sqlite3.Connection) -> sqlite3.Row | None:
    with Tx(conn):
        row = conn.execute(
            "SELECT * FROM sync_task WHERE status='PENDING' AND next_run_ms <= ?"
            " ORDER BY priority, next_run_ms, id LIMIT 1", (now_ms(),)).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE sync_task SET status='RUNNING', attempts=attempts+1, updated_ms=? WHERE id=?",
                     (now_ms(), row["id"]))
    return conn.execute("SELECT * FROM sync_task WHERE id=?", (row["id"],)).fetchone()


def payload(row: sqlite3.Row) -> dict:
    return loads(row["payload"])


def complete(conn: sqlite3.Connection, task_id: int) -> None:
    conn.execute("UPDATE sync_task SET status='DONE', updated_ms=?, last_error=NULL WHERE id=?", (now_ms(), task_id))


def release(conn: sqlite3.Connection, task_id: int, delay_ms: int) -> None:
    """실패가 아닌 사유(서킷 열림, 선행 작업 대기)로 미룸 — 시도 횟수 차감."""
    conn.execute("UPDATE sync_task SET status='PENDING', attempts=MAX(0, attempts-1), next_run_ms=?, updated_ms=?"
                 " WHERE id=?", (now_ms() + delay_ms, now_ms(), task_id))


def fail(conn: sqlite3.Connection, task_id: int, error: str) -> str:
    row = conn.execute("SELECT attempts FROM sync_task WHERE id=?", (task_id,)).fetchone()
    if row["attempts"] >= MAX_ATTEMPTS:
        conn.execute("UPDATE sync_task SET status='FAILED', last_error=?, updated_ms=? WHERE id=?",
                     (error[:500], now_ms(), task_id))
        return "FAILED"
    delay = min(3600_000, 30_000 * 2 ** (row["attempts"] - 1))
    conn.execute("UPDATE sync_task SET status='PENDING', last_error=?, next_run_ms=?, updated_ms=? WHERE id=?",
                 (error[:500], now_ms() + delay, now_ms(), task_id))
    return "RETRY"


def reset_running(conn: sqlite3.Connection) -> int:
    return conn.execute("UPDATE sync_task SET status='PENDING', attempts=MAX(0, attempts-1), updated_ms=?"
                        " WHERE status='RUNNING'", (now_ms(),)).rowcount


def counts(conn: sqlite3.Connection) -> dict:
    out: dict = {}
    for r in conn.execute("SELECT kind, status, COUNT(*) n FROM sync_task GROUP BY kind, status"):
        out.setdefault(r["kind"], {})[r["status"]] = r["n"]
    return out


def pending_count(conn: sqlite3.Connection, kinds: tuple[str, ...] | None = None) -> int:
    if kinds:
        q = "SELECT COUNT(*) FROM sync_task WHERE status IN ('PENDING','RUNNING') AND kind IN (%s)" % ",".join(
            "?" * len(kinds))
        return conn.execute(q, kinds).fetchone()[0]
    return conn.execute("SELECT COUNT(*) FROM sync_task WHERE status IN ('PENDING','RUNNING')").fetchone()[0]


def purge_done(conn: sqlite3.Connection, older_than_ms: int) -> int:
    return conn.execute("DELETE FROM sync_task WHERE status='DONE' AND updated_ms < ?", (older_than_ms,)).rowcount
