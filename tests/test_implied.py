import contextlib
import datetime
import io
import json
import shutil
import tempfile
import unittest
import urllib.parse

from _load import load

import test_masters as tm

im = load("trader-kr-masters", "implied")
NOW = tm.NOW
D = datetime.date


def cap_on(d):
    return (50000 if d.year >= 2026 else 20000) * 1e8 + 1e11      # tm.FakeKrx: 보통주 + 우선주


class BankKrx(tm.FakeKrx):
    def __call__(self, url, headers):
        code, body = tm.FakeKrx.__call__(self, url, headers)
        doc = json.loads(body)
        if doc["OutBlock_1"]:
            doc["OutBlock_1"].append({"ISU_CD": "777770", "ISU_NM": "합성은행", "TDD_CLSPRC": "5000",
                                      "MKTCAP": "500", "LIST_SHRS": "1"})
        return code, json.dumps(doc)


class FiledDart(tm.FakeDart):
    """사업보고서 접수 기록이 있는 판: FY y 는 y+1 년 3월 20일에 처음 냈다 (재무제표 접수번호는 3월 10일)."""
    def __call__(self, url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        if url.split("?")[0].endswith("list.json") and q.get("pblntf_detail_ty") == "A001":
            rows = [{"rcept_dt": "%d0320" % (y + 1), "rcept_no": "%d0320000001" % (y + 1),
                     "report_nm": "사업보고서 (%d.12)" % y} for y in range(2015, 2026)]
            return json.dumps({"status": "000", "total_page": 1, "list": rows}).encode()
        return tm.FakeDart.__call__(self, url)


class Formulas(unittest.TestCase):
    def test_dcf_matches_gordon_when_growth_is_flat(self):
        v = im.dcf_value(100.0, 0.02, 0.08, 10, 0.02)
        self.assertAlmostEqual(v, 100.0 * 1.02 / (0.08 - 0.02), places=6)

    def test_reverse_dcf_recovers_the_growth(self):
        cap = im.dcf_value(50.0, 0.07, 0.09, 10, 0.02)
        g, flag = im.implied_growth(cap, 50.0, 0.09, 10, 0.02)
        self.assertEqual(flag, "")
        self.assertAlmostEqual(g, 0.07, places=9)
        self.assertEqual(im.implied_growth(1.0, 50.0, 0.09), (None, "below"))
        self.assertEqual(im.implied_growth(1e15, 50.0, 0.09), (None, "above"))

    def test_required_roe_explains_the_pbr(self):
        r = 0.08
        roe = im.roe_all_paid(1.5, r)
        self.assertAlmostEqual(1 + (roe - r) / r, 1.5)
        roe2 = im.roe_retained(1.5, r, 0.6)
        g = roe2 * 0.6
        self.assertAlmostEqual((roe2 - g) / (r - g), 1.5)
        self.assertAlmostEqual(im.roe_retained(1.5, r, 0.0), roe)
        self.assertAlmostEqual(im.roe_all_paid(0.4, r), 0.032)    # 장부가 아래면 ROE 가 할인율보다 낮다

    def test_quantiles_and_months(self):
        pts = [{"x": v} for v in (5.0, 1.0, 3.0, 2.0, 4.0)] + [{"x": None}]
        s = im.band_stats(pts, "x", 3.5)
        self.assertEqual((s["n"], s["skipped"], s["min"], s["p25"], s["median"], s["p75"], s["max"]),
                         (5, 1, 1.0, 2.0, 3.0, 4.0, 5.0))
        self.assertAlmostEqual(s["below"], 0.6)
        self.assertEqual(im.months_between(D(2025, 11, 20), D(2026, 2, 3)), [(2025, 11), (2025, 12), (2026, 1)])
        self.assertEqual((im.month_end(2024, 2), im.month_end(2025, 12)), (D(2024, 2, 29), D(2025, 12, 31)))

    def test_known_numbers_wait_for_the_report_that_gave_them(self):
        rows = [{"year": 2019, "src": 2020, "filed": "20210310"}, {"year": 2018, "src": 2018, "filed": "20190310"}]
        filings = {"2019.12": {"first": "20200320"}, "2020.12": {"first": "20210318"}}
        self.assertEqual(im.first_filed(filings, rows[0]), "20210318")    # FY2019 숫자는 FY2020 보고서에서 왔다
        self.assertEqual(im.first_filed({}, rows[1]), "20190310")         # 접수 기록이 없으면 접수번호 날짜
        self.assertEqual(im.known_at(rows, filings, D(2020, 12, 31))["year"], 2018)
        self.assertEqual(im.known_at(rows, filings, D(2021, 3, 18))["year"], 2019)
        self.assertIsNone(im.known_at(rows, filings, D(2019, 3, 9)))


class Analyze(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_it(self, dart=None, krx_t=None, sym=tm.SYM, band=True):
        return im.analyze(sym, NOW, band=band, dart_transport=dart or tm.FakeDart(), krx_transport=krx_t or tm.FakeKrx(),
                          dart_key="k", krx_key="k", dart_cache=self.tmp + "/dart", krx_cache=self.tmp + "/krx",
                          workers=2)

    def test_band_reverse_dcf_and_required_roe(self):
        doc = self.run_it()
        rev = lambda y: tm.vals(y)["Revenue"]
        cap = cap_on(D(2026, 10, 30))
        self.assertEqual((doc["market"]["date"], doc["report"]["year"]), ("2026-10-30", 2025))
        self.assertAlmostEqual(doc["now"]["per"], cap / (0.1 * rev(2025)))
        self.assertAlmostEqual(doc["now"]["pbr"], cap / tm.vals(2025)["EquityAttributableToOwnersOfParent"])
        self.assertAlmostEqual(doc["now"]["dy"] / (0.02 * rev(2025) / cap), 1.0, places=5)   # 배당총액은 백만원 단위
        pts = {p["date"]: p for p in doc["points"]}
        self.assertEqual(doc["points"][0]["date"], "2016-03-31")         # FY2015 가 2016-03-10 에 나온 달부터
        self.assertEqual(doc["points"][-1]["date"], "2026-09-30")         # 지금 달은 '지금' 값으로만
        self.assertEqual(len(doc["points"]), 127)
        self.assertEqual(pts["2016-03-31"]["fy"], 2015)
        self.assertAlmostEqual(pts["2016-03-31"]["per"], cap_on(D(2016, 3, 31)) / (0.1 * rev(2015)))
        # FY2019 재무제표가 없어 FY2020 보고서의 전기 값을 쓴다: 그 보고서가 나온 2021-03 전에는 FY2018
        self.assertEqual((pts["2020-03-31"]["fy"], pts["2020-12-31"]["fy"], pts["2021-03-31"]["fy"]), (2018, 2018, 2020))
        self.assertEqual(pts["2025-12-31"]["date"], "2025-12-31")
        self.assertIn("2025-11-28", pts)                                  # 11월 말일이 일요일이면 앞 거래일
        d = doc["dcf"]
        self.assertEqual(d["base_years"], [2023, 2024, 2025])
        self.assertAlmostEqual(d["base"] / (sum(0.1 * rev(y) for y in (2023, 2024, 2025)) / 3), 1.0, places=9)
        for row in d["grid"]:
            self.assertEqual(row["flag"], "")
            self.assertAlmostEqual(im.dcf_value(d["base"], row["g"], row["r"], 10, 0.02) / cap, 1.0, places=9)
        self.assertEqual([x["r"] for x in d["grid"]], [0.06, 0.08, 0.10])
        rim = doc["rim"]
        self.assertAlmostEqual(rim["keep"], 0.8, places=5)               # 배당 2% / 순이익 10% (매출 대비)
        for row in rim["grid"]:
            self.assertAlmostEqual(row["all_paid"], row["r"] * doc["now"]["pbr"])
            g = row["retained"] * rim["keep"]
            self.assertAlmostEqual((row["retained"] - g) / (row["r"] - g), doc["now"]["pbr"])
        h = doc["history"]
        self.assertEqual((h["from"], h["to"]), (2015, 2025))
        self.assertAlmostEqual(h["rev_cagr"], 0.12)
        self.assertAlmostEqual(h["fcf_cagr"], 0.12)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            im.print_doc(doc)
        text = out.getvalue()
        self.assertIn("가격이 깔고 있는 가정", text)
        self.assertIn("월말 127달", text)
        for bad in ("저평가", "고평가", "추천", "기회", "사라", "팔라", "목표가", "적정"):
            self.assertNotIn(bad, text, bad)
        self.assertIn("투자 권유가 아닙니다", text)

    def test_first_filing_date_sets_when_a_year_counts(self):
        doc = self.run_it(dart=FiledDart())
        pts = {p["date"]: p for p in doc["points"]}
        self.assertEqual(doc["points"][0]["date"], "2016-03-31")
        self.assertEqual(doc["report"]["filed"], "20260320")
        # FY2019 숫자는 FY2020 사업보고서(2021-03-20 첫 접수)에서 왔다
        self.assertEqual((pts["2021-02-26"]["fy"], pts["2021-03-31"]["fy"]), (2018, 2020))

    def test_no_band_still_gives_the_now_values(self):
        doc = self.run_it(band=False)
        self.assertEqual(doc["points"], [])
        self.assertEqual(doc["band"]["per"]["n"], 0)
        self.assertIsNotNone(doc["now"]["per"])
        self.assertEqual(len(doc["dcf"]["grid"]), 3)

    def test_bank_has_no_reverse_dcf(self):
        doc = self.run_it(krx_t=BankKrx(), sym="777770", band=False)
        self.assertIn("금융업", doc["dcf"]["skip"])
        self.assertEqual(doc["dcf"]["grid"], [])
        self.assertAlmostEqual(doc["now"]["pbr"], (500 + 0.0) / 1000)     # 시가총액 500 / 자본 1000
        self.assertIsNone(doc["rim"]["keep"])                             # 배당 자료 없음
        self.assertTrue(all(row["retained"] is None for row in doc["rim"]["grid"]))

    def test_bad_assumptions_stop(self):
        with self.assertRaises(im.ImpliedError):
            im.analyze(tm.SYM, NOW, rs=(0.02,), terminal=0.02, dart_transport=tm.FakeDart(), dart_key="k")
        with self.assertRaises(im.ImpliedError):
            im.analyze(tm.SYM, NOW, horizon=0, dart_transport=tm.FakeDart(), dart_key="k")


if __name__ == "__main__":
    unittest.main()
