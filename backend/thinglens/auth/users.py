"""앱 사용자·세션·고객 범위·감사 로그 ([D] 계획서 §13).

역할
  ADMIN           : 전체 조회 + 스케줄러 제어 + 사용자 관리 + 감사 로그
  ANALYST         : 전체 조회
  CUSTOMER_VIEWER : 지정 고객(및 하위 고객) 소유 엔티티만 조회
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3

from ..db import dumps, now_ms

ROLES = ("ADMIN", "ANALYST", "CUSTOMER_VIEWER")


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, dk_hex = stored.split("$")
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    expected = hash_password(password, bytes.fromhex(salt_hex)).split("$")[2]
    return hmac.compare_digest(expected, dk_hex)


def create_user(conn: sqlite3.Connection, login: str, password: str, role: str,
                customer_ids: list[str] | None = None) -> int:
    if role not in ROLES:
        raise ValueError(f"알 수 없는 역할: {role}")
    if role == "CUSTOMER_VIEWER" and not customer_ids:
        raise ValueError("CUSTOMER_VIEWER 는 고객 ID가 하나 이상 필요합니다")
    if len(password) < 8:
        raise ValueError("비밀번호는 8자 이상")
    cur = conn.execute("INSERT INTO app_user (login, password_hash, role, customer_ids, created_ms) VALUES (?,?,?,?,?)",
                       (login, hash_password(password), role, json.dumps(customer_ids or []), now_ms()))
    return cur.lastrowid


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def login(conn: sqlite3.Connection, login_name: str, password: str, ttl_h: float) -> str | None:
    u = conn.execute("SELECT * FROM app_user WHERE login=? AND active=1", (login_name,)).fetchone()
    if u is None or not verify_password(password, u["password_hash"]):
        audit(conn, None, login_name, "LOGIN_FAILED")
        return None
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO session (token_hash, user_id, expires_ms) VALUES (?, ?, ?)",
                 (_token_hash(token), u["id"], now_ms() + int(ttl_h * 3_600_000)))
    audit(conn, u["id"], u["login"], "LOGIN")
    return token


def logout(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM session WHERE token_hash=?", (_token_hash(token),))


def user_for_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT u.* FROM session s JOIN app_user u ON u.id = s.user_id"
        " WHERE s.token_hash=? AND s.expires_ms > ? AND u.active=1", (_token_hash(token), now_ms())).fetchone()


def audit(conn: sqlite3.Connection, user_id, login_name, action: str, target: str | None = None,
          detail: dict | None = None) -> None:
    conn.execute("INSERT INTO audit_log (ts_ms, user_id, login, action, target, detail_json) VALUES (?,?,?,?,?,?)",
                 (now_ms(), user_id, login_name, action, target, dumps(detail) if detail else None))


def customer_scope(conn: sqlite3.Connection, user: sqlite3.Row) -> set[str] | None:
    """조회 가능한 소유 고객 ID 집합. None = 제한 없음 (ADMIN/ANALYST). 하위 고객 포함."""
    if user["role"] in ("ADMIN", "ANALYST"):
        return None
    roots = set(json.loads(user["customer_ids"] or "[]"))
    children: dict[str, list[str]] = {}
    for r in conn.execute("SELECT id, parent_id FROM customer WHERE parent_id IS NOT NULL"):
        children.setdefault(r["parent_id"], []).append(r["id"])
    scope, stack = set(), list(roots)
    while stack:
        c = stack.pop()
        if c in scope:
            continue
        scope.add(c)
        stack.extend(children.get(c, []))
    return scope
