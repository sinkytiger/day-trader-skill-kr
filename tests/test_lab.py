import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

from _load import load
import synth

lab = load("trader-kr-lab", "lab")
COST = {"commission_rate": 0.00015, "sell_tax_rate": 0.002, "slip_ticks": 1, "max_cost_to_risk": 0.3}


def second_symbol():
    ch = synth.make_history(seed=23)
    return dict(ch, symbol="SYN002", bars=ch["bars"])


class Rule(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.days = lab.full_sessions(synth.make_history())

    def test_plan_day_trade(self):
        Dd = self.days["2026-10-28"]
        v = {"or_minutes": 15, "stop": "or_mid", "target_r": 2.0, "vol_x": None, "cost_filter": False}
        t = lab.run_rule(Dd, v, COST)
        self.assertEqual((t["entry"], t["stop"], t["target"], t["slot"]), (30450, 30100, 31150, 14))
        self.assertIn(t["outcome"], ("stop", "gap", "target", "flat"))
        if t["outcome"] == "stop":
            self.assertEqual(t["exit"], 30050)                      # 1칸 밀림
        self.assertLess(t["R_net"], t["R"])

    def test_gap_below_the_stop_fills_at_the_open(self):
        Dd = [dict(b) for b in self.days["2026-10-28"]]
        Dd[15] = dict(Dd[15], o=29800, h=29850, l=29700, c=29750)
        v = {"or_minutes": 15, "stop": "or_mid", "target_r": 2.0, "vol_x": None, "cost_filter": False}
        t = lab.run_rule(Dd, v, COST)
        self.assertEqual((t["outcome"], t["exit"]), ("gap", 29750))     # 시가 29,800 에서 1칸 아래

    def test_cost_filter_skips_tight_stops(self):
        Dd = self.days["2026-10-28"]
        tight = dict(COST, max_cost_to_risk=0.1)
        v = {"or_minutes": 15, "stop": "or_mid", "target_r": 2.0, "vol_x": None, "cost_filter": True}
        self.assertIsNone(lab.run_rule(Dd, v, tight))               # 비용 69원 > 위험 350원의 10%
        self.assertIsNotNone(lab.run_rule(Dd, v, COST))


class Stats(unittest.TestCase):
    def test_holm_is_monotone_and_bounded(self):
        adj = lab.holm([0.01, 0.04, 0.03, 0.5])
        self.assertEqual(adj[0], 0.04)
        self.assertTrue(all(0 <= a <= 1 for a in adj))
        order = sorted(range(4), key=lambda i: [0.01, 0.04, 0.03, 0.5][i])
        self.assertEqual([adj[i] for i in order], sorted(adj))

    def test_one_sided_p(self):
        self.assertLess(lab.one_sided_p([1.0, 1.1, 0.9, 1.2, 0.8] * 4), 0.001)
        self.assertGreater(lab.one_sided_p([-1.0, -1.1, -0.9, -1.2] * 4), 0.99)
        self.assertEqual(lab.one_sided_p([1.0]), 1.0)


class Experiment(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        by = {"SYN001": lab.full_sessions(synth.make_history()), "SYN002": lab.full_sessions(second_symbol())}
        cls.res = lab.experiment(by, lab.default_grid(), COST, train=30, test=10, min_trades=10)

    def test_structure(self):
        r = self.res
        self.assertEqual(r["rules"], 48)
        self.assertEqual(len(r["table"]), 48)
        self.assertEqual(r["sessions"], 62)
        self.assertGreaterEqual(len(r["folds"]), 3)
        tested = sum(f["test_trades"] for f in r["folds"])
        self.assertEqual(tested, r["oos"]["trades"])
        for f in r["folds"]:
            self.assertLess(f["train"][1], f["test"][0])            # 시험 기간은 언제나 학습 기간 뒤

    def test_costs_and_verdict_on_random_data(self):
        co = self.res["costs"]
        self.assertLess(co["avg_R_net"], co["avg_R"])
        v = lab.verdict(self.res)
        self.assertIn("고른 규칙의 시험 기간 성적", v)
        self.assertNotIn("보정 뒤에도 남는 규칙이 있습니다", v)       # 무작위 걸음에서 남는 규칙이 나오면 안 된다
        self.assertTrue(any("2. 워크포워드" in x for x in lab.report(self.res)))

    def test_too_few_sessions(self):
        by = {"SYN001": dict(list(lab.full_sessions(synth.make_history()).items())[:20])}
        with self.assertRaises(lab.LabError):
            lab.experiment(by, lab.default_grid(), COST, train=30, test=10)


class Command(unittest.TestCase):
    def test_offline_run_writes_lab_json(self):
        tmp = tempfile.mkdtemp()
        old = os.getcwd()
        os.chdir(tmp)
        try:
            for ch in (synth.make_history(), second_symbol()):
                with open(ch["symbol"] + ".json", "w", encoding="utf-8") as fh:
                    json.dump(ch, fh)
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = lab.main(["run", "--symbols", "SYN001,SYN002", "--offline", ".", "--sessions", "62",
                                 "--train", "30", "--test", "10", "--min-trades", "10", "--now", "2026-10-29 08:00",
                                 "--write"])
            self.assertEqual(code, 0, out.getvalue())
            with open("trader-kr/lab.json", encoding="utf-8") as fh:
                doc = json.load(fh)
            self.assertEqual(doc["symbols"], ["SYN001", "SYN002"])
            self.assertIn("verdict", doc)
            self.assertIn("not financial advice", out.getvalue())
        finally:
            os.chdir(old)
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
