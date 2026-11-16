"""저부하 요청 제어: 토큰 버킷 + AIMD 적응형 감속 + 서킷 브레이커 (계획서 §3.3 ②)."""

from __future__ import annotations

import collections
import statistics
import time


class AdaptiveRateLimiter:
    """
    - acquire(): 현재 속도에 맞춰 요청 간격을 벌린다.
    - record(): 응답 지연·오류를 반영. p95 지연이 기준의 2배 초과 또는 429/5xx → 속도 절반.
      정상 응답이 이어지면 상한(ceiling)까지 조금씩 회복.
    - set_ceiling(): 시간대별 예산이 바뀔 때 상한 조정.
    - 서킷: 연속 실패 N회 → open_seconds 동안 요청 금지.
    """

    def __init__(self, ceiling: float, min_rate: float = 0.1, *, clock=time.monotonic, sleep=time.sleep,
                 breaker_failures: int = 5, breaker_open_s: float = 600.0):
        self.ceiling = float(ceiling)
        self.rate = float(ceiling)
        self.min_rate = min_rate
        self._clock = clock
        self._sleep = sleep
        self._next_at = clock()
        self._baseline_ms: float | None = None
        self._recent: collections.deque = collections.deque(maxlen=20)
        self._ok_streak = 0
        self._failures = 0
        self.breaker_failures = breaker_failures
        self.breaker_open_s = breaker_open_s
        self.open_until: float = 0.0
        self.slowdowns = 0

    # -- 예산 ---------------------------------------------------------------
    def set_ceiling(self, ceiling: float) -> None:
        self.ceiling = max(self.min_rate, float(ceiling))
        if self.rate > self.ceiling:
            self.rate = self.ceiling

    @property
    def circuit_open(self) -> bool:
        return self._clock() < self.open_until

    def seconds_until_closed(self) -> float:
        return max(0.0, self.open_until - self._clock())

    # -- 요청 간격 -----------------------------------------------------------
    def acquire(self) -> None:
        now = self._clock()
        if now < self._next_at:
            self._sleep(self._next_at - now)
            now = self._next_at
        self._next_at = now + 1.0 / self.rate

    # -- 피드백 --------------------------------------------------------------
    def record(self, latency_ms: float, ok: bool, throttled: bool = False) -> None:
        if ok and not throttled:
            self._failures = 0
        else:
            self._failures += 1
            if self._failures >= self.breaker_failures:
                self.open_until = self._clock() + self.breaker_open_s
                self._failures = 0

        self._recent.append(latency_ms)
        if self._baseline_ms is None and len(self._recent) >= 5:
            self._baseline_ms = statistics.median(self._recent)
        slow = False
        if self._baseline_ms is not None and len(self._recent) >= 5:
            ordered = sorted(self._recent)
            p95 = ordered[max(0, int(len(ordered) * 0.95) - 1)]
            slow = p95 > max(self._baseline_ms * 2, self._baseline_ms + 200)

        if throttled or not ok or slow:
            new_rate = max(self.min_rate, self.rate / 2)
            if new_rate < self.rate:
                self.slowdowns += 1
            self.rate = new_rate
            self._ok_streak = 0
            self._recent.clear()
        else:
            self._ok_streak += 1
            if self._ok_streak >= 20 and self.rate < self.ceiling:
                self.rate = min(self.ceiling, self.rate + 0.1 * self.ceiling)
                self._ok_streak = 0

    @property
    def baseline_ms(self) -> float | None:
        return self._baseline_ms
