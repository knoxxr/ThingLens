from datetime import datetime

import pytest

from thinglens.db import now_ms
from thinglens.scheduler import queue as q
from thinglens.scheduler.budget import window_for
from thinglens.scheduler.planner import (TIER_HIGH, TIER_INACTIVE, TIER_LOW, TIER_MID, classify_tier,
                                         estimate_interval, plan_chunks)
from thinglens.config import DEFAULT_RATE_WINDOWS
from thinglens.tb.ratelimit import AdaptiveRateLimiter


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


# -- 속도 제한 ------------------------------------------------------------------
def test_limiter_spacing_and_ceiling():
    c = Clock()
    lim = AdaptiveRateLimiter(2.0, clock=c.now, sleep=c.sleep)
    for _ in range(5):
        lim.acquire()
    assert c.t == pytest.approx(2.0)
    lim.set_ceiling(0.5)
    assert lim.rate == 0.5


def test_limiter_backoff_recover_and_floor():
    lim = AdaptiveRateLimiter(2.0, min_rate=0.2)
    lim.record(10, ok=False, throttled=True)
    assert lim.rate == 1.0
    for _ in range(300):
        lim.record(10, ok=True)
    assert lim.rate == 2.0
    for _ in range(4):
        lim.record(10, ok=False)
    assert lim.rate == 0.2


def test_limiter_latency_spike_slows_down():
    lim = AdaptiveRateLimiter(2.0)
    for _ in range(10):
        lim.record(100, ok=True)
    for _ in range(5):
        lim.record(900, ok=True)
    assert lim.rate < 2.0


def test_circuit_breaker_opens_after_consecutive_failures():
    c = Clock()
    lim = AdaptiveRateLimiter(2.0, clock=c.now, sleep=c.sleep, breaker_failures=3, breaker_open_s=600)
    for _ in range(3):
        lim.record(10, ok=False)
    assert lim.circuit_open
    c.t += 601
    assert not lim.circuit_open


# -- 시간대 예산 ------------------------------------------------------------------
@pytest.mark.parametrize("when,expected", [
    (datetime(2026, 11, 16, 10, 0), ("업무시간", 0.2)),   # 월 10시
    (datetime(2026, 11, 16, 3, 0), ("야간", 1.0)),       # 월 03시
    (datetime(2026, 11, 15, 10, 0), ("기본", 0.5)),      # 일 10시
    (datetime(2026, 11, 16, 20, 0), ("기본", 0.5)),      # 월 20시
])
def test_budget_windows(when, expected):
    assert window_for(when, DEFAULT_RATE_WINDOWS, 0.5) == expected


def test_budget_overnight_window():
    w = [{"name": "심야", "days": "fri", "start": "22:00", "end": "02:00", "factor": 1.0}]
    assert window_for(datetime(2026, 11, 20, 23, 0), w, 0.3)[0] == "심야"   # 금 23시
    assert window_for(datetime(2026, 11, 21, 1, 0), w, 0.3)[0] == "심야"    # 토 01시 (금요일 창의 연장)
    assert window_for(datetime(2026, 11, 22, 1, 0), w, 0.3)[0] == "기본"    # 일 01시


# -- 보고 주기·분할 계획 -----------------------------------------------------------
def test_interval_tiers():
    assert classify_tier(estimate_interval([i * 1000 for i in range(20)])) == TIER_HIGH
    assert classify_tier(estimate_interval([i * 60_000 for i in range(20)])) == TIER_MID
    assert classify_tier(estimate_interval([i * 900_000 for i in range(20)])) == TIER_LOW
    assert classify_tier(estimate_interval([1, 2])) == TIER_INACTIVE
    irregular = [0, 1000, 2000, 600_000, 601_000, 3_600_000, 3_601_000, 9_000_000]
    assert classify_tier(estimate_interval(irregular)) == TIER_LOW


def test_plan_chunks_respects_point_cap():
    day = 86_400_000
    chunks = plan_chunks([("current", 1.0)], TIER_HIGH, 0, day, max_points=50_000)
    assert len(chunks) == 4  # 6시간 구간 (21,600점)
    assert all(c.expected_points <= 50_000 for c in chunks)
    assert chunks[0].start_ts == 0 and chunks[-1].end_ts == day
    fast = plan_chunks([("vib", 0.1)], TIER_HIGH, 0, day, max_points=50_000)
    assert all(c.expected_points <= 50_000 for c in fast) and len(fast) > 4


def test_plan_chunks_groups_mid_keys():
    keys = [(f"k{i}", 60.0) for i in range(7)]
    chunks = plan_chunks(keys, TIER_MID, 0, 3 * 86_400_000)
    assert {len(c.keys) for c in chunks} == {5, 2}
    assert len(chunks) == 6  # 2묶음 × 3일
    assert plan_chunks(keys, TIER_INACTIVE, 0, 1) == []


# -- 작업 큐 ---------------------------------------------------------------------
def test_queue_priority_dedupe_and_retry(conn):
    a = q.enqueue(conn, "B", {"x": 1}, 50)
    b = q.enqueue(conn, "A", {"x": 2}, 10, dedupe_key="same")
    assert q.enqueue(conn, "A", {"x": 3}, 10, dedupe_key="same") is None
    row = q.claim_next(conn)
    assert row["id"] == b and row["status"] == "RUNNING"
    assert q.fail(conn, b, "boom") == "RETRY"
    nxt = conn.execute("SELECT next_run_ms FROM sync_task WHERE id=?", (b,)).fetchone()[0]
    assert nxt > now_ms()
    assert q.claim_next(conn)["id"] == a  # b는 백오프 중
    q.complete(conn, a)
    # 완료 후에는 같은 dedupe_key 재등록 가능
    conn.execute("UPDATE sync_task SET status='DONE' WHERE id=?", (b,))
    assert q.enqueue(conn, "A", {}, 10, dedupe_key="same") is not None


def test_queue_gives_up_after_max_attempts(conn):
    t = q.enqueue(conn, "X", {}, 10)
    for _ in range(q.MAX_ATTEMPTS):
        conn.execute("UPDATE sync_task SET next_run_ms=0 WHERE id=?", (t,))
        q.claim_next(conn)
        outcome = q.fail(conn, t, "err")
    assert outcome == "FAILED"


def test_queue_reset_running_on_restart(conn):
    t = q.enqueue(conn, "X", {}, 10)
    q.claim_next(conn)
    assert q.reset_running(conn) == 1
    row = conn.execute("SELECT status, attempts FROM sync_task WHERE id=?", (t,)).fetchone()
    assert row["status"] == "PENDING" and row["attempts"] == 0
