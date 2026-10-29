import datetime
import json
import os
import shutil
import tempfile
import unittest

import _load  # noqa: F401
import synth
from fake_toss import FakeToss
from krcommon import bars, krx, toss


class Clock(object):
    def __init__(self):
        self.t = 1000000.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def client(fake, tmp, clock=None):
    clock = clock or Clock()
    return toss.Client(credentials=("tsck_live_test", "secret"), transport=fake, sleep=clock.sleep, clock=clock,
                       token_path=os.path.join(tmp, "token.json"))


class Auth(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_token_is_cached_on_disk_and_reused(self):
        fake = FakeToss({synth.SYMBOL: synth.make_history()["bars"]})
        c = client(fake, self.tmp)
        c.candles(synth.SYMBOL, "1m", 5)
        c2 = client(fake, self.tmp)
        c2.candles(synth.SYMBOL, "1m", 5)
        self.assertEqual(fake.tokens, 1)
        with open(os.path.join(self.tmp, "token.json")) as fh:
            self.assertIn("expires_at", json.load(fh))

    def test_401_refreshes_the_token_once(self):
        fake = FakeToss(script=[(401, {}, json.dumps({"error": {"code": "invalid-token", "message": ""}}))])
        fake.limits["SYN001"] = {"upperLimitPrice": "39550", "lowerLimitPrice": "21350", "timestamp": "x"}
        c = client(fake, self.tmp)
        self.assertEqual(c.price_limits("SYN001")["upperLimitPrice"], "39550")
        self.assertEqual(fake.tokens, 2)

    def test_429_waits_for_retry_after(self):
        clk = Clock()
        fake = FakeToss(script=[(429, {"Retry-After": "2"}, json.dumps({"error": {"code": "rate-limit-exceeded"}}))])
        fake.limits["SYN001"] = {"upperLimitPrice": "1", "lowerLimitPrice": "1", "timestamp": "x"}
        c = client(fake, self.tmp, clk)
        c.price_limits("SYN001")
        self.assertIn(2.0, clk.slept)

    def test_403_names_the_ip_allowlist(self):
        fake = FakeToss(script=[(403, {}, json.dumps({"error": {"code": "forbidden", "message": ""}}))])
        c = client(fake, self.tmp)
        with self.assertRaises(toss.TossError) as e:
            c.price_limits("SYN001")
        self.assertIn("허용 IP", str(e.exception))

    def test_missing_credentials_say_where_to_put_them(self):
        real = toss.home_dir
        toss.home_dir = lambda: self.tmp
        try:
            with self.assertRaises(toss.TossError) as e:
                toss.find_credentials({"TOSS_CREDENTIALS": os.path.join(self.tmp, "nope.json")})
        finally:
            toss.home_dir = real
        self.assertIn("채팅에는 붙여 넣지 마세요", str(e.exception))

    def test_credentials_file_is_read_without_printing(self):
        path = os.path.join(self.tmp, "cred.json")
        with open(path, "w") as fh:
            json.dump({"client_id": "tsck_live_x", "client_secret": "y"}, fh)
        self.assertEqual(toss.find_credentials({"TOSS_CREDENTIALS": path}), ("tsck_live_x", "y"))

    def test_calls_are_spaced_under_the_group_limit(self):
        clk = Clock()
        fake = FakeToss({synth.SYMBOL: synth.make_history()["bars"]})
        c = client(fake, self.tmp, clk)
        for _ in range(5):
            c.candles(synth.SYMBOL, "1m", 1)
        self.assertGreaterEqual(len([s for s in clk.slept if s > 0]), 4)


class Paging(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.hist = synth.make_history()["bars"]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fetch_pages_back_and_the_cache_saves_calls(self):
        fake = FakeToss({synth.SYMBOL: self.hist})
        c = client(fake, self.tmp)
        cache = os.path.join(self.tmp, "cache")
        start = datetime.datetime(2026, 10, 20, 0, 0)
        end = datetime.datetime(2026, 10, 28, 15, 31)
        now = datetime.datetime(2026, 10, 29, 8, 0)
        got, info = bars.fetch_1m(c, synth.SYMBOL, start, end, cache, now)
        want = [b for b in self.hist if krx.ts_of(start) <= b["t"] + 60 <= krx.ts_of(end)]
        self.assertEqual([b["t"] for b in got], [b["t"] for b in want])
        self.assertGreater(info["calls"], 5)
        again, info2 = bars.fetch_1m(c, synth.SYMBOL, start, end, cache, now)
        self.assertEqual(info2["calls"], 0)
        self.assertEqual(len(again), len(got))
        older, info3 = bars.fetch_1m(c, synth.SYMBOL, datetime.datetime(2026, 10, 15, 0, 0), end, cache, now)
        self.assertGreater(info3["calls"], 0)
        self.assertLess(info3["calls"], info["calls"])
        self.assertEqual(len(older), len([b for b in self.hist if krx.ts_of(datetime.datetime(2026, 10, 15)) <= b["t"] + 60
                                          <= krx.ts_of(end)]))

    def test_bars_after_now_are_never_cached(self):
        fake = FakeToss({synth.SYMBOL: self.hist})
        c = client(fake, self.tmp)
        cache = os.path.join(self.tmp, "cache")
        now = datetime.datetime(2026, 10, 28, 10, 15, 30)
        got, _ = bars.fetch_1m(c, synth.SYMBOL, datetime.datetime(2026, 10, 28, 8, 59), now, cache, now)
        self.assertEqual(krx.kst_of(got[-1]["t"] + 60), datetime.datetime(2026, 10, 28, 10, 15))
        doc = bars.load_cache(cache, synth.SYMBOL)
        self.assertLessEqual(doc["to"], krx.ts_of(datetime.datetime(2026, 10, 28, 10, 15)))

    def upto(self, h, m):
        cut = krx.ts_of(datetime.datetime(2026, 10, 28, h, m))
        return [b for b in self.hist if b["t"] + 60 <= cut]

    def test_a_clock_ahead_of_the_wall_never_claims_missing_bars(self):
        # 실제 11:00 에 --now "15:40" 으로 돌린 경우 (검토에서 찾은 결함)
        fake = FakeToss({synth.SYMBOL: self.upto(11, 0)})
        c = client(fake, self.tmp)
        cache = os.path.join(self.tmp, "cache")
        day = datetime.datetime(2026, 10, 28, 8, 59)
        bars.fetch_1m(c, synth.SYMBOL, day, datetime.datetime(2026, 10, 28, 15, 31), cache,
                      datetime.datetime(2026, 10, 28, 15, 40), wall_kst=datetime.datetime(2026, 10, 28, 11, 0, 30))
        self.assertLessEqual(bars.load_cache(cache, synth.SYMBOL)["to"], krx.ts_of(datetime.datetime(2026, 10, 28, 11, 0)))
        fake.bars[synth.SYMBOL] = self.upto(14, 0)
        wall = datetime.datetime(2026, 10, 28, 14, 0, 30)
        got, info = bars.fetch_1m(c, synth.SYMBOL, day, wall, cache, wall, wall_kst=wall)
        self.assertGreater(info["calls"], 0)
        self.assertEqual(len(got), 300)                       # 09:01 ~ 14:00 봉이 빠짐없이

    def test_api_lag_is_filled_on_the_next_call(self):
        fake = FakeToss({synth.SYMBOL: self.upto(10, 50)})   # 자료가 10분 늦음
        c = client(fake, self.tmp)
        cache = os.path.join(self.tmp, "cache")
        day = datetime.datetime(2026, 10, 28, 8, 59)
        wall = datetime.datetime(2026, 10, 28, 11, 0, 30)
        bars.fetch_1m(c, synth.SYMBOL, day, wall, cache, wall, wall_kst=wall)
        self.assertEqual(krx.kst_of(bars.load_cache(cache, synth.SYMBOL)["to"]), datetime.datetime(2026, 10, 28, 10, 50))
        fake.bars[synth.SYMBOL] = self.upto(11, 5)
        wall = datetime.datetime(2026, 10, 28, 11, 5, 30)
        got, _ = bars.fetch_1m(c, synth.SYMBOL, day, wall, cache, wall, wall_kst=wall)
        self.assertEqual(len(got), 125)                       # 09:01 ~ 11:05

    def test_a_stuck_cursor_does_not_loop_forever(self):
        def stuck(method, url, headers, body):
            if "oauth2" in url:
                return 200, {}, json.dumps({"access_token": "t", "expires_in": 86400})
            return 200, {}, json.dumps({"result": {"candles": [], "nextBefore": "2026-10-28T10:00:00+09:00"}})
        c = toss.Client(credentials=("tsck_live_x", "y"), transport=stuck, sleep=lambda s: None,
                        token_path=os.path.join(self.tmp, "token.json"))
        got, info = bars.fetch_1m(c, synth.SYMBOL, datetime.datetime(2026, 10, 20), datetime.datetime(2026, 10, 28, 10, 0),
                                  None, datetime.datetime(2026, 10, 29, 9, 0))
        self.assertEqual(got, [])
        self.assertLess(info["calls"], 10)


if __name__ == "__main__":
    unittest.main()
