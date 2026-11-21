import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from urllib.parse import parse_qs, urlparse

from _load import load

earn = load("trader-kr-earnings", "earnings")
NOW = datetime.datetime(2026, 11, 21, 9, 0)
CODES = {"005930": {"corp_code": "00000001", "name": "가상전자"}}


def line(acc, rev, op, ni, nip, eps, gp=None):
    """한 보고서의 손익 줄. 값은 (thstrm, thstrm_add, frmtrm_q, frmtrm_add, frmtrm) 묶음."""
    rows = []
    for aid, vals in (("ifrs-full_Revenue", rev), ("dart_OperatingIncomeLoss", op), ("ifrs-full_ProfitLoss", ni),
                      ("ifrs-full_ProfitLossAttributableToOwnersOfParent", nip),
                      ("ifrs-full_BasicEarningsLossPerShare", eps), ("ifrs-full_GrossProfit", gp)):
        if vals is None:
            continue
        r = {"sj_div": "IS", "account_id": aid, "account_nm": aid}
        for col, v in zip(("thstrm_amount", "thstrm_add_amount", "frmtrm_q_amount", "frmtrm_add_amount",
                           "frmtrm_amount"), vals):
            r[col] = "" if v is None else str(v)
        rows.append(r)
    return rows


# 2025 1Q ~ 3Q, 2025 사업보고서, 2026 1Q ~ 3Q. (당기 3개월, 당기 누적, 전년 같은 분기, 전년 누적, 전년 연간)
Q = lambda cur, add, pq, padd: (cur, add, pq, padd, None)        # noqa: E731
A = lambda year, prev: (year, None, None, None, prev)            # noqa: E731
REPORTS = {
    (2025, "11013"): ("20250515000001", line(0, Q(100, 100, 90, 90), Q(10, 10, 9, 9), Q(8, 8, 7, 7), Q(8, 8, 7, 7),
                                              Q(80, 80, 70, 70), Q(40, 40, 36, 36))),
    (2025, "11012"): ("20250814000001", line(0, Q(110, 210, 100, 190), Q(11, 21, 10, 19), Q(9, 17, 8, 15),
                                              Q(9, 17, 8, 15), Q(90, 170, 80, 150), Q(44, 84, 40, 76))),
    (2025, "11014"): ("20251114000001", line(0, Q(120, 330, 105, 295), Q(12, 33, 11, 30), Q(10, 27, 9, 24),
                                              Q(10, 27, 9, 24), Q(100, 270, 90, 240), Q(48, 132, 42, 118))),
    (2025, "11011"): ("20260310000001", line(0, A(460, 400), A(45, 40), A(37, 33), A(37, 33), A(370, 330),
                                              A(180, 160))),
    (2026, "11013"): ("20260515000001", line(0, Q(140, 140, 100, 100), Q(-5, -5, 10, 10), Q(-6, -6, 8, 8),
                                              Q(-6, -6, 8, 8), Q(-50, -50, 80, 80), Q(50, 50, 40, 40))),
    (2026, "11012"): ("20260814000001", line(0, Q(150, 290, 110, 210), Q(15, 10, 11, 21), Q(30, 24, 9, 17),
                                              Q(30, 24, 9, 17), Q(250, 200, 90, 170), Q(60, 110, 44, 84))),
    (2026, "11014"): ("20261116000001", line(0, Q(160, 450, 120, 330), Q(16, 26, 12, 33), Q(12, 36, 10, 27),
                                              Q(12, 36, 10, 27), Q(100, 300, 100, 270), Q(64, 174, 48, 132))),
}


class FakeDart(object):
    def __init__(self, reports=None, filings=None):
        self.reports = REPORTS if reports is None else reports
        self.filings = filings or []
        self.calls = []

    def __call__(self, url):
        u = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        self.calls.append((u.path.rsplit("/", 1)[-1], q.get("bsns_year"), q.get("reprt_code"), q.get("fs_div")))
        if u.path.endswith("company.json"):
            return json.dumps({"status": "000", "corp_name": "가상전자", "induty_code": "264", "acc_mt": "12"})
        if u.path.endswith("fnlttSinglAcntAll.json"):
            got = self.reports.get((int(q["bsns_year"]), q["reprt_code"]))
            if not got or q["fs_div"] != "CFS":
                return json.dumps({"status": "013", "message": "조회된 데이타가 없습니다."})
            rc, rows = got
            return json.dumps({"status": "000", "list": [dict(r, rcept_no=rc) for r in rows]})
        if u.path.endswith("list.json"):
            rows = [r for r in self.filings if q["bgn_de"] <= r["rcept_dt"] <= q["end_de"]]
            if not rows:
                return json.dumps({"status": "013"})
            return json.dumps({"status": "000", "total_page": 1, "list": rows})
        raise AssertionError(url)


class Calc(unittest.TestCase):
    def test_growth_words(self):
        self.assertEqual(earn.growth(110, 100), (0.1, None))
        self.assertEqual(earn.growth(-5, 10), (None, "적자 전환"))
        self.assertEqual(earn.growth(5, -10), (None, "흑자 전환"))
        self.assertEqual(earn.growth(-5, -10), (None, "적자 지속"))
        self.assertEqual(earn.growth(None, 10), (None, None))

    def test_deadline_rolls_past_weekend(self):
        q3 = {"year": 2026, "q": 2}
        nx = earn.next_deadline(q3, "12")
        self.assertEqual((nx["report"], nx["due"]), ("3분기보고서", "2026-11-16"))   # 11-14 토 -> 월
        nx = earn.next_deadline({"year": 2026, "q": 3}, "12")
        self.assertEqual((nx["report"], nx["due"], nx["days"]), ("사업보고서", "2027-03-31", 90))
        self.assertEqual(earn.next_deadline({"year": 2026, "q": 4}, "12")["due"], "2027-05-17")   # 5-15 토
        self.assertIsNone(earn.next_deadline({"year": 2026, "q": 3}, "03"))

    def test_provisional_after_report(self):
        rows = [{"rcept_no": "20261120000009", "rcept_dt": "20261120", "report_nm": "연결재무제표기준영업(잠정)실적(공정공시)"},
                {"rcept_no": "20261110000009", "rcept_dt": "20261110", "report_nm": "영업(잠정)실적(공정공시)"},
                {"rcept_no": "20261121000001", "rcept_dt": "20261121", "report_nm": "매출액또는손익구조30%(대규모법인은15%)이상변경"},
                {"rcept_no": "20261121000002", "rcept_dt": "20261121", "report_nm": "기업설명회(IR)개최"}]
        got = earn.provisional(rows, "2026-11-16")
        self.assertEqual([g["rcept_no"] for g in got], ["20261121000001", "20261120000009"])


class Run(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cache = os.path.join(self.d, "fsq")
        os.environ["DART_API_KEY"] = "x" * 40

    def tearDown(self):
        os.environ.pop("DART_API_KEY", None)
        shutil.rmtree(self.d, ignore_errors=True)

    def run_e(self, *args, fake=None):
        fake = fake or FakeDart()
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = earn.main(list(args) + ["--now", "2026-11-21 09:00", "--cache", self.cache], transport=fake,
                             list_transport=fake, codes=CODES, sleep=None)
        return code, out.getvalue(), err.getvalue(), fake

    def test_quarters_q4_ttm(self):
        code, _, _, _ = self.run_e("005930", "--json")
        self.assertEqual(code, 0)
        code, out, _, fake = self.run_e("005930", "--json", "--quarters", "7")
        r = json.loads(out)[0]
        self.assertEqual([(q["year"], q["q"], q["derived"]) for q in r["quarters"]],
                         [(2026, 3, False), (2026, 2, False), (2026, 1, False), (2025, 4, True), (2025, 3, False),
                          (2025, 2, False), (2025, 1, False)])
        q4 = r["quarters"][3]
        self.assertEqual(q4["cur"]["revenue"], 460 - 330)          # 연간 - 3분기 누적
        self.assertEqual(q4["prev"]["revenue"], 400 - 295)         # 전년 연간 - 전년 3분기 누적
        self.assertEqual(q4["cur"]["eps"], 370 - 270)
        self.assertEqual(q4["cur"]["gross_profit"], 180 - 132)
        self.assertEqual(r["quarters"][0]["prev"]["op"], 12)       # 같은 보고서의 전년 같은 분기
        self.assertEqual(r["ttm"]["revenue"], 160 + 150 + 140 + 130)
        self.assertEqual((r["ttm"]["from"], r["ttm"]["to"]), ("2025 4Q", "2026 3Q"))
        self.assertEqual(r["next"]["due"], "2027-03-31")
        self.assertFalse(r["oneoff"])
        self.assertNotIn(("fnlttSinglAcntAll.json", "2025", "11011", "CFS"), fake.calls)   # 두 번째는 캐시에서

    def test_text(self):
        filings = [{"rcept_no": "20261120000009", "rcept_dt": "20261120", "corp_code": "00000001",
                    "stock_code": "005930", "report_nm": "연결재무제표기준영업(잠정)실적(공정공시)"}]
        code, out, _, _ = self.run_e("005930", "--quarters", "6", fake=FakeDart(filings=filings))
        self.assertEqual(code, 0)
        self.assertIn("005930 가상전자 (연결, 12월 결산)", out)
        self.assertIn("최근 보고서: 2026 3분기보고서, 2026-11-16 접수", out)
        self.assertIn("2025 4Q*", out)
        self.assertIn("적자 전환", out)                             # 2026 1Q 영업이익 -5 (전년 10)
        self.assertIn(earn.NOTE_Q4, out)
        self.assertIn("2026-11-20 에 정기보고서보다 새 공시가 있습니다: 연결재무제표기준영업(잠정)실적(공정공시)", out)
        self.assertIn("법정 기한 2027-03-31", out)
        self.assertIn("not financial advice", out)

    def test_oneoff_and_missing_revenue(self):
        reports = dict(REPORTS)
        reports[(2026, "11014")] = ("20261116000001", line(0, None, Q(16, 26, 12, 33), Q(20, 44, 10, 27),
                                                            Q(20, 44, 10, 27), Q(100, 300, 100, 270)))
        code, out, _, _ = self.run_e("005930", "--quarters", "2", fake=FakeDart(reports=reports))
        self.assertIn("! " + earn.NOTE_ONEOFF, out)
        self.assertIn("매출 줄이 없는 회사", out)

    def test_errors(self):
        code, _, err, _ = self.run_e("12345")
        self.assertEqual(code, 2)
        self.assertIn("6자리", err)
        code, _, err, _ = self.run_e("000660")
        self.assertEqual(code, 2)
        self.assertIn("상장사가 없습니다", err)
        code, _, err, _ = self.run_e("005930", "--quarters", "0")
        self.assertEqual(code, 2)
        os.environ.pop("DART_API_KEY")
        code, _, err, _ = self.run_e("005930")
        self.assertEqual(code, 2)
        self.assertIn("채팅에 붙여 넣지 않습니다", err)


if __name__ == "__main__":
    unittest.main()
