"""시드 데이터가 정답 파일(truth)에 적힌 시나리오를 실제로 담고 있는지 검증 (TB 불필요)."""

import os
import statistics
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import seed_thingsboard as seed  # noqa: E402

NOW = 1_800_000_000_000  # 고정 시각 → 결정적 결과


def series(dev, name, key):
    rows = dev[name]["rows"]
    return [(t, rows[t][key]) for t in sorted(rows) if key in rows[t]]


def corr(a, b):
    ma, mb = statistics.mean(a), statistics.mean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = (sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b)) ** 0.5
    return num / den


class SeedScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dev, cls.truth = seed.build_dataset(NOW, days=4)

    def test_deterministic(self):
        dev2, _ = seed.build_dataset(NOW, days=4)
        self.assertEqual(series(self.dev, "TS-A1", "temperature")[:50], series(dev2, "TS-A1", "temperature")[:50])

    def test_name_variants(self):
        for d, k in self.truth["scenarios"]["name_variants"]["keys"].items():
            self.assertTrue(series(self.dev, d, k), (d, k))

    def test_high_freq_and_lag(self):
        cur = [v for _, v in series(self.dev, "PUMP-A1", "current")]
        prs = [v for _, v in series(self.dev, "PUMP-A1", "p1_out")]
        ts = [t for t, _ in series(self.dev, "PUMP-A1", "current")]
        self.assertEqual(ts[1] - ts[0], 1000)
        lag = self.truth["scenarios"]["lagged_pair"]["lag_s"]
        n = 5000
        r0 = corr(cur[:n], prs[:n])
        r_lag = corr(cur[:n], prs[lag:n + lag])
        self.assertGreater(r_lag, 0.95)
        self.assertGreater(r_lag, r0)

    def test_stuck_sensor(self):
        vals = [v for _, v in series(self.dev, "TS-A4", "temperature")]
        self.assertEqual(set(vals[-3 * 1440:]), {22.0})
        self.assertGreater(len(set(vals[:1440])), 100)

    def test_broken_correlation(self):
        a1 = [v for _, v in series(self.dev, "TS-A1", "temperature")]
        a3 = [v for _, v in series(self.dev, "TS-A3", "Temperature_C")]
        cut = len(a1) - 2 * 1440
        self.assertGreater(corr(a1[:cut], a3[:cut]), 0.8)
        self.assertLess(corr(a1[cut:], a3[cut:]), corr(a1[:cut], a3[:cut]) - 0.3)

    def test_redundant_pair(self):
        a1 = [v for _, v in series(self.dev, "TS-A1", "temperature")]
        a5 = [v for _, v in series(self.dev, "TS-A5", "temperature")]
        self.assertGreater(corr(a1, a5), 0.99)

    def test_missing_relation_pattern(self):
        x1 = [v for _, v in series(self.dev, "TS-X1", "temperature")]
        b1 = [v for _, v in series(self.dev, "TS-B1", "temperature")]
        a1 = [v for _, v in series(self.dev, "TS-A1", "temperature")]
        self.assertIsNone(self.dev["TS-X1"]["parent"])
        self.assertGreater(corr(x1, b1), corr(x1, a1))

    def test_out_of_range_and_counter(self):
        hum = [v for _, v in series(self.dev, "TS-B2", "humidity")]
        self.assertEqual(hum.count(130.0), 15)
        energy = [v for _, v in series(self.dev, "METER-A1", "energy_kwh")]
        self.assertTrue(all(b >= a for a, b in zip(energy, energy[1:])))

    def test_attribute_mismatch_and_inactive(self):
        self.assertEqual(self.dev["TS-A5"]["attrs"]["location"], "Line-B")
        self.assertEqual(self.dev["TS-A5"]["parent"], "Line-A")
        self.assertEqual(len(series(self.dev, "OLD-1", "temperature")), 1)

    def test_refuses_remote_target(self):
        with self.assertRaises(SystemExit):
            seed.main(["--url", "https://tb.company.example", "--username", "x"])


if __name__ == "__main__":
    unittest.main()
