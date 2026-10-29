import contextlib
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest

from _load import load
import synth

tm = load("trader-kr-timemachine", "timemachine")
PLAN = {"symbol": "SYN001", "side": "buy", "date": "2026-10-28", "setup": "orb", "entry": 30450,
        "entry_time": "10:15", "entry_bar": 14, "stop": 30100, "target": 31150, "interval": "5m",
        "opening_range": {"high": 30300, "low": 29900, "minutes": 15}, "stop_rule": "or_mid",
        "commission_rate": 0.00015, "sell_tax_rate": 0.002}
NOW = datetime.datetime(2026, 10, 28, 10, 16)


class Run(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chart = synth.make_history()
        cls.plan = tm.normalize_plan(dict(PLAN))
        cls.doc = tm.time_machine(cls.chart, cls.plan, NOW, replays=2000)

    def test_pool_is_the_61_full_sessions_before_the_plan_day(self):
        m = self.doc["mc"]
        self.assertEqual(m["pool_sessions"], 61)
        self.assertEqual(m["bars_left"], 75 - 14)          # 10:15 ~ 15:20
        self.assertEqual(m["until"], "15:20")
        self.assertEqual(self.doc["data"]["short_sessions_left_out"], [])

    def test_outcomes_add_up_and_the_seed_repeats(self):
        m = self.doc["mc"]
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)
        again = tm.time_machine(self.chart, self.plan, NOW, replays=2000)
        self.assertEqual(again["mc"]["p_target"], m["p_target"])
        self.assertEqual(again["mc"]["avg_R"], m["avg_R"])
        other = tm.time_machine(self.chart, self.plan, NOW, replays=2000, seed=7)
        self.assertNotEqual(other["mc"]["sample_paths"][0], m["sample_paths"][0])

    def test_costs_lower_the_average_r(self):
        self.assertLess(self.doc["mc"]["avg_R_net"], self.doc["mc"]["avg_R"])

    def test_limits_come_from_the_previous_close(self):
        m = self.doc["mc"]
        self.assertIsNotNone(m["limits"])
        self.assertIn("전일 종가", m["limits_source"])

    def test_history_replays_the_same_rule(self):
        h = self.doc["history"]
        self.assertEqual(h["rule"]["stop_rule"], "or_mid")
        self.assertEqual(h["sessions"], 61)
        self.assertEqual(h["triggered"], h["target"] + h["stop"] + h["close"])
        for t in h["trades"]:
            self.assertLess(t["stop"], t["entry"])
            self.assertGreater(t["target"], t["entry"])

    def test_the_future_of_the_plan_day_is_not_used(self):
        cut = tm.time_machine(dict(self.chart), self.plan, NOW, replays=500)
        later = tm.time_machine(dict(self.chart), self.plan, NOW + datetime.timedelta(hours=4), replays=500)
        self.assertEqual(cut["mc"]["p_target"], later["mc"]["p_target"])   # 재료는 계획일 전 거래일뿐

    def test_refusals(self):
        short = dict(self.chart, bars=[b for b in self.chart["bars"]
                                       if b["t"] >= self.chart["bars"][-1]["t"] - 15 * 86400])
        with self.assertRaises(tm.TMError):
            tm.time_machine(short, self.plan, NOW, replays=100)
        with self.assertRaises(tm.TMError):
            tm.time_machine(self.chart, self.plan, NOW, replays=100, until="15:25")
        with self.assertRaises(tm.TMError):
            tm.time_machine(self.chart, self.plan, datetime.datetime(2026, 10, 28, 10, 12), replays=100)
        with self.assertRaises(tm.TMError):
            tm.normalize_plan(dict(PLAN, entry_time="10:17", entry_bar=None))
        with self.assertRaises(tm.TMError):
            tm.normalize_plan(dict(PLAN, session={"open": "10:00", "auction": "15:20"}))

    def test_clamp_holds_prices_inside_the_limits(self):
        pool = tm.build_pool(tm.full_sessions(tm.day_slots(self.chart), "2026-10-28")[0])
        m = tm.simulate(pool, 30450, 30100, 31150, 14, 75, replays=300, limits=(30200, 30700))
        for path in m["sample_paths"]:
            self.assertTrue(all(30200 - 1e-6 <= x <= 30700 + 1e-6 for x in path[1:]))
        self.assertEqual(m["p_target"], 0.0)               # 목표가가 상한 밖이라 닿을 수 없다

    def test_report_and_chart_render(self):
        lines = tm.report(self.doc)
        self.assertTrue(any("목표 먼저" in x for x in lines))
        self.assertTrue(any("비용 뺀 평균 R" in x for x in lines))
        art = tm.render_chart(self.doc)
        self.assertTrue(any(x.endswith("target") for x in art))


class Command(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.getcwd()
        os.chdir(self.tmp)
        os.makedirs("trader-kr")
        with open("trader-kr/plan.json", "w", encoding="utf-8") as fh:
            json.dump(PLAN, fh)
        with open("SYN001.json", "w", encoding="utf-8") as fh:
            json.dump(synth.make_history(), fh)

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_run_writes_the_result_and_sessions_trim(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = tm.main(["run", "--offline", "SYN001.json", "--now", "2026-10-28 10:16", "--replays", "500",
                            "--sessions", "40", "--write"])
        self.assertEqual(code, 0, out.getvalue())
        with open("trader-kr/timemachine.json", encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["mc"]["pool_sessions"], 40)
        self.assertIn("다시 돌리려면 --seed 19", out.getvalue())


if __name__ == "__main__":
    unittest.main()
