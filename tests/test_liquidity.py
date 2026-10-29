import unittest

from _load import load
from krcommon import liquidity

size = load("trader-kr-budget", "size")
risk = load("trader-kr-risk", "check")

BOOK = {"asks": [{"price": "30500", "quantity": "50"}, {"price": "30550", "quantity": "50"},
                 {"price": "30600", "quantity": "500"}],
        "bids": [{"price": "30450", "quantity": "40"}, {"price": "30400", "quantity": "40"},
                 {"price": "30350", "quantity": "40"}]}


class Book(unittest.TestCase):
    def test_spread_and_market_buy(self):
        asks, bids = liquidity.levels(BOOK)
        sp = liquidity.spread(asks, bids)
        self.assertEqual((sp["ask"], sp["bid"], sp["ticks"], sp["mid"]), (30500, 30450, 1, 30475))
        b = liquidity.buy_cost(BOOK, 80)                 # 50 @30,500 + 30 @30,550
        self.assertAlmostEqual(b["avg"], (50 * 30500 + 30 * 30550) / 80.0)
        self.assertAlmostEqual(b["mid_saving"], (b["avg"] - 30475) * 80)
        self.assertEqual(b["levels"], 2)

    def test_stop_slippage_walks_the_bids_and_counts_what_is_left(self):
        s = liquidity.stop_slippage(BOOK, 60)            # 40 @0 칸 + 20 @1 칸(50원)
        self.assertAlmostEqual(s["per_share"], 20 * 50 / 60.0)
        self.assertEqual(s["unfilled"], 0)
        big = liquidity.stop_slippage(BOOK, 200)         # 보이는 120주를 넘음
        self.assertEqual(big["unfilled"], 80)
        self.assertGreater(big["per_share"], s["per_share"])
        self.assertIsNone(liquidity.stop_slippage({"asks": [], "bids": []}, 10))


PLAN = {"symbol": "SYN001", "side": "buy", "date": "2026-10-28", "setup": "orb", "entry": 30450, "stop": 30100,
        "target": 31150, "flat_by": "15:15"}
CFG = {"budget": 5000000, "max_loss_per_trade_krw": 50000}


class Sizing(unittest.TestCase):
    def test_thin_book_slippage_trips_the_cost_filter(self):
        s = size.size_trade(PLAN, CFG, today="2026-10-28", book=BOOK)
        self.assertIn("예상 밀림", s["refusal"])                 # 밀림 49.6원 + 비용 69.3원 > 위험 350원의 30%
        self.assertIsNone(size.order_proposal(s))

    def test_book_slippage_lowers_the_size(self):
        cfg = dict(CFG, max_cost_to_risk=0.9)
        plain = size.size_trade(PLAN, cfg, today="2026-10-28")
        thin = size.size_trade(PLAN, cfg, today="2026-10-28", book=BOOK)
        self.assertLess(thin["qty"], plain["qty"])
        self.assertGreater(thin["stop_slippage_per_share"], 0)
        self.assertLessEqual(thin["max_loss"], 50000)
        self.assertIsNotNone(thin["book_buy"])
        text = size.plan_report(thin, size.order_proposal(thin))
        self.assertIn("중간가·지정가로 들어가면", text)
        self.assertIn("손절가보다 주당", text)

    def test_cost_filter_refuses_tiny_risk(self):
        tight = dict(PLAN, stop=30350, target=30650)     # 위험 100원, 손절 때 비용 약 70원
        s = size.size_trade(tight, CFG, today="2026-10-28")
        self.assertIn("한도 30%", s["refusal"])
        s2 = size.size_trade(tight, dict(CFG, max_cost_to_risk=0.9), today="2026-10-28")
        self.assertNotIn("한도 30%", s2["refusal"] or "")


class Risk(unittest.TestCase):
    def test_book_slippage_and_cost_filter_in_risk(self):
        import datetime
        from fake_toss import stock_info
        from krcommon import krx
        order = {"symbol": "SYN001", "side": "buy", "qty": 200, "type": "market", "time_in_force": "day",
                 "exit": {"stop_price": 30100, "take_profit_price": 31150}, "plan": {"entry": 30450, "date": "2026-10-28"}}
        st = {"SYN001": {"stock": stock_info("SYN001"), "warnings": [], "orderbook": BOOK,
                         "price_limits": {"upperLimitPrice": "39550", "lowerLimitPrice": "21350"}}}
        now = datetime.datetime(2026, 10, 28, 10, 16)
        r = risk.check([order], CFG, None, None, st, now, krx.Session(now.date(), source="toss-calendar"))
        res = r["results"][0]
        self.assertGreater(res["max_loss"], 200 * 350)             # 밀림을 넣으니 손실이 커짐
        self.assertTrue(any("최대 손실" in x for x in res["reasons"]))
        self.assertTrue(any("다 못 팝니다" in x for x in res["warnings"]))
        tight = dict(order, qty=10, exit={"stop_price": 30350, "take_profit_price": 30650})
        r2 = risk.check([tight], CFG, None, None, {"SYN001": dict(st["SYN001"], orderbook=None)}, now,
                        krx.Session(now.date(), source="toss-calendar"))
        self.assertTrue(any(x.startswith("비용:") for x in r2["results"][0]["reasons"]))


if __name__ == "__main__":
    unittest.main()
