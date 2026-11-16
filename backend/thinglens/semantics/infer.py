"""데이터 종류·의미 추론 (계획서 §5.0). TB 요청 없이 저장된 샘플·범위로만 계산하는 순수 함수."""

from __future__ import annotations

import json
import math
import re
import statistics
from dataclasses import dataclass, field

from .dictionary import (ATTRIBUTE_ROLES, BINARY, CATEG, CONT, COUNTER, SCOPE_DEFAULT_ROLE, SEMANTICS,
                         TB_DYNAMIC_ATTRIBUTES, UNIT_TOKENS, UNKNOWN)

TIMESTAMP, JSON_, TEXT = "timestamp", "json", "text"
DATA_TYPE_KO = {CONT: "연속 수치", COUNTER: "누적 카운터", BINARY: "이진·상태", CATEG: "범주형",
                TIMESTAMP: "타임스탬프", JSON_: "JSON", TEXT: "식별자·텍스트", None: "판단 불가"}

AUTO_THRESHOLD = 0.7     # 이상이면 자동 판정
REVIEW_THRESHOLD = 0.4   # 이상이면 후보로 검토 대기, 미만이면 '모름'

_HANGUL = re.compile(r"[가-힣]+")


# --------------------------------------------------------------------------- #
# 토큰화
# --------------------------------------------------------------------------- #
def tokenize(key: str) -> list[str]:
    """camelCase·snake·kebab·숫자 경계로 분리, 소문자. 한글 덩어리는 그대로 토큰."""
    s = re.sub(r"([a-z])([A-Z])", r"\1_\2", key)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
    s = re.sub(r"([A-Za-z])(\d)", r"\1_\2", s)
    s = re.sub(r"(\d)([A-Za-z])", r"\1_\2", s)
    s = re.sub(r"([가-힣])(\d)", r"\1_\2", s)
    s = re.sub(r"(\d)([가-힣])", r"\1_\2", s)
    return [t for t in re.split(r"[^0-9A-Za-z가-힣]+", s.lower()) if t]


def compact(key: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", key.lower())


# --------------------------------------------------------------------------- #
# 값 파싱·통계·데이터 종류
# --------------------------------------------------------------------------- #
def _to_num(v):
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    if isinstance(v, str):
        s = v.strip()
        if s.lower() in ("true", "false"):
            return None
        try:
            f = float(s)
            return f if math.isfinite(f) else None
        except ValueError:
            return None
    return None


def _is_bool(v) -> bool:
    return isinstance(v, bool) or (isinstance(v, str) and v.strip().lower() in ("true", "false"))


@dataclass
class Stats:
    n: int = 0
    numeric_ratio: float = 0.0
    unique: int = 0
    min: float | None = None
    max: float | None = None
    p5: float | None = None
    p95: float | None = None
    mean: float | None = None
    std: float | None = None
    integer_ratio: float = 0.0
    monotonic_ratio: float | None = None   # 연속 값 중 증가·유지 비율
    decreases: int = 0
    constant: bool = False

    def as_dict(self) -> dict:
        return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


def compute_stats(points: list[tuple[int, object]]) -> Stats:
    """points: (ts, value) 목록 — 여러 시간대 창을 합친 것. ts 순 정렬 후 계산."""
    st = Stats()
    pts = sorted(points, key=lambda p: p[0])
    vals = [v for _, v in pts]
    st.n = len(vals)
    if not vals:
        return st
    st.unique = len({json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else str(v) for v in vals})
    st.constant = st.unique == 1
    nums = [x for x in (_to_num(v) for v in vals) if x is not None]
    st.numeric_ratio = len(nums) / len(vals)
    if nums:
        s = sorted(nums)
        st.min, st.max = s[0], s[-1]
        st.p5 = s[int(0.05 * (len(s) - 1))]
        st.p95 = s[int(math.ceil(0.95 * (len(s) - 1)))]
        st.mean = statistics.fmean(nums)
        st.std = statistics.pstdev(nums) if len(nums) > 1 else 0.0
        st.integer_ratio = sum(1 for x in nums if float(x).is_integer()) / len(nums)
        if len(nums) >= 3 and st.numeric_ratio > 0.95:
            diffs = [b - a for a, b in zip(nums, nums[1:])]
            st.monotonic_ratio = sum(1 for d in diffs if d >= 0) / len(diffs)
            st.decreases = sum(1 for d in diffs if d < 0)
    return st


def _looks_like_counter(st: Stats, windows: list[list[tuple[int, object]]] | None) -> bool:
    """누적 카운터 판정. 짧은 창 안에서는 느린 파형도 단조 증가처럼 보이므로,
    서로 떨어진 시간대 창이 있으면 '장기 평균 증가율 ≈ 국소 증가율'(누적의 성질)을 요구한다."""
    if not (st.monotonic_ratio is not None and st.monotonic_ratio >= 0.95 and st.max > st.min
            and st.unique >= max(5, st.n * 0.3)):
        return False
    wins = [sorted(((t, x) for t, x in ((t, _to_num(v)) for t, v in w) if x is not None)) for w in (windows or [])]
    wins = [w for w in wins if len(w) >= 3]
    if len(wins) >= 2:
        wins.sort(key=lambda w: w[0][0])
        old, new = wins[0], wins[-1]
        gap = new[-1][0] - old[0][0]
        if gap < 12 * 3_600_000:
            return False
        avg_rate = (new[-1][1] - old[0][1]) / gap
        span = new[-1][0] - new[0][0]
        local_rate = (new[-1][1] - new[0][1]) / span if span > 0 else 0
        if avg_rate <= 0 or local_rate < 0:
            return False
        ratio = local_rate / avg_rate
        return 0.1 <= ratio <= 10
    # 창이 하나뿐이면 6시간 이상 이어진 단조 증가만 인정
    span = (max(t for w in wins for t, _ in w) - min(t for w in wins for t, _ in w)) if wins else 0
    return span >= 6 * 3_600_000


def infer_data_type(points: list[tuple[int, object]], st: Stats | None = None,
                    windows: list[list[tuple[int, object]]] | None = None) -> str | None:
    st = st or compute_stats(points)
    windows = windows if windows is not None else [points]
    if st.n < 3:
        return None
    vals = [v for _, v in points]
    if all(_is_bool(v) for v in vals):
        return BINARY
    if st.numeric_ratio >= 0.95:
        if st.unique <= 2 and st.integer_ratio == 1.0 and st.min is not None and st.min >= 0 and st.max <= 1:
            return BINARY
        if st.integer_ratio == 1.0 and st.min is not None and (
                1e12 <= st.min and st.max < 3e12 or 1e9 <= st.min and st.max < 3e9):
            return TIMESTAMP
        if _looks_like_counter(st, windows):
            return COUNTER
        if st.integer_ratio == 1.0 and st.unique <= 10 and st.n >= 20:
            return CATEG
        return CONT
    strs = [v for v in vals if isinstance(v, str)]
    if strs and sum(1 for v in strs if v.strip()[:1] in ("{", "[")) / len(strs) > 0.8:
        return JSON_
    if st.unique <= max(10, int(st.n * 0.2)):
        return BINARY if st.unique == 2 else CATEG
    return TEXT


# --------------------------------------------------------------------------- #
# 의미 추론
# --------------------------------------------------------------------------- #
@dataclass
class Candidate:
    semantic: str
    score: float = 0.0
    evidence: list[str] = field(default_factory=list)

    def add(self, w: float, why: str):
        self.score += w
        self.evidence.append(f"{'+' if w >= 0 else ''}{w:.2f} {why}")


@dataclass
class SemanticResult:
    semantic: str
    unit: str | None
    unit_source: str | None   # suffix | default | rule
    confidence: float
    status: str               # AUTO | REVIEW | CONFIRMED
    evidence: list[str]
    alternatives: list[dict]


def _unit_from_tokens(tokens: list[str]) -> tuple[str, str] | None:
    # 마지막 토큰이 단위인 경우만 (예: temperature_c, power_kw). 첫 토큰 단위는 의미 이름일 수 있음
    if len(tokens) >= 2 and tokens[-1] in UNIT_TOKENS:
        return UNIT_TOKENS[tokens[-1]]
    return None


def infer_semantic(key: str, data_type: str | None, st: Stats | None, profile: str | None = None,
                   rule: dict | None = None) -> SemanticResult:
    """키 이름·단위·값 범위·데이터 종류·프로파일 문맥으로 의미 후보 점수화."""
    if rule:  # 사용자 확정
        sem = rule["semantic"]
        unit = rule.get("unit") or (SEMANTICS.get(sem, {}).get("default_unit"))
        return SemanticResult(sem, unit, "rule", 1.0, "CONFIRMED",
                              [f"사용자 확정 ({rule.get('created_by') or '-'})"], [])
    tokens = tokenize(key)
    ckey = compact(key)
    unit = _unit_from_tokens(tokens)
    name_tokens = tokens[:-1] if unit else tokens
    prof_tokens = set(tokenize(profile or ""))
    cands: list[Candidate] = []
    for sid, sd in SEMANTICS.items():
        c = Candidate(sid)
        aliases = [a.lower() for a in sd["aliases"]]
        if any(t in aliases for t in name_tokens):
            c.add(0.55, "키 이름 일치")
        elif any(len(a) >= 4 and a in ckey for a in aliases if not _HANGUL.fullmatch(a)) or \
                any(_HANGUL.fullmatch(a) and a in key for a in aliases):
            c.add(0.4, "키 이름에 포함")
        elif any(t in sd["weak"] for t in name_tokens):
            c.add(0.25, "약한 이름 단서(한 글자)")
        if unit:
            if sd["units"] and unit[0] == sd["units"]:
                c.add(0.2, f"단위 접미사 {unit[1]}")
            elif sd["units"]:
                c.add(-0.2, f"단위 {unit[1]} 불일치")
        if sid == "timestamp" and data_type == TIMESTAMP and c.score <= 0:
            c.add(0.5, "값이 epoch 시각 형태")
        if c.score <= 0 and not (prof_tokens & set(sd["aliases"])):
            continue  # 이름·단위 근거가 전혀 없으면 값 범위만으로는 후보로 올리지 않음
        if prof_tokens & set(a.lower() for a in sd["aliases"]):
            c.add(0.15, f"프로파일 이름({profile})")
        if data_type:
            if data_type in sd["types"]:
                c.add(0.2 if sd["typical"] is None else 0.1, f"데이터 종류({DATA_TYPE_KO[data_type]}) 부합")
            elif data_type == CATEG and CONT in sd["types"] and st and st.numeric_ratio >= 0.95:
                c.add(0.05, "정수 값 (연속 수치로 해석)")
            else:
                c.add(-0.3, f"데이터 종류({DATA_TYPE_KO.get(data_type, data_type)}) 불일치")
        if st and st.p5 is not None and sd["typical"]:
            lo, hi = sd["typical"]
            if lo <= st.p5 and st.p95 <= hi:
                c.add(0.15, f"값 범위 {st.p5:g}~{st.p95:g} 이 일반 범위 안")
            else:
                phys = sd["physical"]
                if phys and ((phys[0] is not None and st.p5 < phys[0]) or (phys[1] is not None and st.p95 > phys[1])):
                    c.add(-0.4, f"값 범위 {st.p5:g}~{st.p95:g} 이 물리 범위 밖")
        cands.append(c)
    cands.sort(key=lambda c: -c.score)
    alts = [{"semantic": c.semantic, "score": round(min(1, c.score), 2)} for c in cands[1:4] if c.score > 0.2]
    if not cands or cands[0].score < REVIEW_THRESHOLD:
        ev = cands[0].evidence if cands else ["이름·단위 단서 없음"]
        return SemanticResult(UNKNOWN, unit[1] if unit else None, "suffix" if unit else None,
                              round(max(0.0, cands[0].score) if cands else 0.0, 2), "REVIEW", ev,
                              [{"semantic": c.semantic, "score": round(min(1, c.score), 2)} for c in cands[:3]
                               if c.score > 0])
    best = cands[0]
    conf = round(max(0.0, min(1.0, best.score)), 2)
    # 1·2위가 근소하면 검토 (예: 'p' → 압력? 전력?)
    if len(cands) > 1 and cands[1].score > 0 and best.score - cands[1].score < 0.1:
        conf = min(conf, 0.6)
        best.evidence.append(f"2위 후보({cands[1].semantic})와 차이 작음")
    status = "AUTO" if conf >= AUTO_THRESHOLD else "REVIEW"
    if unit:
        u, src = unit[1], "suffix"
    else:
        u, src = SEMANTICS[best.semantic]["default_unit"], "default"
    return SemanticResult(best.semantic, u, src if u else None, conf, status, best.evidence, alts)


# --------------------------------------------------------------------------- #
# 속성 역할 (계획서 §5.0 (3))
# --------------------------------------------------------------------------- #
def classify_attribute(key: str, scope: str, value, change_count: int = 0) -> dict:
    tokens = tokenize(key)
    ckey = compact(key)
    role, why = None, ""
    if key in TB_DYNAMIC_ATTRIBUTES:  # TB가 자동 관리하는 접속·활동 상태 속성
        return {"role": "status", "role_reason": "TB 기본 상태 속성", "dynamic": True,
                "value_type": "number" if _to_num(value) is not None else ("boolean" if _is_bool(value) else "string")}
    for rid, rd in ATTRIBUTE_ROLES.items():
        aliases = [a.lower() for a in rd["aliases"]]
        if ckey in aliases or any(t in aliases for t in tokens):
            role, why = rid, "키 이름"
            break
    if role is None:
        role, why = SCOPE_DEFAULT_ROLE.get(scope, "metadata"), "범위(scope) 기본값"
    dynamic = key in TB_DYNAMIC_ATTRIBUTES or change_count >= 2
    return {"role": role, "role_reason": why, "dynamic": dynamic,
            "value_type": "number" if _to_num(value) is not None else
            ("boolean" if _is_bool(value) else "json" if isinstance(value, (dict, list)) else "string")}
