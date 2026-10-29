import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

from _load import load
import synth

exitp = load("trader-kr-exit", "exit")


class Compute(unittest.TestCase):
    def test_or_mid_stop_and_2r_target_on_ticks(self):
        c = exitp.compute(30450, "or_mid", None, "r", 2.0, 30300, 29900)
        self.assertEqual(c["stop"], 30100)           # (30,300 + 29,900) / 2
        self.assertEqual(c["risk"], 350)
        self.assertEqual(c["target"], 31150)         # 30,450 + 2 x 350
        self.assertEqual(c["reward_risk"], 2.0)
        self.assertLess(c["reward_risk_net"], 2.0)   # 수수료와 매도세를 빼면 줄어든다
        self.assertAlmostEqual(c["reward_risk_net"], round((700 - 71.1) / (350 + 69.2825), 2), places=2)

    def test_stops_round_down_and_targets_round_to_the_tick(self):
        c = exitp.compute(30450, "pct", 1.0, "r", 1.5)
        self.assertEqual(c["stop"], 30100)           # 30,145.5 -> 30,100 (50원 단위 내림)
        self.assertIn("내림", c["stop_detail"])
        self.assertEqual(c["target"], 31000)         # 30,450 + 1.5 x 350 = 30,975, 반 칸은 올림
        c2 = exitp.compute(30450, "price", 30150, "r", 1.5)
        self.assertEqual(c2["target"], 30900)        # 30,450 + 450 = 30,900

    def test_a_midpoint_entry_rounds_up_to_the_ask(self):
        c = exitp.compute(245250, "price", 241750, "r", 2.0)     # 2026-10-29 삼성전자 중간가 체결가
        self.assertEqual(c["entry"], 245500)
        self.assertEqual(c["stop"], 241500)                       # 241,750 도 500원 단위로 내림
        self.assertTrue(any("호가 사이 체결가" in w for w in c["warnings"]))

    def test_too_tight_a_stop_is_refused(self):
        with self.assertRaises(exitp.PlanError) as e:
            exitp.compute(30450, "price", 30425, "r", 2.0)
        self.assertIn("호가 2칸", str(e.exception))

    def test_target_above_the_upper_limit_is_refused(self):
        with self.assertRaises(exitp.PlanError) as e:
            exitp.compute(30450, "price", 30100, "price", 40000, upper_limit=39550)
        self.assertIn("상한가", str(e.exception))

    def test_wrong_sides_are_refused(self):
        for args in ((30450, "price", 30500, "r", 2.0), (30450, "price", 30100, "price", 30400)):
            with self.assertRaises(exitp.PlanError):
                exitp.compute(*args)


class Commands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.getcwd()
        os.chdir(self.tmp)
        os.makedirs("trader-kr")
        with open("trader-kr/entry.json", "w", encoding="utf-8") as fh:
            json.dump({"created_by": "trader-kr-entry", "symbol": "SYN001", "setup": "orb", "side": "buy",
                       "date": "2026-10-28", "status": "triggered", "time": "10:15", "bar_start": "10:10",
                       "bar_index": 14, "price": 30450, "level": 30300, "interval": "5m",
                       "opening_range": {"high": 30300, "low": 29900, "minutes": 15}}, fh)
        with open("SYN001.json", "w", encoding="utf-8") as fh:
            json.dump(synth.make_history(), fh)

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = exitp.main(argv)
        return code, out.getvalue() + err.getvalue()

    def test_plan_from_entry_json(self):
        code, text = self.run_main(["plan", "--no-network", "--now", "2026-10-28 10:16", "--prev-close", "30000",
                                    "--write"])
        self.assertEqual(code, 0, text)
        with open("trader-kr/plan.json", encoding="utf-8") as fh:
            plan = json.load(fh)
        self.assertEqual((plan["stop"], plan["target"], plan["flat_by"]), (30100, 31150, "15:15"))
        self.assertEqual(plan["price_limits"]["upper"], 39000)
        self.assertIn("손익비", text)

    def test_atr_stop_reads_the_session_bars(self):
        code, text = self.run_main(["plan", "--no-network", "--now", "2026-10-28 10:16", "--stop", "atr:1",
                                    "--offline", "SYN001.json", "--json"])
        self.assertEqual(code, 0, text)
        plan = json.loads(text)
        self.assertEqual(plan["atr_bars"], 14)
        self.assertLess(plan["stop"], 30450)

    def test_flat_by_after_the_auction_is_refused(self):
        code, text = self.run_main(["plan", "--no-network", "--now", "2026-10-28 10:16", "--flat-by", "15:25"])
        self.assertEqual(code, 1)
        self.assertIn("15:20", text)

    def test_untriggered_entry_needs_an_explicit_price(self):
        with open("trader-kr/entry.json", encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["status"] = "waiting"
        with open("trader-kr/entry.json", "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        code, text = self.run_main(["plan", "--no-network", "--now", "2026-10-28 10:16"])
        self.assertEqual(code, 1)
        code, text = self.run_main(["plan", "--no-network", "--now", "2026-10-28 10:16", "--entry", "30450"])
        self.assertEqual(code, 0, text)


if __name__ == "__main__":
    unittest.main()
