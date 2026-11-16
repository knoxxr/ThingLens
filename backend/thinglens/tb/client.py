"""ThingsBoard REST 클라이언트 (읽기 전용).

- GET 과 조회용 POST(entitiesQuery)만 허용 — 쓰기 엔드포인트 호출은 코드 수준에서 차단
- JWT 로그인·토큰 갱신, 429/5xx 재시도(Retry-After 준수), 적응형 속도 제한, 서킷 브레이커
- 모든 요청의 지연·상태를 콜백(on_request)으로 보고 → tb_request_log 저장·모니터링
"""

from __future__ import annotations

import time
from typing import Callable

import httpx

from .ratelimit import AdaptiveRateLimiter

READ_ONLY_POST_PATHS = ("/api/entitiesQuery/find", "/api/entitiesQuery/count", "/api/auth/login", "/api/auth/token")


class TbError(Exception):
    def __init__(self, status: int | None, message: str, retry_after: float | None = None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.retry_after = retry_after


class CircuitOpen(TbError):
    def __init__(self, seconds: float):
        super().__init__(None, f"서킷 열림 — {seconds:.0f}초 후 재개")
        self.seconds = seconds


class TbClient:
    def __init__(self, base_url: str, username: str, password: str, limiter: AdaptiveRateLimiter, *,
                 timeout: float = 30.0, verify: bool | str = True, transport: httpx.BaseTransport | None = None,
                 max_retries: int = 3, sleep=time.sleep,
                 on_request: Callable[[str, float, int | None], None] | None = None):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self._password = password
        self.limiter = limiter
        self.max_retries = max_retries
        self._sleep = sleep
        self._on_request = on_request
        self._token: str | None = None
        self._refresh_token: str | None = None
        self.http = httpx.Client(base_url=self.base_url, timeout=timeout, verify=verify, transport=transport,
                                 headers={"Accept": "application/json"})

    def close(self) -> None:
        self.http.close()

    # -- 인증 ------------------------------------------------------------------
    def login(self) -> None:
        data = self._send("POST", "/api/auth/login",
                          json={"username": self.username, "password": self._password}, auth=False, category="auth")
        self._token, self._refresh_token = data["token"], data.get("refreshToken")

    def _refresh(self) -> None:
        if not self._refresh_token:
            self.login()
            return
        try:
            data = self._send("POST", "/api/auth/token", json={"refreshToken": self._refresh_token},
                              auth=False, category="auth")
            self._token, self._refresh_token = data["token"], data.get("refreshToken", self._refresh_token)
        except TbError:
            self.login()  # refresh 토큰도 만료 → 재로그인

    # -- 공개 API --------------------------------------------------------------
    def get(self, path: str, params: dict | None = None, category: str = "other"):
        return self._request("GET", path, params=params, category=category)

    def query(self, path: str, body: dict, category: str = "query"):
        if path not in READ_ONLY_POST_PATHS:
            raise ValueError(f"읽기 전용 클라이언트: POST {path} 는 허용되지 않음")
        return self._request("POST", path, json=body, category=category)

    # -- 내부 ------------------------------------------------------------------
    def _request(self, method, path, params=None, json=None, category="other"):
        attempt, refreshed = 0, False
        while True:
            try:
                if self._token is None:
                    self.login()  # 로그인 실패(5xx 등)도 아래 재시도 규칙을 따른다
                return self._send(method, path, params=params, json=json, category=category)
            except CircuitOpen:
                raise
            except TbError as e:
                if e.status == 401 and not refreshed:
                    refreshed = True
                    self._refresh()
                    continue
                retryable = e.status in (429, 500, 502, 503, 504) or e.status is None
                if not retryable or attempt >= self.max_retries:
                    raise
                attempt += 1
                self._sleep(e.retry_after or min(60, 2 ** attempt))

    def _send(self, method, path, params=None, json=None, auth=True, category="other"):
        if self.limiter.circuit_open:
            raise CircuitOpen(self.limiter.seconds_until_closed())
        headers = {"X-Authorization": f"Bearer {self._token}"} if auth and self._token else {}
        self.limiter.acquire()
        started = time.monotonic()
        try:
            resp = self.http.request(method, path, params=params, json=json, headers=headers)
        except httpx.HTTPError as e:
            latency = (time.monotonic() - started) * 1000
            self.limiter.record(latency, ok=False)
            self._report(category, latency, None)
            raise TbError(None, f"연결 오류: {e}") from e
        latency = (time.monotonic() - started) * 1000
        status = resp.status_code
        self._report(category, latency, status)
        if status < 400:
            self.limiter.record(latency, ok=True)
            return resp.json() if resp.content else None
        throttled, server_err = status == 429, status >= 500
        # 4xx(404 등)는 TB 부하 신호가 아니므로 감속하지 않음
        self.limiter.record(latency, ok=not server_err, throttled=throttled)
        ra = resp.headers.get("Retry-After")
        raise TbError(status, resp.text[:300], float(ra) if ra and ra.isdigit() else None)

    def _report(self, category, latency, status):
        if self._on_request:
            try:
                self._on_request(category, latency, status)
            except Exception:
                pass
