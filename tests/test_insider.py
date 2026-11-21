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
from krcommon import dartdoc, watch

ins = load("trader-kr-insider", "insider")

HOLD_XML = """<?xml version="1.0" encoding="utf-8"?>
<DOCUMENT><DOCUMENT-NAME>임원ㆍ주요주주 특정증권등 소유상황보고서</DOCUMENT-NAME>
<TABLE><TBODY>
<TR><TD>보고자 :</TD><TE>홍 길 동</TE></TR>
<TR><TD>회 사 명</TD><TE>가상전자</TE></TR>
<TR><TD>법인구분</TD><TU>유가증권시장상장법인</TU><TD>회사코드</TD><TE>005930</TE></TR>
<TR><TD>보고구분</TD><TE>변동</TE><TD>보고자 구분</TD><TE>개인(국내)</TE></TR>
<TR><TD>생년월일 또는 사업자등록번호</TD><TE>900101</TE></TR>
<TR><TD>발행회사와의 관계</TD><TD>임원(등기여부)</TD><TE>비등기임원</TE><TD>직위명</TD><TE>상무</TE></TR>
<TR><TD>주요주주</TD><TE>-</TE></TR>
</TBODY></TABLE>
<TITLE>다. 세부변동내역</TITLE>
<TABLE><TBODY>
<TR><TH>보고사유</TH><TH>변동일*</TH><TH>특정증권등의종류</TH><TH>소 유 주 식 수 (주)</TH><TH>취득/처분단가(원)**</TH></TR>
<TR><TH>변동전</TH><TH>증감</TH><TH>변동후</TH></TR>
<TR><TD>장내매도(-)</TD><TD>2026년 11월 13일</TD><TD>보통주</TD><TD>5,000</TD><TD>1,206</TD><TD>3,794</TD><TD>197,664</TD><TD>-</TD><TD>-</TD></TR>
<TR><TD>증여(-)</TD><TD>2026년 11월 16일</TD><TD>보통주</TD><TD>3,794</TD><TD>-155</TD><TD>3,639</TD><TD>188,700</TD><TD>-</TD><TD>2026년 10월 13일</TD></TR>
<TR><TD>장내매수(+)</TD><TD>2026년 11월 16일</TD><TD>우선주</TD><TD>0</TD><TD>286</TD><TD>286</TD><TD>173,800</TD><TD>-</TD><TD>-</TD></TR>
<TR><TD>합 계</TD><TD>-</TD><TD>-1,075</TD><TD>-</TD></TR>
</TBODY></TABLE>
<P>* 증권시장에서 주식등을 매매한 경우에는 그 계약결제일</P>
</DOCUMENT>"""

PLAN_XML = """<?xml version="1.0" encoding="utf-8"?>
<DOCUMENT><DOCUMENT-NAME>임원ㆍ주요주주특정증권등거래계획보고서</DOCUMENT-NAME>
<TABLE><TBODY>
<TR><TD>보고자 :</TD><TE>가상홀딩스</TE></TR>
<TR><TD>회 사 명</TD><TE>가상화학</TE></TR>
<TR><TD>법인구분</TD><TU>코스닥상장법인</TU><TD>회사코드</TD><TE>777770</TE></TR>
<TR><TD>보고자 구분</TD><TE>국내법인</TE></TR>
<TR><TD>발행회사와의관계</TD><TD>임원(등기여부)</TD><TE>-</TE><TD>직위명</TD><TE>-</TE></TR>
<TR><TD>주요주주</TD><TE>10%이상주주</TE></TR>
</TBODY></TABLE>
<TITLE>다. 거래계획</TITLE>
<P>(1) 거래목적</P>
<TABLE><TBODY><TR><TH>거래목적</TH></TR><TR><TD>지분 매각을 통한 유동성 확보</TD></TR></TBODY></TABLE>
<P>(2) 거래수량 및 거래금액</P>
<TABLE><TBODY>
<TR><TH>거래개시일(결제일)</TH><TH>거래종료일(결제일)</TH><TH>거래기간</TH><TH>거래방법</TH></TR>
<TR><TD>2026년 12월 21일</TD><TD>2026년 12월 31일</TD><TD>11</TD><TD>시간외매매(-)</TD><TD>보통주</TD><TD>-5,763,531</TD><TD>4,975</TD><TD>28,673,566,725</TD><TD>-</TD></TR>
</TBODY></TABLE>
<P>① 특정증권등의 수 : 주권인 경우 그 주식의 수</P>
</DOCUMENT>"""


def zipped(xml, rcept):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(rcept + ".xml", xml.encode("utf-8"))
    return buf.getvalue()


def filing(no, day, sym, name, report, corp_code, cls="Y", filer="홍길동"):
    return {"corp_cls": cls, "corp_name": name, "corp_code": corp_code, "stock_code": sym, "report_nm": report,
            "rcept_no": no, "flr_nm": filer, "rcept_dt": day, "rm": ""}


ROWS = [filing("20261118000151", "20261118", "005930", "가상전자", "임원ㆍ주요주주특정증권등소유상황보고서", "00000001"),
        filing("20261118000152", "20261118", "005930", "가상전자", "연결재무제표기준영업(잠정)실적(공정공시)", "00000001"),
        filing("20261120000525", "20261120", "777770", "가상화학", "임원ㆍ주요주주특정증권등거래계획보고서", "00000002",
               cls="K", filer="가상홀딩스"),
        filing("20261120000600", "20261120", "", "비상장", "임원ㆍ주요주주특정증권등소유상황보고서", "00000003", cls="E")]
DOCS = {"20261118000151": HOLD_XML, "20261120000525": PLAN_XML}
CODES = {"005930": {"corp_code": "00000001", "name": "가상전자"}, "777770": {"corp_code": "00000002", "name": "가상화학"}}


class FakeDart(object):
    def __init__(self):
        self.calls = []

    def __call__(self, url):
        q = parse_qs(urlparse(url).query)
        self.calls.append(q)
        if "corp_code" in q:
            rows = [r for r in ROWS if r["corp_code"] == q["corp_code"][0]
                    and q["bgn_de"][0] <= r["rcept_dt"] <= q["end_de"][0]]
        else:
            rows = [r for r in ROWS if r["rcept_dt"] == q["bgn_de"][0]]
        if not rows:
            return json.dumps({"status": "013", "message": "조회된 데이타가 없습니다."})
        return json.dumps({"status": "000", "total_page": 1, "list": rows})


def fake_doc(url):
    rc = parse_qs(urlparse(url).query)["rcept_no"][0]
    return zipped(DOCS[rc], rc)


class Parse(unittest.TestCase):
    def test_kind(self):
        self.assertEqual(ins.kind_of("임원ㆍ주요주주특정증권등소유상황보고서"), "소유상황")
        self.assertEqual(ins.kind_of("[기재정정]임원ㆍ주요주주 특정증권등 거래계획보고서"), "거래계획")
        self.assertEqual(ins.kind_of("임원ㆍ주요주주특정증권등거래계획보고서(철회)"), "거래계획 철회")
        self.assertIsNone(ins.kind_of("주식등의대량보유상황보고서(일반)"))

    def test_numbers_and_dates(self):
        self.assertEqual(ins.to_int("14,428,256"), 14428256)
        self.assertEqual(ins.to_int("-2,000"), -2000)
        self.assertEqual(ins.to_int("(1,234)"), 1234)
        self.assertIsNone(ins.to_int("-"))
        self.assertEqual(ins.to_date("2026년 11월 3일"), "2026-11-03")
        self.assertEqual(ins.to_date("2026.11.03"), "2026-11-03")
        self.assertIsNone(ins.to_date("-"))

    def test_holding(self):
        text = dartdoc.read(zipped(HOLD_XML, "20261118000151"))[0][1]
        h = ins.parse_holding(text)
        self.assertEqual(h["type"], "변동")
        self.assertEqual(h["relation"], "임원 비등기임원 상무")
        self.assertEqual([(r["reason"], r["change"], r["after"], r["price"]) for r in h["rows"]],
                         [("장내매도(-)", -1206, 3794, 197664), ("증여(-)", -155, 3639, 188700),
                          ("장내매수(+)", 286, 286, 173800)])
        self.assertEqual(h["rows"][1]["plan_date"], "2026-10-13")
        rec = ins.summarize(dict(h, kind="소유상황", date="2026-11-18"))
        self.assertEqual(rec["net"], {"보통주": -1361, "우선주": 286})
        self.assertEqual(rec["after"], {"보통주": 3639, "우선주": 286})
        self.assertEqual(rec["market_rows"], 2)                          # 증여는 매매가 아니다
        self.assertEqual(rec["market_amount"], -1206 * 197664 + 286 * 173800)
        self.assertEqual(rec["last_change"], "2026-11-16")
        self.assertEqual(rec["weekdays_to_filing"], 2)

    def test_plan(self):
        text = dartdoc.read(zipped(PLAN_XML, "20261120000525"))[0][1]
        p = ins.parse_plan(text)
        self.assertEqual(p["purpose"], "지분 매각을 통한 유동성 확보")
        self.assertEqual(p["relation"], "주요주주 10%이상주주")
        self.assertEqual(p["rows"], [{"start": "2026-12-21", "end": "2026-12-31", "days": 11, "method": "시간외매매(-)",
                                      "kind": "보통주", "qty": -5763531, "price": 4975, "amount": 28673566725}])

    def test_weekdays(self):
        self.assertEqual(ins.weekdays_between("2026-10-30", "2026-11-09"), 6)      # 금 -> 다음다음 월
        self.assertEqual(ins.weekdays_between("2026-11-20", "2026-11-20"), 0)


class Run(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.root = os.path.join(self.d, "trader-kr")
        os.makedirs(self.root)
        with open(os.path.join(self.root, "journal.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"symbol": "005930", "exit_price": None}) + "\n")
        self.fake = FakeDart()
        os.environ["DART_API_KEY"] = "x" * 40

    def tearDown(self):
        os.environ.pop("DART_API_KEY", None)
        shutil.rmtree(self.d, ignore_errors=True)

    def run_ins(self, *args, codes=CODES):
        out, err = io.StringIO(), io.StringIO()
        base = ["--now", "2026-11-21 09:00", "--state", os.path.join(self.d, "state.json"),
                "--cache-dir", os.path.join(self.d, "cache"), "--doc-cache", os.path.join(self.d, "docs"),
                "--root", self.root] if (not args or args[0] != "show") else ["--doc-cache", os.path.join(self.d, "docs")]
        with redirect_stdout(out), redirect_stderr(err):
            code = ins.main(list(args) + base, transport=self.fake, doc_transport=fake_doc, codes=codes)
        return code, out.getvalue(), err.getvalue()

    def test_watched_reads_and_remembers(self):
        code, out, _ = self.run_ins("--symbols", "005930", "777770", "--days", "7")
        self.assertEqual(code, 0)
        self.assertIn("새 보고 2건 (거래계획 1, 소유상황 1)", out)
        self.assertIn("[소유상황] 005930 가상전자 2026-11-18 접수, 보고자 홍길동 (유가, 임원 비등기임원 상무, 변동, 보유 중)", out)
        self.assertIn("장내매도(-) 보통주 -1,206주 @197,664원, 결제·변동일 2026-11-13", out)
        self.assertIn("증여(-) 보통주 -155주 @188,700원, 결제·변동일 2026-11-16, 거래계획 2026-10-13 보고분", out)
        self.assertIn("합계 보통주 -1,361주, 우선주 +286주. 장내 매매 금액 -1.89억원 (2줄)", out)
        self.assertIn("계획: 시간외매매(-) 보통주 5,763,531주 @4,975원 (약 286.74억원), 2026-12-21 ~ 2026-12-31 (11일)", out)
        self.assertIn("목적: 지분 매각을 통한 유동성 확보", out)
        self.assertNotIn("잠정", out)                                     # 두 보고만
        for need in ("시행령 제200조 ④", "시행령 제200조의3 ⑥", "미공개 정보가 아닙니다", "not financial advice"):
            self.assertIn(need, out)
        self.assertNotIn("900101", out)
        self.assertEqual(sorted(q["corp_code"][0] for q in self.fake.calls), ["00000001", "00000002"])
        code, out, _ = self.run_ins("--symbols", "005930", "777770", "--days", "7")
        self.assertEqual(code, 1)
        self.assertIn("새 보고가 없습니다", out)

    def test_watch_files_missing_code_and_no_read(self):
        code, out, _ = self.run_ins("--symbols", "123450", "005930", "--no-read", "--dry-run", "--days", "7")
        self.assertEqual(code, 0)
        self.assertIn("회사 고유번호를 찾지 못한 종목: 123450", out)
        self.assertNotIn("장내매도", out)
        code, out, _ = self.run_ins("--dry-run", "--days", "7")                    # 일지의 열린 거래에서
        self.assertIn("관심 종목 1개", out)

    def test_market_plans(self):
        code, out, _ = self.run_ins("market", "--since", "2026-11-18", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("시장 전체 (유가·코스닥)", out)
        self.assertIn("새 보고 2건", out)
        self.assertNotIn("비상장", out)
        self.assertNotIn("계획:", out)                                    # market 은 기본으로 원문을 안 읽는다
        code, out, _ = self.run_ins("market", "--since", "2026-11-18", "--plans", "--read", "--json")
        doc = json.loads(out)
        self.assertEqual([x["kind"] for x in doc["items"]], ["거래계획"])
        self.assertEqual(doc["items"][0]["rows"][0]["qty"], -5763531)

    def test_show(self):
        code, out, _ = self.run_ins("show", "20261118000151")
        self.assertEqual(code, 0)
        self.assertIn("[소유상황] 005930 가상전자 2026-11-18 접수, 보고자 홍길동", out)
        self.assertIn("마지막 변동일 2026-11-16 부터 접수까지 평일 2일", out)

    def test_errors(self):
        code, _, err = self.run_ins("--symbols", "12")
        self.assertEqual(code, 2)
        self.assertIn("6자리", err)
        os.environ.pop("DART_API_KEY")
        code, _, err = self.run_ins("--symbols", "005930")
        self.assertEqual(code, 2)
        self.assertIn("채팅에 붙여 넣지 않습니다", err)


class Watch(unittest.TestCase):
    def test_window_default_and_limits(self):
        now = datetime.datetime(2026, 11, 23, 8, 0)
        self.assertEqual(watch.window(now)[0], datetime.date(2026, 11, 20))
        self.assertEqual(len(watch.window(now, days=90)), 90)


if __name__ == "__main__":
    unittest.main()
