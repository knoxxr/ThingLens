import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.dirname(__file__))

import tb_diagnose as d  # noqa: E402
from mock_tb_server import MockTb, MockTbServer  # noqa: E402


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.slept = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept += s
        self.t += s


class IntervalTests(unittest.TestCase):
    def test_regular_one_second(self):
        info = d.estimate_interval([i * 1000 for i in range(20)])
        self.assertAlmostEqual(info["interval_s"], 1.0)
        self.assertFalse(info["irregular"])
        self.assertEqual(d.classify_tier(info), d.TIER_HIGH)

    def test_tiers(self):
        self.assertEqual(d.classify_tier(d.estimate_interval([i * 60_000 for i in range(10)])), d.TIER_MID)
        self.assertEqual(d.classify_tier(d.estimate_interval([i * 900_000 for i in range(10)])), d.TIER_LOW)
        self.assertEqual(d.classify_tier(None), d.TIER_INACTIVE)

    def test_too_few_points(self):
        self.assertIsNone(d.estimate_interval([1000, 2000]))
        self.assertIsNone(d.estimate_interval([]))

    def test_irregular_event_driven(self):
        ts = [0, 1000, 2000, 3000, 600_000, 601_000, 3_600_000, 3_601_000, 3_602_000, 9_000_000]
        info = d.estimate_interval(ts)
        self.assertTrue(info["irregular"])
        self.assertEqual(d.classify_tier(info), d.TIER_LOW)

    def test_unsorted_and_duplicates(self):
        info = d.estimate_interval([5000, 1000, 3000, 3000, 2000, 4000])
        self.assertAlmostEqual(info["interval_s"], 1.0)


class ProjectionTests(unittest.TestCase):
    def test_high_tier_chunking_respects_point_cap(self):
        # 1초 키: 6시간 = 21,600점 < 50,000 → 하루 4회
        self.assertAlmostEqual(d.high_tier_requests_per_day(1.0), 4.0)
        # 0.1초 키: 6시간이면 216,000점 → 구간 축소 (50,000점 = 5000초)
        self.assertAlmostEqual(d.high_tier_requests_per_day(0.1), 86_400 / 5000)

    def test_project_collection(self):
        ents = [{"ts_keys_info": {
            "fast": {"tier": d.TIER_HIGH, "interval_s": 1.0},
            "m1": {"tier": d.TIER_MID, "interval_s": 60.0},
            "m2": {"tier": d.TIER_MID, "interval_s": 60.0},
            "slow": {"tier": d.TIER_LOW, "interval_s": 3600.0},
            "dead": {"tier": d.TIER_INACTIVE, "interval_s": None},
        }}]
        p = d.project_collection(ents, rate=2.0)
        self.assertEqual(p["backfill_high_requests"], 4 * 7)        # 1키 × 4/일 × 7일
        self.assertEqual(p["backfill_other_requests"], 1 * 30 + 1 * 5)  # 중빈도 1묶음×30일 + 저빈도 1묶음×5구간
        self.assertEqual(p["daily_incremental_requests"], 4 + 1 + 1)
        self.assertEqual(p["raw_points_per_day"], 86_400 + 1440 * 2 + 24)
        self.assertEqual(p["agg_1min_rows_per_day"], 1440 * 3 + 24)


class RateLimiterTests(unittest.TestCase):
    def test_spacing(self):
        clk = FakeClock()
        rl = d.AdaptiveRateLimiter(2.0, clock=clk.now, sleep=clk.sleep)
        for _ in range(5):
            rl.acquire()
        self.assertAlmostEqual(clk.slept, 2.0)  # 5건 → 첫 건 즉시, 이후 0.5초 간격

    def test_backoff_on_429_and_recovery(self):
        clk = FakeClock()
        rl = d.AdaptiveRateLimiter(2.0, clock=clk.now, sleep=clk.sleep)
        rl.record(50, ok=False, throttled=True)
        self.assertAlmostEqual(rl.rate, 1.0)
        rl.record(50, ok=False, throttled=True)
        self.assertAlmostEqual(rl.rate, 0.5)
        for _ in range(200):
            rl.record(50, ok=True)
        self.assertAlmostEqual(rl.rate, 2.0)
        self.assertEqual(rl.slowdowns, 2)

    def test_backoff_on_latency_spike(self):
        rl = d.AdaptiveRateLimiter(2.0)
        for _ in range(10):
            rl.record(100, ok=True)
        for _ in range(5):
            rl.record(1000, ok=True)
        self.assertLess(rl.rate, 2.0)

    def test_min_rate_floor(self):
        rl = d.AdaptiveRateLimiter(1.0, min_rate=0.2)
        for _ in range(20):
            rl.record(10, ok=False)
        self.assertAlmostEqual(rl.rate, 0.2)


def _fleet():
    devices = []
    for i in range(7):
        devices.append({"id": "d%d" % i, "name": "Secret-Device-%d" % i, "profile": "TempSensor",
                        "ts_keys": {"temperature": 1.0, "humidity": 60.0, "battery": 3600.0},
                        "attr_keys": ["fw", "location"]})
    devices.append({"id": "d-dead", "name": "Secret-Dead", "profile": "TempSensor",
                    "ts_keys": {"temperature": None}, "attr_keys": []})
    assets = [{"id": "a0", "name": "Secret-Building", "profile": "Building", "ts_keys": {}, "attr_keys": ["addr"]}]
    relations = [("a0", "d%d" % i, "Contains") for i in range(7)]
    return devices, assets, relations


class EndToEndTests(unittest.TestCase):
    def run_diag(self, tb, extra=()):
        with MockTbServer(tb) as srv, tempfile.TemporaryDirectory() as out:
            args = d.parse_args(["--url", srv.url, "--username", "u", "--rate", "20", "--out", out] + list(extra))
            limiter = d.AdaptiveRateLimiter(args.rate)
            client = d.TbClient(srv.url, "u", "pw", limiter, sleep=lambda s: None, log=lambda *a: None)
            summary = d.Diagnoser(client, args, log=lambda *a: None).run()
            files = {}
            for n in ("summary.md", "summary.json", "details.jsonl"):
                with open(os.path.join(out, n), encoding="utf-8") as f:
                    files[n] = f.read()
            return summary, files, client

    def test_full_run(self):
        devices, assets, relations = _fleet()
        summary, files, client = self.run_diag(MockTb(devices, assets, relations))
        self.assertTrue(summary["meta"]["completed"])
        self.assertEqual(summary["entity_counts"], {"DEVICE": 8, "ASSET": 1})
        self.assertEqual(summary["tier_counts"][d.TIER_HIGH], 7)
        self.assertEqual(summary["tier_counts"][d.TIER_MID], 7)
        self.assertEqual(summary["tier_counts"][d.TIER_LOW], 7)
        self.assertEqual(summary["tier_counts"][d.TIER_INACTIVE], 1)
        self.assertEqual(summary["devices_without_relations"], 1)  # d-dead
        self.assertEqual(summary["meta"]["authority"], "TENANT_ADMIN")
        self.assertIn("사용 가능", summary["meta"]["pe_entity_groups"])
        temp = [p for p in summary["profiles"] if p["profile"] == "TempSensor"][0]
        tkey = [k for k in temp["ts_keys"] if k["key"] == "temperature"][0]
        self.assertEqual(tkey["interval_s_median"], 1.0)
        # 공유용 요약에 엔티티 이름·ID가 없어야 함
        for name in ("summary.md", "summary.json"):
            self.assertNotIn("Secret-", files[name])
            self.assertNotIn('"d1"', files[name])
        self.assertIn("Secret-Device-0", files["details.jsonl"])
        self.assertIn("# ThingLens 사전 진단 리포트", files["summary.md"])
        json.loads(files["summary.json"])

    def test_token_refresh_and_retry(self):
        devices, assets, relations = _fleet()
        tb = MockTb(devices, assets, relations, expire_token_after=5,
                    fail_once={"/api/plugins/telemetry/DEVICE/d3": 429})
        summary, _, client = self.run_diag(tb)
        self.assertTrue(summary["meta"]["completed"])
        self.assertGreaterEqual(tb.token_gen, 1)
        self.assertGreaterEqual(client.limiter.slowdowns, 1)
        self.assertEqual(summary["tier_counts"][d.TIER_HIGH], 7)

    def test_skip_flags_reduce_requests(self):
        devices, assets, relations = _fleet()
        tb = MockTb(devices, assets, relations)
        self.run_diag(tb, ["--skip-attributes", "--skip-relations"])
        paths = [p for _, p in tb.requests]
        self.assertFalse(any(p.endswith("keys/attributes") for p in paths))
        self.assertFalse(any(p == "/api/relations/info" for p in paths))

    def test_only_read_methods(self):
        devices, assets, relations = _fleet()
        tb = MockTb(devices, assets, relations)
        self.run_diag(tb)
        posts = {p for m, p in tb.requests if m == "POST"}
        self.assertLessEqual(posts, {"/api/auth/login", "/api/auth/token", "/api/entitiesQuery/find"})

    def test_bad_password(self):
        devices, assets, relations = _fleet()
        with MockTbServer(MockTb(devices, assets, relations)) as srv:
            limiter = d.AdaptiveRateLimiter(20)
            client = d.TbClient(srv.url, "u", "wrong", limiter, sleep=lambda s: None, log=lambda *a: None)
            with self.assertRaises(d.TbError) as cm:
                client.login()
            self.assertEqual(cm.exception.status, 401)

    def test_max_entities(self):
        devices, assets, relations = _fleet()
        summary, _, _ = self.run_diag(MockTb(devices, assets, relations), ["--max-entities", "3"])
        self.assertEqual(sum(summary["entity_counts"].values()), 3)


if __name__ == "__main__":
    unittest.main()
