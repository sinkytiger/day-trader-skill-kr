import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from urllib.parse import parse_qs, urlparse

from _load import load
from krcommon import dart, dartdoc

news = load("trader-kr-news", "news")
NOW = datetime.datetime(2026, 11, 23, 8, 30)          # 월요일 아침


def filing(no, day, sym, name, report, cls="Y", filer=None, rm=""):
    return {"corp_cls": cls, "corp_name": name, "corp_code": "00000000", "stock_code": sym, "report_nm": report,
            "rcept_no": no, "flr_nm": filer or name, "rcept_dt": day.replace("-", ""), "rm": rm}


DAYS = {
    "20261120": [filing("20261120000001", "2026-11-20", "005930", "가상전자", "연결재무제표기준영업(잠정)실적(공정공시)"),
                 filing("20261120000002", "2026-11-20", "000660", "가상반도체", "[기재정정]타법인주식및출자증권취득결정",
                        rm="유"),
                 filing("20261120000003", "2026-11-20", "123450", "가상바이오", "주권매매거래정지기간변경", cls="K",
                        filer="코스닥시장본부"),
                 filing("20261120000004", "2026-11-20", "", "비상장회사", "유상증자결정", cls="E")],
    "20261121": [],
    "20261122": [],
    "20261123": [filing("20261123000005", "2026-11-23", "005930", "가상전자", "임원ㆍ주요주주특정증권등소유상황보고서",
                        filer="홍길동"),
                 filing("20261123000006", "2026-11-23", "777770", "가상화학", "유상증자결정", cls="K")],
}


class FakeDart(object):
    def __init__(self, days=None):
        self.days = DAYS if days is None else days
        self.calls = []

    def __call__(self, url):
        q = parse_qs(urlparse(url).query)
        self.calls.append(q["bgn_de"][0])
        rows = self.days.get(q["bgn_de"][0], [])
        if not rows:
            return json.dumps({"status": "013", "message": "조회된 데이타가 없습니다."})
        return json.dumps({"status": "000", "total_page": 1, "list": rows})


def doc_zip(xml, name="20261120000002.xml"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, xml.encode("utf-8"))
    return buf.getvalue()


INSIDER_XML = """<?xml version="1.0" encoding="utf-8"?>
<DOCUMENT><DOCUMENT-NAME>임원ㆍ주요주주 특정증권등 소유상황보고서</DOCUMENT-NAME>
<STYLE>.xforms * { font-family: 돋움체;}</STYLE>
<TABLE><TBODY>
<TR><TD>보고자 :</TD>
<TE>홍길동</TE></TR>
<TR><TD>생년월일 또는 사업자등록번호</TD><TE>900101</TE></TR>
<TR><TD>주소(본점소재지)[읍ㆍ면ㆍ동까지만 기재]</TD><TE>서울특별시 가상구 가상동</TE></TR>
<TR><TD>성 명</TD><TE>김담당</TE><TD>팩스번호</TD><TE>02-123-4567</TE></TR>
<TR><TD>전화번호</TD><TE>031-987-6543</TE><TD>이메일</TD><TE>ir@example.com</TE></TR>
<TR><TD>소유 주식수</TD><TE>12,345</TE></TR>
</TBODY></TABLE>
<P>비고&cr;둘째 줄</P>
</DOCUMENT>"""


class Tags(unittest.TestCase):
    def test_tags_and_hot(self):
        self.assertEqual(news.tags_of("연결재무제표기준영업(잠정)실적(공정공시)"), ["실적"])
        self.assertTrue(news.is_hot("연결재무제표기준영업(잠정)실적(공정공시)"))
        self.assertEqual(news.tags_of("[기재정정]타법인주식및출자증권취득결정"), ["정정", "계약·투자"])
        self.assertFalse(news.is_hot("[기재정정]타법인주식및출자증권취득결정"))
        self.assertEqual(news.tags_of("임원ㆍ주요주주특정증권등소유상황보고서"), ["지분"])
        self.assertTrue(news.is_hot("임원ㆍ주요주주 특정증권등 거래계획보고서"))      # 빈칸과 가운뎃점을 빼고 견준다
        self.assertEqual(news.tags_of("주권매매거래정지기간변경"), ["거래소조치"])
        self.assertTrue(news.is_hot("최대주주변경을수반하는주식양수도계약체결"))
        self.assertEqual(news.tags_of("기업설명회(IR)개최"), ["기타"])

    def test_item_fields(self):
        it = news.item(DAYS["20261120"][1])
        self.assertEqual(it["date"], "2026-11-20")
        self.assertEqual(it["market"], "유가")
        self.assertEqual(it["notes"], [])                       # "유" 는 소관 표시일 뿐
        it = news.item(dict(DAYS["20261120"][1], rm="유정"))
        self.assertEqual(it["notes"], ["뒤에 정정 있음"])
        self.assertEqual(it["url"], "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261120000002")


class Window(unittest.TestCase):
    def test_monday_reaches_back_to_friday(self):
        days = news.window(NOW)
        self.assertEqual([d.isoformat() for d in days], ["2026-11-20", "2026-11-21", "2026-11-22", "2026-11-23"])

    def test_days_and_since(self):
        self.assertEqual(len(news.window(NOW, days=1)), 1)
        self.assertEqual(news.window(NOW, since="2026-11-22")[0], datetime.date(2026, 11, 22))
        with self.assertRaises(news.NewsError):
            news.window(NOW, days=0)
        with self.assertRaises(news.NewsError):
            news.window(NOW, since="2026-12-01")


class Files(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.root = os.path.join(self.d, "trader-kr")
        os.makedirs(os.path.join(self.root, "today", "000660"))

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def write(self, rel, doc):
        with open(os.path.join(self.root, rel), "w", encoding="utf-8") as fh:
            fh.write(doc if isinstance(doc, str) else json.dumps(doc, ensure_ascii=False))

    def test_watched_from_every_file_once(self):
        self.write("watchlist.json", {"hits": [{"symbol": "005930", "name": "가상전자"}, {"symbol": "bad"}]})
        self.write("plan.json", {"symbol": "005930", "name": "가상전자"})
        self.write("today/000660/plan.json", {"symbol": "000660", "name": "가상반도체"})
        self.write("journal.jsonl", "\n".join([json.dumps({"symbol": "123450", "exit_price": None}),
                                               json.dumps({"symbol": "999990", "exit_price": 1000}), "not json"]))
        got = news.watched(self.root)
        self.assertEqual(got, [("005930", "가상전자", "스캐너"), ("000660", "가상반도체", "오늘 계획"),
                               ("123450", None, "일지 (열린 거래)")])

    def test_no_files_no_symbols(self):
        self.assertEqual(news.watched(self.root), [])


class Run(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.state = os.path.join(self.d, "news.json")
        self.cache = os.path.join(self.d, "cache")
        self.fake = FakeDart()
        os.environ["DART_API_KEY"] = "x" * 40

    def tearDown(self):
        os.environ.pop("DART_API_KEY", None)
        shutil.rmtree(self.d, ignore_errors=True)

    def run_news(self, *args, doc_transport=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = news.main(list(args), transport=self.fake, doc_transport=doc_transport)
        return code, out.getvalue(), err.getvalue()

    def base(self):
        return ["--now", "2026-11-23 08:30", "--state", self.state, "--cache-dir", self.cache]

    def test_new_then_nothing_new(self):
        code, out, _ = self.run_news(*(["--symbols", "005930", "000660"] + self.base()))
        self.assertEqual(code, 0)
        self.assertIn("2026-11-20 ~ 2026-11-23", out)
        self.assertIn("새 공시 3건 (! 중요 1건)", out)
        self.assertIn("! 2026-11-20 [실적] 연결재무제표기준영업(잠정)실적(공정공시)", out)
        self.assertIn("[정정, 계약·투자] [기재정정]타법인주식및출자증권취득결정", out)
        self.assertIn("제출 홍길동", out)
        self.assertLess(out.index("20261123000005"), out.index("20261120000001"))      # 최신 먼저
        self.assertIn("not financial advice", out)
        self.assertEqual(sorted(self.fake.calls), ["20261120", "20261121", "20261122", "20261123"])
        with open(self.state, encoding="utf-8") as fh:
            self.assertEqual(sorted(json.load(fh)["seen"]), ["20261120000001", "20261120000002", "20261123000005"])
        code, out, _ = self.run_news(*(["--symbols", "005930", "000660"] + self.base()))
        self.assertEqual(code, 1)
        self.assertIn("005930 가상전자: 새 공시 없음", out)
        code, out, _ = self.run_news(*(["--symbols", "005930", "--all"] + self.base()))
        self.assertEqual(code, 0)
        self.assertIn("본 것", out)

    def test_dry_run_keeps_state(self):
        self.run_news(*(["--symbols", "005930", "--dry-run"] + self.base()))
        self.assertFalse(os.path.exists(self.state))

    def test_json(self):
        code, out, _ = self.run_news(*(["--symbols", "000660", "--json"] + self.base()))
        doc = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(doc["symbols"], [{"symbol": "000660", "name": None, "from": "--symbols"}])
        self.assertEqual(doc["by_symbol"]["000660"][0]["tags"], ["정정", "계약·투자"])

    def test_market_shows_hot_listed_only(self):
        code, out, _ = self.run_news(*(["market"] + self.base()))
        self.assertEqual(code, 0)
        self.assertIn("! 777770 가상화학 2026-11-23 [자금조달] 유상증자결정", out)
        self.assertIn("! 123450 가상바이오 2026-11-20 [거래소조치] 주권매매거래정지기간변경", out)
        self.assertIn("! 005930 가상전자", out)
        self.assertNotIn("비상장회사", out)                     # 상장사만
        self.assertNotIn("소유상황보고서", out)                 # ! 가 아닌 것은 빼고
        code, out, _ = self.run_news(*(["market"] + self.base()))
        self.assertEqual(code, 1)
        code, out, _ = self.run_news(*(["--symbols", "777770"] + self.base()))
        self.assertEqual(code, 0)                              # 시장 보기에서 본 것은 관심 종목 기록과 따로

    def test_bad_input_and_missing_key(self):
        code, _, err = self.run_news(*(["--symbols", "12345"] + self.base()))
        self.assertEqual(code, 2)
        self.assertIn("6자리", err)
        os.environ.pop("DART_API_KEY")
        code, _, err = self.run_news(*(["--symbols", "005930", "--root", self.d] + self.base()))
        self.assertEqual(code, 2)
        self.assertIn("python krcommon/keys.py dart", err)
        self.assertIn("채팅에 붙여 넣지 않습니다", err)

    def test_no_watch_symbols(self):
        code, _, err = self.run_news(*(["--root", os.path.join(self.d, "none")] + self.base()))
        self.assertEqual(code, 2)
        self.assertIn("관심 종목이 없습니다", err)

    def test_read_document(self):
        def fake_doc(url):
            q = parse_qs(urlparse(url).query)
            self.assertEqual(q["rcept_no"], ["20261120000002"])
            return doc_zip(INSIDER_XML)
        args = ["read", "20261120000002", "--doc-cache", os.path.join(self.d, "docs")]
        code, out, _ = self.run_news(*args, doc_transport=fake_doc)
        self.assertEqual(code, 0)
        self.assertIn("보고자 : | 홍길동", out)
        self.assertIn("소유 주식수 | 12,345", out)
        for gone in ("900101", "가상동", "김담당", "02-123-4567", "031-987-6543", "ir@example.com", "font-family"):
            self.assertNotIn(gone, out)
        self.assertIn("비고\n둘째 줄", out)
        code, out, _ = self.run_news(*(args + ["--find", "소유 주식수"]), doc_transport=lambda url: b"")   # 캐시에서
        self.assertIn("12,345", out)
        self.assertNotIn("보고자 :", out)                     # 찾은 줄 앞뒤 두 줄만

    def test_read_errors(self):
        code, _, err = self.run_news("read", "123")
        self.assertEqual(code, 2)
        self.assertIn("14자리", err)
        bad = ('<?xml version="1.0"?><result><status>014</status><message>파일이 존재하지 않습니다.</message>'
               '</result>').encode("utf-8")
        code, _, err = self.run_news("read", "20261120000009", "--doc-cache", "", doc_transport=lambda url: bad)
        self.assertEqual(code, 2)
        self.assertIn("014", err)


class Doc(unittest.TestCase):
    def test_text_keeps_rows_and_drops_markup(self):
        t = dartdoc.to_text("<TABLE><TR>\n<TD>a</TD>\n<TE>b</TE>\n</TR><TR><TD>c</TD><TU>d</TU></TR></TABLE>")
        self.assertEqual(t, "a | b\nc | d")

    def test_read_rejects_non_zip(self):
        with self.assertRaises(dart.DartError):
            dartdoc.read(b"not a zip")


if __name__ == "__main__":
    unittest.main()
