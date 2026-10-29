import contextlib
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest

from _load import load
from fake_toss import stock_info
from krcommon import krx

risk = load("trader-kr-risk", "check")
DAY = datetime.date(2026, 10, 28)
NOW = datetime.datetime(2026, 10, 28, 10, 16)
CFG = {"budget": 5000000, "max_loss_per_trade_krw": 50000, "max_daily_loss_krw": 150000, "max_trades_per_day": 3,
       "max_position_pct": 75, "max_order_krw": 4000000, "cash_buffer_pct": 10, "min_reward_risk": 1.0}
ORDER = {"symbol": "SYN001", "name": "합성종목", "side": "buy", "qty": 119, "type": "market", "time_in_force": "day",
         "exit": {"stop_price": 30100, "take_profit_price": 31150, "flat_by": "15:15"},
         "plan": {"entry": 30450, "stop": 30100, "target": 31150}}
LIMITS = {"timestamp": "2026-10-28T09:00:00+09:00", "upperLimitPrice": "39550", "lowerLimitPrice": "21350",
          "currency": "KRW"}


def status(**kw):
    w = kw.pop("warnings", [])
    return {"SYN001": {"stock": stock_info("SYN001", **kw), "warnings": w, "price_limits": LIMITS}}


def run(orders, cfg=CFG, st=None, now=NOW, session="std", portfolio=None, state=None):
    sess = krx.Session(now.date(), source="toss-calendar") if session == "std" else session
    return risk.check(orders, cfg, portfolio, state, status() if st is None else st, now, sess)


class Check(unittest.TestCase):
    def test_a_clean_order_passes(self):
        r = run([ORDER])
        self.assertEqual(r["pass"], 1, r["results"][0]["reasons"])
        res = r["results"][0]
        self.assertLessEqual(res["max_loss"], 50000)
        self.assertLess(res["reward_risk_net"], 2.0)

    def test_warning_listed_stocks_are_blocked(self):
        for kind, word in (("INVESTMENT_WARNING", "투자경고"), ("OVERHEATED", "단기과열"), ("VI_DYNAMIC", "동적 VI")):
            r = run([ORDER], st=status(warnings=[{"warningType": kind, "exchange": "KRX", "startDate": "2026-10-27",
                                                  "endDate": None}]))
            self.assertEqual(r["block"], 1, kind)
            self.assertTrue(any(word in x for x in r["results"][0]["reasons"]), kind)

    def test_an_expired_warning_does_not_block(self):
        r = run([ORDER], st=status(warnings=[{"warningType": "INVESTMENT_WARNING", "startDate": "2026-10-01",
                                              "endDate": "2026-10-10"}]))
        self.assertEqual(r["pass"], 1)

    def test_stock_status_rules(self):
        cases = [dict(security_type="ETF"), dict(common=False), dict(suspended=True), dict(liquidation=True)]
        for kw in cases:
            self.assertEqual(run([ORDER], st=status(**kw))["block"], 1, kw)
        self.assertEqual(run([ORDER], cfg=dict(CFG, allow_etf=True), st=status(security_type="ETF"))["pass"], 1)

    def test_unknown_status_fails_closed(self):
        r = run([ORDER], st={})
        self.assertEqual(r["block"], 1)
        self.assertTrue(any("상태 모름" in x for x in r["results"][0]["reasons"]))
        r2 = run([ORDER], cfg=dict(CFG, require_market_status=False), st={})
        self.assertEqual(r2["pass"], 1)

    def test_time_window(self):
        for now, word in ((datetime.datetime(2026, 10, 28, 9, 10), "09:15 전"),
                          (datetime.datetime(2026, 10, 28, 14, 55), "14:50 뒤"),
                          (datetime.datetime(2026, 10, 28, 15, 22), "접속매매 시간이 아닙니다")):
            r = run([ORDER], now=now)
            self.assertTrue(any(word in x for x in r["results"][0]["reasons"]), (now, r["results"][0]["reasons"]))
        r = run([ORDER], session=None)
        self.assertTrue(any("휴장" in x for x in r["results"][0]["reasons"]))

    def test_loss_reward_and_limits(self):
        big = dict(ORDER, qty=200)
        self.assertTrue(any("최대 손실" in x for x in run([big])["results"][0]["reasons"]))
        poor = dict(ORDER, exit=dict(ORDER["exit"], take_profit_price=30500))
        self.assertTrue(any("손익비" in x for x in run([poor])["results"][0]["reasons"]))
        nostop = dict(ORDER, exit={}, plan={"entry": 30450})
        self.assertTrue(any("손절" in x for x in run([nostop])["results"][0]["reasons"]))
        tight = {"SYN001": dict(status()["SYN001"], price_limits=dict(LIMITS, upperLimitPrice="31000"))}
        self.assertTrue(any("상한가" in x for x in run([ORDER], st=tight)["results"][0]["reasons"]))
        off = dict(ORDER, type="limit", limit_price=30460)
        self.assertTrue(any("호가 단위" in x for x in run([off])["results"][0]["reasons"]))

    def test_daily_loss_and_trade_count_across_a_batch(self):
        other = dict(ORDER, symbol="SYN002")
        st = dict(status(), SYN002=status()["SYN001"])
        r = run([ORDER, other], cfg=dict(CFG, max_daily_loss_krw=60000, max_position_pct=100,
                                         max_order_krw=10000000, budget=20000000), st=st)
        self.assertEqual([x["verdict"] for x in r["results"]], ["PASS", "BLOCK"])
        self.assertTrue(any("하루 손실" in x for x in r["results"][1]["reasons"]))
        state = {"orders": [{"status": "placed", "date": "2026-10-28", "symbol": "X", "side": "buy", "kind": "entry",
                             "max_loss": 1000}] * 3}
        r2 = run([ORDER], state=state)
        self.assertTrue(any("거래 수" in x for x in r2["results"][0]["reasons"]))

    def test_unknown_calendar_fails_closed(self):
        r = run([ORDER], session=krx.Session(DAY))                  # source == "default"
        self.assertTrue(any("휴장일인지 모릅니다" in x for x in r["results"][0]["reasons"]))
        r2 = run([ORDER], cfg=dict(CFG, require_market_status=False), session=krx.Session(DAY))
        self.assertEqual(r2["pass"], 1)

    def test_warning_dates_in_other_formats_still_block(self):
        for start in ("2026-10-28T00:00:00+09:00", "20261028", "언젠가"):
            st = status(warnings=[{"warningType": "INVESTMENT_WARNING", "startDate": start, "endDate": None}])
            self.assertEqual(run([ORDER], st=st)["block"], 1, start)

    def test_a_higher_live_price_measures_the_loss(self):
        st = status()
        st["SYN001"]["price"] = {"lastPrice": "30900", "timestamp": "2026-10-28T10:15:00+09:00"}
        r = run([ORDER], st=st)
        self.assertEqual(r["results"][0]["entry_ref"], 30900)
        self.assertTrue(any("최대 손실" in x for x in r["results"][0]["reasons"]))
        old = status()
        old["SYN001"]["price"] = {"lastPrice": "30900", "timestamp": "2026-10-28T09:00:00+09:00"}
        r2 = run([ORDER], st=old)
        self.assertEqual(r2["results"][0]["entry_ref"], 30450)
        self.assertTrue(any("현재가가 없어" in x for x in r2["results"][0]["warnings"]))

    def test_a_price_already_below_the_stop_blocks(self):
        st = status()
        st["SYN001"]["price"] = {"lastPrice": "30000", "timestamp": "2026-10-28T10:15:00+09:00"}
        r = run([ORDER], st=st)
        self.assertTrue(any("계획이 이미 깨졌습니다" in x for x in r["results"][0]["reasons"]))
        lim = dict(ORDER, type="limit", limit_price=30450)
        self.assertEqual(run([lim], st=st)["block"], 1)

    def test_yesterdays_plan_is_blocked(self):
        o = dict(ORDER, plan=dict(ORDER["plan"], date="2026-10-27"))
        r = run([o])
        self.assertTrue(any("계획 날짜" in x for x in r["results"][0]["reasons"]))

    def test_exits_stay_open_after_the_window(self):
        sell = {"symbol": "SYN001", "side": "sell", "qty": 119, "type": "market", "time_in_force": "day",
                "plan": {"entry": 30450}}
        pf = {"cash": 1000000, "positions": [{"symbol": "SYN001", "qty": 119, "price": 30450}]}
        r = run([sell], now=datetime.datetime(2026, 10, 28, 15, 0), portfolio=pf)
        self.assertEqual(r["pass"], 1)
        self.assertEqual(r["results"][0]["kind"], "exit")
        short = dict(sell, qty=500)
        self.assertEqual(run([short], portfolio=pf)["block"], 1)


class Command(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.getcwd()
        os.chdir(self.tmp)
        os.makedirs("trader-kr")
        for name, doc in (("orders", {"orders": [ORDER]}), ("config", CFG), ("status", status())):
            with open("trader-kr/%s.json" % name, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
        cal = krx.Calendar({"2026-10-28": krx.Session(DAY, source="toss-calendar")})
        krx.save_calendar("trader-kr/cache/calendar.json", cal)

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_offline_check_records_the_fingerprint(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = risk.main(["--no-network", "--status", "trader-kr/status.json", "--now", "2026-10-28 10:16"])
        self.assertEqual(code, 0, out.getvalue())
        with open("trader-kr/state/risk.json", encoding="utf-8") as fh:
            rec = json.load(fh)
        self.assertEqual(rec["results"][0]["fingerprint"], risk.fingerprint(ORDER))
        self.assertNotIn("휴장일인지 모릅니다", out.getvalue())

    def test_offline_check_without_a_calendar_blocks(self):
        os.remove("trader-kr/cache/calendar.json")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = risk.main(["--no-network", "--status", "trader-kr/status.json", "--now", "2026-10-28 10:16",
                              "--no-write"])
        self.assertEqual(code, 1)
        self.assertIn("휴장일인지 모릅니다", out.getvalue())


if __name__ == "__main__":
    unittest.main()
