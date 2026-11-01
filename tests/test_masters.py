import contextlib
import datetime
import io
import json
import shutil
import tempfile
import unittest
import urllib.parse
import zipfile

from _load import load
from krcommon import financials as fin

ms = load("trader-kr-masters", "masters")
NOW = datetime.datetime(2026, 11, 1, 10, 0)
SYM, PREF, CORP, BANK = "123450", "123455", "00000001", "00000002"


def vals(y):
    """합성 회사: 매출 연 12% 성장, 영업이익률 해마다 0.1%p 오름, 자본 연 8% 성장."""
    k = y - 2015
    rev = 2e12 * 1.12 ** k
    eqp = 4e12 * 1.08 ** k
    eq = eqp * 1.02
    cl = 1e12 * 1.08 ** k
    liab = 0.5 * eq
    return {"Revenue": rev, "OperatingIncomeLoss": (0.15 + 0.001 * k) * rev, "ProfitLoss": 0.105 * rev,
            "ProfitLossAttributableToOwnersOfParent": 0.1 * rev, "EquityAttributableToOwnersOfParent": eqp,
            "Equity": eq, "Assets": eq + liab, "Liabilities": liab, "CurrentAssets": 3e12 * 1.08 ** k,
            "CurrentLiabilities": cl, "NoncurrentLiabilities": liab - cl, "Inventories": 0.2e12 * 1.05 ** k,
            "CashAndCashEquivalents": 0.5e12, "CashFlowsFromUsedInOperatingActivities": 0.15 * rev,
            "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities": -0.05 * rev,
            "BasicEarningsLossPerShare": 0.1 * rev / 1e8}


SJ = {"Revenue": "IS", "OperatingIncomeLoss": "IS", "ProfitLoss": "IS", "ProfitLossAttributableToOwnersOfParent": "IS",
      "BasicEarningsLossPerShare": "IS", "CashFlowsFromUsedInOperatingActivities": "CF",
      "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities": "CF"}


FILINGS = [{"rcept_no": "20260915000001", "rcept_dt": "20260915", "report_nm": "기타경영사항(자율공시)"},
           {"rcept_no": "20261029000002", "rcept_dt": "20261029", "report_nm": "연결재무제표기준영업(잠정)실적(공정공시)"}]


class FakeDart(object):
    def __init__(self, missing=(2019,)):
        self.missing, self.calls = set(missing), []

    def __call__(self, url):
        u = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        self.calls.append((u.path.rsplit("/", 1)[-1], q.get("corp_code"), q.get("bsns_year"), q.get("fs_div")))
        if u.path.endswith("corpCode.xml"):
            xml = ("<result><list><corp_code>%s</corp_code><corp_name>합성전자</corp_name><stock_code>%s</stock_code></list>"
                   "<list><corp_code>%s</corp_code><corp_name>합성은행</corp_name><stock_code>777770</stock_code></list>"
                   "<list><corp_code>00000009</corp_code><corp_name>비상장</corp_name><stock_code> </stock_code></list>"
                   "</result>") % (CORP, SYM, BANK)
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("CORPCODE.xml", xml)
            return buf.getvalue()
        if u.path.endswith("list.json"):                      # 회사 공시 목록 (논지 점검)
            lo, hi = q["bgn_de"], q["end_de"]
            got = [x for x in FILINGS if lo <= x["rcept_dt"] <= hi and q.get("corp_code") == CORP]
            return json.dumps({"status": "000" if got else "013", "total_page": 1, "list": got}).encode()
        y = int(q["bsns_year"])
        if u.path.endswith("alotMatter.json"):
            if q["corp_code"] != CORP or y < 2015:
                return json.dumps({"status": "013"}).encode()
            return json.dumps({"status": "000", "list": [
                {"se": "주당 현금배당금(원)", "stock_knd": "보통주", "thstrm": "1,000"},
                {"se": "주당 현금배당금(원)", "stock_knd": "우선주", "thstrm": "1,050"},
                {"se": "현금배당금총액(백만원)", "thstrm": "{:,.0f}".format(0.02 * vals(y)["Revenue"] / 1e6)}]}).encode()
        if q["corp_code"] == BANK:
            if q["fs_div"] == "CFS" or y < 2023 or y > 2025:
                return json.dumps({"status": "013"}).encode()
            rows = [{"sj_div": "BS", "account_id": "ifrs-full_Equity", "account_nm": "자본총계", "thstrm_amount": "1000",
                     "frmtrm_amount": "900", "rcept_no": "%d0320000001" % (y + 1)},
                    {"sj_div": "IS", "account_id": "ifrs-full_ProfitLoss", "account_nm": "당기순이익", "thstrm_amount": "80",
                     "frmtrm_amount": "70", "rcept_no": "%d0320000001" % (y + 1)}]
            return json.dumps({"status": "000", "list": rows}).encode()
        if y in self.missing or y < 2015 or y > 2025 or q["fs_div"] != "CFS":
            return json.dumps({"status": "013", "message": "조회된 데이타가 없습니다."}).encode()
        pre = "ifrs_" if y <= 2018 else "ifrs-full_"
        rows = []
        for k, v in vals(y).items():
            aid = ("dart_" if k == "OperatingIncomeLoss" else pre) + k
            rows.append({"sj_div": SJ.get(k, "BS"), "account_id": aid, "account_nm": k, "thstrm_amount": "%.0f" % v,
                         "frmtrm_amount": "%.0f" % vals(y - 1)[k], "rcept_no": "%d0310000123" % (y + 1)})
        return json.dumps({"status": "000", "list": rows}).encode()


class FakeKrx(object):
    def __call__(self, url, headers):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        d = datetime.datetime.strptime(q["basDd"], "%Y%m%d").date()
        if d.weekday() >= 5 or "ksq" in url:
            return 200, json.dumps({"OutBlock_1": []})
        px = 50000 if d.year >= 2026 else 20000
        return 200, json.dumps({"OutBlock_1": [
            {"ISU_CD": SYM, "ISU_NM": "합성전자", "TDD_CLSPRC": str(px), "MKTCAP": str(px * 100000000),
             "LIST_SHRS": "100000000", "CMPPREVDD_PRC": "0"},
            {"ISU_CD": PREF, "ISU_NM": "합성전자우", "TDD_CLSPRC": "10000", "MKTCAP": "100000000000"},
            {"ISU_CD": "999990", "ISU_NM": "남", "TDD_CLSPRC": "1", "MKTCAP": "999999999999999"}]})


class Financials(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def hist(self, fake=None, years=10):
        return fin.history(SYM, years, key="k", cache=self.tmp, now_kst=NOW, transport=fake or FakeDart())

    def test_old_and_new_account_ids_and_capex_sign(self):
        info, rows = self.hist()
        self.assertEqual(info["name"], "합성전자")
        self.assertEqual([r["year"] for r in rows], list(range(2016, 2026)))
        r16 = rows[0]                                             # 2016 보고서는 ifrs_ 접두어
        self.assertAlmostEqual(r16["revenue"], round(vals(2016)["Revenue"]))
        self.assertAlmostEqual(r16["op"], round(vals(2016)["OperatingIncomeLoss"]))
        self.assertGreater(r16["capex"], 0)                         # 음수로 와도 크기로
        self.assertEqual(r16["filed"], "20170310")
        self.assertEqual((r16["dps"], r16["fs_div"]), (1000.0, "CFS"))

    def test_missing_year_comes_from_next_years_prior_column(self):
        _, rows = self.hist()
        r19 = [r for r in rows if r["year"] == 2019][0]
        self.assertAlmostEqual(r19["ni_parent"], round(vals(2019)["ProfitLossAttributableToOwnersOfParent"]))
        self.assertEqual(r19["filed"], "20210310")                  # 2020 보고서에서 가져옴

    def test_cache_means_no_second_call(self):
        fake = FakeDart()
        self.hist(fake)
        n = len(fake.calls)
        self.hist(fake)
        self.assertEqual(len(fake.calls), n)

    def test_separate_statements_when_there_is_no_consolidated_one(self):
        _, rows = fin.history("777770", 10, key="k", cache=self.tmp, now_kst=NOW, transport=FakeDart())
        self.assertEqual([r["year"] for r in rows], [2022, 2023, 2024, 2025])
        self.assertEqual(rows[-1]["fs_div"], "OFS")
        self.assertEqual(rows[-1]["ni_parent"], 80.0)                # 지배주주 몫이 없으면 당기순이익
        self.assertIsNone(rows[-1]["current_assets"])

    def test_no_key_and_unknown_symbol(self):
        with self.assertRaises(fin.FinError):
            fin.history(SYM, key="", cache=self.tmp, now_kst=NOW, transport=FakeDart())
        with self.assertRaises(fin.FinError):
            fin.history("000020", key="k", cache=self.tmp, now_kst=NOW, transport=FakeDart())

    def test_find_by_name_puts_the_exact_name_first(self):
        hits = ms.find("합성", key="k", cache=self.tmp, now_kst=NOW, transport=FakeDart())
        self.assertEqual(hits, [(SYM, "합성전자"), ("777770", "합성은행")])          # 같은 길이면 코드 순
        self.assertEqual(ms.find("합성은행", key="k", cache=self.tmp, now_kst=NOW, transport=FakeDart())[0][0], "777770")
        self.assertEqual(ms.find("합성 전자", key="k", cache=self.tmp, now_kst=NOW, transport=FakeDart()),
                         [(SYM, "합성전자")])
        self.assertEqual(ms.find("없는회사", key="k", cache=self.tmp, now_kst=NOW, transport=FakeDart()), [])

    def test_market_cap_adds_preferred_shares(self):
        m = fin.market(SYM, key="k", now_kst=NOW, transport=FakeKrx(), cache_dir=self.tmp)
        self.assertEqual(m["date"], "2026-10-30")
        self.assertEqual((m["mktcap"], m["mktcap_common"], m["price"]), (5.1e12, 5e12, 50000.0))


class Checklists(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.doc = ms.check(SYM, 10, NOW, None, FakeDart(), FakeKrx(), "k", "k", cls.tmp, cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def by(self, k):
        return {c["id"]: c["ok"] for c in self.doc[k]}

    def test_graham_all_eight(self):
        self.assertEqual(self.by("graham"), {"G%d" % i: True for i in range(1, 9)})
        self.assertEqual(self.doc["graham_summary"], {"total": 8, "met": 8, "not_met": 0, "no_data": 0})

    def test_buffett_roe_and_book_value_fall_short(self):
        self.assertEqual(self.by("buffett"), {"B1": False, "B2": False, "B3": True, "B4": True, "B5": True, "B6": False,
                                              "B7": True})
        b7 = [c for c in self.doc["buffett"] if c["id"] == "B7"][0]
        self.assertIn("2021-04-01", b7["value"])

    def test_lynch_stalwart_with_cheap_growth(self):
        self.assertTrue(self.doc["lynch_kind"].startswith("우량 성장 (순이익 5년 연 12.0%)"))
        self.assertEqual(self.by("lynch"), {"L%d" % i: True for i in range(1, 7)})

    def test_no_signal_words_anywhere(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ms.print_doc(self.doc)
        out = buf.getvalue()
        self.assertIn("매수·매도 신호가 아니고 투자 권유가 아닙니다", out)
        text = out + json.dumps(self.doc, ensure_ascii=False)
        for bad in ("bullish", "bearish", "강세", "약세", "매수 추천", "목표가", "signal"):
            self.assertNotIn(bad, text)

    def test_turnaround_and_cyclical_kinds(self):
        rows = [{"year": 2016 + i, "ni_parent": x} for i, x in enumerate([5, 6, -1, -2, 3])]
        self.assertTrue(ms.lynch_kind(rows)[0].startswith("회생"))
        rows = [{"year": 2016 + i, "ni_parent": x} for i, x in enumerate([5, -1, 4, -2, 3, 4])]
        self.assertTrue(ms.lynch_kind(rows)[0].startswith("경기순환"))


class Thesis(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dir = self.tmp + "/thesis"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_cli(self, *args):
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = ms.main(list(args) + ["--dir", self.dir])
        return rc, buf.getvalue() + err.getvalue()

    def new(self):
        return self.run_cli("thesis", "new", "--symbol", SYM, "--now", "2026-09-01 10:00", "--statement", "이익이 꾸준히 큰다",
                            "--pillar", "매출이 큼 | 매출성장 >= 10%", "--pillar", "이익률 | 영업이익률 >= 30%",
                            "--risk", "빚 | 부채비율 > 100%", "--risk", "비쌈 | PER > 8", "--risk", "경쟁",
                            "--catalyst", "2026-10-29 | 3분기 실적", "--catalyst", "2027-01-30 | 4분기 실적",
                            "--target", "사용자 목표", "--exit", "사용자 손절")

    def check(self, now, save=True):
        return ms.thesis_check(SYM, now, self.dir, FakeDart(), FakeKrx(), "k", "k", self.tmp, self.tmp, save)

    def test_new_needs_a_falsifiable_risk_and_never_overwrites(self):
        rc, out = self.run_cli("thesis", "new", "--symbol", SYM, "--statement", "좋다", "--pillar", "좋음")
        self.assertEqual(rc, 2)
        self.assertIn("반증할 수 없는", out)
        self.assertEqual(self.new()[0], 0)
        rc, out = self.new()
        self.assertEqual(rc, 2)
        self.assertIn("덮어쓰지 않습니다", out)

    def test_conditions_parse_and_reject_bad_units(self):
        self.assertEqual(ms.parse_cond("매출성장 >= 5%"), {"metric": "매출성장", "op": ">=", "value": 0.05})
        self.assertEqual(ms.parse_cond("잉여현금 > 10조")["value"], 1e13)
        self.assertEqual(ms.parse_cond("PER <= 15")["value"], 15.0)
        for bad in ("매출성장 >= 5", "PER <= 15%", "모름 > 1", "PBR 1.5"):
            with self.assertRaises(ms.MastersError):
                ms.parse_cond(bad)

    def test_check_scores_numbers_filings_and_catalysts(self):
        self.new()
        res = self.check(ms.krx.parse_now("2026-11-01 10:00"))
        card = {c["id"]: c for c in res["card"]}
        self.assertEqual((card["P1"]["ok"], card["P2"]["ok"]), (True, False))      # 매출 12% 성장, 영업이익률 16%
        self.assertEqual((card["R1"]["ok"], card["R2"]["ok"], card["R3"]["ok"]), (False, True, None))  # PER 약 8.2
        self.assertEqual([x["rcept_dt"] for x in res["filings"]], ["20260915", "20261029"])
        self.assertEqual([c["id"] for c in res["catalysts_due"]], ["C1"])
        self.assertEqual([c["id"] for c in res["catalysts_next"]], ["C2"])
        doc = ms.load_thesis(SYM, self.dir)
        self.assertEqual(doc["name"], "합성전자")
        self.assertEqual(doc["checks"][-1]["filings_until"], "2026-11-01")

    def test_second_check_shows_only_new_filings_and_a_trend(self):
        self.new()
        self.check(ms.krx.parse_now("2026-10-01 10:00"))
        res = self.check(ms.krx.parse_now("2026-11-01 10:00"))
        self.assertEqual([x["rcept_dt"] for x in res["filings"]], ["20261029"])
        trends = {c["id"]: c["trend"] for c in res["card"]}
        self.assertEqual((trends["P1"], trends["R2"], trends["R3"]), ("같음", "같음", ""))   # 같은 숫자, 조건 없는 위험

    def test_add_log_and_catalyst_result_then_show_offline(self):
        self.new()
        rc, _ = self.run_cli("thesis", "add", "--symbol", SYM, "--now", "2026-09-10 10:00", "--data", "새 공장 발표",
                             "--impact", "P1:강화", "--impact", "R3:약화", "--action", "그대로 둠", "--conviction", "보통",
                             "--pillar", "배당 | 배당수익률 >= 1%")
        self.assertEqual(rc, 0)
        self.assertEqual(self.run_cli("thesis", "add", "--symbol", SYM, "--done", "C1:예상 웃돎")[0], 0)
        self.assertEqual(self.run_cli("thesis", "add", "--symbol", SYM, "--data", "x", "--impact", "P9:강화")[0], 2)
        doc = ms.load_thesis(SYM, self.dir)
        self.assertEqual([p["id"] for p in doc["pillars"]], ["P1", "P2", "P3"])
        self.assertEqual(doc["log"][0]["impact"], [{"id": "P1", "dir": "강화"}, {"id": "R3", "dir": "약화"}])
        self.assertEqual(doc["catalysts"][0]["done"]["result"], "예상 웃돎")
        rc, out = self.run_cli("thesis", "show", "--symbol", SYM, "--now", "2027-01-15 10:00")
        self.assertEqual(rc, 0)
        self.assertIn("점검할 때입니다", out)                      # 2026-09-10 뒤로 92일 넘게 안 봄
        self.assertIn("조치: 그대로 둠", out)

    def test_check_output_suggests_no_action(self):
        self.new()
        res = self.check(ms.krx.parse_now("2026-11-01 10:00"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ms.print_check(res)
        out = buf.getvalue()
        self.assertIn("조치를 제안하지 않고", out)
        self.assertIn("사용자가 적은 목표: 사용자 목표 / 손절 조건: 사용자 손절", out)
        for bad in ("매수하세요", "매도하세요", "파세요", "사세요", "줄이세요", "늘리세요", "bullish", "bearish"):
            self.assertNotIn(bad, out)


if __name__ == "__main__":
    unittest.main()
