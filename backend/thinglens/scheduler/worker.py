"""백그라운드 수집 워커 (별도 프로세스: `thinglens scheduler`).

한 번에 하나의 작업만 순서대로 처리한다 (동시 요청 1). 매 단계마다
  1) 시간대 예산으로 속도 상한 조정  2) 일시정지·서킷 확인  3) 주기 도래 시 동기화 주기 생성
  4) 우선순위가 가장 높은 작업 1개 실행
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from ..config import Settings
from ..db import now_ms
from ..sync.handlers import HANDLERS, Ctx
from ..tb.adapter import TbAdapter
from ..tb.client import CircuitOpen, TbClient, TbError
from . import queue as q
from .budget import window_for

log = logging.getLogger("thinglens.scheduler")


class Worker:
    def __init__(self, conn, client: TbClient, settings: Settings, *, clock=None):
        self.conn = conn
        self.client = client
        self.settings = settings
        self.ctx = Ctx(conn, TbAdapter(client, settings.page_size), settings)
        self._clock = clock or (lambda: datetime.now(ZoneInfo(settings.timezone)))
        self._last_housekeeping = 0
        client._on_request = self._log_request
        q.reset_running(conn)  # 비정상 종료 후 재시작 → 이어서 수행

    # -- 요청 로그 ------------------------------------------------------------
    def _log_request(self, category, latency_ms, status):
        self.conn.execute("INSERT INTO tb_request_log (ts_ms, category, latency_ms, status) VALUES (?, ?, ?, ?)",
                          (now_ms(), category, latency_ms, status))

    # -- 상태 ----------------------------------------------------------------
    def _state(self):
        return self.conn.execute("SELECT * FROM scheduler_state WHERE id=1").fetchone()

    def _apply_budget(self) -> str:
        name, factor = window_for(self._clock(), self.settings.rate_windows, self.settings.rate_default_factor)
        self.client.limiter.set_ceiling(self.settings.rate_max * factor)
        return name

    def _heartbeat(self, window_name: str, error: str | None = None):
        lim = self.client.limiter
        open_until = now_ms() + int(lim.seconds_until_closed() * 1000) if lim.circuit_open else None
        self.conn.execute(
            "UPDATE scheduler_state SET current_rate=?, window_name=?, circuit_open_until_ms=?, last_heartbeat_ms=?,"
            " last_error=COALESCE(?, last_error) WHERE id=1",
            (round(lim.rate, 3), window_name, open_until, now_ms(), error))

    def maybe_schedule_cycle(self) -> bool:
        running = self.conn.execute("SELECT 1 FROM sync_cycle WHERE status='RUNNING' LIMIT 1").fetchone()
        if running:
            return False
        last = self.conn.execute("SELECT MAX(started_ms) FROM sync_cycle").fetchone()[0]
        due = last is None or now_ms() - last >= self.settings.meta_sync_interval_h * 3_600_000
        if due:
            return q.enqueue(self.conn, "START_CYCLE", {}, q.PRIO_CYCLE, "cycle") is not None
        return False

    def _housekeeping(self):
        if now_ms() - self._last_housekeeping < 3_600_000:
            return
        self._last_housekeeping = now_ms()
        cutoff = now_ms() - int(self.settings.request_log_retention_d * 86_400_000)
        self.conn.execute("DELETE FROM tb_request_log WHERE ts_ms < ?", (cutoff,))
        self.conn.execute("DELETE FROM session WHERE expires_ms < ?", (now_ms(),))
        q.purge_done(self.conn, now_ms() - 7 * 86_400_000)

    # -- 한 단계 -------------------------------------------------------------
    def step(self) -> str:
        window = self._apply_budget()
        self._housekeeping()
        if self._state()["paused"]:
            self._heartbeat(window)
            return "paused"
        if self.client.limiter.circuit_open:
            self._heartbeat(window)
            return "circuit_open"
        self.maybe_schedule_cycle()
        row = q.claim_next(self.conn)
        if row is None:
            self._heartbeat(window)
            return "idle"
        kind, payload = row["kind"], q.payload(row)
        try:
            result = HANDLERS[kind](self.ctx, payload)
            if result == "WAIT":
                q.release(self.conn, row["id"], 30_000)
            else:
                q.complete(self.conn, row["id"])
            self._heartbeat(window)
            return kind
        except CircuitOpen as e:
            q.release(self.conn, row["id"], int(e.seconds * 1000) + 1000)
            self._heartbeat(window, str(e))
            return "circuit_open"
        except (TbError, Exception) as e:  # noqa: BLE001 — 작업 단위로 격리
            outcome = q.fail(self.conn, row["id"], f"{type(e).__name__}: {e}")
            log.warning("작업 실패 %s %s → %s: %s", kind, payload, outcome, e)
            self._heartbeat(window, f"{kind}: {e}")
            return "failed"

    def run_forever(self, stop: threading.Event | None = None, idle_sleep: float = 2.0) -> None:
        stop = stop or threading.Event()
        log.info("스케줄러 시작 (상한 %.2f req/s)", self.settings.rate_max)
        while not stop.is_set():
            outcome = self.step()
            if outcome in ("idle", "paused", "circuit_open"):
                stop.wait(idle_sleep if outcome != "paused" else 5.0)

    def run_until_idle(self, max_steps: int = 100_000) -> int:
        """테스트·수동 실행용: 처리할 작업이 없을 때까지 실행. 실행한 작업 수 반환."""
        n = 0
        for _ in range(max_steps):
            outcome = self.step()
            if outcome in ("idle", "paused", "circuit_open"):
                # FINISH_CYCLE 이 WAIT 으로 미뤄진 경우 등 — 대기 작업이 있으면 바로 당겨서 계속
                pending = self.conn.execute(
                    "SELECT MIN(next_run_ms) FROM sync_task WHERE status='PENDING'").fetchone()[0]
                if pending is None or outcome != "idle":
                    return n
                self.conn.execute("UPDATE sync_task SET next_run_ms=? WHERE status='PENDING'", (now_ms(),))
                continue
            n += 1
        return n


def build_worker(conn, settings: Settings, transport=None, sleep=time.sleep) -> Worker:
    from ..tb.ratelimit import AdaptiveRateLimiter
    verify: bool | str = settings.tb_ca_bundle or settings.tb_verify_tls
    limiter = AdaptiveRateLimiter(settings.rate_max, settings.rate_min, sleep=sleep)
    client = TbClient(settings.tb_url, settings.tb_username, settings.tb_password, limiter,
                      timeout=settings.tb_timeout_s, verify=verify, transport=transport, sleep=sleep)
    return Worker(conn, client, settings)
