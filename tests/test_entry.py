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
from krcommon import krx

entry = load("trader-kr-entry", "entry")
DAY = synth.PLAN_DAY


def at(h, m, s=0):
    return datetime.datetime.combine(DAY, datetime.time(h, m, s))


class Evaluate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chart = synth.make_history()
        cls.session = krx.Session(DAY)

    def ev(self, now, **kw):
        return entry.evaluate(self.chart, now, self.session, **kw)

    def test_the_10_10_bar_triggers_at_10_15(self):
        r = self.ev(at(10, 15, 5))
        self.assertEqual(r["status"], "triggered")
        self.assertEqual(r["trigger"]["time"], "10:15")
        self.assertEqual(r["trigger"]["bar_start"], "10:10")
        self.assertEqual(r["trigger"]["bar_index"], 14)
        self.assertEqual(r["trigger"]["price"], 30450)
        self.assertEqual(r["opening_range"]["high"], 30300)
        self.assertEqual(r["opening_range"]["low"], 29900)

    def test_a_bar_still_forming_does_not_count(self):
        r = self.ev(at(10, 13))
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(r["forming"]["bar_start"], "10:10")
        self.assertEqual(r["forming"]["minutes"], 3)
        self.assertIsNone(r["trigger"])

    def test_range_forming_and_pre_open(self):
        self.assertEqual(self.ev(at(9, 10))["phase"], "forming")
        self.assertEqual(self.ev(at(8, 40))["phase"], "pre-open")

    def test_holiday_is_no_data(self):
        r = entry.evaluate(self.chart, at(10, 15), None)
        self.assertEqual(r["status"], "no-data")

    def test_level_setup_and_volume_filter(self):
        r = self.ev(at(10, 15, 5), setup="level", level=30420)
        self.assertEqual(r["status"], "triggered")
        thin = self.ev(at(10, 15, 5), min_volume_x=50)
        self.assertEqual(thin["status"], "waiting")
        self.assertIn("거래량", thin["reason"])

    def test_no_breakout_expires_after_the_cutoff(self):
        flat = synth.flat_day()
        r = entry.evaluate(flat, at(14, 51), self.session)
        self.assertEqual(r["status"], "expired")
        r2 = entry.evaluate(flat, at(11, 0), self.session)
        self.assertEqual(r2["status"], "waiting")

    def test_late_open_day_moves_the_range(self):
        s = krx.Session(DAY, datetime.time(10, 0), datetime.time(15, 20), datetime.time(15, 30), "toss-calendar")
        r = entry.evaluate(self.chart, at(10, 10), s)
        self.assertEqual(r["phase"], "forming")
        self.assertEqual(r["no_entries_before"], "10:15")


class Commands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.getcwd()
        os.chdir(self.tmp)
        with open("SYN001.json", "w", encoding="utf-8") as fh:
            json.dump(synth.make_history(), fh)

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_main(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = entry.main(argv)
        return code, out.getvalue()

    def test_check_writes_entry_json(self):
        code, text = self.run_main(["check", "SYN001", "--offline", "SYN001.json", "--now", "2026-10-28 10:15:05",
                                    "--write"])
        self.assertEqual(code, 0, text)
        self.assertIn("신호 10:15, 30,450", text)
        self.assertIn("주문은 내지 않았습니다", text)
        with open("trader-kr/entry.json", encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual((doc["status"], doc["price"], doc["time"]), ("triggered", 30450, "10:15"))
        self.assertEqual(doc["opening_range"], {"high": 30300, "low": 29900, "minutes": 15})

    def test_watch_replays_the_day_on_a_simulated_clock(self):
        code, text = self.run_main(["watch", "SYN001", "--offline", ".", "--now", "2026-10-28 09:05",
                                    "--out", "trader-kr/entry.json"])
        self.assertEqual(code, 0, text)
        self.assertIn("시가 범위 확정: 고가 30,300, 저가 29,900", text)
        self.assertIn("신호: 30,300 위에서 끝남", text)
        self.assertTrue(os.path.exists("trader-kr/entry.json"))

    def test_shorts_are_refused(self):
        code, _ = self.run_main(["check", "SYN001", "--offline", "SYN001.json", "--side", "short"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
