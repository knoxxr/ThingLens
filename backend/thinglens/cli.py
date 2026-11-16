"""명령행: thinglens {init-db|user-add|scheduler|sync-once|api|status}"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import signal
import sys
import threading

from .config import Settings
from .db import connect


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="thinglens")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="DB 생성·마이그레이션")
    ua = sub.add_parser("user-add", help="앱 사용자 추가")
    ua.add_argument("login")
    ua.add_argument("--role", choices=["ADMIN", "ANALYST", "CUSTOMER_VIEWER"], required=True)
    ua.add_argument("--customers", default="", help="CUSTOMER_VIEWER 의 TB 고객 ID (쉼표 구분)")
    sub.add_parser("scheduler", help="백그라운드 수집 워커 실행 (상주)")
    so = sub.add_parser("sync-once", help="동기화 주기 1회를 끝까지 실행 후 종료 (점검·시험용)")
    so.add_argument("--force", action="store_true", help="주기 도래 전이라도 새 주기 시작")
    so.add_argument("--full", action="store_true", help="관계·속성·키까지 모든 엔티티 상세 재확인")
    api = sub.add_parser("api", help="REST API 서버 실행")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8000)
    sub.add_parser("status", help="스케줄러·큐 상태 출력")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = Settings.from_env()

    if args.cmd == "init-db":
        connect(s.db_path).close()
        print(f"DB 준비 완료: {s.db_path}")
        return 0

    if args.cmd == "user-add":
        from .auth.users import create_user
        pw = getpass.getpass("비밀번호: ")
        if pw != getpass.getpass("비밀번호 확인: "):
            print("비밀번호가 일치하지 않습니다", file=sys.stderr)
            return 1
        customers = [c.strip() for c in args.customers.split(",") if c.strip()]
        create_user(connect(s.db_path), args.login, pw, args.role, customers)
        print(f"사용자 추가: {args.login} ({args.role})")
        return 0

    if args.cmd in ("scheduler", "sync-once"):
        from .scheduler import queue as q
        from .scheduler.worker import build_worker
        if not s.tb_username or not s.tb_password:
            print("TB_USERNAME / TB_PASSWORD 환경변수를 설정하세요", file=sys.stderr)
            return 1
        conn = connect(s.db_path)
        w = build_worker(conn, s)
        if args.cmd == "sync-once":
            if args.force and not conn.execute("SELECT 1 FROM sync_cycle WHERE status='RUNNING'").fetchone():
                q.enqueue(conn, "START_CYCLE", {"full": args.full}, q.PRIO_CYCLE, "cycle")
            n = w.run_until_idle()
            print(f"처리한 작업 {n}개")
            return 0
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        w.run_forever(stop)
        return 0

    if args.cmd == "api":
        import uvicorn
        from .api.app import create_app
        uvicorn.run(create_app(s), host=args.host, port=args.port)
        return 0

    if args.cmd == "status":
        from .scheduler import queue as q
        conn = connect(s.db_path)
        st = dict(conn.execute("SELECT * FROM scheduler_state WHERE id=1").fetchone())
        st["queue"] = q.counts(conn)
        st["last_cycles"] = [dict(r) for r in conn.execute("SELECT * FROM sync_cycle ORDER BY id DESC LIMIT 3")]
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
