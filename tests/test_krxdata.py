import contextlib
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.parse

from _load import load
from fake_toss import FakeToss, stock_info
from krcommon import data, krxdata, toss

scan = load("trader-kr-scanner", "scan")
HOLIDAY = datetime.date(2026, 10, 9)        # 시험용 휴장일 (한글날)


def closes_for(day):
    """날짜마다 다른 합성 종가: SYN001 은 꾸준히 오르다 마지막 날 20일 신고가."""
    n = (day - datetime.date(2026, 7, 1)).days
    return 10000 + 10 * n


class FakeKrx(object):
    def __init__(self, last_day, status=200):
        self.last_day, self.status, self.calls = last_day, status, []

    def __call__(self, url, headers):
        u = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        self.calls.append((u.path, q, dict(headers)))
        if self.status != 200:
            return self.status, ""
        day = datetime.datetime.strptime(q["basDd"], "%Y%m%d").date()
        if day.weekday() >= 5 or day == HOLIDAY or day > self.last_day:
            return 200, json.dumps({"OutBlock_1": []})
        if u.path.endswith("stk_bydd_trd"):
            c = closes_for(day)
            rows = [{"BAS_DD": q["basDd"], "ISU_CD": "SYN001", "ISU_NM": "합성종목", "MKT_NM": "KOSPI",
                     "TDD_CLSPRC": "{:,}".format(c), "TDD_OPNPRC": "{:,}".format(c - 10),
                     "TDD_HGPRC": "{:,}".format(c + 20), "TDD_LWPRC": "{:,}".format(c - 20), "ACC_TRDVOL": "200,000",
                     "ACC_TRDVAL": "{:,}".format(c * 200000), "MKTCAP": "-"}]
        else:
            rows = [{"BAS_DD": q["basDd"], "ISU_CD": "SYN002", "ISU_NM": "코스닥합성", "MKT_NM": "KOSDAQ",
                     "TDD_CLSPRC": "5,000", "TDD_OPNPRC": "-", "TDD_HGPRC": "5,100", "TDD_LWPRC": "4,900",
                     "ACC_TRDVOL": "10", "ACC_TRDVAL": "50,000", "MKTCAP": "-"},
                    {"BAS_DD": q["basDd"], "ISU_CD": "SYN003", "ISU_NM": "거래없음", "TDD_CLSPRC": "-"}]
        return 200, json.dumps({"OutBlock_1": rows})


class KrxData(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.now = datetime.datetime(2026, 10, 29, 8, 40)     # 10-28 자료가 막 올라온 뒤

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_parsing_and_the_key_goes_in_a_header(self):
        fake = FakeKrx(datetime.date(2026, 10, 28))
        rows = krxdata.fetch_day(datetime.date(2026, 10, 28), "secret-krx-key", fake)
        self.assertEqual(rows["SYN001"]["c"], closes_for(datetime.date(2026, 10, 28)))
        self.assertEqual(rows["SYN001"]["value"], closes_for(datetime.date(2026, 10, 28)) * 200000)
        self.assertIsNone(rows["SYN002"]["o"])                      # "-" 는 없음
        self.assertNotIn("SYN003", rows)                            # 종가 없는 행은 뺌
        for path, q, headers in fake.calls:
            self.assertEqual(headers["AUTH_KEY"], "secret-krx-key")
            self.assertNotIn("secret-krx-key", path + json.dumps(q))

    def test_series_skips_weekends_holidays_and_unpublished_days(self):
        fake = FakeKrx(datetime.date(2026, 10, 29))
        cache = os.path.join(self.tmp, "krx")
        series, info = krxdata.daily_series(["SYN001", "SYN002"], datetime.date(2026, 10, 29), 15, "k", cache,
                                            self.now, fake, lambda s: None)
        dates = [r["date"] for r in series["SYN001"]]
        self.assertEqual(len(dates), 15)
        self.assertEqual(dates[-1], "2026-10-28")                   # 10-29 는 아직 발표 전
        self.assertNotIn(HOLIDAY.isoformat(), dates)
        self.assertEqual(info["sessions"], 15)
        n_calls = len(fake.calls)
        krxdata.daily_series(["SYN001"], datetime.date(2026, 10, 29), 15, "k", cache, self.now, fake, lambda s: None)
        self.assertEqual(len(fake.calls), n_calls)                  # 두 번째는 캐시에서
        self.assertTrue(os.path.exists(os.path.join(cache, HOLIDAY.strftime("%Y%m%d") + ".json")))
        self.assertFalse(os.path.exists(os.path.join(cache, "20261029.json")))

    def test_an_unapproved_key_says_what_to_apply_for(self):
        with self.assertRaises(krxdata.KrxDataError) as e:
            krxdata.fetch_day(datetime.date(2026, 10, 28), "k", FakeKrx(datetime.date(2026, 10, 28), status=401))
        self.assertIn("일별매매정보", str(e.exception))
        self.assertIn("이용신청", str(e.exception))

    def test_key_from_env_or_file(self):
        self.assertEqual(krxdata.api_key({"KRX_API_KEY": " abc "}), "abc")
        os.makedirs(os.path.join(self.tmp, ".krx"))
        with open(os.path.join(self.tmp, ".krx", "api_key"), "w") as fh:
            fh.write("from-file\n")
        self.assertEqual(krxdata.api_key({}, home=self.tmp), "from-file")
        self.assertIsNone(krxdata.api_key({}, home=os.path.join(self.tmp, "none")))


class DailyScanOnKrx(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.getcwd()
        os.chdir(self.tmp)
        f = FakeToss()
        f.stocks = {"SYN001": stock_info("SYN001"), "SYN002": stock_info("SYN002", "코스닥합성")}
        f.ranking = ["SYN001", "SYN002"]
        data.set_client(toss.Client(credentials=("tsck_live_test", "s"), transport=f, sleep=lambda s: None,
                                    token_path=os.path.join(self.tmp, "token.json")))
        self._saved = (krxdata.api_key, krxdata._http, krxdata.PAUSE)
        krxdata.PAUSE = 0
        krxdata.api_key = lambda env=None, home=None: "test-key"
        krxdata._http = FakeKrx(datetime.date(2026, 10, 28))

    def tearDown(self):
        krxdata.api_key, krxdata._http, krxdata.PAUSE = self._saved
        data.set_client(None)
        os.chdir(self.old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_daily_scan_uses_krx_closes(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = scan.main(["daily", "--now", "2026-10-29 08:40", "--min-value", "1000000"])
        self.assertEqual(code, 0, out.getvalue())
        with open("trader-kr/watchlist.json", encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["source"], "krx")
        self.assertEqual([h["symbol"] for h in doc["hits"]], ["SYN001"])
        self.assertIn("breakout_20d", doc["hits"][0]["setups"])
        self.assertEqual(doc["hits"][0]["price"], closes_for(datetime.date(2026, 10, 28)))
        self.assertEqual(doc["as_of"], "2026-10-28 종가")
        self.assertIn("KRX 공식 일봉", out.getvalue())

    def test_without_a_key_toss_is_used_and_flagged(self):
        krxdata.api_key = lambda env=None, home=None: None
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            scan.main(["daily", "--now", "2026-10-29 08:40", "--no-write"])
        self.assertIn("20:00 NXT", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(scan.main(["daily", "--source", "krx", "--now", "2026-10-29 08:40"]), 1)


if __name__ == "__main__":
    unittest.main()
