import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import load
from fake_toss import FakeToss
from krcommon import data, krx, toss

al = load("trader-kr-alerts", "alerts")
DAY = datetime.date(2026, 11, 23)
S = krx.Session(DAY)                                                  # 09:00 / 15:20 / 15:30
LATE = krx.Session(DAY, datetime.time(10, 0), datetime.time(16, 20), datetime.time(16, 30), source="toss-calendar")


def at(hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime.datetime.combine(DAY, datetime.time(h, m))


def q(price, ts="2026-11-23T10:00:00+09:00"):
    return {"price": float(price), "ts": ts}


def fresh():
    return {"rang": {}, "base": {}}


class Clock(unittest.TestCase):
    def test_status(self):
        self.assertEqual(al.status(at("08:59"), S)[0], "pre")
        self.assertEqual(al.status(at("09:00"), S)[0], "open")
        self.assertEqual(al.status(at("15:25"), S)[0], "auction")
        self.assertEqual(al.status(at("15:30"), S)[0], "after")
        self.assertEqual(al.status(at("10:00"), None), ("closed", "휴장"))

    def test_next_open_skips_holiday(self):
        cal = krx.Calendar({"2026-11-24": None})
        self.assertEqual(al.next_open(at("16:00"), cal).day, datetime.date(2026, 11, 25))
        self.assertEqual(al.next_open(at("08:00"), cal).day, DAY)


class Evaluate(unittest.TestCase):
    def test_price_rings_once_and_rearms(self):
        rules = [{"id": "a", "type": "below", "symbol": "005930", "price": 100.0},
                 {"id": "b", "type": "above", "symbol": "005930", "price": 120.0, "rearm": True}]
        st = fresh()
        self.assertEqual(al.evaluate(rules, at("10:00"), S, {"005930": q(110)}, {}, st), [])
        got = al.evaluate(rules, at("10:05"), S, {"005930": q(99)}, {}, st)
        self.assertEqual([r["id"] for r, _ in got], ["a"])
        self.assertIn("99원이 100원 이하", got[0][1])
        self.assertEqual(al.evaluate(rules, at("10:10"), S, {"005930": q(98)}, {}, st), [])     # 한 번만
        self.assertEqual([r["id"] for r, _ in al.evaluate(rules, at("10:15"), S, {"005930": q(121)}, {}, st)], ["b"])
        al.evaluate(rules, at("10:20"), S, {"005930": q(110)}, {}, st)                       # 되돌아감 -> 다시 걸림
        self.assertNotIn("b", st["rang"])
        self.assertEqual([r["id"] for r, _ in al.evaluate(rules, at("10:25"), S, {"005930": q(125)}, {}, st)], ["b"])

    def test_price_waits_for_the_session_and_today(self):
        rules = [{"id": "a", "type": "stop", "symbol": "005930", "price": 100.0, "date": "2026-11-23"},
                 {"id": "y", "type": "stop", "symbol": "005930", "price": 100.0, "date": "2026-11-20"}]
        st = fresh()
        self.assertEqual(al.evaluate(rules, at("08:50"), S, {"005930": q(90)}, {}, st), [])       # 장 전
        self.assertEqual(al.evaluate(rules, at("10:00"), None, {"005930": q(90)}, {}, st), [])    # 휴장
        got = al.evaluate(rules, at("10:00"), S, {"005930": q(90, "2026-11-23T09:30:00+09:00")}, {}, st)
        self.assertEqual([r["id"] for r, _ in got], ["a"])                                       # 어제 규칙은 아님
        self.assertIn("15분 넘게 지난 시세", got[0][1])
        self.assertIn("broker.py watch", got[0][1])
        st2 = fresh()
        self.assertEqual(len(al.evaluate(rules, at("08:50"), S, {"005930": q(90)}, {}, st2, any_time=True)), 1)

    def test_pct_direction_once_a_day(self):
        rules = [{"id": "p", "type": "pct_move", "symbol": "005930", "pct": 3, "direction": "down"}]
        st = fresh()
        base = {"005930": 100.0}
        self.assertEqual(al.evaluate(rules, at("10:00"), S, {"005930": q(104)}, base, st), [])   # 위로는 아님
        got = al.evaluate(rules, at("10:05"), S, {"005930": q(96.5)}, base, st)
        self.assertIn("기준가 100원 대비 -3.50%", got[0][1])
        self.assertEqual(al.evaluate(rules, at("11:00"), S, {"005930": q(90)}, base, st), [])
        tomorrow = datetime.datetime(2026, 11, 24, 10, 0)
        self.assertEqual(len(al.evaluate(rules, tomorrow, krx.Session(tomorrow.date()), {"005930": q(90)}, base, st)), 1)

    def test_time_and_bell(self):
        rules = [{"id": "f", "type": "time", "at": "15:15", "label": "정리 시각", "flat": True, "symbol": "005930"},
                 {"id": "t", "type": "time", "at": "15:10", "label": "정리 준비", "before_close": True},
                 {"id": "o", "type": "opening_bell"}]
        st = fresh()
        self.assertEqual([r["id"] for r, _ in al.evaluate(rules, at("09:01"), S, {}, {}, st)], ["o"])
        got = dict((r["id"], m) for r, m in al.evaluate(rules, at("15:16"), S, {}, {}, st))
        self.assertEqual(sorted(got), ["f", "t"])
        self.assertIn("broker.py flatten", got["f"])
        self.assertEqual(al.evaluate(rules, at("15:20"), S, {}, {}, st), [])
        st = fresh()                                               # 개장이 한 시간 늦은 날
        late = dict((r["id"], m) for r, m in al.evaluate(rules, at("15:16"), LATE, {}, {}, st))
        self.assertEqual(sorted(late), ["f", "o"])                 # 계획의 정리 시각은 계획이 이미 정했다
        late = dict((r["id"], m) for r, m in al.evaluate(rules, at("16:11"), LATE, {}, {}, st))
        self.assertIn("16:10 (개장이 늦은 날이라 15:10 에서 옮김)", late["t"])
        self.assertEqual(al.evaluate(rules, at("12:00"), None, {}, {}, fresh()), [])


class Build(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.plan = os.path.join(self.d, "plan.json")
        krx.write_json(self.plan, {"symbol": "SYN001", "name": "합성종목", "date": "2026-11-23", "stop": 30100,
                                   "target": 31150, "flat_by": "15:15"})

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def args(self, **kw):
        base = {k: None for k in ("symbol", "above", "below", "stop", "target", "pct", "direction", "at", "label")}
        base.update({"from_plan": False, "plan": self.plan, "opening_bell": False, "before_close": False,
                     "today": False, "rearm": False})
        base.update(kw)
        return type("A", (), base)()

    def test_from_plan_replaces_old(self):
        keep, new = al.build(self.args(from_plan=True), [], DAY)
        self.assertEqual([(r["id"], r["type"]) for r in new],
                         [("SYN001-plan-stop", "stop"), ("SYN001-plan-target", "target"), ("SYN001-plan-flat-by", "time")])
        self.assertEqual(new[2]["at"], "15:15")
        krx.write_json(self.plan, {"symbol": "SYN001", "date": "2026-11-23", "stop": 30000, "target": 31150})
        keep, again = al.build(self.args(from_plan=True), keep + new + [{"id": "x", "type": "opening_bell"}], DAY)
        self.assertEqual([r["id"] for r in keep], ["x"])
        self.assertEqual(again[0]["price"], 30000.0)

    def test_checks(self):
        with self.assertRaises(al.AlertError):
            al.build(self.args(symbol="005930"), [], DAY)
        with self.assertRaises(al.AlertError):
            al.build(self.args(symbol="005930", above=1.0, below=2.0), [], DAY)
        with self.assertRaises(al.AlertError):
            al.build(self.args(symbol="005930", pct=40.0), [], DAY)
        _, new = al.build(self.args(symbol="005930", pct=3.0, direction="down", today=True), [], DAY)
        self.assertEqual((new[0]["id"], new[0]["date"]), ("005930-pct3d", "2026-11-23"))
        _, new = al.build(self.args(at="9:05", label="점검"), [], DAY)
        self.assertEqual((new[0]["id"], new[0]["at"]), ("time-0905", "09:05"))
        _, bell = al.build(self.args(opening_bell=True), [], DAY)
        with self.assertRaises(al.AlertError):
            al.build(self.args(opening_bell=True), bell, DAY)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.old = os.getcwd()
        os.chdir(self.d)
        self.fake = FakeToss()
        self.fake.prices = {"005930": 237500}
        self.fake.limits = {"005930": {"upperLimitPrice": "314500", "lowerLimitPrice": "169500"}}
        reg = lambda d: {"date": d, "integrated": {"regularMarket": {                      # noqa: E731
            "startTime": d + "T09:00:00+09:00", "singlePriceAuctionStartTime": d + "T15:20:00+09:00",
            "endTime": d + "T15:30:00+09:00"}}}
        self.fake.calendar = {"2026-11-23": {"previousBusinessDay": reg("2026-11-20"), "today": reg("2026-11-23"),
                                             "nextBusinessDay": reg("2026-11-24")}}
        data.set_client(toss.Client(credentials=("tsck_live_test", "s"), transport=self.fake, sleep=lambda s: None,
                                    token_path=os.path.join(self.d, "token.json")))

    def tearDown(self):
        data.set_client(None)
        os.chdir(self.old)
        shutil.rmtree(self.d, ignore_errors=True)

    def run_a(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = al.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_add_check_list_reset(self):
        code, out, _ = self.run_a("add", "--symbol", "005930", "--below", "238000", "--now", "2026-11-23 10:00")
        self.assertEqual(code, 0)
        self.assertIn("005930-below-238000", out)
        self.run_a("add", "--symbol", "005930", "--pct", "2", "--direction", "up", "--now", "2026-11-23 10:00")
        code, out, _ = self.run_a("check", "--now", "2026-11-23 10:16")
        self.assertEqual(code, 0)
        self.assertIn("2026-11-23 (월) 10:16 한국 시간, 접속매매 중 (15:20 까지).", out)
        self.assertIn("ALERT 아래로 005930: 현재가 237,500원이 238,000원 이하입니다", out)
        self.assertNotIn("기준가", out)                                  # +2% 위로는 아직
        self.assertIn("not financial advice", out)
        with open(os.path.join("trader-kr", "state", "alerts.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["base"]["2026-11-23"]["005930"], 242000.0)   # 상·하한가의 가운데
        code, out, _ = self.run_a("check", "--now", "2026-11-23 10:20")
        self.assertEqual(code, 1)
        self.assertIn("울린 것 없음", out)
        code, out, _ = self.run_a("list")
        self.assertIn("[울림 2026-11-23 10:16:00]", out)
        self.run_a("reset", "005930-below-238000")
        self.assertEqual(self.run_a("check", "--now", "2026-11-23 10:25")[0], 0)
        code, _, err = self.run_a("remove", "nope")
        self.assertEqual(code, 2)

    def test_quote_and_clock(self):
        code, out, _ = self.run_a("quote", "005930", "--now", "2026-11-23 10:16")
        self.assertIn("005930 237,500원 (10:16 시세), 기준가 242,000원 대비 -1.86%", out)
        code, out, _ = self.run_a("clock", "--now", "2026-11-23 08:30")
        self.assertIn("장 전 (09:00 개장)", out)
        self.assertIn("다음 개장: 2026-11-23 09:00.", out)

    def test_holiday_check_skips_prices(self):
        self.run_a("add", "--symbol", "005930", "--below", "300000")
        code, out, _ = self.run_a("check", "--now", "2026-11-22 11:00", "--no-network")
        self.assertEqual(code, 1)
        self.assertIn("휴장", out)
        self.assertIn("정규장 밖이라 가격 알림은 보지 않았습니다", out)


if __name__ == "__main__":
    unittest.main()
