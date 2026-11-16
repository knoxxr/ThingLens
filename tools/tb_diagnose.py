#!/usr/bin/env python3
"""ThingLens 사전 진단 스크립트 (ThingsBoard 읽기 전용).

운영 ThingsBoard에 아주 적은 부하로 접속해 다음을 측정한다.
  - 디바이스/에셋 수, 프로파일 분포
  - 엔티티별 텔레메트리/속성 키 수, 관계 수
  - 키별 보고 주기 (키당 최근 원시값 N개만 조회)
  - TB API 응답 지연 (스로틀링 기준값)
  - 위 결과로 본 수집 요청량·소요 시간·로컬 저장 용량 추정

설계 원칙
  - Python 3.8+ 표준 라이브러리만 사용 (폐쇄망에서 파일 하나만 복사해 실행)
  - GET 및 조회용 POST(entitiesQuery)만 사용, TB 데이터를 절대 변경하지 않음
  - 토큰 버킷 속도 제한(기본 2 req/s) + 응답 지연/오류 시 자동 감속
  - Ctrl+C 로 중단해도 그때까지의 결과로 리포트 작성

출력 (--out 디렉터리)
  - summary.md   : 외부 공유용 요약 (엔티티 이름·ID 미포함, 프로파일·키 이름만 포함)
  - summary.json : 위 요약의 기계 판독용
  - details.jsonl: 엔티티별 상세 (사내 보관용, 엔티티 이름·ID 포함)

사용 예
  python3 tb_diagnose.py --url https://tb.example.local --username reader@example.local
  (비밀번호는 TB_PASSWORD 환경변수 또는 실행 시 입력)
"""

import argparse
import collections
import getpass
import json
import math
import os
import ssl
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

__version__ = "0.1.0"

DAY_MS = 86_400_000

# 보고 주기 등급 (개발 계획서 §3.5)
TIER_HIGH = "high"          # < 10초
TIER_MID = "mid"            # 10초 ~ 5분
TIER_LOW = "low"            # > 5분 또는 불규칙
TIER_INACTIVE = "inactive"  # 조회 기간 내 데이터 부족
TIER_ORDER = [TIER_HIGH, TIER_MID, TIER_LOW, TIER_INACTIVE]
TIER_LABEL = {
    TIER_HIGH: "고빈도 (<10초)",
    TIER_MID: "중빈도 (10초~5분)",
    TIER_LOW: "저빈도·불규칙 (>5분)",
    TIER_INACTIVE: "비활성·판단불가",
}

# 수집 정책 상수 (계획서 §3.5, §3.7)
MAX_POINTS_PER_REQUEST = 50_000
HIGH_CHUNK_HOURS = 6
KEYS_PER_REQUEST = 5
LOW_CHUNK_DAYS = 7


# --------------------------------------------------------------------------- #
# 속도 제한
# --------------------------------------------------------------------------- #
class AdaptiveRateLimiter:
    """토큰 버킷 + AIMD. 지연이 기준의 2배를 넘거나 429/5xx면 속도를 절반으로."""

    def __init__(self, rate, min_rate=0.2, clock=time.monotonic, sleep=time.sleep):
        self.max_rate = float(rate)
        self.rate = float(rate)
        self.min_rate = min_rate
        self._clock = clock
        self._sleep = sleep
        self._next_at = clock()
        self._baseline_ms = None
        self._recent = collections.deque(maxlen=20)
        self._ok_streak = 0
        self.slowdowns = 0

    def acquire(self):
        now = self._clock()
        if now < self._next_at:
            self._sleep(self._next_at - now)
            now = self._next_at
        self._next_at = now + 1.0 / self.rate

    def record(self, latency_ms, ok, throttled=False):
        self._recent.append(latency_ms)
        if self._baseline_ms is None and len(self._recent) >= 5:
            self._baseline_ms = statistics.median(self._recent)
        slow = False
        if self._baseline_ms is not None and len(self._recent) >= 5:
            p95 = sorted(self._recent)[int(len(self._recent) * 0.95) - 1]
            slow = p95 > max(self._baseline_ms * 2, self._baseline_ms + 200)
        if throttled or not ok or slow:
            self._decrease()
        else:
            self._ok_streak += 1
            if self._ok_streak >= 20 and self.rate < self.max_rate:
                self.rate = min(self.max_rate, self.rate + 0.1 * self.max_rate)
                self._ok_streak = 0

    def _decrease(self):
        new_rate = max(self.min_rate, self.rate / 2)
        if new_rate < self.rate:
            self.slowdowns += 1
        self.rate = new_rate
        self._ok_streak = 0
        self._recent.clear()  # 감속 후 새로 측정


# --------------------------------------------------------------------------- #
# ThingsBoard 클라이언트 (읽기 전용)
# --------------------------------------------------------------------------- #
class TbError(Exception):
    def __init__(self, status, message):
        super().__init__("HTTP %s: %s" % (status, message))
        self.status = status


class TbClient:
    def __init__(self, base_url, username, password, limiter, timeout=30,
                 ssl_context=None, max_retries=3, sleep=time.sleep, log=print):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self._password = password
        self.limiter = limiter
        self.timeout = timeout
        self.ssl_context = ssl_context
        self.max_retries = max_retries
        self._sleep = sleep
        self._log = log
        self._token = None
        self._refresh_token = None
        self.request_count = 0
        self.error_count = 0
        self.latencies = collections.defaultdict(list)  # category -> [ms]
        self._consecutive_failures = 0

    # -- 인증 ------------------------------------------------------------- #
    def login(self):
        data = self._raw("POST", "/api/auth/login",
                         body={"username": self.username, "password": self._password},
                         auth=False, category="auth")
        self._token = data["token"]
        self._refresh_token = data.get("refreshToken")
        self._password = None  # 메모리에 오래 두지 않음

    def _refresh(self):
        if not self._refresh_token:
            raise TbError(401, "토큰이 만료되었고 refresh token이 없습니다")
        data = self._raw("POST", "/api/auth/token",
                         body={"refreshToken": self._refresh_token},
                         auth=False, category="auth")
        self._token = data["token"]
        self._refresh_token = data.get("refreshToken", self._refresh_token)

    # -- 요청 ------------------------------------------------------------- #
    def get(self, path, params=None, category="other"):
        return self._request("GET", path, params=params, category=category)

    def query(self, path, body, category="query"):
        """조회 전용 POST (entitiesQuery 등)."""
        return self._request("POST", path, body=body, category=category)

    def _request(self, method, path, params=None, body=None, category="other"):
        attempt = 0
        refreshed = False
        while True:
            try:
                return self._raw(method, path, params=params, body=body, category=category)
            except TbError as e:
                if e.status == 401 and not refreshed:
                    refreshed = True
                    self._refresh()
                    continue
                retryable = e.status in (429, 500, 502, 503, 504) or e.status is None
                if not retryable or attempt >= self.max_retries:
                    raise
                attempt += 1
                wait = getattr(e, "retry_after", None) or min(60, 2 ** attempt)
                self._log("  재시도 %d/%d (%s), %.0f초 대기" % (attempt, self.max_retries, e, wait))
                self._sleep(wait)

    def _raw(self, method, path, params=None, body=None, auth=True, category="other"):
        if self._consecutive_failures >= 5:
            self._log("  연속 오류 5회 — 60초 동안 요청을 멈춥니다")
            self._sleep(60)
            self._consecutive_failures = 0

        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params, safe=",")
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if auth and self._token:
            headers["X-Authorization"] = "Bearer " + self._token
        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        self.limiter.acquire()
        started = time.monotonic()
        self.request_count += 1
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ssl_context) as resp:
                raw = resp.read()
            latency = (time.monotonic() - started) * 1000
            self.latencies[category].append(latency)
            self.limiter.record(latency, ok=True)
            self._consecutive_failures = 0
            return json.loads(raw.decode("utf-8")) if raw else None
        except urllib.error.HTTPError as e:
            latency = (time.monotonic() - started) * 1000
            throttled = e.code == 429
            server_err = e.code >= 500
            self.limiter.record(latency, ok=not server_err, throttled=throttled)
            if throttled or server_err:
                self._consecutive_failures += 1
                self.error_count += 1  # 404 등 조회 실패는 부하 오류로 세지 않음
            try:
                msg = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                msg = e.reason
            err = TbError(e.code, msg)
            ra = e.headers.get("Retry-After") if e.headers else None
            if ra and ra.isdigit():
                err.retry_after = int(ra)
            raise err
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            latency = (time.monotonic() - started) * 1000
            self.limiter.record(latency, ok=False)
            self._consecutive_failures += 1
            self.error_count += 1
            raise TbError(None, "연결 오류: %s" % e)


# --------------------------------------------------------------------------- #
# 분석 함수 (순수 함수 — 단위 테스트 대상)
# --------------------------------------------------------------------------- #
def estimate_interval(timestamps_ms):
    """원시값 타임스탬프로 보고 주기(초)와 규칙성을 추정. 점이 3개 미만이면 None."""
    ts = sorted(set(int(t) for t in timestamps_ms))
    if len(ts) < 3:
        return None
    diffs = [b - a for a, b in zip(ts, ts[1:]) if b > a]
    if len(diffs) < 2:
        return None
    diffs.sort()
    median_s = statistics.median(diffs) / 1000.0
    p10 = diffs[max(0, int(len(diffs) * 0.1))]
    p90 = diffs[min(len(diffs) - 1, int(len(diffs) * 0.9))]
    irregular = p10 > 0 and (p90 / p10) > 5
    return {"interval_s": median_s, "irregular": irregular, "points": len(ts)}


def classify_tier(interval_info):
    if interval_info is None:
        return TIER_INACTIVE
    if interval_info["irregular"]:
        return TIER_LOW
    s = interval_info["interval_s"]
    if s < 10:
        return TIER_HIGH
    if s <= 300:
        return TIER_MID
    return TIER_LOW


def high_tier_requests_per_day(interval_s):
    """고빈도 키 1개를 하루치 수집하는 데 필요한 요청 수 (요청당 원시값 상한 반영)."""
    interval_s = max(interval_s, 0.001)
    chunk_hours = min(HIGH_CHUNK_HOURS, MAX_POINTS_PER_REQUEST * interval_s / 3600.0)
    return 24.0 / chunk_hours


def project_collection(entities, rate, backfill_days_high=7, backfill_days_other=30):
    """엔티티별 키 등급으로 백필/일일 증분 요청 수, 소요 시간, 저장량을 추정."""
    backfill_high = backfill_other = daily = 0.0
    raw_points_per_day = 0.0
    agg_rows_per_day = 0.0
    for ent in entities:
        by_tier = collections.defaultdict(list)
        for k in ent.get("ts_keys_info", {}).values():
            by_tier[k["tier"]].append(k)
        for k in by_tier[TIER_HIGH]:
            rpd = high_tier_requests_per_day(k["interval_s"])
            backfill_high += rpd * backfill_days_high
            daily += rpd
        n_mid = len(by_tier[TIER_MID])
        if n_mid:
            rpd = math.ceil(n_mid / KEYS_PER_REQUEST)
            backfill_other += rpd * backfill_days_other
            daily += rpd
        n_low = len(by_tier[TIER_LOW])
        if n_low:
            per_chunk = math.ceil(n_low / KEYS_PER_REQUEST)
            backfill_other += per_chunk * math.ceil(backfill_days_other / LOW_CHUNK_DAYS)
            daily += per_chunk  # 저빈도도 하루 1회 증분 확인
        for tier in (TIER_HIGH, TIER_MID, TIER_LOW):
            for k in by_tier[tier]:
                ppd = 86_400.0 / max(k["interval_s"], 0.001)
                raw_points_per_day += ppd
                agg_rows_per_day += min(1440.0, ppd)
    rate = max(rate, 0.01)
    # 1분 집계 1행 ≈ ts+avg+min+max+count, Parquet 압축 후 대략 16바이트로 가정 (러프 추정)
    agg_gb_per_year = agg_rows_per_day * 365 * 16 / 1e9
    return {
        "rate_req_per_s": rate,
        "backfill_high_days": backfill_days_high,
        "backfill_other_days": backfill_days_other,
        "backfill_high_requests": round(backfill_high),
        "backfill_other_requests": round(backfill_other),
        "backfill_hours": round((backfill_high + backfill_other) / rate / 3600, 2),
        "daily_incremental_requests": round(daily),
        "daily_incremental_minutes": round(daily / rate / 60, 1),
        "raw_points_per_day": round(raw_points_per_day),
        "agg_1min_rows_per_day": round(agg_rows_per_day),
        "agg_1min_gb_per_year_rough": round(agg_gb_per_year, 2),
    }


def percentile(values, p):
    if not values:
        return None
    v = sorted(values)
    idx = min(len(v) - 1, max(0, int(math.ceil(len(v) * p / 100.0)) - 1))
    return v[idx]


def build_summary(entities, client_stats, meta, projection):
    """외부 공유용 요약. 엔티티 이름·ID는 넣지 않는다."""
    summary = {"meta": meta, "client": client_stats, "projection": projection}
    by_type = collections.Counter(e["entity_type"] for e in entities)
    summary["entity_counts"] = dict(by_type)

    profiles = collections.defaultdict(lambda: {
        "entity_type": None, "count": 0, "ts_keys": collections.Counter(),
        "attr_key_counts": [], "relation_counts": [],
        "key_intervals": collections.defaultdict(list), "key_tiers": collections.defaultdict(collections.Counter),
    })
    tier_counts = collections.Counter()
    interval_hist = collections.Counter()
    for e in entities:
        p = profiles[(e["entity_type"], e.get("profile") or "(없음)")]
        p["entity_type"] = e["entity_type"]
        p["count"] += 1
        if e.get("attr_key_count") is not None:
            p["attr_key_counts"].append(e["attr_key_count"])
        if e.get("relation_count") is not None:
            p["relation_counts"].append(e["relation_count"])
        for key, info in e.get("ts_keys_info", {}).items():
            p["ts_keys"][key] += 1
            p["key_tiers"][key][info["tier"]] += 1
            tier_counts[info["tier"]] += 1
            if info.get("interval_s") is not None:
                p["key_intervals"][key].append(info["interval_s"])
                interval_hist[_interval_bucket(info["interval_s"])] += 1
            else:
                interval_hist["판단불가"] += 1

    prof_out = []
    for (etype, name), p in sorted(profiles.items(), key=lambda x: (-x[1]["count"], x[0])):
        keys_out = []
        for key, n in p["ts_keys"].most_common():
            iv = p["key_intervals"].get(key) or []
            keys_out.append({
                "key": key,
                "entities_with_key": n,
                "interval_s_median": round(statistics.median(iv), 3) if iv else None,
                "interval_s_min": round(min(iv), 3) if iv else None,
                "tiers": dict(p["key_tiers"][key]),
            })
        prof_out.append({
            "entity_type": etype,
            "profile": name,
            "count": p["count"],
            "attr_keys_avg": round(statistics.mean(p["attr_key_counts"]), 1) if p["attr_key_counts"] else None,
            "relations_avg": round(statistics.mean(p["relation_counts"]), 1) if p["relation_counts"] else None,
            "ts_keys": keys_out,
        })
    summary["profiles"] = prof_out
    summary["tier_counts"] = {t: tier_counts.get(t, 0) for t in TIER_ORDER}
    summary["interval_histogram"] = dict(interval_hist)
    summary["total_ts_keys"] = sum(tier_counts.values())
    no_rel = sum(1 for e in entities if e["entity_type"] == "DEVICE" and e.get("relation_count") == 0
                 and e.get("relation_to_count") == 0)
    summary["devices_without_relations"] = no_rel
    return summary


INTERVAL_BUCKETS = [(1.5, "≤1초"), (10, "1~10초"), (60, "10초~1분"), (300, "1~5분"),
                    (3600, "5분~1시간"), (float("inf"), ">1시간")]


def _interval_bucket(s):
    for limit, label in INTERVAL_BUCKETS:
        if s <= limit:
            return label
    return ">1시간"


def render_markdown(summary):
    m = summary["meta"]
    c = summary["client"]
    pj = summary["projection"]
    L = []
    L.append("# ThingLens 사전 진단 리포트")
    L.append("")
    L.append("- 진단 시각: %s" % m["started_at"])
    L.append("- 스크립트 버전: %s" % m["script_version"])
    L.append("- 완료 여부: %s" % ("완료" if m["completed"] else "**중단됨 (부분 결과)**"))
    if m.get("error"):
        L.append("- 중단 원인: %s" % m["error"])
    L.append("- 사용자 권한: %s" % m.get("authority", "알 수 없음"))
    L.append("- TB 버전 정보: %s" % (m.get("tb_version") or "API로 확인 불가 — TB 화면에서 확인 필요"))
    L.append("- PE 엔티티 그룹 API: %s" % m.get("pe_entity_groups", "확인 안 함"))
    L.append("- 소요 시간: %.1f분, 요청 %d건, 부하 오류(429·5xx·연결) %d건, 자동 감속 %d회"
             % (m["elapsed_s"] / 60, c["requests"], c["errors"], c["slowdowns"]))
    L.append("")
    L.append("## 1. 엔티티 규모")
    L.append("")
    L.append("| 유형 | 수 |")
    L.append("|---|---|")
    for t, n in sorted(summary["entity_counts"].items()):
        L.append("| %s | %d |" % (t, n))
    L.append("| 텔레메트리 키 (엔티티×키) | %d |" % summary["total_ts_keys"])
    L.append("| 관계가 전혀 없는 디바이스 | %d |" % summary["devices_without_relations"])
    L.append("")
    L.append("## 2. 보고 주기 분포")
    L.append("")
    L.append("| 등급 | 키 수 |")
    L.append("|---|---|")
    for t in TIER_ORDER:
        L.append("| %s | %d |" % (TIER_LABEL[t], summary["tier_counts"][t]))
    L.append("")
    L.append("| 보고 주기 | 키 수 |")
    L.append("|---|---|")
    order = [lbl for _, lbl in INTERVAL_BUCKETS] + ["판단불가"]
    for lbl in order:
        if lbl in summary["interval_histogram"]:
            L.append("| %s | %d |" % (lbl, summary["interval_histogram"][lbl]))
    L.append("")
    L.append("## 3. 수집량 추정 (속도 %.1f req/s 기준)" % pj["rate_req_per_s"])
    L.append("")
    L.append("| 항목 | 값 |")
    L.append("|---|---|")
    L.append("| 초기 백필 요청 (고빈도 %d일) | %s |" % (pj["backfill_high_days"], f"{pj['backfill_high_requests']:,}"))
    L.append("| 초기 백필 요청 (나머지 %d일) | %s |" % (pj["backfill_other_days"], f"{pj['backfill_other_requests']:,}"))
    L.append("| 초기 백필 소요 | 약 %.1f시간 |" % pj["backfill_hours"])
    L.append("| 일일 증분 요청 | %s건 (약 %.1f분) |" % (f"{pj['daily_incremental_requests']:,}", pj["daily_incremental_minutes"]))
    L.append("| 하루 원시값 발생량 | %s건 |" % f"{pj['raw_points_per_day']:,}")
    L.append("| 1분 집계 저장량 (대략) | 연 %.1fGB |" % pj["agg_1min_gb_per_year_rough"])
    L.append("")
    L.append("## 4. TB API 응답 지연 (스로틀링 기준값)")
    L.append("")
    L.append("| 요청 종류 | 건수 | p50 ms | p95 ms |")
    L.append("|---|---|---|---|")
    for cat, st in sorted(c["latency"].items()):
        L.append("| %s | %d | %s | %s |" % (cat, st["count"], st["p50_ms"], st["p95_ms"]))
    L.append("")
    L.append("## 5. 프로파일별 키 (상위)")
    L.append("")
    for p in summary["profiles"]:
        L.append("### %s · %s (%d개)" % (p["entity_type"], p["profile"], p["count"]))
        L.append("")
        extra = []
        if p["attr_keys_avg"] is not None:
            extra.append("평균 속성 키 %s개" % p["attr_keys_avg"])
        if p["relations_avg"] is not None:
            extra.append("평균 관계(from) %s개" % p["relations_avg"])
        if extra:
            L.append("- " + ", ".join(extra))
            L.append("")
        if not p["ts_keys"]:
            L.append("텔레메트리 키 없음")
            L.append("")
            continue
        L.append("| 키 | 보유 엔티티 | 주기 중앙값(초) | 최소 주기(초) | 등급 분포 |")
        L.append("|---|---|---|---|---|")
        for k in p["ts_keys"][:30]:
            tiers = ", ".join("%s %d" % (TIER_LABEL[t].split(" ")[0], n) for t, n in sorted(k["tiers"].items()))
            L.append("| `%s` | %d | %s | %s | %s |" % (
                k["key"].replace("|", "\\|"), k["entities_with_key"],
                k["interval_s_median"] if k["interval_s_median"] is not None else "-",
                k["interval_s_min"] if k["interval_s_min"] is not None else "-", tiers))
        if len(p["ts_keys"]) > 30:
            L.append("")
            L.append("… 외 %d개 키 (summary.json 참고)" % (len(p["ts_keys"]) - 30))
        L.append("")
    L.append("---")
    L.append("이 리포트에는 엔티티 이름·ID가 포함되지 않습니다. 엔티티별 상세는 details.jsonl(사내 보관용)에 있습니다.")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------- #
# 진단 실행
# --------------------------------------------------------------------------- #
def _entity_query_body(entity_type, page, page_size):
    return {
        "entityFilter": {"type": "entityType", "entityType": entity_type},
        "entityFields": [
            {"type": "ENTITY_FIELD", "key": "name"},
            {"type": "ENTITY_FIELD", "key": "type"},
            {"type": "ENTITY_FIELD", "key": "createdTime"},
        ],
        "latestValues": [],
        "pageLink": {
            "page": page,
            "pageSize": page_size,
            "sortOrder": {"key": {"type": "ENTITY_FIELD", "key": "createdTime"}, "direction": "ASC"},
        },
    }


def _field(row, key):
    try:
        return row["latest"]["ENTITY_FIELD"][key]["value"]
    except (KeyError, TypeError):
        return None


class Diagnoser:
    def __init__(self, client, args, log=print, now_ms=None):
        self.c = client
        self.args = args
        self.log = log
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))
        self.entities = []
        self.meta = {}

    def run(self):
        started = time.monotonic()
        self.meta = {
            "script_version": __version__,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "completed": False,
            "rate_limit": self.args.rate,
            "sample_points": self.args.sample_points,
        }
        try:
            self.log("[1/4] 로그인 및 환경 확인")
            self.c.login()
            self._probe_environment()
            self.log("[2/4] 엔티티 목록 조회")
            for etype in ("DEVICE", "ASSET"):
                self._list_entities(etype)
            total = len(self.entities)
            self.log("      디바이스 %d개, 에셋 %d개" % (
                sum(1 for e in self.entities if e["entity_type"] == "DEVICE"),
                sum(1 for e in self.entities if e["entity_type"] == "ASSET")))
            est = self._estimate_requests()
            self.log("      예상 요청 약 %d건, 예상 소요 약 %.0f분 (%.1f req/s)" % (
                est, est / self.args.rate / 60, self.args.rate))
            self.log("[3/4] 엔티티별 키·보고 주기 측정")
            for i, ent in enumerate(self.entities, 1):
                self._inspect_entity(ent)
                if i % 25 == 0 or i == total:
                    self.log("      %d/%d (현재 속도 %.2f req/s, 요청 %d건)" % (
                        i, total, self.c.limiter.rate, self.c.request_count))
            self.meta["completed"] = True
        except KeyboardInterrupt:
            self.log("\n중단되었습니다. 지금까지의 결과로 리포트를 작성합니다.")
        except TbError as e:
            if not self.entities:
                raise  # 로그인·목록 단계 실패는 리포트 없이 종료
            self.meta["error"] = str(e)
            self.log("\n오류로 중단되었습니다 (%s). 지금까지의 결과로 리포트를 작성합니다." % e)
        finally:
            self.meta["elapsed_s"] = time.monotonic() - started
        self.log("[4/4] 리포트 작성")
        return self._write_reports()

    def _probe_environment(self):
        try:
            user = self.c.get("/api/auth/user", category="meta")
            self.meta["authority"] = user.get("authority")
        except TbError as e:
            self.meta["authority"] = "확인 실패 (%s)" % e.status
        # 버전 정보: 버전·권한에 따라 제공되지 않을 수 있어 실패해도 계속 진행
        for path in ("/api/system/info",):
            try:
                info = self.c.get(path, category="meta")
                if isinstance(info, dict):
                    ver = info.get("version") or info.get("tbVersion")
                    if ver:
                        self.meta["tb_version"] = str(ver)
                        break
            except TbError:
                pass
        try:
            groups = self.c.get("/api/entityGroups/DEVICE", params={"pageSize": 1, "page": 0}, category="meta")
            if isinstance(groups, dict):
                n = groups.get("totalElements")
            elif isinstance(groups, list):
                n = len(groups)
            else:
                n = None
            self.meta["pe_entity_groups"] = "사용 가능 (디바이스 그룹 %s개)" % (n if n is not None else "?")
        except TbError as e:
            self.meta["pe_entity_groups"] = "사용 불가 (HTTP %s)" % e.status

    def _list_entities(self, etype):
        page = 0
        while True:
            if self._reached_max():
                return
            body = _entity_query_body(etype, page, 100)
            data = self.c.query("/api/entitiesQuery/find", body, category="entity_list")
            for row in data.get("data", []):
                if self._reached_max():
                    return
                self.entities.append({
                    "entity_type": etype,
                    "id": row["entityId"]["id"],
                    "name": _field(row, "name"),
                    "profile": _field(row, "type"),
                    "created_time": _field(row, "createdTime"),
                })
            if not data.get("hasNext"):
                return
            page += 1

    def _reached_max(self):
        return bool(self.args.max_entities) and len(self.entities) >= self.args.max_entities

    def _estimate_requests(self):
        per_entity = 1 + 1.3  # 키 목록 + 샘플(재조회 포함 대략)
        if not self.args.skip_attributes:
            per_entity += 1
        if not self.args.skip_relations:
            per_entity += 2
        return int(len(self.entities) * per_entity)

    def _inspect_entity(self, ent):
        etype, eid = ent["entity_type"], ent["id"]
        base = "/api/plugins/telemetry/%s/%s" % (etype, eid)
        try:
            keys = self.c.get(base + "/keys/timeseries", category="keys") or []
        except TbError as e:
            ent["error"] = "keys/timeseries: %s" % e
            keys = []
        ent["ts_keys"] = keys

        if not self.args.skip_attributes:
            try:
                attrs = self.c.get(base + "/keys/attributes", category="keys") or []
                ent["attr_key_count"] = len(attrs)
            except TbError as e:
                ent.setdefault("error", "keys/attributes: %s" % e)

        if not self.args.skip_relations:
            try:
                rel_from = self.c.get("/api/relations/info",
                                      params={"fromId": eid, "fromType": etype}, category="relations") or []
                rel_to = self.c.get("/api/relations/info",
                                    params={"toId": eid, "toType": etype}, category="relations") or []
                ent["relation_count"] = len(rel_from)
                ent["relation_to_count"] = len(rel_to)
                ent["relation_types"] = sorted(set(r.get("type") for r in rel_from + rel_to if r.get("type")))
            except TbError as e:
                ent.setdefault("error", "relations: %s" % e)

        ent["ts_keys_info"] = self._sample_keys(base, keys)

    def _sample_keys(self, base, keys):
        """키당 최근 원시값 N개. 1일 → 부족하면 30일로 한 번 더 (Cassandra 파티션 스캔 최소화)."""
        result = {}
        pending = list(keys)
        for lookback_days in (1, 30):
            if not pending:
                break
            still_short = []
            for i in range(0, len(pending), 20):
                batch = pending[i:i + 20]
                end = self.now_ms()
                params = {
                    "keys": ",".join(batch),
                    "startTs": end - lookback_days * DAY_MS,
                    "endTs": end,
                    "limit": self.args.sample_points,
                    "agg": "NONE",
                    "orderBy": "DESC",
                }
                try:
                    data = self.c.get(base + "/values/timeseries", params=params, category="samples") or {}
                except TbError:
                    data = {}
                for key in batch:
                    pts = data.get(key) or []
                    info = estimate_interval([p["ts"] for p in pts if "ts" in p])
                    last_ts = max((p["ts"] for p in pts if "ts" in p), default=None)
                    entry = {
                        "interval_s": info["interval_s"] if info else None,
                        "irregular": info["irregular"] if info else None,
                        "points": len(pts),
                        "last_ts": last_ts,
                        "lookback_days": lookback_days,
                        "value_kind": _value_kind(pts),
                    }
                    entry["tier"] = classify_tier(info)
                    result[key] = entry
                    if info is None:
                        still_short.append(key)
            pending = still_short
        return result

    def _write_reports(self):
        out = self.args.out
        os.makedirs(out, exist_ok=True)
        client_stats = {
            "requests": self.c.request_count,
            "errors": self.c.error_count,
            "slowdowns": self.c.limiter.slowdowns,
            "final_rate": round(self.c.limiter.rate, 3),
            "latency": {
                cat: {"count": len(v), "p50_ms": round(percentile(v, 50)), "p95_ms": round(percentile(v, 95))}
                for cat, v in self.c.latencies.items() if v
            },
        }
        projection = project_collection(self.entities, self.args.plan_rate)
        summary = build_summary(self.entities, client_stats, self.meta, projection)
        with open(os.path.join(out, "summary.json"), "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        with open(os.path.join(out, "summary.md"), "w", encoding="utf-8") as f:
            f.write(render_markdown(summary))
        with open(os.path.join(out, "details.jsonl"), "w", encoding="utf-8") as f:
            for e in self.entities:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        self.log("완료: %s" % os.path.abspath(out))
        self.log("  - summary.md   (외부 공유 가능: 엔티티 이름·ID 없음)")
        self.log("  - details.jsonl (사내 보관용)")
        return summary


def _value_kind(points):
    """샘플값의 대략적 형태 (숫자/불리언/문자열/JSON)."""
    kinds = collections.Counter()
    for p in points:
        v = p.get("value")
        if isinstance(v, bool):
            kinds["boolean"] += 1
            continue
        if isinstance(v, (int, float)):
            kinds["number"] += 1
            continue
        if isinstance(v, str):
            s = v.strip()
            if s.lower() in ("true", "false"):
                kinds["boolean"] += 1
            elif s.startswith("{") or s.startswith("["):
                kinds["json"] += 1
            else:
                try:
                    float(s)
                    kinds["number"] += 1
                except ValueError:
                    kinds["string"] += 1
    return kinds.most_common(1)[0][0] if kinds else None


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="ThingLens 사전 진단 (ThingsBoard 읽기 전용, 저부하)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--url", required=True, help="ThingsBoard 주소 (예: https://tb.example.local)")
    ap.add_argument("--username", required=True, help="읽기 전용 계정 (PE Generic Role 권장)")
    ap.add_argument("--rate", type=float, default=2.0, help="최대 요청 속도 (req/s)")
    ap.add_argument("--plan-rate", type=float, default=2.0,
                    help="수집량 추정에 쓸 운영 수집 속도 (req/s)")
    ap.add_argument("--sample-points", type=int, default=20, help="키당 조회할 최근 원시값 수")
    ap.add_argument("--max-entities", type=int, default=0, help="조회할 최대 엔티티 수 (0=전체, 시험용)")
    ap.add_argument("--skip-attributes", action="store_true", help="속성 키 조회 생략")
    ap.add_argument("--skip-relations", action="store_true", help="관계 조회 생략")
    ap.add_argument("--out", default="tb_diagnose_report", help="리포트 출력 디렉터리")
    ap.add_argument("--timeout", type=float, default=30, help="요청 타임아웃(초)")
    ap.add_argument("--ca-bundle", help="사내 CA 인증서 파일 (자체 서명 인증서용)")
    ap.add_argument("--insecure", action="store_true",
                    help="TLS 인증서 검증 끄기 (권장하지 않음, --ca-bundle 사용 권장)")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)
    if args.plan_rate <= 0:
        ap.error("--plan-rate 는 0보다 커야 합니다")
    if args.rate <= 0 or args.rate > 20:
        ap.error("--rate 는 0 초과 20 이하로 지정하세요")
    if args.sample_points < 3 or args.sample_points > 1000:
        ap.error("--sample-points 는 3~1000 사이로 지정하세요")
    return args


def make_ssl_context(args):
    if args.insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        print("경고: TLS 인증서 검증을 끈 상태로 접속합니다.", file=sys.stderr)
        return ctx
    if args.ca_bundle:
        return ssl.create_default_context(cafile=args.ca_bundle)
    return None


def main(argv=None):
    args = parse_args(argv)
    password = os.environ.get("TB_PASSWORD") or getpass.getpass("ThingsBoard 비밀번호: ")
    limiter = AdaptiveRateLimiter(args.rate)
    client = TbClient(args.url, args.username, password, limiter,
                      timeout=args.timeout, ssl_context=make_ssl_context(args))
    try:
        Diagnoser(client, args).run()
    except TbError as e:
        print("오류: %s" % e, file=sys.stderr)
        if e.status == 401:
            print("  계정/비밀번호를 확인하세요.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
