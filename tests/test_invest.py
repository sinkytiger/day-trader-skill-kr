import contextlib
import datetime
import io
import json
import math
import os
import shutil
import tempfile
import unittest
import urllib.parse

from _load import load
from krcommon import krxdata

inv = load("trader-kr-invest", "invest")
ZERO = {"commission": 0.0, "tax": 0.0, "slip": 0.0}
HOLIDAY = datetime.date(2010, 2, 15)


def row(sym, c, chg, cap, name=None, market="KOSPI", value=1e9):
    return {"symbol": sym, "name": name or "종목" + sym, "market": market, "c": c, "chg": chg, "mktcap": cap,
            "value": value}


def make_store(tmp, growth, start=datetime.date(2010, 1, 4), end=datetime.date(2011, 6, 30), shares=None,
               extra=None):
    """growth: {종목: 하루 수익률}. 가격은 1000 에서 시작해 매일 그만큼 오른다. extra(d, rows) 로 고친다."""
    px = {s: 1000.0 for s in growth}
    for d in inv.weekdays(start, end):
        if d == HOLIDAY:
            inv.write_day(tmp, d, {})
            continue
        rows = {}
        for s, g in sorted(growth.items()):
            new = px[s] * (1 + g)
            rows[s] = row(s, new, new - px[s], new * (shares or {}).get(s, 1000.0))
            px[s] = new
        if extra:
            extra(d, rows)
        inv.write_day(tmp, d, rows)


class Data(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_store_keeps_common_shares_and_counts_both_markets(self):
        d = datetime.date(2010, 1, 4)
        inv.write_day(self.tmp, d, {"000010": row("000010", 100.0, 1.0, 5e9), "000015": row("000015", 50.0, 0.0, 1e9),
                                    "000020": row("000020", 10.0, -1.0, 1e9, name="가나스팩1호", market="KOSDAQ")})
        doc = inv.read_day(self.tmp, d)
        self.assertEqual([r[0] for r in doc["rows"]], ["000010", "000020"])     # 우선주(끝자리 5) 빠짐
        self.assertEqual(doc["n"], {"KOSPI": 2, "KOSDAQ": 1})
        self.assertEqual(doc["rows"][0], ["000010", "K", "종목000010", 100.0, 1.0, 5e9, 1e9])
        self.assertTrue(inv.is_spac(doc["rows"][1][2]))

    def test_parse_row_keeps_change_against_base_price(self):
        r = krxdata.parse_row({"ISU_CD": "005930", "ISU_NM": "삼성전자", "TDD_CLSPRC": "51,900", "CMPPREVDD_PRC": "-1,100",
                               "FLUC_RT": "-2.08", "LIST_SHRS": "6,419,324,700", "MKTCAP": "1", "SECT_TP_NM": "-"},
                              "KOSPI")
        self.assertEqual((r["chg"], r["rate"], r["shares"]), (-1100.0, -2.08, 6419324700.0))
        self.assertAlmostEqual(r["c"] / (r["c"] - r["chg"]) - 1, -1100 / 53000.0)


class FakeKrx(object):
    def __init__(self, empty_from=None, fail_on=None):
        self.empty_from, self.fail_on, self.calls = empty_from, fail_on, 0

    def __call__(self, url, headers):
        self.calls += 1
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        d = datetime.datetime.strptime(q["basDd"], "%Y%m%d").date()
        if self.fail_on and d >= self.fail_on:
            return 401, ""
        if d == HOLIDAY or (self.empty_from and d >= self.empty_from):
            return 200, json.dumps({"OutBlock_1": []})
        return 200, json.dumps({"OutBlock_1": [{"ISU_CD": "000010", "ISU_NM": "가", "TDD_CLSPRC": "100",
                                                "CMPPREVDD_PRC": "1", "MKTCAP": "1000", "ACC_TRDVAL": "5"}]})


class Download(unittest.TestCase):
    NOW = datetime.datetime(2010, 6, 1, 9, 0)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_downloads_only_missing_days_and_keeps_holidays(self):
        fk = FakeKrx()
        res = inv.download(datetime.date(2010, 2, 10), datetime.date(2010, 2, 19), "k", self.tmp, self.NOW, fk,
                           workers=2)
        self.assertEqual((res["wanted"], res["fetched"], res["stopped"]), (8, 8, None))
        self.assertEqual(inv.read_day(self.tmp, HOLIDAY)["rows"], [])
        again = inv.download(datetime.date(2010, 2, 10), datetime.date(2010, 2, 19), "k", self.tmp, self.NOW, fk)
        self.assertEqual(again["wanted"], 0)
        self.assertEqual(fk.calls, 16)

    def test_stops_on_a_key_error_and_resumes_later(self):
        res = inv.download(datetime.date(2010, 3, 1), datetime.date(2010, 3, 12), "k", self.tmp, self.NOW,
                           FakeKrx(fail_on=datetime.date(2010, 3, 8)), workers=1)
        self.assertIn("KRX 오류", res["stopped"])
        self.assertIsNotNone(inv.read_day(self.tmp, datetime.date(2010, 3, 5)))
        self.assertIsNone(inv.read_day(self.tmp, datetime.date(2010, 3, 8)))

    def test_a_long_run_of_empty_weekdays_is_not_saved_as_holidays(self):
        res = inv.download(datetime.date(2010, 3, 1), datetime.date(2010, 3, 31), "k", self.tmp, self.NOW,
                           FakeKrx(empty_from=datetime.date(2010, 3, 10)), workers=1)
        self.assertIn("이어서 비었", res["stopped"])
        self.assertIsNotNone(inv.read_day(self.tmp, datetime.date(2010, 3, 9)))
        self.assertIsNone(inv.read_day(self.tmp, datetime.date(2010, 3, 10)))

    def test_call_cap(self):
        res = inv.download(datetime.date(2010, 3, 1), datetime.date(2010, 3, 31), "k", self.tmp, self.NOW,
                           FakeKrx(), max_calls=10, workers=1)
        self.assertEqual((res["fetched"], res["calls"]), (5, 10))
        self.assertIn("상한", res["stopped"])


class PanelMath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_split_is_seamless_and_first_day_return_is_ignored(self):
        def split(d, rows):
            if d >= datetime.date(2010, 1, 6):            # 1월 6일 50:1 분할. 기준가가 같이 조정되어 전일대비는 작다
                r = rows["000010"]
                rows["000010"] = row("000010", r["c"] / 50, r["chg"] / 50, r["mktcap"])
        make_store(self.tmp, {"000010": 0.01}, end=datetime.date(2010, 1, 8), extra=split)
        P = inv.load_panel(self.tmp, datetime.date(2010, 1, 8))
        self.assertAlmostEqual(P.final["000010"][1], 1.01 ** 4)        # 1월 5 ~ 8일 4번, 분할 날도 1%
        self.assertEqual(P.no_chg, 0)

    def test_month_anchors_skip_a_month_cut_in_the_middle(self):
        ds = [datetime.date(2010, 1, 28), datetime.date(2010, 1, 29), datetime.date(2010, 2, 1),
              datetime.date(2010, 2, 2)]
        self.assertEqual(inv.month_anchors(ds), [("2010-01", ds[1], ds[2])])
        ds2 = ds[:2]
        self.assertEqual(inv.month_anchors(ds2), [("2010-01", ds[1], None)])     # 1월 29일 금요일이 달 끝

    def test_delisting_versions_and_halts(self):
        def events(d, rows):
            if d > datetime.date(2010, 3, 15):
                rows.pop("000020")                        # 3월 15일 뒤 영영 사라짐
            if datetime.date(2010, 3, 25) <= d <= datetime.date(2010, 4, 5):
                rows.pop("000030")                        # 거래정지 뒤 다시 거래
        make_store(self.tmp, {"000010": 0.0, "000020": 0.001, "000030": 0.002}, end=datetime.date(2010, 5, 31),
                   extra=events)
        P = inv.load_panel(self.tmp)
        a, b = datetime.date(2010, 3, 1), datetime.date(2010, 4, 1)          # 2월과 3월의 체결일
        self.assertEqual([E for _, _, E in P.months[1:3]], [a, b])
        self.assertAlmostEqual(P.growth("000020", a, b, "A"), 1.001 ** 10)    # 3월 2일 ~ 15일 10거래일
        self.assertEqual(P.growth("000020", a, b, "B"), 0.0)
        g30 = P.growth("000030", a, b, "B")                                 # 정지 전 3월 24일 가격 (낙관적)
        self.assertAlmostEqual(g30, 1.002 ** 17)
        self.assertIn("000030", P.lk[b])
        self.assertNotIn("000020", P.lk[b])


class CodeReuse(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_a_code_back_after_a_year_is_a_new_listing(self):
        def relist(d, rows):
            if datetime.date(2010, 3, 1) <= d < datetime.date(2011, 4, 1):
                rows.pop("000020")                        # 상장폐지 뒤 1년 넘게 없다가 다시 상장
            elif d == datetime.date(2011, 4, 1):
                r = rows["000020"]
                rows["000020"] = row("000020", r["c"], r["c"] / 2, r["mktcap"])     # 공모가 기준 +100%
        make_store(self.tmp, {"000010": 0.0, "000020": 0.001}, end=datetime.date(2011, 6, 30), extra=relist)
        P = inv.load_panel(self.tmp)
        self.assertEqual([c for c, _, _ in P.reused], ["000020"])
        self.assertEqual(P.final["000020"][0], datetime.date(2010, 2, 26))           # 앞 회사는 그날 사라짐
        self.assertEqual(P.first["000020*"], datetime.date(2011, 4, 1))              # 새 회사는 첫 거래일부터
        n = len([d for d in P.dates if d > datetime.date(2011, 4, 1)])
        self.assertAlmostEqual(P.final["000020*"][1], 1.001 ** n)                     # 첫날 +100% 는 안 셈
        self.assertEqual(P.growth("000020", datetime.date(2010, 2, 1), datetime.date(2010, 3, 31), "B"), 0.0)


class Strategies(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.growth = {"000010": 0.002, "000020": 0.001, "000030": -0.001, "000040": 0.0005, "000050": 0.003}
        shares = {"000010": 1000.0, "000020": 3000.0, "000030": 4000.0, "000040": 2000.0, "000050": 10.0}

        def spac(d, rows):
            rows["000060"] = row("000060", 1000.0, 0.0, 9e12, name="큰스팩")
            if d >= datetime.date(2010, 6, 1):
                rows["000070"] = row("000070", 1000.0, 0.0, 5e6)                 # 이력이 짧은 큰 종목
        make_store(cls.tmp, cls.growth, shares=shares, extra=spac)
        cls.P = inv.load_panel(cls.tmp)
        cls.old = inv.UNIVERSE
        inv.UNIVERSE = 4

    @classmethod
    def tearDownClass(cls):
        inv.UNIVERSE = cls.old
        shutil.rmtree(cls.tmp)

    def test_universe_needs_twelve_months_and_skips_spacs(self):
        U = inv.universe(self.P, self.P.mi["2011-01"])
        self.assertEqual([s for s, _ in U], ["000020", "000030", "000040", "000010"])   # 시가총액 큰 순 4개
        self.assertIsNone(inv.universe(self.P, self.P.mi["2010-12"]))

    def test_rule_picks(self):
        i = self.P.mi["2011-01"]
        pick = lambda v: sorted(inv.targets(self.P, i, v))              # noqa: E731
        self.assertEqual(pick({"fam": "mom", "n": 2, "reb": "M"}), ["000010", "000020"])
        self.assertEqual(pick({"fam": "rev", "n": 1, "reb": "M"}), ["000030"])
        self.assertEqual(pick({"fam": "small", "n": 2, "reb": "Q"}), ["000010", "000040"])
        self.assertEqual(pick({"fam": "hi52", "n": 3, "reb": "M"}), ["000010", "000020", "000040"])
        w = inv.targets(self.P, i, inv.BENCH)
        self.assertAlmostEqual(sum(w.values()), 1.0)
        self.assertGreater(w["000030"], w["000010"])

    def test_cost_free_monthly_return_is_the_average_growth(self):
        out = inv.backtest(self.P, {"fam": "ew", "reb": "M"}, "2011-02", "2011-03", "A", ZERO)
        i = self.P.mi["2011-01"]
        E, b = self.P.months[i][2], self.P.period_end(i)
        U = [s for s, _ in inv.universe(self.P, i)]
        want = sum(self.P.growth(s, E, b) for s in U) / len(U) - 1
        self.assertAlmostEqual(out[0]["r"], want)
        self.assertEqual([x["m"] for x in out], ["2011-02", "2011-03"])

    def test_costs_are_charged_on_traded_weight_only(self):
        v = {"fam": "mom", "n": 2, "reb": "Q"}
        free = inv.backtest(self.P, v, "2011-02", "2011-04", "A", ZERO)
        paid = inv.backtest(self.P, v, "2011-02", "2011-04", "A", inv.COST)
        buy = inv.COST["commission"] + inv.COST["slip"]
        self.assertAlmostEqual(1 + paid[0]["r"], (1 + free[0]["r"]) * (1 - buy))     # 처음에 다 삼
        self.assertAlmostEqual(paid[1]["r"], free[1]["r"])                         # 3월 말 신호 전까지 그대로
        self.assertAlmostEqual(paid[0]["turnover"], 0.5)

    def test_trend_goes_to_cash_when_the_market_is_below_its_average(self):
        mi = inv.market_index(self.P)
        self.assertEqual(list(mi)[0], "2010-01")
        i = self.P.mi["2011-01"]
        tg = inv.targets(self.P, i, {"fam": "trend", "ma": 10, "reb": "M"}, mi)
        self.assertEqual(sorted(tg), sorted(inv.targets(self.P, i, inv.BENCH)))   # 오르는 시장이라 B1 그대로
        flat = dict(mi)
        flat["2011-01"] = 0.0
        self.assertEqual(inv.targets(self.P, i, {"fam": "trend", "ma": 10, "reb": "M"}, flat), {})

    def test_run_prints_history_only_never_current_holdings(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = inv.main(["run", "--rule", "모멘텀12-1 30종목 매달", "--from", "2011-02", "--to", "2011-05",
                           "--store", self.tmp])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("월 초과수익 평균", out)
        for s in self.growth:
            self.assertNotIn(s, out)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            inv.main(["run", "--rule", "B1 시가총액가중 4", "--from", "2011-02", "--to", "2011-05", "--store",
                      self.tmp, "--json"])
        doc = json.loads(buf.getvalue())
        self.assertNotIn("holdings", json.dumps(doc))
        self.assertEqual(doc["months"], 4)


class Stats(unittest.TestCase):
    def test_newey_west_with_no_lag_is_the_plain_standard_error(self):
        xs = [0.01, -0.02, 0.03, 0.0, 0.015, -0.005]
        st = inv.nw(xs, lag=0)
        m = sum(xs) / len(xs)
        self.assertAlmostEqual(st["se"], math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs) / len(xs)))
        self.assertGreater(inv.nw([0.01, 0.012, 0.011, 0.013, 0.009])["mean"], 0)

    def test_perf_and_holm(self):
        p = inv.perf([0.1, -0.5, 0.2])
        self.assertAlmostEqual(p["mdd"], -0.5)
        self.assertAlmostEqual(p["cagr"], (1.1 * 0.5 * 1.2) ** 4 - 1)
        self.assertEqual(inv.holm([0.01, 0.04, 0.03]), [0.03, 0.06, 0.06])

    def test_dca_lump_sum_wins_in_a_rising_market(self):
        res = inv.dca([0.01] * 40, months=36)
        self.assertEqual((res["windows"], res["lump_wins"]), (5, 1.0))
        drip = sum(1.01 ** k for k in range(1, 37))
        self.assertAlmostEqual(res["median_ratio"], 36 * 1.01 ** 36 / drip)

    def test_sixteen_preregistered_rules_with_unique_names(self):
        R = inv.rules()
        self.assertEqual(len(R), 16)
        self.assertEqual(len({inv.name(v) for v in R} | {inv.name(inv.BENCH)}), 17)
        self.assertEqual(inv.find_rule("추세타이밍 12개월"), {"fam": "trend", "ma": 12, "reb": "M"})


if __name__ == "__main__":
    unittest.main()
