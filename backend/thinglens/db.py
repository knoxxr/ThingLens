"""SQLite 저장소 (메타데이터·작업 큐·이력·사용자). WAL 모드로 API·스케줄러 두 프로세스가 공유한다."""

from __future__ import annotations

import json
import os
import sqlite3
import time

MIGRATIONS = [
    # v1 — Phase 1
    """
    CREATE TABLE entity (
        id TEXT PRIMARY KEY,
        entity_type TEXT NOT NULL,          -- DEVICE | ASSET
        name TEXT, label TEXT, profile TEXT,
        owner_id TEXT, owner_name TEXT,     -- 소유 고객 (없으면 NULL = 테넌트)
        active INTEGER,
        tb_version INTEGER,
        created_time INTEGER,
        first_seen_ms INTEGER, last_seen_ms INTEGER, deleted_ms INTEGER,
        detail_synced_ms INTEGER, profile_synced_ms INTEGER
    );
    CREATE INDEX entity_owner ON entity(owner_id);
    CREATE INDEX entity_type_name ON entity(entity_type, name);

    CREATE TABLE customer (
        id TEXT PRIMARY KEY, title TEXT, parent_id TEXT, last_seen_ms INTEGER
    );

    CREATE TABLE relation (
        from_id TEXT NOT NULL, from_type TEXT NOT NULL,
        to_id TEXT NOT NULL, to_type TEXT NOT NULL,
        rel_type TEXT NOT NULL, type_group TEXT NOT NULL DEFAULT 'COMMON',
        PRIMARY KEY (from_id, to_id, rel_type, type_group)
    );
    CREATE INDEX relation_to ON relation(to_id);

    CREATE TABLE attribute (
        entity_id TEXT NOT NULL, scope TEXT NOT NULL, key TEXT NOT NULL,
        value_json TEXT, last_update_ts INTEGER,
        PRIMARY KEY (entity_id, scope, key)
    );

    CREATE TABLE ts_key (
        entity_id TEXT NOT NULL, key TEXT NOT NULL, first_seen_ms INTEGER,
        PRIMARY KEY (entity_id, key)
    );

    CREATE TABLE key_profile (
        entity_id TEXT NOT NULL, key TEXT NOT NULL,
        interval_s REAL, irregular INTEGER, tier TEXT, points INTEGER,
        last_ts INTEGER, value_kind TEXT, profiled_ms INTEGER,
        PRIMARY KEY (entity_id, key)
    );

    CREATE TABLE sync_task (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL, priority INTEGER NOT NULL, payload TEXT NOT NULL,
        dedupe_key TEXT, status TEXT NOT NULL,   -- PENDING | RUNNING | DONE | FAILED
        attempts INTEGER NOT NULL DEFAULT 0,
        next_run_ms INTEGER NOT NULL, created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL,
        last_error TEXT
    );
    CREATE INDEX sync_task_pick ON sync_task(status, priority, next_run_ms);
    CREATE UNIQUE INDEX sync_task_dedupe ON sync_task(dedupe_key) WHERE status IN ('PENDING', 'RUNNING');

    CREATE TABLE sync_cycle (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_ms INTEGER NOT NULL, finished_ms INTEGER, status TEXT NOT NULL,
        entities_seen INTEGER DEFAULT 0, changes INTEGER DEFAULT 0
    );

    CREATE TABLE metadata_change (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_id INTEGER, ts_ms INTEGER NOT NULL,
        entity_id TEXT, entity_type TEXT,
        change_type TEXT NOT NULL, field TEXT,
        before_json TEXT, after_json TEXT
    );
    CREATE INDEX metadata_change_entity ON metadata_change(entity_id, ts_ms);
    CREATE INDEX metadata_change_ts ON metadata_change(ts_ms);

    CREATE TABLE scheduler_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        paused INTEGER NOT NULL DEFAULT 0, paused_by TEXT, paused_ms INTEGER,
        current_rate REAL, window_name TEXT, circuit_open_until_ms INTEGER,
        last_heartbeat_ms INTEGER, last_error TEXT
    );
    INSERT INTO scheduler_state (id, paused) VALUES (1, 0);

    CREATE TABLE tb_request_log (
        ts_ms INTEGER NOT NULL, category TEXT, latency_ms REAL, status INTEGER
    );
    CREATE INDEX tb_request_log_ts ON tb_request_log(ts_ms);

    CREATE TABLE app_user (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        login TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
        role TEXT NOT NULL,                 -- ADMIN | ANALYST | CUSTOMER_VIEWER
        customer_ids TEXT NOT NULL DEFAULT '[]',
        active INTEGER NOT NULL DEFAULT 1, created_ms INTEGER NOT NULL
    );

    CREATE TABLE session (
        token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires_ms INTEGER NOT NULL
    );

    CREATE TABLE audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_ms INTEGER NOT NULL, user_id INTEGER, login TEXT,
        action TEXT NOT NULL, target TEXT, detail_json TEXT
    );
    """,
    # v2 — 전체 재확인 주기 표시 (수동 '지금 동기화')
    """
    ALTER TABLE sync_cycle ADD COLUMN full INTEGER NOT NULL DEFAULT 0;
    """,
    # v3 — 워커가 실제로 적용 중인 설정 (API가 자기 설정 대신 이것을 보여주도록)
    """
    ALTER TABLE scheduler_state ADD COLUMN worker_config TEXT;
    """,
    # v4 — Phase 2: 샘플·범위·의미 추론·사용자 확정·품질
    """
    CREATE TABLE key_sample (
        entity_id TEXT NOT NULL, key TEXT NOT NULL,
        sampled_ms INTEGER NOT NULL,
        windows_json TEXT NOT NULL,          -- [[[ts, value], ...] 최근 창, 1일 전 창, 7일 전 창]
        PRIMARY KEY (entity_id, key)
    );
    CREATE TABLE key_envelope (
        entity_id TEXT NOT NULL, key TEXT NOT NULL, kind TEXT NOT NULL,   -- profile(7일) | daily(24시간)
        start_ts INTEGER, end_ts INTEGER, min REAL, max REAL, count INTEGER, computed_ms INTEGER,
        PRIMARY KEY (entity_id, key, kind)
    );
    CREATE TABLE key_analysis (
        entity_id TEXT NOT NULL, source TEXT NOT NULL, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL,
        data_type TEXT, stats_json TEXT,
        semantic TEXT, unit TEXT, unit_source TEXT, confidence REAL, status TEXT,
        evidence_json TEXT, alternatives_json TEXT,
        role TEXT, dynamic INTEGER,                         -- 속성 전용
        analyzed_ms INTEGER,
        PRIMARY KEY (entity_id, source, scope, key)
    );
    CREATE INDEX key_analysis_semantic ON key_analysis(semantic);
    CREATE TABLE semantic_rule (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        profile TEXT,                                       -- NULL = 모든 프로파일의 같은 키 이름
        key TEXT NOT NULL, semantic TEXT NOT NULL, unit TEXT,
        created_by TEXT, created_ms INTEGER NOT NULL,
        UNIQUE (profile, key)
    );
    CREATE TABLE quality_finding (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id TEXT NOT NULL, key TEXT NOT NULL, check_type TEXT NOT NULL,
        severity TEXT NOT NULL, message TEXT, detail_json TEXT,
        first_detected_ms INTEGER NOT NULL, last_detected_ms INTEGER NOT NULL, resolved_ms INTEGER
    );
    CREATE INDEX quality_finding_open ON quality_finding(entity_id, key, check_type, resolved_ms);
    CREATE TABLE quality_score (
        entity_id TEXT NOT NULL, key TEXT NOT NULL,         -- key '' = 엔티티 종합
        score INTEGER, computed_ms INTEGER,
        PRIMARY KEY (entity_id, key)
    );
    ALTER TABLE entity ADD COLUMN quality_checked_ms INTEGER;
    """,
    # v5 — Phase 3: [PE] 엔티티 그룹
    """
    CREATE TABLE entity_group (
        id TEXT PRIMARY KEY, group_type TEXT NOT NULL, name TEXT, owner_id TEXT, last_seen_ms INTEGER
    );
    CREATE TABLE entity_group_member (
        group_id TEXT NOT NULL, entity_id TEXT NOT NULL, PRIMARY KEY (group_id, entity_id)
    );
    CREATE INDEX entity_group_member_entity ON entity_group_member(entity_id);
    """,
]


def now_ms() -> int:
    return int(time.time() * 1000)


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] or 0
    for i, sql in enumerate(MIGRATIONS, start=1):
        if i <= current:
            continue
        conn.execute("BEGIN")
        for stmt in _split_sql(sql):
            conn.execute(stmt)
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (i,))
        conn.execute("COMMIT")


def _split_sql(script: str):
    buf = []
    for line in script.splitlines():
        stripped = line.split("--", 1)[0]
        buf.append(stripped)
        if stripped.rstrip().endswith(";"):
            stmt = "\n".join(buf).strip()
            if stmt.strip(";").strip():
                yield stmt
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        yield tail


class Tx:
    """with Tx(conn): ... — 짧은 쓰기 트랜잭션."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


def dumps(v) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def loads(s):
    return None if s is None else json.loads(s)
