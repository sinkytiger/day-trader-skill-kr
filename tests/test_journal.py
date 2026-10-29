import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

from _load import load

journal = load("trader-kr-journal", "journal")
PLAN = {"symbol": "SYN001", "name": "합성종목", "side": "buy", "date": "2026-10-28", "setup": "orb", "entry": 30450,
        "entry_time": "10:15", "stop": 30100, "target": 31150, "flat_by": "15:15"}


class Journal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.getcwd()
        os.chdir(self.tmp)
        os.makedirs("trader-kr")
        for n, d in (("plan", PLAN), ("config", {"max_loss_per_trade_krw": 50000})):
            with open("trader-kr/%s.json" % n, "w", encoding="utf-8") as fh:
                json.dump(d, fh)

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = journal.main(argv)
        return code, out.getvalue() + err.getvalue()

    def test_a_clean_trade(self):
        code, text = self.call(["open", "--entry-price", "30450", "--qty", "119", "--entry-time", "10:16",
                                "--now", "2026-10-28 10:16"])
        self.assertEqual(code, 0, text)
        self.assertNotIn("주의", text)
        code, text = self.call(["close", "last", "--exit-price", "31150", "--exit-time", "11:05", "--reason", "target"])
        self.assertEqual(code, 0, text)
        self.assertIn("비용 뺀 R +1.8", text)
        s, ev = journal.summarize(journal.load(), {})
        self.assertEqual((s["closed"], s["win_rate"]), (1, 1.0))
        self.assertAlmostEqual(ev[0]["R"], 2.0)
        self.assertEqual(ev[0]["flags"], [])

    def test_bad_habits_are_counted(self):
        self.call(["open", "--entry-price", "30500", "--qty", "119", "--entry-time", "10:31", "--now", "2026-10-28 10:31"])
        code, text = self.call(["close", "last", "--exit-price", "30000", "--exit-time", "15:20", "--reason", "manual"])
        self.assertIn("손절 어김", text)
        self.assertIn("청산 시각 15:15 를 넘김", text)
        self.call(["open", "--entry-price", "30450", "--qty", "300", "--entry-time", "10:15", "--now", "2026-10-28 10:15"])
        self.call(["close", "last", "--exit-price", "30100", "--exit-time", "10:40", "--reason", "stop"])
        s, ev = journal.summarize(journal.load(), {"max_loss_per_trade_krw": 50000})
        self.assertEqual(s["closed"], 2)
        self.assertEqual(s["late"]["count"], 1)
        self.assertEqual(s["stop_breaches"]["count"], 1)
        self.assertEqual(s["stop_breaches"]["extra_loss"], 119 * 100)
        self.assertEqual(s["flat_violations"], 1)
        self.assertEqual(s["oversize"], 1)
        code, text = self.call(["report"])
        self.assertIn("늦은 진입(신호 뒤 15분 넘음) 1건", text)
        self.assertIn("작은 표본", text)

    def test_ids_list_and_errors(self):
        self.call(["open", "--entry-price", "30450", "--qty", "1", "--now", "2026-10-28 10:16"])
        self.call(["open", "--entry-price", "30450", "--qty", "1", "--now", "2026-10-28 10:20"])
        ids = [r["id"] for r in journal.load()]
        self.assertEqual(ids, ["20261028-SYN001-1", "20261028-SYN001-2"])
        code, text = self.call(["close", "nope", "--exit-price", "1", "--reason", "stop"])
        self.assertEqual(code, 1)
        self.call(["close", ids[0], "--exit-price", "30500", "--reason", "manual", "--exit-time", "11:00"])
        code, text = self.call(["close", ids[0], "--exit-price", "30500", "--reason", "manual"])
        self.assertEqual(code, 1)
        self.assertIn("이미 청산", text)
        code, text = self.call(["list", "--open"])
        self.assertIn(ids[1], text)
        self.assertNotIn(ids[0], text)


if __name__ == "__main__":
    unittest.main()
