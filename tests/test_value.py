import contextlib
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.parse
import zipfile

from _load import load
from krcommon import financials as fin

inv = load("trader-kr-invest", "invest")
vm = load("trader-kr-invest", "value")
NOW = datetime.datetime(2026, 11, 1, 10, 0)
START, END = datetime.date(2013, 1, 2), datetime.date(2018, 6, 29)
D = datetime.date

# 종목: (KRX 이름, 하루 수익률, 주식 수, DART 이름, 업종, 결산월, 마지막 거래일, 지배주주 자본(FY2015), 순이익, 주당배당금)
WORLD = {
    "100000": ("가치전자", 0.0004, 1e7, "가치전자(주)", "264", "12", None, 4e11, 4e10, 500.0),
    "200000": ("성장소프트", 0.0008, 1e7, "성장소프트(주)", "582", "12", None, 2e10, 1e10, 0.0),
    "300000": ("합성은행", 0.0001, 1e7, "합성은행(주)", "64110", "12", None, 9e11, 9e10, 1000.0),
    "400000": ("합성홀딩스", 0.0003, 1e7, "합성홀딩스(주)", "64992", "12", None, 3e11, 2e10, 200.0),
    "500000": ("합성리츠", 0.0002, 1e7, "합성위탁관리부동산투자회사", "68112", "12", None, 1e11, 5e9, 400.0),
    "600000": ("삼월결산", 0.0002, 1e7, "삼월결산(주)", "201", "03", None, 1e11, 5e9, 100.0),
    "700000": ("늦은공시", 0.0005, 1e7, "늦은공시(주)", "301", "12", None, 1e11, 1e10, 0.0),
    "800000": ("상폐예정", -0.0005, 1e7, "상폐예정(주)", "202", "12", D(2016, 11, 15), 2e11, -1e10, 300.0),
    "900000": ("합성금융지주", 0.0002, 1e7, "합성금융지주(주)", "64992", "12", None, 8e11, 6e10, 700.0),
}
PREF = ("100005", 5000.0, 1e10)            # 가치전자 우선주 (KRX 표에만)
CORRECTED = ("100000", 2016, "20170601")    # 가치전자 FY2016 은 T(2017-04-28) 뒤에 정정


def corp(sym):
    return "00" + sym


def first_filed(sym, y):
    if sym == "700000" and y == 2015:
        return "20160510"                    # 늦게 낸 사업보고서
    return "%d0330" % (y + 1)


def make_closes():
    out, px = {}, {s: 10000.0 for s in WORLD}
    d = START
    while d <= END:
        if d.weekday() < 5:
            for s, w in WORLD.items():
                if w[6] and d > w[6]:
                    continue
                px[s] *= 1 + w[1]
                out.setdefault(s, {})[d] = px[s]
        d += datetime.timedelta(days=1)
    return out


CLOSE = make_closes()


def last_dec_day(y):
    d = D(y, 12, 31)
    while d.weekday() >= 5:
        d -= datetime.timedelta(days=1)
    return d


def vals(sym, y):
    w = WORLD[sym]
    k = 1.1 ** (y - 2015)
    eqp, ni = w[7] * k, w[8] * k
    out = {"Revenue": 10 * abs(ni) + 1e11, "OperatingIncomeLoss": 1.2 * ni, "ProfitLoss": ni * 1.02,
           "ProfitLossAttributableToOwnersOfParent": ni, "EquityAttributableToOwnersOfParent": eqp, "Equity": eqp * 1.02,
           "Assets": eqp * 1.6, "Liabilities": eqp * 0.58, "CurrentAssets": eqp * 0.8, "CurrentLiabilities": eqp * 0.3,
           "NoncurrentLiabilities": eqp * 0.28, "Inventories": 1e10 * k,
           "CashFlowsFromUsedInOperatingActivities": ni * 1.3,
           "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities": -0.3 * abs(ni)}
    if sym == "900000":                      # 금융지주: 유동·비유동 구분이 없다
        for k2 in ("CurrentAssets", "CurrentLiabilities", "NoncurrentLiabilities", "Inventories"):
            del out[k2]
    return out


SJ = {"Revenue": "IS", "OperatingIncomeLoss": "IS", "ProfitLoss": "IS", "ProfitLossAttributableToOwnersOfParent": "IS",
      "CashFlowsFromUsedInOperatingActivities": "CF",
      "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities": "CF"}


def has_fs(sym, y):
    w = WORLD[sym]
    return 2015 <= y <= 2017 and not (w[6] and y >= w[6].year)


def dps(sym, y):
    return WORLD[sym][9] if has_fs(sym, y) or (WORLD[sym][4].startswith("6") and 2015 <= y <= 2017) else None


class FakeDart(object):
    def __init__(self):
        self.calls = []

    def __call__(self, url):
        u = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        name = u.path.rsplit("/", 1)[-1]
        self.calls.append((name, q.get("corp_code"), q.get("bsns_year"), q.get("fs_div")))
        if name == "corpCode.xml":
            xml = "<result>%s</result>" % "".join(
                "<list><corp_code>%s</corp_code><corp_name>%s</corp_name><stock_code>%s</stock_code></list>"
                % (corp(s), w[3], s) for s, w in WORLD.items())
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("CORPCODE.xml", xml)
            return buf.getvalue()
        sym = q["corp_code"][2:]
        w = WORLD[sym]
        if name == "company.json":
            return json.dumps({"status": "000", "corp_name": w[3], "induty_code": w[4], "acc_mt": w[5]}).encode()
        if name == "list.json":
            rows = []
            for y in range(2015, 2018):
                if not has_fs(sym, y) and not w[4].startswith("6"):
                    continue
                mt = "12" if w[5] == "12" else w[5]
                f = first_filed(sym, y)
                rows.append({"rcept_dt": f, "rcept_no": f + "000001", "report_nm": "사업보고서 (%d.%s)" % (y, mt)})
                if (sym, y) == CORRECTED[:2]:
                    rows.append({"rcept_dt": CORRECTED[2], "rcept_no": CORRECTED[2] + "000002",
                                 "report_nm": "[기재정정]사업보고서 (%d.12)" % y})
            return json.dumps({"status": "000" if rows else "013", "total_page": 1, "list": rows}).encode()
        y = int(q["bsns_year"])
        if name == "alotMatter.json":
            d = dps(sym, y)
            if d is None:
                return json.dumps({"status": "013"}).encode()
            per = {"se": "주당 현금배당금(원)", "stock_knd": "보통주"}
            yld = {"se": "현금배당수익률(%)", "stock_knd": "보통주"}
            for col, yy in (("thstrm", y), ("frmtrm", y - 1), ("lwfr", y - 2)):
                c = CLOSE.get(sym, {}).get(last_dec_day(yy))
                per[col] = "{:,.0f}".format(d)
                yld[col] = "%.4f" % (100 * d / c) if c and d else "-"
            rows = [{"se": "현금배당금총액(백만원)", "thstrm": "1,000", "frmtrm": "1,000", "lwfr": "1,000"}, per, yld]
            return json.dumps({"status": "000", "list": rows}).encode()
        if name == "fnlttSinglAcntAll.json":
            if q["fs_div"] != "CFS" or not has_fs(sym, y):
                return json.dumps({"status": "013"}).encode()
            no = (CORRECTED[2] + "000002") if (sym, y) == CORRECTED[:2] else first_filed(sym, y) + "000001"
            rows = []
            for k, v in vals(sym, y).items():
                rows.append({"sj_div": SJ.get(k, "BS"), "account_id": "ifrs-full_" + k, "account_nm": k,
                             "thstrm_amount": "%.0f" % v, "frmtrm_amount": "%.0f" % vals(sym, y - 1)[k],
                             "bfefrmtrm_amount": "%.0f" % vals(sym, y - 2)[k], "rcept_no": no})
            return json.dumps({"status": "000", "list": rows}).encode()
        raise AssertionError(url)


class FakeKrx(object):
    def __call__(self, url, headers):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        d = datetime.datetime.strptime(q["basDd"], "%Y%m%d").date()
        if "ksq" in url:
            return 200, json.dumps({"OutBlock_1": []})
        out = []
        for s, w in WORLD.items():
            c = CLOSE[s].get(d)
            if c:
                out.append({"ISU_CD": s, "ISU_NM": w[0], "TDD_CLSPRC": "%.2f" % c, "CMPPREVDD_PRC": "0",
                            "MKTCAP": "%.0f" % (c * w[2]), "LIST_SHRS": "%.0f" % w[2], "ACC_TRDVAL": "1000"})
        if out:
            out.append({"ISU_CD": PREF[0], "ISU_NM": "가치전자우", "TDD_CLSPRC": "%.0f" % PREF[1], "MKTCAP": "%.0f" % PREF[2]})
        return 200, json.dumps({"OutBlock_1": out})


def write_store(store):
    prev = {}
    d = START
    while d <= END:
        if d.weekday() < 5:
            rows = {}
            for s, w in WORLD.items():
                c = CLOSE[s].get(d)
                if c is None:
                    continue
                p = prev.get(s, c / (1 + w[1]))
                rows[s] = {"symbol": s, "name": w[0], "market": "KOSPI", "c": c, "chg": c - p, "mktcap": c * w[2],
                           "value": 1e9}
                prev[s] = c
            inv.write_day(store, d, rows)
        d += datetime.timedelta(days=1)


class World(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.store = os.path.join(cls.tmp, "invest")
        cls.dart = os.path.join(cls.tmp, "dart")
        cls.krx = os.path.join(cls.tmp, "krx")
        write_store(cls.store)
        cls.P = inv.load_panel(cls.store, END)
        cls.fd = FakeDart()
        cls.res = vm.download(cls.P, "k", "k", cls.dart, cls.krx, NOW, cls.fd, FakeKrx(), workers=2)
        cls.S = vm.Store(cls.P, cls.dart, cls.krx, cls.store)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def month(self, m):
        return self.P.mi[m]

    def test_download_skips_statements_of_banks_reits_and_other_year_ends(self):
        self.assertIsNone(self.res["stopped"])
        self.assertEqual(self.res["firms"], len(WORLD))
        fs = {c[1][2:] for c in self.fd.calls if c[0] == "fnlttSinglAcntAll.json"}
        self.assertNotIn("300000", fs)                     # 은행
        self.assertNotIn("500000", fs)                     # 리츠
        self.assertNotIn("600000", fs)                     # 3월 결산
        self.assertIn("400000", fs)                        # 지주회사는 유동자산을 봐야 해서 받는다
        alot = {c[1][2:] for c in self.fd.calls if c[0] == "alotMatter.json"}
        self.assertIn("300000", alot)                      # 배당은 기준(BV)에 들어가니 모두 받는다
        n = len(self.fd.calls)
        again = vm.download(self.P, "k", "k", self.dart, self.krx, NOW, self.fd, FakeKrx())
        self.assertIsNone(again["stopped"])
        self.assertEqual(len(self.fd.calls), n)            # 모두 캐시에서

    def test_call_cap_stops_and_says_so(self):
        tmp = tempfile.mkdtemp()
        try:
            res = vm.download(self.P, "k", "k", tmp, self.krx, NOW, FakeDart(), FakeKrx(), max_calls=5, workers=1)
            self.assertIn("호출 상한", res["stopped"])
            self.assertLessEqual(res["calls"], 5)
            self.assertLess(res["done"], len(WORLD))             # 멈춘 뒤 건너뛴 회사는 세지 않는다
        finally:
            shutil.rmtree(tmp)

    def test_dropped_connections_are_retried_then_give_up(self):
        flaky = {"n": 0}
        fd = FakeDart()

        def transport(url):
            flaky["n"] += 1
            if flaky["n"] % 3 == 1:
                raise fin.FinError("DART 에 연결하지 못했습니다: Remote end closed connection without response")
            return fd(url)
        tmp = tempfile.mkdtemp()
        try:
            res = vm.download(self.P, "k", "k", tmp, self.krx, NOW, transport, FakeKrx(), workers=1, sleep=lambda x: None)
            self.assertIsNone(res["stopped"])
            self.assertGreater(res["retried"], 0)
            self.assertEqual(res["done"], len(WORLD))
        finally:
            shutil.rmtree(tmp)

        def dead(url):
            raise fin.FinError("DART 에 연결하지 못했습니다: timed out")
        waits = []
        c = vm.Calls(dead, 100, wait=1.0, sleep=waits.append)
        with self.assertRaises(fin.FinError):
            c("u")
        self.assertEqual((c.n, waits), (vm.RETRIES + 1, [1.0, 2.0, 4.0, 8.0]))
        bad = vm.Calls(lambda url: (_ for _ in ()).throw(fin.FinError("DART 재무제표 응답 020: 요청 제한")), 100,
                       sleep=waits.append)
        with self.assertRaises(fin.FinError):
            bad("u")
        self.assertEqual(bad.n, 1)                             # 요청 제한 같은 응답 오류는 다시 부르지 않는다

    def test_value_universe_excludes_financials_reits_other_year_ends_and_late_filers(self):
        F, why = self.S.features(self.month("2016-04"))
        self.assertEqual(sorted(F), ["100000", "200000", "400000", "800000"])
        self.assertEqual(why["금융"], 2)                    # 은행, 금융지주 (유동자산 없음)
        self.assertEqual((why["리츠"], why["12월 결산 아님"], why["FY2015 사업보고서가 T 까지 없음"]), (1, 1, 1))
        F17, _ = self.S.features(self.month("2017-04"))
        self.assertIn("700000", F17)                       # 다음 해에는 냈다
        self.assertNotIn("800000", F17)                    # 상장폐지

    def test_filed_report_without_statement_data_is_not_counted_as_late(self):
        tmp = tempfile.mkdtemp()
        try:
            dart = os.path.join(tmp, "dart")
            shutil.copytree(self.dart, dart)
            with open(os.path.join(dart, "fs", "%s_2015.json" % corp("900000")), "w", encoding="utf-8") as f:
                json.dump({"data": None}, f)               # 접수는 했는데 OpenDART 재무제표가 빈 응답
            S = vm.Store(self.P, dart, self.krx, self.store)
            F, why = S.features(self.month("2016-04"))
            self.assertNotIn("900000", F)
            self.assertEqual((why["재무제표 자료 없음 (FY2015 보고서는 T 까지 접수)"], why["금융"],
                              why["FY2015 사업보고서가 T 까지 없음"]), (1, 1, 1))
            r = vm.check(self.P, S)["by_signal"][0]
            self.assertAlmostEqual(r["filed_ratio"], len(F) / (len(F) + 1))   # 늦게 낸 700000 만 '안 냄'
        finally:
            shutil.rmtree(tmp)

    def test_numbers_at_the_signal_day(self):
        F, _ = self.S.features(self.month("2016-04"))
        T = D(2016, 4, 29)
        a = F["100000"]
        cap = CLOSE["100000"][T] * 1e7 + PREF[2]           # 우선주 시가총액 포함
        self.assertAlmostEqual(a["cap"] / cap, 1.0, places=9)
        self.assertAlmostEqual(a["pbr"], a["cap"] / 4e11)
        self.assertAlmostEqual(a["ey"], 4e10 / a["cap"])
        self.assertAlmostEqual(a["dy"], 500.0 / CLOSE["100000"][T], places=6)
        self.assertIsNone(F["800000"]["ey"])               # 적자
        self.assertFalse(a["revised_after"])
        F17, _ = self.S.features(self.month("2017-04"))
        self.assertTrue(F17["100000"]["revised_after"])    # FY2016 재무제표가 T 뒤 정정본
        self.assertEqual(F17["100000"]["graham"], sum(1 for c in vm.ms.graham(*self._rows("100000", D(2017, 4, 28)))
                                                      if c["ok"] is True))

    def _rows(self, sym, T):
        docs = {y: fin.statements(corp(sym), y, "k", self.dart, NOW) for y in range(2015, T.year)}
        divs = {y: fin.dividend_table(corp(sym), y, "k", self.dart, NOW) for y in range(2015, T.year)}
        rows = vm.ms.derive(fin.pit_rows(docs, divs, fin.annual_filings(corp(sym), "k", self.dart, NOW), T))
        return rows, self.S.cap(sym, T)

    def test_rules_pick_from_the_value_universe_and_rebalance_once_a_year(self):
        old = vm.MIN_V
        vm.MIN_V = 1
        try:
            st = vm.run_one(self.P, self.S, vm.find_rule("저PBR 30종목 연1회"), "2016-05", "2018-05", cost=inv.COST)
        finally:
            vm.MIN_V = old
        self.assertEqual((st["months"], st["rule"], st["dividends"]), (25, "저PBR 30종목 연1회", True))
        tg = self.S.targets()(self.P, self.month("2016-04"), vm.find_rule("저PBR 30종목 연1회"))
        self.assertEqual(sorted(tg), ["100000", "200000", "400000", "800000"])     # 30개보다 적으면 있는 만큼
        bt = inv.backtest(self.P, vm.find_rule("저PBR 30종목 연1회"), "2016-05", "2018-05", "A", inv.COST, 1.0,
                          self.S.targets(), self.S.income())
        turned = [x["m"] for x in bt if x["turnover"] > 0]
        self.assertEqual(turned[0], "2016-05")
        self.assertTrue(set(turned) <= {"2016-05", "2017-05", "2018-05"})

    def test_r4_features_and_cap_weights_without_low_fscores(self):
        i = self.month("2016-04")
        F, _ = self.S.features(i)
        for x in F.values():
            self.assertIn("fscore", x)
            self.assertIsNone(x["gpa"])                              # 합성 재무제표에는 매출총이익이 없다
        self.assertIsNotNone(F["100000"]["fscore"])                  # FY2015 보고서의 세 열로 세 해
        w = self.S.targets()(self.P, i, vm.BV_F)
        bv = inv.targets(self.P, i, {"fam": "cap", "reb": "Y"})
        self.assertEqual(w, vm.drop_low_f(bv, F, vm.MAX_F))
        self.assertAlmostEqual(sum(w.values()), 1.0)
        self.assertIn("300000", w)                                   # 은행은 V 밖이라 그대로 둔다

    def test_forward_lock_scores_only_finished_months_after_the_lock(self):
        old = vm.MIN_V
        vm.MIN_V = 1                                                 # 합성 세계의 V 는 작다 (잠글 때와 채점할 때 같게)
        tmp = tempfile.mkdtemp()
        try:
            judged = ["저PBR 30종목 연1회", vm.BV_F["label"]]
            now = datetime.datetime(2017, 5, 1, 10, 0)              # 5월 첫 평일, 장 마감 전
            doc, path = vm.forward_lock(judged, now, "t1", tmp, judge_months=12)
            self.assertEqual((doc["first_month"], len(doc["rules"]), doc["judged"]), ("2017-05", 13, judged))
            with self.assertRaises(vm.ValueLabError):
                vm.forward_lock(judged, now, "t1", tmp, judge_months=12)              # 덮어쓰지 않음
            for bad in (["없는 규칙"], [], judged * 2, [v["label"] for v in vm.tracked()][:7]):
                with self.assertRaises(vm.ValueLabError):
                    vm.forward_lock(bad, now, "t2", tmp)
            with self.assertRaises(vm.ValueLabError):
                vm.forward_lock(judged, now, "t2", tmp, judge_months=6)
            res = vm.forward_update(vm.read_lock(path), self.P, self.S)
            # 자료는 2018-06-29 에 끝난다: 6월은 다음 달 첫 거래일이 없어 아직 안 끝난 달
            self.assertEqual((res["months"][0], res["months"][-1], len(res["months"])), ("2017-05", "2018-05", 13))
            self.assertEqual([r["rule"] for r in res["rules"]][:2], judged)
            st = vm.run_one(self.P, self.S, vm.find_rule(judged[0]), "2017-05", "2018-05", "A", inv.COST)
            b = dict(st["bench_monthly"])
            self.assertEqual(res["rules"][0]["excess"], [r - b[m] for m, r in st["monthly"]])
            self.assertEqual(res["verdict"]["state"], "done")                         # 13 >= 12: 첫 12달로 판정
            self.assertIn("p_holm", res["rules"][0]["judge"])
            self.assertNotIn("judge", res["rules"][2])                               # 참고 규칙은 판정하지 않음
            text = "\n".join(vm.forward_report(res))
            for s in WORLD:
                self.assertNotIn(s, text)                                            # 종목 목록은 내지 않는다
            late, _ = vm.forward_lock(judged, datetime.datetime(2017, 5, 1, 16, 0), "t3", tmp)
            self.assertEqual(late["first_month"], "2017-06")                         # 장 마감 뒤면 다음 달부터
            self.assertEqual(vm.forward_update(vm.read_lock(vm.forward_paths(tmp, "t3")[0]), self.P, self.S)
                             ["verdict"]["state"], "before")                         # 12 / 36달
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
            d["judge_months"] = 6
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(d, fh)
            with self.assertRaises(vm.ValueLabError):
                vm.read_lock(path)                                                   # 잠근 파일을 고침
            vm.MAX_F = 3
            try:
                with self.assertRaises(vm.ValueLabError):
                    vm.read_lock(vm.forward_paths(tmp, "t3")[0])                     # 계산 상수가 바뀜
            finally:
                vm.MAX_F = 2
        finally:
            vm.MIN_V = old
            shutil.rmtree(tmp)

    def test_too_small_value_universe_stops_the_run(self):
        with self.assertRaises(vm.ValueLabError):
            vm.run_one(self.P, self.S, vm.find_rule("저PBR 30종목 연1회"), "2016-05", "2018-05")

    def test_first_month_must_follow_an_april_signal(self):
        with self.assertRaises(vm.ValueLabError):
            vm.run_one(self.P, self.S, vm.find_rule("저PBR 30종목 연1회"), "2016-06", "2018-05")

    def test_dividends_go_back_into_the_stock_on_the_last_trading_day_of_december(self):
        f = self.S.income()
        X = last_dec_day(2016)
        mult = f("100000", D(2016, 12, 1), D(2017, 1, 2))
        self.assertAlmostEqual(mult, 1 + 500.0 / CLOSE["100000"][X])
        self.assertEqual(f("100000", D(2017, 1, 2), D(2017, 2, 1)), 1.0)          # 12월이 없는 기간
        self.assertEqual(f("800000", D(2016, 12, 1), D(2017, 1, 2)), 1.0)         # 그 전에 상장폐지
        self.assertEqual(f("200000", D(2016, 12, 1), D(2017, 1, 2)), 1.0)         # 배당 없음

    def test_strict_drops_companies_whose_latest_report_was_corrected_after_the_signal(self):
        v = vm.find_rule("EWV 가치 대상 동일가중 연1회")
        i = self.month("2017-04")
        self.assertIn("100000", self.S.targets()(self.P, i, v))
        self.assertNotIn("100000", self.S.targets(strict=True)(self.P, i, v))

    def test_check_reports_late_filers_and_dividend_yields(self):
        old = (vm.MIN_V, vm.FILED_MIN)
        try:
            vm.MIN_V = 1
            res = vm.check(self.P, self.S)
            self.assertTrue(any("2016-04-29" in f and "접수된 비율" in f for f in res["fails"]))    # 4 / 5 = 80%
            vm.FILED_MIN = 0.5
            res = vm.check(self.P, self.S)
            self.assertEqual(res["fails"], [])
            self.assertLess(res["yield"]["median"], 1e-4)          # 회사가 적은 배당수익률과 같게 만든 자료
            self.assertEqual(res["gaps"]["fs"][0], 0)
        finally:
            vm.MIN_V, vm.FILED_MIN = old


class PointInTime(unittest.TestCase):
    def doc(self, no, y):
        rows = [{"sj_div": "BS", "account_id": "ifrs-full_Assets", "account_nm": "자산총계", "thstrm_amount": str(y * 10),
                 "frmtrm_amount": str((y - 1) * 10), "bfefrmtrm_amount": str((y - 2) * 10), "rcept_no": no}]
        return {"fs_div": "CFS", "rcept_no": no, "list": rows}

    def test_only_reports_filed_by_the_day_and_older_years_from_later_reports(self):
        docs = {2015: self.doc("20160330000001", 2015), 2016: self.doc("20170601000002", 2016), 2017: None}
        filings = {"2015.12": {"first": "20160330"}, "2016.12": {"first": "20170330"}}
        self.assertEqual(fin.pit_rows(docs, {}, filings, D(2016, 3, 1)), [])
        r = fin.pit_rows(docs, {}, filings, D(2016, 4, 29))
        self.assertEqual([(x["year"], x["assets"], x["src"]) for x in r],
                         [(2013, 20130.0, 2015), (2014, 20140.0, 2015), (2015, 20150.0, 2015)])
        r = fin.pit_rows(docs, {}, filings, D(2017, 4, 28))
        self.assertEqual([x["year"] for x in r], [2013, 2014, 2015, 2016])
        self.assertEqual((r[-1]["src"], r[-1]["revised_after"], r[-1]["filed"]), (2016, True, "20170330"))
        self.assertFalse(fin.pit_rows(docs, {}, filings, D(2017, 6, 30))[-1]["revised_after"])

    def test_missing_year_comes_from_the_next_report(self):
        docs = {2015: None, 2016: self.doc("20170330000001", 2016)}
        r = fin.pit_rows(docs, {}, {}, D(2017, 4, 28))               # 접수 기록이 없으면 접수번호 날짜
        self.assertEqual([(x["year"], x["src"]) for x in r], [(2014, 2016), (2015, 2016), (2016, 2016)])
        self.assertEqual(fin.pit_rows(docs, {}, {}, D(2017, 3, 29)), [])

    def test_dividends_follow_the_same_report_columns(self):
        docs = {2015: self.doc("20160330000001", 2015)}
        divs = {2015: {"thstrm": {"dps": 30.0, "total": 3.0}, "frmtrm": {"dps": 20.0, "total": 2.0},
                       "lwfr": {"dps": 10.0, "total": 1.0}}}
        r = fin.pit_rows(docs, divs, {}, D(2016, 4, 29))
        self.assertEqual([x["dps"] for x in r], [10.0, 20.0, 30.0])
        self.assertIsNone(fin.pit_rows(docs, {}, {}, D(2016, 4, 29))[0]["dps"])

    def test_slim_docs_give_the_same_rows(self):
        docs = {2015: self.doc("20160330000001", 2015), 2016: self.doc("20170330000001", 2016)}
        full = fin.pit_rows(docs, {}, {}, D(2017, 4, 28))
        small = fin.pit_rows({y: fin.slim(d) for y, d in docs.items()}, {}, {}, D(2017, 4, 28))
        self.assertEqual(full, small)


class Filings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_first_filing_date_ignores_corrections_and_the_cache_is_reused(self):
        fd = FakeDart()
        p = fin.annual_filings(corp("100000"), "k", self.tmp, NOW, fd)
        self.assertEqual(p["2016.12"], {"first": "20170330", "first_no": "20170330000001", "n": 2})
        self.assertEqual(p["2015.12"]["n"], 1)
        n = len(fd.calls)
        fin.annual_filings(corp("100000"), "k", self.tmp, NOW, fd)
        self.assertEqual(len(fd.calls), n)
        fin.annual_filings(corp("100000"), "k", self.tmp, NOW + datetime.timedelta(days=1), fd)
        self.assertEqual(len(fd.calls), n + 1)                   # 받은 날 뒤의 날짜가 필요하면 다시 받는다

    def test_company_and_three_dividend_columns(self):
        fd = FakeDart()
        self.assertEqual(fin.company(corp("400000"), "k", self.tmp, fd),
                         {"name": "합성홀딩스(주)", "induty_code": "64992", "acc_mt": "12"})
        t = fin.dividend_table(corp("100000"), 2017, "k", self.tmp, NOW, fd)
        self.assertEqual([t[c]["dps"] for c in fin.DIV_COLS], [500.0, 500.0, 500.0])
        self.assertAlmostEqual(t["thstrm"]["yield"], 500.0 / CLOSE["100000"][last_dec_day(2017)], places=5)
        self.assertEqual(fin.dividends(corp("100000"), 2017, "k", self.tmp, NOW, fd), {"dps": 500.0, "total": 1e9})

    def test_financial_holding_companies_are_told_apart_by_their_balance_sheet(self):
        self.assertTrue(vm.is_financial({"induty_code": "64110"}, True))
        self.assertTrue(vm.is_financial({"induty_code": "64992"}, False))
        self.assertFalse(vm.is_financial({"induty_code": "64992"}, True))
        self.assertFalse(vm.is_financial({"induty_code": "264"}, False))


class Picks(unittest.TestCase):
    def feats(self):
        base = {"pbr": None, "ey": None, "dy": None, "roe": None, "mom": None, "graham": 0, "g7": None, "buffett": 0,
                "b1": None, "lynch": 0, "peg": None, "revised_after": False, "cap": 1.0}
        F = {}
        for s, kw in (("A", {"pbr": 0.5, "ey": 0.10, "dy": 0.01, "roe": 0.05, "mom": -0.2, "graham": 6, "g7": 8.0}),
                      ("B", {"pbr": 0.8, "ey": 0.20, "dy": 0.05, "roe": 0.20, "mom": 0.3, "graham": 6, "g7": 5.0}),
                      ("C", {"pbr": 2.0, "ey": None, "dy": None, "roe": 0.15, "mom": 0.5, "graham": 7, "g7": None,
                             "buffett": 5, "b1": 0.2, "lynch": 3, "peg": 0.9}),
                      ("D", {"pbr": 0.5, "ey": 0.05, "dy": 0.03, "roe": None, "mom": 0.0, "buffett": 5, "b1": 0.3,
                             "lynch": 3, "peg": 0.5})):
            F[s] = dict(base, **kw)
        return F

    def test_each_rule_order_and_ties(self):
        F = self.feats()
        top = lambda fam, n: list(vm.pick(F, {"fam": fam, "n": n}))    # noqa: E731
        self.assertEqual(top("pbr", 2), ["A", "D"])                     # 같은 PBR 은 코드 순
        self.assertEqual(top("per", 1), ["B"])                          # 이익수익률 높은 = PER 낮은
        self.assertEqual(sorted(top("per", 9)), ["A", "B", "D"])        # 적자(None) 는 빠짐
        self.assertEqual(top("dy", 1), ["B"])
        self.assertEqual(top("pbr_roe", 1), ["B"])                      # PBR 2위 + ROE 1위 (D 는 ROE 없음)
        self.assertEqual(top("graham", 3), ["C", "B", "A"])             # 7개 먼저, 6개끼리는 G7 낮은 순
        self.assertEqual(top("buffett", 2), ["D", "C"])                 # 같으면 평균 ROE 높은 순
        self.assertEqual(top("lynch", 2), ["D", "C"])                   # 같으면 PEG 낮은 순
        w = vm.pick(F, {"fam": "ewv"})
        self.assertEqual(sorted(w.values()), [0.25] * 4)

    def test_sum_of_ranks_ties_go_by_code(self):
        F = self.feats()
        # pbr 순위: A1 D2 B3 C4 (A, D 는 같은 0.5 -> 코드 순), 모멘텀 순위: C1 B2 D3 A4
        r = {s: a + b for s, a, b in zip("ABCD", (1, 3, 4, 2), (4, 2, 1, 3))}
        self.assertEqual(r, {"A": 5, "B": 5, "C": 5, "D": 5})
        self.assertEqual(list(vm.pick(F, {"fam": "pbr_mom", "n": 4})), ["A", "B", "C", "D"])

    def test_eight_preregistered_rules_with_unique_names(self):
        R = vm.rules()
        self.assertEqual([v["fam"] for v in R], ["pbr", "per", "dy", "pbr_roe", "pbr_mom", "graham", "buffett", "lynch"])
        names = [v["label"] for v in R] + [vm.BENCH["label"], vm.EWV["label"]]
        self.assertEqual(len(set(names)), 10)
        for v in R:
            self.assertEqual((v["n"], v["reb"]), (30, "Y"))
            self.assertEqual(vm.find_rule(v["label"]), v)

    def test_cli_rules_and_no_current_holdings_in_the_source(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(vm.main(["rules"]), 0)
        self.assertIn("저PBR 30종목 연1회", out.getvalue())
        with open(vm.__file__, encoding="utf-8") as fh:
            src = fh.read()
        for bad in ("trader-kr-broker", "order-cash", "/api/v1/orders"):
            self.assertNotIn(bad, src)


class R4(unittest.TestCase):
    def rows(self):
        return [{"year": 2013, "assets": 100.0, "revenue": 80.0},
                {"year": 2014, "assets": 110.0, "ni": 5.0, "ocf": 8.0, "revenue": 90.0, "gross_profit": 27.0, "op": 9.0,
                 "current_assets": 50.0, "current_liabilities": 40.0, "noncurrent_liabilities": 30.0},
                {"year": 2015, "assets": 120.0, "ni": 9.0, "ocf": 12.0, "revenue": 110.0, "gross_profit": 36.0, "op": 12.0,
                 "current_assets": 60.0, "current_liabilities": 40.0, "noncurrent_liabilities": 30.0}]

    def test_fscore_eight_signals(self):
        rows = self.rows()
        # ROA 9/110 > 0, CFO > 0, ROA 0.082 > 5/100, CFO/기초자산 12/110 > ROA, 비유동부채/자산 0.25 < 0.273,
        # 유동비율 1.5 > 1.25, 매출총이익률 0.327 > 0.3, 매출/기초자산 1.0 > 0.9
        self.assertEqual(vm.fscore(rows), 8)
        rows[2]["ocf"] = -1.0
        self.assertEqual(vm.fscore(rows), 6)                       # 2, 4 를 잃음
        rows = self.rows()
        del rows[1]["gross_profit"]
        self.assertEqual(vm.fscore(rows), 8)                       # 매출총이익이 한 해 없으면 영업이익률 12/110 > 9/90
        rows[2]["op"] = 10.0
        self.assertEqual(vm.fscore(rows), 7)                       # 영업이익률 0.091 < 0.1
        rows = self.rows()
        del rows[2]["noncurrent_liabilities"]
        self.assertEqual(vm.fscore(rows), 7)                       # 숫자가 없는 신호는 0
        rows = self.rows()
        rows[0]["year"] = 2012
        self.assertIsNone(vm.fscore(rows))                         # 세 해가 이어져 있지 않음
        self.assertIsNone(vm.fscore(rows[1:]))
        self.assertAlmostEqual(vm.gpa(self.rows()), 36.0 / 120.0)
        self.assertIsNone(vm.gpa([{"year": 2015, "assets": 10.0}]))

    def test_r4_picks(self):
        F = {s: {"pbr": p, "fscore": f, "gpa": g} for s, p, f, g in (
            ("A", 0.5, 3, 0.10), ("B", 0.6, 7, 0.30), ("C", 0.7, 8, None), ("D", 1.0, 8, 0.40), ("E", 2.0, 8, 0.20),
            ("G", None, 8, 0.50))}
        top = lambda fam, n: list(vm.pick(F, {"fam": fam, "n": n}))    # noqa: E731
        # PBR 이 있는 5개의 앞 3분의1 (올림) = A, B. 그 안에서 F-score 높은 순. C 는 F 8 이어도 3분의1 밖
        self.assertEqual(top("f_pbr", 5), ["B", "A"])
        F["B"]["fscore"] = None
        self.assertEqual(top("f_pbr", 5), ["A"])                       # F-score 없으면 빠짐
        self.assertEqual(top("gpa", 9), ["G", "D", "B", "E", "A"])     # 매출총이익 없는 C 는 빠짐
        # PBR 순위 A1 B2 D3 E4, 매출총이익/자산 순위 D1 B2 E3 A4 (C, G 는 하나가 없음)
        self.assertEqual(top("gpa_pbr", 4), ["B", "D", "A", "E"])

    def test_cap_weights_without_low_fscores(self):
        F = {"A": {"fscore": 2}, "B": {"fscore": 5}, "C": {"fscore": None}}
        w = vm.drop_low_f({"A": 0.5, "B": 0.3, "C": 0.1, "X": 0.1}, F, 2)
        self.assertEqual(sorted(w), ["B", "C", "X"])                  # V 밖(X)과 F-score 없는 종목(C)은 둔다
        self.assertAlmostEqual(sum(w.values()), 1.0)
        self.assertAlmostEqual(w["B"], 0.6)

    def test_four_r4_rules(self):
        R = vm.rules_r4()
        self.assertEqual([v["fam"] for v in R], ["f_pbr", "gpa", "gpa_pbr", "bv_f"])
        self.assertEqual(R[3]["max_f"], 2)
        names = [v["label"] for v in vm.rules() + R] + [vm.BENCH["label"], vm.EWV["label"]]
        self.assertEqual(len(set(names)), 14)
        for v in R:
            self.assertEqual(vm.find_rule(v["label"]), v)
        self.assertEqual(len(vm.rules()), 8)                         # r3 의 8개는 그대로


class EngineHooks(unittest.TestCase):
    def test_yearly_rebalancing_month_and_label_names(self):
        self.assertEqual(inv.YEAR_MONTH, 4)
        self.assertEqual(inv.name({"fam": "x", "label": "이름표"}), "이름표")
        self.assertEqual(inv.name(inv.BENCH), "B1 시가총액가중 300")


if __name__ == "__main__":
    unittest.main()
