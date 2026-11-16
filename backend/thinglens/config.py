"""설정. 모든 값은 환경변수로 덮어쓸 수 있다 (THINGLENS_*, TB_*)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

# 시간대별 수집 예산 (계획서 §3.3 ①). 먼저 일치하는 창이 적용되고, 없으면 default_factor.
DEFAULT_RATE_WINDOWS = [
    {"name": "야간", "days": "mon-sun", "start": "01:00", "end": "06:00", "factor": 1.0},
    {"name": "업무시간", "days": "mon-fri", "start": "08:00", "end": "19:00", "factor": 0.2},
]


def _env(name, default=None, cast=str):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return cast(v)


def _bool(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    db_path: str = "data/thinglens.db"
    timezone: str = "Asia/Seoul"

    # ThingsBoard 접속 (읽기 전용 계정)
    tb_url: str = "http://localhost:8080"
    tb_username: str = ""
    tb_password: str = ""
    tb_verify_tls: bool = True
    tb_ca_bundle: str | None = None
    tb_timeout_s: float = 30.0

    # 저부하 수집 (계획서 §3.3, §3.5)
    rate_max: float = 2.0                 # req/s 상한 (시간대 계수 곱하기 전)
    rate_min: float = 0.1
    rate_windows: list = field(default_factory=lambda: list(DEFAULT_RATE_WINDOWS))
    rate_default_factor: float = 0.5
    page_size: int = 100
    max_points_per_request: int = 50_000
    sample_points: int = 20

    # 주기
    meta_sync_interval_h: float = 6.0     # 엔티티 목록 동기화 주기
    detail_sync_interval_h: float = 24.0  # 엔티티 상세(관계·속성·키) 재확인 주기
    profile_interval_d: float = 7.0       # 키 보고 주기 재측정 주기
    request_log_retention_d: float = 7.0

    # 앱 로그인
    session_ttl_h: float = 12.0

    # 웹 화면 (빌드 결과 디렉터리). 없으면 API만 제공
    web_dir: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        s.db_path = _env("THINGLENS_DB", s.db_path)
        s.timezone = _env("THINGLENS_TZ", s.timezone)
        s.tb_url = _env("TB_URL", s.tb_url)
        s.tb_username = _env("TB_USERNAME", s.tb_username)
        s.tb_password = _env("TB_PASSWORD", s.tb_password)
        s.tb_verify_tls = _env("TB_VERIFY_TLS", s.tb_verify_tls, _bool)
        s.tb_ca_bundle = _env("TB_CA_BUNDLE", s.tb_ca_bundle)
        s.tb_timeout_s = _env("TB_TIMEOUT_S", s.tb_timeout_s, float)
        s.rate_max = _env("THINGLENS_RATE_MAX", s.rate_max, float)
        s.rate_default_factor = _env("THINGLENS_RATE_DEFAULT_FACTOR", s.rate_default_factor, float)
        windows = _env("THINGLENS_RATE_WINDOWS")
        if windows:
            s.rate_windows = json.loads(windows)
        s.max_points_per_request = _env("THINGLENS_MAX_POINTS_PER_REQUEST", s.max_points_per_request, int)
        s.sample_points = _env("THINGLENS_SAMPLE_POINTS", s.sample_points, int)
        s.meta_sync_interval_h = _env("THINGLENS_META_SYNC_INTERVAL_H", s.meta_sync_interval_h, float)
        s.detail_sync_interval_h = _env("THINGLENS_DETAIL_SYNC_INTERVAL_H", s.detail_sync_interval_h, float)
        s.profile_interval_d = _env("THINGLENS_PROFILE_INTERVAL_D", s.profile_interval_d, float)
        s.session_ttl_h = _env("THINGLENS_SESSION_TTL_H", s.session_ttl_h, float)
        s.web_dir = _env("THINGLENS_WEB_DIR", s.web_dir)
        return s
