"""데이터 품질 점검·점수 ([B] 계획서 §13.2). 저장된 샘플·범위(envelope)로만 계산 — TB 추가 요청 없음."""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from ..semantics.dictionary import SEMANTICS
from ..semantics.infer import BINARY, CATEG, CONT, COUNTER, _to_num

CRITICAL, WARNING, INFO = "critical", "warning", "info"
PENALTY = {CRITICAL: 50, WARNING: 20, INFO: 5}
CHECK_KO = {
    "NO_DATA": "데이터 없음", "STALE": "보고 중단", "FLATLINE": "값 고정", "CONSTANT": "값 변화 없음",
    "OUT_OF_RANGE": "물리 범위 이탈", "SPIKE": "급격한 튐", "LOW_RECEIVE_RATE": "수신률 저하",
    "FUTURE_TS": "미래 타임스탬프", "COUNTER_RESET": "카운터 리셋", "MIXED_TYPE": "숫자·문자 혼재",
}
DAY_MS = 86_400_000


@dataclass
class Finding:
    check: str
    severity: str
    message: str
    detail: dict


def run_checks(*, now_ms: int, windows: list[list[tuple[int, object]]], interval_s: float | None,
               irregular: bool, tier: str | None, data_type: str | None, semantic: str | None,
               semantic_conf: float, envelope: dict | None, first_seen_ms: int | None) -> list[Finding]:
    """
    windows  : [최근 창, 1일 전 창, 7일 전 창 ...] 각 (ts, value) 목록 — 최근 창이 첫 번째
    envelope : {"start","end","min","max","count"} — TB 단일 구간 집계 (없을 수 있음)
    """
    out: list[Finding] = []
    latest = sorted(windows[0], key=lambda p: p[0]) if windows else []
    all_pts = [p for w in windows for p in w]
    last_ts = max((t for t, _ in all_pts), default=None)

    # 데이터 없음 / 보고 중단 -------------------------------------------------------
    if tier == "inactive" or last_ts is None:
        out.append(Finding("NO_DATA", CRITICAL if last_ts is None or now_ms - last_ts > 30 * DAY_MS else WARNING,
                           "최근 30일 동안 판단할 만큼의 데이터가 없습니다",
                           {"last_ts": last_ts}))
        return out
    age = now_ms - last_ts
    if interval_s and not irregular:
        threshold = max(10 * interval_s * 1000, 3_600_000)
        if age > threshold:
            sev = CRITICAL if age > DAY_MS else WARNING
            out.append(Finding("STALE", sev, f"마지막 값 이후 {_dur(age)} 동안 보고 없음 (평소 {_dur(interval_s * 1000)} 주기)",
                               {"last_ts": last_ts, "age_ms": age, "interval_s": interval_s}))

    # 미래 타임스탬프 -----------------------------------------------------------------
    future = [t for t, _ in all_pts if t > now_ms + 300_000]
    if future:
        out.append(Finding("FUTURE_TS", WARNING, f"현재보다 미래 시각의 값 {len(future)}개", {"max_ts": max(future)}))

    nums_latest = [x for x in (_to_num(v) for _, v in latest) if x is not None]
    # 숫자·문자 혼재 --------------------------------------------------------------------
    vals = [v for _, v in all_pts]
    numeric = [x for x in (_to_num(v) for v in vals) if x is not None]
    if vals and 0.05 < len(numeric) / len(vals) < 0.95:
        out.append(Finding("MIXED_TYPE", WARNING, f"숫자 {len(numeric)}개, 숫자가 아닌 값 {len(vals) - len(numeric)}개 혼재",
                           {"non_numeric_examples": [v for v in vals if _to_num(v) is None][:3]}))

    # 값 고정 -----------------------------------------------------------------------
    if data_type in (CONT, COUNTER) and len(nums_latest) >= 10 and len(set(nums_latest)) == 1:
        older = [x for w in windows[1:] for x in (_to_num(v) for _, v in w) if x is not None]
        if len(set(older)) > 1:
            out.append(Finding("FLATLINE", CRITICAL,
                               f"최근 값 {len(nums_latest)}개가 모두 {nums_latest[0]:g} 로 고정 (이전에는 변동)",
                               {"value": nums_latest[0], "since_ts": latest[0][0]}))
        elif data_type == CONT:
            out.append(Finding("CONSTANT", INFO, f"조회한 모든 구간에서 값이 {nums_latest[0]:g} 로 일정",
                               {"value": nums_latest[0]}))

    # 물리 범위 이탈 ----------------------------------------------------------------
    sd = SEMANTICS.get(semantic or "")
    if sd and sd.get("physical") and semantic_conf >= 0.6:
        lo, hi = sd["physical"]
        cand = numeric + ([envelope["min"], envelope["max"]] if envelope and envelope.get("min") is not None else [])
        bad = [x for x in cand if (lo is not None and x < lo) or (hi is not None and x > hi)]
        if bad:
            out.append(Finding("OUT_OF_RANGE", CRITICAL,
                               f"{sd['ko']} 물리 범위({_rng(lo, hi)}) 밖의 값 발견: {min(bad):g} ~ {max(bad):g}",
                               {"min": min(bad), "max": max(bad), "physical": [lo, hi],
                                "window": [envelope.get("start"), envelope.get("end")] if envelope else None}))

    # 급격한 튐 (최근 창, 로버스트 z) -----------------------------------------------------
    if data_type == CONT and len(nums_latest) >= 10:
        med = statistics.median(nums_latest)
        mad = statistics.median(abs(x - med) for x in nums_latest)
        if mad > 0:
            spikes = [x for x in nums_latest if abs(x - med) / (1.4826 * mad) > 10]
            if spikes:
                out.append(Finding("SPIKE", WARNING, f"최근 값 중 {len(spikes)}개가 주변 값과 크게 다름",
                                   {"examples": spikes[:3], "median": med}))

    # 수신률 저하 (envelope 의 개수 vs 기대 개수) -------------------------------------------
    if envelope and envelope.get("count") is not None and interval_s and not irregular and tier in ("high", "mid"):
        span = envelope["end"] - envelope["start"]
        covered = span if not first_seen_ms else min(span, envelope["end"] - first_seen_ms)
        # 보고 중단된 키는 STALE 로 이미 표시 — 마지막 값까지만 기대
        covered = min(covered, max(0, last_ts - envelope["start"]))
        expected = covered / (interval_s * 1000)
        if expected >= 20:
            rate = envelope["count"] / expected
            if rate < 0.7:
                out.append(Finding("LOW_RECEIVE_RATE", CRITICAL if rate < 0.3 else WARNING,
                                   f"수신률 {rate:.0%} (기대 {expected:.0f}개 중 {envelope['count']}개)",
                                   {"rate": round(rate, 3), "expected": round(expected), "count": envelope["count"]}))

    # 카운터 리셋 ---------------------------------------------------------------------
    if data_type == COUNTER:
        seq = [x for x in (_to_num(v) for _, v in sorted(all_pts, key=lambda p: p[0])) if x is not None]
        resets = sum(1 for a, b in zip(seq, seq[1:]) if b < a)
        if resets:
            out.append(Finding("COUNTER_RESET", INFO, f"누적값 감소(리셋) {resets}회", {"resets": resets}))
    return out


def score(findings: list[Finding]) -> int:
    return max(0, 100 - sum(PENALTY[f.severity] for f in findings))


def _dur(ms: float) -> str:
    s = ms / 1000
    if s < 120:
        return f"{s:.0f}초"
    if s < 7200:
        return f"{s / 60:.0f}분"
    if s < 172800:
        return f"{s / 3600:.0f}시간"
    return f"{s / 86400:.0f}일"


def _rng(lo, hi) -> str:
    return f"{'' if lo is None else f'{lo:g}'}~{'' if hi is None else f'{hi:g}'}"


_UNUSED = (BINARY, CATEG)  # 이진·범주형은 현재 품질 점검 대상 아님 (상태 변화는 정상 동작)
