"""오늘 장 한 번에: 시각마다 맞는 일을 하고, 진입 시간에는 후보마다 계획부터 점검까지 이어서 한 장으로 낸다."""

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
from krcommon import dart, data, demo, krx, toss

today = load("trader-kr-today", "today")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class Offline(unittest.TestCase):
    """demo.py 가 만드는 합성 폴더로, 네트워크 없이."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.getcwd()
        charts = demo.prepare(self.tmp)
        os.chdir(self.tmp)
        self.syn = charts["SYN001"]
        self.status(demo.price_at(self.syn, "10:15"))

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def status(self, last):
        krx.write_json("status.json", demo.status_doc(self.syn, last))

    def call(self, now, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = today.main(["--offline", "bars", "--status", "status.json", "--now", now, "--replays", "300"]
                              + list(extra))
        return code, out.getvalue()

    def test_entry_window_plans_the_signal_and_passes(self):
        code, text = self.call("2026-10-28 10:15:30")
        self.assertEqual(code, 0, text)
        self.assertIn("1. PASS      SYN001 합성종목  신호 10:15 (방금), 30,450", text)
        self.assertIn("손절 30,100 / 목표 31,150 (2R)", text)
        self.assertIn("119주 = 3,623,550원", text)
        self.assertIn("재생 300번: 목표 먼저", text)
        self.assertIn("주문은 내지 않았습니다", text)
        doc = read("trader-kr/today.json")
        self.assertEqual(doc["phase"], "entries")
        self.assertEqual([c["verdict"] for c in doc["candidates"]], ["PASS"])
        for name in ("entry", "plan", "orders", "risk", "timemachine"):
            self.assertTrue(os.path.exists("trader-kr/today/SYN001/%s.json" % name), name)
        with open("trader-kr/today/report.md", encoding="utf-8") as fh:
            report = fh.read()
        self.assertIn("SYN001 위험 점검", report)
        self.assertIn("python trader-kr-risk/check.py", report)
        self.assertIs(read("trader-kr/today/SYN001/orders.json")["send_orders"], False)

    def test_a_price_that_ran_away_gives_no_order_and_says_why(self):
        self.status(30600)
        code, text = self.call("2026-10-28 10:15:30", "--sim", "0")
        self.assertEqual(code, 0, text)
        self.assertIn("주문 없음", text)
        self.assertIn("이유: 비용 뺀 손익비 0.84", text)
        self.assertFalse(os.path.exists("trader-kr/today/SYN001/orders.json"))
        self.assertNotIn("재생", text)

    def test_without_a_budget_it_plans_but_does_not_size(self):
        krx.write_json("trader-kr/config.json", {})
        code, text = self.call("2026-10-28 10:15:30")
        self.assertEqual(code, 0, text)
        self.assertIn("예산이 없어 손절·목표까지만 정했습니다", text)
        self.assertIn("1. 계획만    SYN001", text)
        self.assertIn("손절 30,100 / 목표 31,150 (2R)", text)
        self.assertNotIn("119주", text)
        self.assertFalse(os.path.exists("trader-kr/today/SYN001/orders.json"))
        code, text = self.call("2026-10-28 10:15:30", "--budget", "5000000", "--sim", "0")
        self.assertIn("1. PASS      SYN001", text)

    def test_config_stop_rule_flows_into_the_card(self):
        krx.write_json("trader-kr/config.json", dict(krx.read_json("trader-kr/config.json", "설정"), stop_rule="pct:5"))
        code, text = self.call("2026-10-28 10:15:30", "--sim", "0")
        self.assertEqual(code, 0, text)
        self.assertIn("손절 28,900 / 목표", text)

    def test_each_time_of_day(self):
        cases = {"09:05": ("forming", "시가 범위 (15분) 형성 중"),
                 "15:00": ("late", "새 진입 마감(14:50)이 지났습니다"),
                 "15:40": ("after", "장이 끝났습니다")}
        for hhmm, (ph, words) in cases.items():
            code, text = self.call("2026-10-28 " + hhmm)
            self.assertEqual(code, 0, text)
            self.assertIn(words, text, hhmm)
            self.assertEqual(read("trader-kr/today.json")["phase"], ph, hhmm)
        code, text = self.call("2026-10-31 10:00")
        self.assertIn("휴장입니다", text)

    def test_open_trades_show_up_after_the_entry_cutoff(self):
        with open("trader-kr/journal.jsonl", "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": "x", "symbol": "SYN001", "qty": 119, "entry_price": 30500, "entry_time": "10:15",
                                 "stop": 30100, "flat_by": "15:15", "date": "2026-10-28"}) + "\n")
        code, text = self.call("2026-10-28 15:00")
        self.assertIn("열린 거래 1건", text)
        self.assertIn("SYN001 119주, 진입 30,500 (10:15), 손절 30,100, 15:15 까지 정리", text)

    def test_bad_arguments(self):
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            self.assertEqual(today.main(["--max", "0"]), 2)


class Online(unittest.TestCase):
    """가짜 토스증권 서버로: 순위 -> 스캔 -> 후보마다 계획과 점검 -> 재생."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.getcwd()
        os.chdir(self.tmp)
        f = FakeToss({"SYN001": synth.make_history()["bars"], "SYN002": synth.flat_day()["bars"]})
        f.stocks = {"SYN001": stock_info("SYN001"), "SYN002": stock_info("SYN002", "평평종목"),
                    "SYN003": stock_info("SYN003", "합성ETF", security_type="ETF")}
        f.ranking = ["SYN003", "SYN001", "SYN002"]
        day = {"startTime": "2026-10-28T09:00:00+09:00", "singlePriceAuctionStartTime": "2026-10-28T15:20:00+09:00",
               "endTime": "2026-10-28T15:30:00+09:00"}
        f.calendar = {"2026-10-28": {"previousBusinessDay": {"date": "2026-10-27"},
                                     "today": {"date": "2026-10-28", "integrated": {"regularMarket": day}},
                                     "nextBusinessDay": {"date": "2026-10-29"}}}
        f.limits = {"SYN001": {"timestamp": "x", "upperLimitPrice": "39550", "lowerLimitPrice": "21350"}}
        f.prices = {"SYN001": 30450}
        self.fake = f
        data.set_client(toss.Client(credentials=("tsck_live_test", "s"), transport=f, sleep=lambda s: None,
                                    token_path=os.path.join(self.tmp, "token.json")))
        self._dart = (dart.api_key, dart._http)
        dart.api_key = lambda env=None: "test-key"
        dart._http = lambda url, timeout=20: json.dumps({"status": "000", "total_page": 1, "list": []})
        os.makedirs("trader-kr")
        krx.write_json("trader-kr/config.json", {"budget": 5000000})

    def tearDown(self):
        dart.api_key, dart._http = self._dart
        data.set_client(None)
        os.chdir(self.old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = today.main(list(argv))
        return code, out.getvalue()

    def test_one_call_does_the_whole_morning(self):
        code, text = self.call("--now", "2026-10-28 10:16", "--replays", "300", "--sim", "1")
        self.assertEqual(code, 0, text)
        self.assertIn("시가 범위 돌파 1종목", text)
        self.assertIn("1. PASS      SYN001", text)
        self.assertIn("재생 300번", text)
        self.assertNotIn("SYN003", text)                     # ETF 는 스캔에서 빠짐
        paths = {p for _, p, _ in self.fake.log}
        for p in ("/api/v1/rankings", "/api/v1/candles", "/api/v1/price-limits", "/api/v1/orderbook",
                  "/api/v1/prices", "/api/v1/market-calendar/KR"):
            self.assertIn(p, paths)
        self.assertFalse(any("order" in p and "orderbook" not in p for p in paths))

    def test_a_warning_blocks_with_the_reason(self):
        self.fake.warnings["SYN001"] = [{"warningType": "INVESTMENT_WARNING", "exchange": "KRX",
                                         "startDate": "2026-10-28", "endDate": None}]
        code, text = self.call("--now", "2026-10-28 10:16", "--sim", "1", "--replays", "300")
        self.assertEqual(code, 0, text)
        self.assertIn("1. BLOCK     SYN001", text)
        self.assertIn("투자경고종목", text)
        self.assertNotIn("재생 300번", text)                 # 막힌 후보는 재생하지 않음


if __name__ == "__main__":
    unittest.main()
