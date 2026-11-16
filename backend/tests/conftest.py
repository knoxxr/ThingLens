import pytest

from thinglens.config import Settings
from thinglens.db import connect
from thinglens.scheduler.worker import build_worker

from fake_tb import FakeTb, build_fleet


@pytest.fixture
def settings(tmp_path):
    s = Settings()
    s.db_path = str(tmp_path / "t.db")
    s.tb_url = "http://tb.test"
    s.tb_username = "reader"
    s.tb_password = "pw"
    s.rate_max = 1000.0
    s.rate_windows = []
    s.rate_default_factor = 1.0
    s.page_size = 2  # 페이지 넘김까지 검증
    return s


@pytest.fixture
def conn(settings):
    c = connect(settings.db_path)
    yield c
    c.close()


@pytest.fixture
def tb():
    return FakeTb("CE")


@pytest.fixture
def fleet(tb):
    return build_fleet(tb)


@pytest.fixture
def worker(conn, settings, tb, fleet):
    return build_worker(conn, settings, transport=tb.transport(), sleep=lambda s: None)
