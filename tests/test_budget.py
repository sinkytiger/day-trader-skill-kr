import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

from _load import load

size = load("trader-kr-budget", "size")
PLAN = {"symbol": "SYN001", "name": "합성종목", "side": "buy", "date": "2026-10-28", "setup": "orb", "entry": 30450,
        "entry_time": "10:15", "stop": 30100, "target": 31150, "flat_by": "15:15"}
CFG = {"budget": 5000000, "max_loss_per_trade_krw": 50000, "max_position_pct": 75, "max_order_krw": 4000000,
       "cash_buffer_pct": 10, "commission_rate": 0.00015, "sell_tax_rate": 0.002, "min_reward_risk": 1.0}


class SizeTrade(unittest.TestCase):
    def test_max_loss_with_costs_binds(self):
        s = size.size_trade(PLAN, CFG, today="2026-10-28")
        # 손절 때 주당 손실 = 350 + 30,450 x 0.015% + 30,100 x (0.015% + 0.20%) = 419.28원
        self.assertEqual(s["qty"], 119)                    # 50,000 / 419.28 = 119.2
        self.assertEqual(s["bound_by"], "최대 손실")
        self.assertLessEqual(s["max_loss"], 50000)
        caps = {l["name"]: l["shares"] for l in s["limits"]}
        self.assertEqual(caps["종목 한도"], 123)            # 3,750,000 / 30,450
        self.assertEqual(caps["주문 한도"], 131)
        o = size.order_proposal(s)
        self.assertEqual((o["qty"], o["type"], o["exit"]["stop_price"], o["exit"]["take_profit_price"]),
                         (119, "market", 30100, 31150))

    def test_a_higher_live_price_sizes_smaller(self):
        s = size.size_trade(PLAN, CFG, price=30600, today="2026-10-28")
        self.assertEqual(s["entry"], 30600)
        self.assertEqual(s["entry_from"], "price")
        self.assertLess(s["qty"], 119)

    def test_refusals(self):
        far = size.size_trade(dict(PLAN, stop=20000, target=60000), dict(CFG, max_loss_per_trade_krw=5000),
                              today="2026-10-28")
        self.assertEqual(far["qty"], 0)
        self.assertIsNone(size.order_proposal(far))
        self.assertIn("최대 손실", far["refusal"])
        poor = size.size_trade(dict(PLAN, target=30500), CFG, today="2026-10-28")
        self.assertIn("손익비", poor["refusal"])
        with self.assertRaises(size.SizeError):
            size.size_trade(PLAN, CFG, today="2026-10-29")
        with self.assertRaises(size.SizeError):
            size.size_trade(PLAN, CFG, limit=30460, today="2026-10-28")   # 호가 단위 아님
        with self.assertRaises(size.SizeError):
            size.size_trade(dict(PLAN, side="sell"), CFG, today="2026-10-28")

    def test_a_live_price_below_the_stop_is_refused(self):
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(PLAN, CFG, price=30000, today="2026-10-28")
        self.assertIn("계획이 이미 깨졌습니다", str(e.exception))

    def test_a_live_price_rounds_up_not_down(self):
        s = size.size_trade(PLAN, CFG, price=30470, today="2026-10-28")
        self.assertEqual(s["entry"], 30500)                 # 30,470 -> 50원 단위 올림

    def test_portfolio_cash_caps_the_size(self):
        s = size.size_trade(PLAN, CFG, cash=1000000, today="2026-10-28")
        self.assertEqual(s["bound_by"], "현금")
        self.assertLessEqual(s["cost"] * 1.00015, 1000000 - 500000)


class Command(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.getcwd()
        os.chdir(self.tmp)
        os.makedirs("trader-kr")
        for name, doc in (("plan", PLAN), ("config", CFG)):
            with open("trader-kr/%s.json" % name, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_orders_json(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = size.main(["--no-network", "--now", "2026-10-28 10:16", "--write"])
        self.assertEqual(code, 0, out.getvalue())
        with open("trader-kr/orders.json", encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["status"], "proposed")
        self.assertIs(doc["send_orders"], False)
        self.assertEqual(doc["orders"][0]["qty"], 119)
        self.assertIn("아무것도 보내지 않았습니다", out.getvalue())


if __name__ == "__main__":
    unittest.main()
