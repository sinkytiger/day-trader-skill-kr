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

    def test_percent_stop_without_a_target(self):
        Dd = self.days["2026-10-28"]
        v = {"or_minutes": 15, "stop": "pct", "pct": 5.0, "target_r": None, "vol_x": None, "cost_filter": False}
        t = lab.run_rule(Dd, v, COST)
        self.assertEqual(t["stop"], lab.krx.floor_tick(t["entry"] * 0.95))
        self.assertIsNone(t["target"])
        self.assertIn(t["outcome"], ("flat", "stop", "gap"))

    def test_grid_adds_percent_stop_rules_only_when_asked(self):
        self.assertEqual(len(lab.default_grid()), 48)
        g = lab.default_grid(5)
        self.assertEqual(len(g), 80)
        names = [lab.name(v) for v in g]
        self.assertIn("ORB15 5%손절 목표없음", names)
        self.assertIn("ORB30 5%손절 1.5R 거래량1.5x 비용필터", names)
        self.assertEqual(len(set(names)), 80)

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


class Forward(unittest.TestCase):
    RULE = "ORB15 중간손절 1R"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.getcwd()
        os.chdir(self.tmp)
        self.charts = {"SYN001": synth.make_history(), "SYN002": second_symbol()}
        for sym, ch in self.charts.items():
            self.save(sym, ch)

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def save(self, sym, ch):
        with open(sym + ".json", "w", encoding="utf-8") as fh:
            json.dump(dict(ch, symbol=sym), fh)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = lab.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def lock(self, symbols="SYN001,SYN002", rule=RULE, now="2026-10-20 16:00", name="t1"):
        return self.cli("forward", "lock", "--rule", rule, "--symbols", symbols, "--name", name, "--now", now)

    def update(self, name="t1", now="2026-10-29 08:00"):
        code, out, err = self.cli("forward", "update", "--name", name, "--offline", ".", "--now", now, "--json")
        return code, (json.loads(out) if code == 0 else None), err

    def test_lock_refuses_to_overwrite(self):
        code, out, _ = self.lock()
        self.assertEqual(code, 0)
        self.assertIn("forward update --name t1", out)
        self.assertIn("not financial advice", out)
        path = os.path.join("trader-kr", "forward", "t1.lock.json")
        with open(path, encoding="utf-8") as fh:
            before = fh.read()
        code, _, err = self.lock(rule="ORB30 저가손절 2R")
        self.assertEqual(code, 1)
        self.assertIn("이미 있습니다", err)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before)
        doc = json.loads(before)
        self.assertEqual((doc["after"], doc["first_session"], doc["judge_trades"]), ("2026-10-20", "2026-10-21", 200))

    def test_rule_names_must_match_the_lab(self):
        self.assertEqual(self.lock(rule="ORB99 중간손절 1R")[0], 1)
        code, out, _ = self.lock(rule="ORB30 5%손절 1.5R", name="pct")       # X% 손절은 이름에서 X 를 읽는다
        self.assertEqual(code, 0, out)

    def test_only_sessions_after_the_lock_count(self):
        self.lock()
        code, res, err = self.update()
        self.assertEqual(code, 0, err)
        after_days = [d.isoformat() for d in synth.business_days(synth.PLAN_DAY, 62) if d.isoformat() > "2026-10-20"]
        self.assertEqual(res["sessions"], {"SYN001": len(after_days), "SYN002": len(after_days)})
        self.assertEqual(res["dates"], [after_days[0], after_days[-1]])
        self.assertTrue(all(t["date"] > "2026-10-20" for t in res["trades"]))
        self.assertTrue(os.path.exists(os.path.join("trader-kr", "forward", "t1.json")))

    def test_trades_equal_the_lab_engine(self):
        self.lock()
        _, res, _ = self.update()
        v = lab.find_rule(self.RULE)
        want = []
        for sym, ch in self.charts.items():
            for d, Dd in lab.full_sessions(dict(ch, symbol=sym)).items():
                t = lab.run_rule(Dd, v, COST) if d > "2026-10-20" else None
                if t:
                    want.append((d, sym, t["exit"], t["outcome"], round(t["R_net"], 6)))
        got = [(t["date"], t["symbol"], t["exit"], t["outcome"], t["R_net"]) for t in res["trades"]]
        self.assertGreater(len(want), 0)
        self.assertEqual(sorted(got), sorted(want))
        self.assertEqual(res["stats"]["trades"], len(want))
        self.assertEqual(res["verdict"], "WAIT")

    def test_missing_symbol_data_stops_the_update(self):
        self.lock(symbols="SYN001,SYN003")
        code, _, err = self.update()
        self.assertEqual(code, 1)
        self.assertIn("SYN003", err)
        self.assertIn("멈춥니다", err)
        self.assertFalse(os.path.exists(os.path.join("trader-kr", "forward", "t1.json")))

    def test_a_symbol_with_no_sessions_after_the_lock_stops_the_update(self):
        cut = lab.krx.ts_of(lab.datetime.datetime(2026, 10, 21, 0, 0))
        ch = self.charts["SYN002"]
        self.save("SYN002", dict(ch, bars=[b for b in ch["bars"] if b["t"] < cut]))
        self.lock()
        code, _, err = self.update()
        self.assertEqual(code, 1)
        self.assertIn("0일", err)

    def test_an_edited_lock_or_engine_stops_the_update(self):
        self.lock()
        path = os.path.join("trader-kr", "forward", "t1.lock.json")
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(dict(doc, symbols=["SYN001"]), fh)
        code, _, err = self.update()
        self.assertEqual(code, 1)
        self.assertIn("바뀌었습니다", err)
        doc2 = dict(doc, engine="0" * 16)                           # 지문까지 다시 맞춰도 체결 계산이 다르면 멈춘다
        doc2["hash"] = lab.lock_hash(doc2)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc2, fh)
        code, _, err = self.update()
        self.assertEqual(code, 1)
        self.assertIn("run_rule", err)

    def test_nothing_to_judge_before_the_first_session_closes(self):
        self.lock(now="2026-10-28 16:00")
        code, res, err = self.update(now="2026-10-29 08:00")
        self.assertEqual(code, 0, err)
        self.assertEqual((res["dates"], res["stats"]["trades"], res["verdict"]), (None, 0, "WAIT"))
        code, out, _ = self.cli("forward", "update", "--offline", ".", "--now", "2026-10-29 08:00")   # 잠금이 하나면 --name 생략
        self.assertEqual(code, 0)
        self.assertIn("아직 잠근 날 뒤에", out)


class Verdict(unittest.TestCase):
    @staticmethod
    def trades(nets):
        return [{"R": x, "R_net": x, "date": "2026-11-%02d" % (1 + i // 10)} for i, x in enumerate(nets)]

    def judge(self, nets, judge=200):
        st = lab.forward_stats(self.trades(nets), judge)
        return lab.forward_verdict(st, judge)[0], st

    def test_thresholds(self):
        self.assertEqual(self.judge([0.5] * 199)[0], "WAIT")                     # 199건: 구간이 0보다 커도 아직
        code, st = self.judge([0.4, 0.6] * 100)
        self.assertEqual(code, "PASS")
        self.assertEqual((st["judged"]["trades"], st["judged"]["through"]), (200, "2026-11-20"))
        self.assertEqual(self.judge([-1.0, 1.0] * 100)[0], "FAIL")                 # 평균 0: 구간 아래쪽이 0 아래

    def test_only_the_first_judge_trades_decide(self):
        code, st = self.judge([-0.2] * 200 + [5.0] * 100)                          # 뒤에 좋아져도 판정은 처음 200건
        self.assertEqual(code, "FAIL")
        self.assertGreater(st["avg_R_net"], 0)


if __name__ == "__main__":
    unittest.main()
