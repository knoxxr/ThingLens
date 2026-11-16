"""보고 주기 등급과 요청당 원시값 상한으로 시계열 수집 요청을 분할 (계획서 §3.5).

Phase 0 실측(docs/phase0/cassandra-benchmark.md)에 따라 모든 수집은 원시값(agg=NONE)으로 하고,
요청 1건이 가져오는 원시값 수(= TB/Cassandra가 읽는 양)를 max_points 이하로 제한한다.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

TIER_HIGH, TIER_MID, TIER_LOW, TIER_INACTIVE = "high", "mid", "low", "inactive"
HOUR_MS, DAY_MS = 3_600_000, 86_400_000

# 등급별 기본값: (요청당 최대 구간, 요청당 키 수)
TIER_DEFAULTS = {
    TIER_HIGH: (6 * HOUR_MS, 1),
    TIER_MID: (DAY_MS, 5),
    TIER_LOW: (7 * DAY_MS, 5),
}


def estimate_interval(timestamps_ms) -> dict | None:
    """원시 타임스탬프 → 보고 주기(초)·불규칙 여부. 점이 3개 미만이면 None."""
    ts = sorted(set(int(t) for t in timestamps_ms))
    if len(ts) < 3:
        return None
    diffs = sorted(b - a for a, b in zip(ts, ts[1:]) if b > a)
    if len(diffs) < 2:
        return None
    p10 = diffs[max(0, int(len(diffs) * 0.1))]
    p90 = diffs[min(len(diffs) - 1, int(len(diffs) * 0.9))]
    return {"interval_s": statistics.median(diffs) / 1000.0,
            "irregular": bool(p10 > 0 and p90 / p10 > 5), "points": len(ts)}


def classify_tier(info: dict | None) -> str:
    if info is None:
        return TIER_INACTIVE
    if info["irregular"]:
        return TIER_LOW
    s = info["interval_s"]
    return TIER_HIGH if s < 10 else TIER_MID if s <= 300 else TIER_LOW


@dataclass(frozen=True)
class FetchChunk:
    keys: tuple[str, ...]
    start_ts: int
    end_ts: int
    expected_points: int   # 키 합계 예상 원시값 수
    limit: int             # 키당 limit (예상치 + 여유)


def plan_chunks(keys_with_interval: list[tuple[str, float]], tier: str, start_ts: int, end_ts: int,
                max_points: int = 50_000) -> list[FetchChunk]:
    """
    같은 엔티티·같은 등급의 키들을 (키 묶음 × 시간 구간) 요청으로 나눈다.
    - 키 묶음 크기와 구간 길이는 등급 기본값에서 시작해, 예상 원시값이 max_points를 넘으면 구간을 줄인다.
    - 가장 빠른 키 기준으로 구간을 정한다 (묶음 안 최악의 경우).
    """
    if tier == TIER_INACTIVE or not keys_with_interval or end_ts <= start_ts:
        return []
    span_default, per_req = TIER_DEFAULTS[tier]
    keys_sorted = sorted(keys_with_interval, key=lambda kv: kv[1])
    chunks: list[FetchChunk] = []
    for i in range(0, len(keys_sorted), per_req):
        group = keys_sorted[i:i + per_req]
        pts_per_ms = sum(1.0 / (max(iv, 0.001) * 1000) for _, iv in group)
        span = min(span_default, int(max_points / pts_per_ms))
        span = max(span, 60_000)  # 최소 1분
        t = start_ts
        while t < end_ts:
            e = min(end_ts, t + span)
            expected = int(math.ceil((e - t) * pts_per_ms))
            fastest = group[0][1]
            per_key = int(math.ceil((e - t) / (max(fastest, 0.001) * 1000)))
            chunks.append(FetchChunk(tuple(k for k, _ in group), t, e, expected,
                                     min(max_points, int(per_key * 1.2) + 10)))
            t = e
    return chunks
