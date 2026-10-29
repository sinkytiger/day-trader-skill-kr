"""가짜 토스증권 서버로 온라인 경로 전체를 돌린다: 순위 -> 스캔 -> 진입 -> 계획 -> 재생 -> 수량 -> 점검."""

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

from _load import load
import synth
from fake_toss import FakeToss, stock_info
from krcommon import data, toss

scan = load("trader-kr-scanner", "scan")
entry = load("trader-kr-entry", "entry")
exitp = load("trader-kr-exit", "exit")
tm = load("trader-kr-timemachine", "timemachine")
size = load("trader-kr-budget", "size")
risk = load("trader-kr-risk", "check")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def day(date, open_="09:00", auction="15:20", close="15:30"):
    return {"date": date, "integrated": {"regularMarket": {
        "startTime": "%sT%s:00+09:00" % (date, open_), "singlePriceAuctionStartTime": "%sT%s:00+09:00" % (date, auction),
        "endTime": "%sT%s:00+09:00" % (date, close)}}}


class Online(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.getcwd()
        os.chdir(self.tmp)
        f = FakeToss({"SYN001": synth.make_history()["bars"], "SYN002": synth.flat_day()["bars"]})
        f.stocks = {"SYN001": stock_info("SYN001"), "SYN002": stock_info("SYN002", "평평종목"),
                    "SYN003": stock_info("SYN003", "합성ETF", security_type="ETF")}
        f.ranking = ["SYN003", "SYN001", "SYN002"]
        f.calendar = {"2026-10-28": {"previousBusinessDay": day("2026-10-27"), "today": day("2026-10-28"),
                                     "nextBusinessDay": day("2026-10-29")}}
        f.limits = {"SYN001": {"timestamp": "x", "upperLimitPrice": "39550", "lowerLimitPrice": "21350"}}
        f.prices = {"SYN001": 30450}
        self.fake = f
        data.set_client(toss.Client(credentials=("tsck_live_test", "s"), transport=f, sleep=lambda s: None,
                                    token_path=os.path.join(self.tmp, "token.json")))
        with open("config.json", "w", encoding="utf-8") as fh:
            json.dump({"budget": 5000000}, fh)

    def tearDown(self):
        data.set_client(None)
        os.chdir(self.old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, mod, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mod.main(argv)
        return code, out.getvalue() + err.getvalue()

    def test_the_whole_day(self):
        now = "2026-10-28 10:15:05"
        code, text = self.call(scan, ["orb", "--now", now])
        self.assertEqual(code, 0, text)
        wl = read("trader-kr/watchlist.json")
        self.assertEqual([h["symbol"] for h in wl["hits"]], ["SYN001"])
        self.assertIn("주식·보통주·거래 가능만 2개", wl["candidates"])       # ETF 는 빠짐
        self.assertEqual(wl["session"]["source"], "toss-calendar")

        code, text = self.call(entry, ["check", "SYN001", "--now", now, "--write"])
        self.assertEqual(code, 0, text)
        self.assertNotIn("평일 기본 시간", text)

        code, text = self.call(exitp, ["plan", "--now", now, "--write"])
        self.assertEqual(code, 0, text)
        plan = read("trader-kr/plan.json")
        self.assertEqual(plan["price_limits"]["source"], "토스증권 상·하한가")

        calls0 = data.client().calls
        code, text = self.call(tm, ["run", "--now", now, "--replays", "500", "--write"])
        self.assertEqual(code, 0, text)
        self.assertGreater(data.client().calls, calls0)
        calls1 = data.client().calls
        code, text = self.call(tm, ["run", "--now", now, "--replays", "500"])
        self.assertEqual(code, 0, text)
        self.assertLessEqual(data.client().calls - calls1, 2)              # 두 번째는 캐시에서

        code, text = self.call(size, ["--config", "config.json", "--now", now, "--write"])
        self.assertEqual(code, 0, text)
        self.assertIn("토스증권 현재가 30,450", text)

        code, text = self.call(risk, ["--config", "config.json", "--now", "2026-10-28 10:16"])
        self.assertEqual(code, 0, text)
        self.assertTrue(os.path.exists("trader-kr/state/market_status.json"))

        self.fake.warnings["SYN001"] = [{"warningType": "INVESTMENT_WARNING", "exchange": "KRX",
                                         "startDate": "2026-10-28", "endDate": None}]
        code, text = self.call(risk, ["--config", "config.json", "--now", "2026-10-28 10:16"])
        self.assertEqual(code, 1)
        self.assertIn("투자경고종목", text)

    def test_a_failed_status_call_blocks(self):
        with open("orders.json", "w", encoding="utf-8") as fh:
            json.dump({"orders": [{"symbol": "SYN009", "side": "buy", "qty": 1, "type": "market",
                                   "time_in_force": "day", "exit": {"stop_price": 30100, "take_profit_price": 31150},
                                   "plan": {"entry": 30450}}]}, fh)
        code, text = self.call(risk, ["--orders", "orders.json", "--config", "config.json", "--now", "2026-10-28 10:16"])
        self.assertEqual(code, 1)
        self.assertIn("상태 모름", text)


if __name__ == "__main__":
    unittest.main()
