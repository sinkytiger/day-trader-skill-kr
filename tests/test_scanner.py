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

scan = load("trader-kr-scanner", "scan")
DAY = synth.PLAN_DAY


class Orb(unittest.TestCase):
    def test_hit_and_miss(self):
        hit = synth.make_history()
        miss = dict(synth.flat_day(), symbol="SYN002")
        s = krx.Session(DAY)
        cutoff = datetime.datetime(2026, 10, 28, 10, 15)
        hits, counts = scan.scan_orb({"SYN001": hit, "SYN002": miss}, ["SYN001", "SYN002"], DAY, cutoff, s, 15)
        self.assertEqual(counts["scanned"], 2)
        self.assertEqual([h["symbol"] for h in hits], ["SYN001"])
        self.assertEqual((hits[0]["at"], hits[0]["price"], hits[0]["or_high"]), ("10:15", 30450, 30300))

    def test_a_missing_bar_makes_the_chart_unusable(self):
        ch = synth.make_history()
        gap = krx.ts_of(datetime.datetime(2026, 10, 28, 9, 32))
        ch = dict(ch, bars=[b for b in ch["bars"] if b["t"] != gap])
        _, counts = scan.scan_orb({"SYN001": ch}, ["SYN001"], DAY, datetime.datetime(2026, 10, 28, 10, 15),
                                  krx.Session(DAY), 15)
        self.assertEqual(counts["unusable"], 1)

    def test_window_errors(self):
        s = krx.Session(DAY)
        with self.assertRaises(scan.ScanError):
            scan.orb_window(datetime.datetime(2026, 10, 28, 9, 12), None, 15, None, s)
        with self.assertRaises(scan.ScanError):
            scan.orb_window(datetime.datetime(2026, 10, 28, 15, 0), None, 15, datetime.time(14, 50), s)
        with self.assertRaises(scan.ScanError):
            scan.orb_window(datetime.datetime(2026, 10, 28, 10, 0), None, 15, None, None)

    def test_offline_command(self):
        tmp = tempfile.mkdtemp()
        old = os.getcwd()
        os.chdir(tmp)
        try:
            with open("SYN001.json", "w", encoding="utf-8") as fh:
                json.dump(synth.make_history(), fh)
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = scan.main(["orb", "--offline", ".", "--now", "2026-10-28 10:15", "--out", "trader-kr/watchlist.json"])
            self.assertEqual(code, 0, out.getvalue())
            with open("trader-kr/watchlist.json", encoding="utf-8") as fh:
                doc = json.load(fh)
            self.assertEqual(doc["hits"][0]["symbol"], "SYN001")
        finally:
            os.chdir(old)
            shutil.rmtree(tmp, ignore_errors=True)


class Daily(unittest.TestCase):
    def rows(self, closes, value=2e9):
        start = datetime.date(2026, 8, 1)
        return [{"date": (start + datetime.timedelta(days=i)).isoformat(), "c": c, "v": value / c}
                for i, c in enumerate(closes)]

    def test_breakout_and_liquidity(self):
        up = [10000 + 10 * i for i in range(40)] + [11000]
        now = datetime.datetime(2026, 10, 1, 8, 30)
        hits, counts, _ = scan.scan_daily({"A00001": self.rows(up), "A00002": self.rows(up, 1e8)},
                                          ["A00001", "A00002"], now, 1000, 1e9)
        self.assertEqual([h["symbol"] for h in hits], ["A00001"])
        self.assertIn("breakout_20d", hits[0]["setups"])
        self.assertEqual(counts["illiquid"], 1)

    def test_todays_bar_waits_for_the_close(self):
        closes = [10000] * 40
        rows = self.rows(closes)
        today = datetime.date.fromisoformat(rows[-1]["date"])
        kept = scan.daily_closes(rows, datetime.datetime.combine(today, datetime.time(11, 0)))
        self.assertEqual(len(kept), 39)
        kept = scan.daily_closes(rows, datetime.datetime.combine(today, datetime.time(15, 31)))
        self.assertEqual(len(kept), 39)                     # NXT 애프터마켓이 20:00 까지
        kept = scan.daily_closes(rows, datetime.datetime.combine(today, datetime.time(20, 1)))
        self.assertEqual(len(kept), 40)

    def test_rsi_matches_wilder(self):
        self.assertEqual(scan.rsi([1] * 20), 100.0)
        down = [100 - i for i in range(20)]
        self.assertLess(scan.rsi(down), 1)


if __name__ == "__main__":
    unittest.main()
