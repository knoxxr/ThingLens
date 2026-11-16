"""시간대별 요청 예산 (계획서 §3.3 ①)."""

from __future__ import annotations

from datetime import datetime, time as dtime

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _days(spec: str) -> set[int]:
    out: set[int] = set()
    for part in spec.lower().split(","):
        part = part.strip()
        if "-" in part:
            a, b = (DAYS.index(x) for x in part.split("-"))
            i = a
            while True:
                out.add(i)
                if i == b:
                    break
                i = (i + 1) % 7
        elif part:
            out.add(DAYS.index(part))
    return out


def _t(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def window_for(now: datetime, windows: list[dict], default_factor: float) -> tuple[str, float]:
    """현재 시각에 적용되는 (창 이름, 계수). 자정을 넘는 창(22:00~02:00)도 지원."""
    wd, t = now.weekday(), now.time()
    for w in windows:
        start, end = _t(w["start"]), _t(w["end"])
        days = _days(w.get("days", "mon-sun"))
        if start <= end:
            hit = wd in days and start <= t < end
        else:  # 자정 넘김: 시작 요일 기준
            hit = (wd in days and t >= start) or (((wd - 1) % 7) in days and t < end)
        if hit:
            return w.get("name", f'{w["start"]}-{w["end"]}'), float(w["factor"])
    return "기본", float(default_factor)
