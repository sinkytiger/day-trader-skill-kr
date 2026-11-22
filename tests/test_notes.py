import datetime
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import load

notes = load("trader-kr-notes", "notes")
markets = load("trader-kr-notes", "markets")
uswatch = load("trader-kr-notes", "uswatch")
study = load("trader-kr-notes", "study")
share = load("trader-kr-notes", "share")
basics = load("trader-kr-notes", "basics")
tg = load("trader-kr-notes", "tg")


class InTempDir(unittest.TestCase):
    """결과를 프로젝트 폴더 기준 trader-kr/notes/ 에 쓰므로 빈 임시 폴더에서 돈다."""

    def setUp(self):
        self.old = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp, ignore_errors=True)


class Dispatcher(unittest.TestCase):
    def test_unknown_part_prints_usage(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(notes.main([]), 1)
            self.assertEqual(notes.main(["buy"]), 1)
        self.assertIn("notes.py markets", out.getvalue())

    def test_every_part_has_a_file_with_main(self):
        for name in notes.PARTS:
            self.assertTrue(callable(notes.load(name).main), name)


class Markets(unittest.TestCase):
    PAGE = ('x &quot;priceEarnings&quot;:{&quot;formattedValue&quot;:&quot;28.88&quot;,'
            '&quot;formattedAsOfDate&quot;:&quot;Nov 05, 2026&quot;} y')

    def test_reads_escaped_ratio_from_product_page(self):
        self.assertEqual(markets.ishares_ratio(self.PAGE, "priceEarnings"), (28.88, "2026-11-05"))
        self.assertIsNone(markets.ishares_ratio(self.PAGE, "priceBook"))

    def test_message_has_numbers_and_no_dashboard_link(self):
        rows = [{"market": m, "ytd_krw": v} for m, v in (("가", 30.0), ("나", 5.0), ("다", -1.0), ("라", -20.0))]
        cms = [{"commodity": "금", "wk_change": 1.25}]
        basis = [{"item": k, "value": v} for k, v in (
            ("갱신일", "2026-11-21"), ("원·달러 최근", 1334.8), ("원·달러 최근 날짜", "2026-11-20"),
            ("원화 변화 (달러 대비, %)", -2.0), ("올해 수익률 기준일", "2026-11-20"), ("원자재 기준일", "2026-11-20"))]
        msg = markets.message(rows, cms, basis)
        self.assertIn("상위: 가 +30.0%, 나 +5.0%, 다 -1.0%", msg)
        self.assertIn("하위: 라 -20.0%", msg)
        self.assertIn("원화 2.0% 약세", msg)
        self.assertNotIn("claude.ai", msg)


class UsWatch(InTempDir):
    def test_trailing_sum_uses_latest_year_and_quarters(self):
        f = [{"start": "2025-01-01", "end": "2025-12-31", "val": 10.0},
             {"start": "2026-01-01", "end": "2026-09-30", "val": 9.0},
             {"start": "2025-01-01", "end": "2025-09-30", "val": 6.0}]
        self.assertEqual(uswatch.ttm(f), (13.0, "2026-09-30", "TTM"))
        self.assertEqual(uswatch.ttm(f[:1]), (10.0, "2025-12-31", "FY"))
        self.assertIsNone(uswatch.ttm(f[:2]))           # 1년 전 같은 누적이 없으면 계산하지 않는다

    def test_new_filings_window_items_and_form4(self):
        subs = {"filings": {"recent": {
            "form": ["8-K", "4", "4", "10-Q", "8-K"],
            "filingDate": ["2026-11-18", "2026-11-17", "2026-11-01", "2026-11-19", "2026-10-01"],
            "items": ["2.02,9.01", "", "", "", "5.02"]}}}
        out, f4 = uswatch.new_filings([subs], "2026-11-14", "2026-11-20")
        self.assertEqual(f4, 1)
        self.assertEqual([(x["form"], x["items"]) for x in out], [("8-K", ["실적 발표"]), ("10-Q", [])])

    def test_stops_without_a_watchlist(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(uswatch.main(), 2)
        self.assertIn("us-watchlist.json", err.getvalue())
        self.assertFalse(os.path.exists(os.path.join("trader-kr", "notes", "us-watch", "out", "message.txt")))

    def test_example_watchlist_is_marked_as_an_example(self):
        with open(os.path.join(os.path.dirname(uswatch.__file__), "us-watchlist.example.json"), encoding="utf-8") as fh:
            wl = json.load(fh)
        self.assertIn("추천이 아니다", wl["note"])
        self.assertEqual(sorted(wl["names"]), sorted(wl["tickers"]))


class Study(unittest.TestCase):
    def test_each_year_comes_from_the_latest_report_that_holds_it(self):
        plan = study.source_plan([2021, 2022, 2023, 2024, 2025])
        self.assertEqual(plan[2025], (2025, "thstrm_amount"))
        self.assertEqual(plan[2023], (2025, "bfefrmtrm_amount"))
        self.assertEqual(plan[2022], (2024, "bfefrmtrm_amount"))
        self.assertEqual(plan[2021], (2023, "bfefrmtrm_amount"))

    def test_eps_adds_continuing_and_discontinued_when_no_total(self):
        rows = [{"sj_div": "CIS", "account_id": "ifrs-full_BasicEarningsLossPerShareFromContinuingOperations", "thstrm_amount": "27,920"},
                {"sj_div": "CIS", "account_id": "ifrs-full_BasicEarningsLossPerShareFromDiscontinuedOperations", "thstrm_amount": "1589"}]
        self.assertEqual(study.kr_eps(rows, "thstrm_amount"), (29509.0, "계속+중단영업"))
        total = rows + [{"sj_div": "CIS", "account_id": "ifrs-full_BasicEarningsLossPerShare", "thstrm_amount": "100"}]
        self.assertEqual(study.kr_eps(total, "thstrm_amount"), (100.0, "합계"))

    def test_operating_cash_flow_found_by_name_without_standard_code(self):
        rows = [{"sj_div": "CF", "account_id": "-표준계정코드 미사용-", "account_nm": "영업활동순현금흐름 합계", "thstrm_amount": "1,234"}]
        self.assertEqual(study.kr_row(rows, "thstrm_amount")["ocf"], 1234.0)

    def test_naver_na_is_not_a_number(self):
        self.assertIsNone(study.num("N/A"))
        self.assertEqual(study.jo_eok("1,057조 5,918억"), 1057e12 + 5918e8)


class Share(InTempDir):
    def test_yearly_comparison(self):
        qy = {2013: 100.0, 2014: 110.0, 2015: 121.0}
        iy = {2013: 100.0, 2014: 120.0, 2015: 126.0}
        r = share.yearly_vs(qy, iy, 2016)
        self.assertEqual([(x["year"], x["diff"]) for x in r], [("2014", -10.0), ("2015", 5.0)])

    def test_stops_and_writes_report_when_inputs_are_missing(self):
        share.problems.clear()
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(share.main(), 2)
        with open(os.path.join("trader-kr", "notes", "share", "report.json"), encoding="utf-8") as fh:
            rep = json.load(fh)
        self.assertFalse(rep["ok"])
        self.assertTrue(rep["problems"])
        self.assertFalse(os.path.exists(os.path.join("trader-kr", "notes", "share", "firms.json")))


class Basics(unittest.TestCase):
    def test_drawdowns_only_past_the_limit_with_recovery(self):
        d = datetime.date
        pts = [(d(2020, 1, 1), 100.0), (d(2020, 2, 1), 70.0), (d(2020, 3, 1), 101.0),
               (d(2020, 4, 1), 85.0), (d(2020, 5, 1), 102.0), (d(2020, 6, 1), 75.0)]
        eps = basics.drawdowns(pts)
        self.assertEqual([(e["peak"], e["drop"], e["recovered"]) for e in eps],
                         [("2020-01-01", -30.0, "2020-03-01"), ("2020-05-01", -26.5, None)])
        self.assertEqual(eps[0]["months_to_recover"], 2.0)


class Telegram(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_path, self.old_env = tg.conf_path, {k: os.environ.pop(k, None) for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
        tg.conf_path = lambda: os.path.join(self.tmp, "bot.json")

    def tearDown(self):
        tg.conf_path = self.old_path
        for k, v in self.old_env.items():
            if v is not None:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_send(self, text, *args):
        old = sys.stdin
        sys.stdin = io.TextIOWrapper(io.BytesIO(text.encode("utf-8")), encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = tg.main(["send"] + list(args))
        finally:
            sys.stdin = old
        return code, out.getvalue() + err.getvalue()

    def test_split_keeps_every_part_under_the_limit(self):
        text = "\n".join("줄%04d " % i + "x" * 50 for i in range(300))
        parts = tg.split(text)
        self.assertTrue(all(0 < len(p) <= tg.LIMIT for p in parts))
        self.assertEqual("\n".join(parts), text)
        self.assertEqual([len(p) for p in tg.split("a" * tg.LIMIT + "\nb")], [tg.LIMIT, 1])

    def test_dry_run_sends_nothing(self):
        code, out = self.run_send("첫 줄\n둘째 줄", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("보내지 않음", out)

    def test_without_setup_nothing_is_sent(self):
        code, out = self.run_send("보내지 마")
        self.assertEqual(code, 3)
        self.assertIn("setup", out)
        self.assertEqual(self.run_send("")[0], 1)


if __name__ == "__main__":
    unittest.main()
