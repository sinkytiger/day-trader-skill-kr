import datetime
import json
import os
import shutil
import tempfile
import unittest
import urllib.parse

import _load  # noqa: F401
from krcommon import dart

DAY = datetime.date(2026, 10, 28)
ROWS = [{"corp_name": "합성종목", "stock_code": "SYN001", "report_nm": "연결재무제표기준영업(잠정)실적(공정공시)",
         "rcept_no": "20261028000001"},
        {"corp_name": "합성종목", "stock_code": "SYN001", "report_nm": "임원ㆍ주요주주특정증권등소유상황보고서",
         "rcept_no": "20261028000002"},
        {"corp_name": "다른회사", "stock_code": "SYN002", "report_nm": "주주총회소집결의", "rcept_no": "20261028000003"}]


class Fake(object):
    def __init__(self, rows, per=2, status="000"):
        self.rows, self.per, self.status, self.calls = rows, per, status, []

    def __call__(self, url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        self.calls.append(q)
        if self.status != "000":
            return json.dumps({"status": self.status, "message": "x"})
        page = int(q["page_no"])
        total = max(1, -(-len(self.rows) // self.per))
        chunk = self.rows[(page - 1) * self.per:page * self.per]
        return json.dumps({"status": "000", "page_no": page, "total_page": total, "list": chunk})


class Dart(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_pages_are_joined_and_filtered_by_symbol(self):
        fake = Fake(ROWS)
        rows, note = dart.check("SYN001", DAY, self.tmp, datetime.datetime(2026, 10, 28, 10, 0),
                                {"DART_API_KEY": "k"}, fake, lambda s: None)
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual([r["rcept_no"] for r in rows], ["20261028000001", "20261028000002"])
        self.assertIn("잠정)실적", note)
        self.assertIn("외 1건", note)

    def test_cache_refreshes_today_but_keeps_past_days(self):
        fake = Fake(ROWS)
        env = {"DART_API_KEY": "k"}
        now = datetime.datetime(2026, 10, 28, 10, 0)
        dart.check("SYN001", DAY, self.tmp, now, env, fake, lambda s: None)
        dart.check("SYN001", DAY, self.tmp, now + datetime.timedelta(minutes=5), env, fake, lambda s: None)
        self.assertEqual(len(fake.calls), 2)                        # 10분 안: 캐시
        dart.check("SYN001", DAY, self.tmp, now + datetime.timedelta(minutes=11), env, fake, lambda s: None)
        self.assertEqual(len(fake.calls), 4)                        # 10분 지남: 다시
        dart.check("SYN001", DAY, self.tmp, datetime.datetime(2026, 10, 29, 9, 0), env, fake, lambda s: None)
        dart.check("SYN001", DAY, self.tmp, datetime.datetime(2026, 10, 30, 9, 0), env, fake, lambda s: None)
        self.assertEqual(len(fake.calls), 6)                        # 다음 날 받은 목록은 확정

    def test_no_key_no_data_and_errors(self):
        rows, note = dart.check("SYN001", DAY, self.tmp, env={})
        self.assertIsNone(rows)
        self.assertIn("DART_API_KEY", note)
        rows, note = dart.check("SYN001", DAY, None, env={"DART_API_KEY": "k"}, transport=Fake([], status="013"))
        self.assertEqual((rows, note), ([], None))
        rows, note = dart.check("SYN001", DAY, None, env={"DART_API_KEY": "k"}, transport=Fake([], status="020"))
        self.assertIsNone(rows)
        self.assertIn("확인하지 못했습니다", note)

    def test_the_key_never_appears_in_messages(self):
        _, note = dart.check("SYN001", DAY, None, env={"DART_API_KEY": "secret-key-123"}, transport=Fake([], status="010"))
        self.assertNotIn("secret-key-123", note)
        self.assertFalse(os.listdir(self.tmp))


if __name__ == "__main__":
    unittest.main()
