#!/usr/bin/env python3
"""실적 요약 (trader-kr-earnings). 표준 라이브러리만 쓴다.

원본 (미국판 trader-analyst) 은 SEC XBRL 로 분기 실적을 읽었다. 국내판은 회사가 DART 에 낸 분기·반기·사업보고서의
전체 재무제표 (fnlttSinglAcntAll) 를 읽는다. 연결재무제표가 있으면 연결, 없으면 별도.

    python earnings.py 005930                          # 최근 8분기
    python earnings.py 005930 000660 --quarters 12 --json
    python earnings.py                                 # 관심 종목 (스캐너 결과, 계획, 일지의 열린 거래)

분기 값: 1·2·3분기는 그 분기보고서의 3개월 값 (thstrm_amount), 전년 같은 분기는 같은 보고서의 전기 값
(frmtrm_q_amount, 회사가 다시 작성한 기준). 4분기는 분기보고서가 없어서 사업보고서 연간 값에서 3분기 누적
(thstrm_add_amount) 을 뺀 계산값이고, 표에 * 로 적는다. 계산한 4분기 EPS 는 주식 수가 바뀌면 어긋나는 어림이다.

다음 정기보고서의 법정 기한: 분기·반기보고서는 그 기간이 끝난 뒤 45일 이내 (자본시장법 제160조 ①), 사업보고서는
사업연도가 끝난 뒤 90일 이내 (제159조 ①). 말일이 토요일이나 공휴일이면 다음 날로 미뤄진다 (민법 제161조). 이
스크립트는 토·일만 미루고 공휴일은 모른다. 실제 발표일은 아니고, 잠정실적 공시는 더 일찍 나올 수 있다.

캐시: trader-kr/cache/dart/fsq/<고유번호>_<연도>_<보고서>.json. 키: DART_API_KEY (값은 찍지 않는다).
"""

import argparse
import datetime
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
from krcommon import dart, dartdoc, financials as fin, krx, watch  # noqa: E402

CACHE = os.path.join("trader-kr", "cache", "dart", "fsq")
REPORTS = (("11013", 1, "1분기보고서"), ("11012", 2, "반기보고서"), ("11014", 3, "3분기보고서"), ("11011", 4, "사업보고서"))
FIELDS = ("revenue", "gross_profit", "op", "ni", "ni_parent", "eps")
QUARTER_END = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
NOTE_Q4 = "* 4분기는 사업보고서 연간 값에서 3분기 누적을 뺀 계산값입니다. 계산한 4분기 EPS 는 어림입니다."
NOTE_ONEOFF = "순이익이 영업이익보다 큽니다: 영업 밖 이익(금융, 지분법, 자산 처분 같은 일회성)이 들어 있습니다."
SAFETY = "교육과 도구일 뿐 투자 권유가 아닙니다 (not financial advice)."


class EarningsError(Exception):
    pass


# ---------------------------------------------------------------- 받기

def report(corp, year, code, key, cache=CACHE, now_kst=None, transport=None, sleep=time.sleep, refresh=False):
    """그 해 그 보고서의 전체 재무제표 {"fs_div", "rcept_no", "list"}. 없으면 None.
    받은 것은 캐시하고, '없음' 은 두 해 이상 지난 것만 캐시한다 (아직 낼 수 있는 보고서는 다시 묻는다)."""
    now_kst = now_kst or krx.kst_now()
    path = os.path.join(cache, "%s_%d_%s.json" % (corp, year, code)) if cache else None
    if path and not refresh:
        doc = fin._read(path)
        if doc is not None:
            return doc.get("data")
    got = None
    for fs in ("CFS", "OFS"):
        res = fin._json(fin._get("fnlttSinglAcntAll.json", {"corp_code": corp, "bsns_year": year, "reprt_code": code,
                                                            "fs_div": fs}, key, transport))
        st = str(res.get("status"))
        if st == "000" and res.get("list"):
            L = res["list"]
            got = {"fs_div": fs, "rcept_no": L[0].get("rcept_no"),
                   "list": [r for r in L if r.get("sj_div") in ("IS", "CIS")]}
            break
        if st != "013":
            raise EarningsError("DART 재무제표 응답 %s: %s" % (st, res.get("message") or ""))
        if sleep:
            sleep(0.1)
    if path and (got is not None or year < now_kst.year - 1):
        fin._write(path, {"data": got})
    return got


def values(doc, col):
    return {f: fin.pick(doc["list"], f, col) for f in FIELDS}


def _minus(a, b):
    return {f: (a[f] - b[f]) if a[f] is not None and b[f] is not None else None for f in FIELDS}


def quarters(docs):
    """docs[(연도, 보고서 코드)] -> 분기 목록 (최신 먼저). 4분기는 사업보고서 - 3분기 누적."""
    out, notes = [], []
    for y in sorted({y for y, _ in docs}):
        for code, q, name in REPORTS:
            d = docs.get((y, code))
            if not d:
                continue
            base = {"year": y, "q": q, "report": name, "rcept_no": d["rcept_no"], "fs_div": d["fs_div"],
                    "filed": "%s-%s-%s" % (d["rcept_no"][:4], d["rcept_no"][4:6], d["rcept_no"][6:8])}
            if q < 4:
                out.append(dict(base, derived=False, cur=values(d, "thstrm_amount"), prev=values(d, "frmtrm_q_amount")))
                continue
            d3 = docs.get((y, "11014"))
            if not d3:
                notes.append("%d 4분기: 3분기보고서가 없어 계산하지 못했습니다" % y)
                continue
            if d3["fs_div"] != d["fs_div"]:
                notes.append("%d 4분기: 사업보고서(%s)와 3분기보고서(%s)의 기준이 달라 계산하지 않았습니다" % (
                    y, d["fs_div"], d3["fs_div"]))
                continue
            out.append(dict(base, derived=True, cur=_minus(values(d, "thstrm_amount"), values(d3, "thstrm_add_amount")),
                            prev=_minus(values(d, "frmtrm_amount"), values(d3, "frmtrm_add_amount"))))
    out.sort(key=lambda x: (x["year"], x["q"]), reverse=True)
    return out, notes


# ---------------------------------------------------------------- 계산

def growth(cur, prev):
    """(비율 또는 None, 말). 부호가 바뀌면 비율 대신 전환이라고 말한다."""
    if cur is None or prev is None:
        return None, None
    if prev > 0 and cur < 0:
        return None, "적자 전환"
    if prev < 0 and cur > 0:
        return None, "흑자 전환"
    if prev < 0 and cur < 0:
        return None, "적자 지속"
    if prev == 0:
        return None, None
    return (cur - prev) / abs(prev), None


def ratio(a, b):
    return a / b if a is not None and b else None


def consecutive(qs):
    for a, b in zip(qs, qs[1:]):
        if (a["year"] * 4 + a["q"]) - (b["year"] * 4 + b["q"]) != 1:
            return False
    return True


def ttm(qs):
    """최근 4분기 합. 연속한 4분기가 없으면 None."""
    last4 = qs[:4]
    if len(last4) < 4 or not consecutive(last4):
        return None
    out = {}
    for f in ("revenue", "op", "ni", "ni_parent", "eps"):
        vals = [q["cur"][f] for q in last4]
        out[f] = sum(vals) if all(v is not None for v in vals) else None
    out["from"] = "%d %dQ" % (last4[-1]["year"], last4[-1]["q"])
    out["to"] = "%d %dQ" % (last4[0]["year"], last4[0]["q"])
    return out


def next_deadline(latest, acc_mt):
    """가장 최근 분기 다음 정기보고서와 법정 기한 (12월 결산만). 말일이 토·일이면 다음 월요일."""
    if acc_mt != "12" or not latest:
        return None
    y, q = (latest["year"], latest["q"] + 1) if latest["q"] < 4 else (latest["year"] + 1, 1)
    m, d = QUARTER_END[q]
    end = datetime.date(y, m, d)
    due = end + datetime.timedelta(days=90 if q == 4 else 45)
    while due.weekday() >= 5:
        due += datetime.timedelta(days=1)
    return {"year": y, "q": q, "report": dict((qq, nm) for _, qq, nm in REPORTS)[q], "period_end": end.isoformat(),
            "due": due.isoformat(), "days": 90 if q == 4 else 45}


def provisional(rows, after):
    """정기보고서 접수일 뒤에 나온 잠정실적·손익구조변경 공시."""
    out = []
    for r in rows:
        n = "".join(str(r.get("report_nm") or "").split())
        if (("잠정" in n and "실적" in n) or "손익구조" in n) and str(r.get("rcept_dt") or "") > after.replace("-", ""):
            out.append({"rcept_no": r.get("rcept_no"), "report": " ".join(str(r.get("report_nm")).split()),
                        "date": "%s-%s-%s" % (r["rcept_dt"][:4], r["rcept_dt"][4:6], r["rcept_dt"][6:8]),
                        "url": dartdoc.viewer_url(r.get("rcept_no"))})
    return sorted(out, key=lambda x: x["rcept_no"], reverse=True)


# ---------------------------------------------------------------- 회사 하나

def analyze(symbol, key, now, n_quarters=8, codes=None, transport=None, list_transport=None, cache=CACHE,
            refresh=False, sleep=time.sleep):
    base = os.path.dirname(os.path.normpath(cache)) if cache else fin.CACHE      # 고유번호·기업개황 캐시
    codes = codes if codes is not None else fin.corp_codes(key, cache=base, now_kst=now, transport=transport)
    c = codes.get(symbol)
    if not c:
        raise EarningsError("DART 에 %s 상장사가 없습니다" % symbol)
    corp = c["corp_code"]
    info = fin.company(corp, key, cache=base, transport=transport)
    docs = {}
    years = range(now.year - (n_quarters + 3) // 4 - 1, now.year + 1)
    for y in years:
        for code, _, _ in REPORTS:
            d = report(corp, y, code, key, cache, now, transport, sleep, refresh)
            if d:
                docs[(y, code)] = d
    qs, notes = quarters(docs)
    shown = qs[:n_quarters]
    latest = shown[0] if shown else None
    res = {"symbol": symbol, "name": info.get("name") or c.get("name"), "corp_code": corp, "acc_mt": info.get("acc_mt"),
           "quarters": shown, "ttm": ttm(qs), "notes": notes, "next": next_deadline(latest, info.get("acc_mt"))}
    if latest:
        res["provisional"] = provisional(dart.corp_list(corp, datetime.date.fromisoformat(latest["filed"]), now.date(),
                                                        key, list_transport), latest["filed"])
        cur = latest["cur"]
        res["oneoff"] = cur["ni"] is not None and cur["op"] is not None and cur["ni"] > cur["op"] > 0
    return res


# ---------------------------------------------------------------- 출력

def unit_of(qs):
    big = max([abs(q["cur"]["revenue"] or 0) for q in qs] + [abs(q["cur"]["op"] or 0) for q in qs] + [0])
    return (1e12, "조", 2) if big >= 1e12 else (1e8, "억", 0)


def money(x, unit):
    if x is None:
        return "-"
    div, name, digits = unit
    return ("%s%s" % (format(round(x / div, digits), ",.%df" % digits), name))


def pct(x, digits=1):
    return "-" if x is None else "%+.*f%%" % (digits, x * 100)


def yoy(cur, prev):
    g, word = growth(cur, prev)
    return word or pct(g)


def row(vals, cols):
    """한글 폭을 세어 맞춘 한 줄. 첫 칸은 왼쪽, 나머지는 오른쪽 정렬."""
    out = []
    for v, (_, w, right) in zip(vals, cols):
        gap = " " * max(0, w - krx.text_width(v))
        out.append(gap + v if right else v + gap)
    return "  " + " ".join(out)


def text_company(r):
    qs = r["quarters"]
    L = ["%s %s (%s%s)" % (r["symbol"], r["name"] or "", "연결" if (qs and qs[0]["fs_div"] == "CFS") else "별도",
                           ", %s월 결산" % r["acc_mt"] if r.get("acc_mt") else "")]
    if not qs:
        L.append("  DART 에 분기 재무제표가 없습니다 (2015년 이후 보고서만 API 로 나옵니다).")
        return L
    unit = unit_of(qs)
    L.append("  최근 보고서: %d %s, %s 접수 (접수번호 %s)" % (qs[0]["year"], qs[0]["report"], qs[0]["filed"], qs[0]["rcept_no"]))
    cols = (("분기", 9, False), ("매출", 10, True), ("전년비", 9, True), ("영업이익", 10, True), ("전년비", 9, True),
            ("영업률", 7, True), ("순이익", 10, True), ("전년비", 9, True), ("EPS", 9, True))
    L.append(row([c[0] for c in cols], cols))
    for q in qs:
        c, p = q["cur"], q["prev"]
        ni = c["ni_parent"] if c["ni_parent"] is not None else c["ni"]
        ni_prev = p["ni_parent"] if c["ni_parent"] is not None else p["ni"]
        opm = ratio(c["op"], c["revenue"])
        L.append(row(["%d %dQ%s" % (q["year"], q["q"], "*" if q["derived"] else ""), money(c["revenue"], unit),
                      yoy(c["revenue"], p["revenue"]), money(c["op"], unit), yoy(c["op"], p["op"]),
                      "-" if opm is None else "%.1f%%" % (opm * 100), money(ni, unit), yoy(ni, ni_prev),
                      "-" if c["eps"] is None else format(int(round(c["eps"])), ",")], cols))
    q0 = qs[0]["cur"]
    gm = ratio(q0["gross_profit"], q0["revenue"])
    L.append("  최근 분기 이익률: 매출총이익률 %s, 영업이익률 %s, 순이익률 %s%s" % (
        "-" if gm is None else "%.1f%%" % (gm * 100),
        "-" if ratio(q0["op"], q0["revenue"]) is None else "%.1f%%" % (ratio(q0["op"], q0["revenue"]) * 100),
        "-" if ratio(q0["ni"], q0["revenue"]) is None else "%.1f%%" % (ratio(q0["ni"], q0["revenue"]) * 100),
        " (매출 줄이 없는 회사: 금융사 등. 이익률을 내지 않습니다)" if q0["revenue"] is None
        else " (매출총이익 줄이 없는 회사)" if gm is None else ""))
    t = r.get("ttm")
    if t:
        L.append("  최근 4분기 합 (%s ~ %s): 매출 %s, 영업이익 %s, 순이익 %s, EPS %s원" % (
            t["from"], t["to"], money(t["revenue"], unit), money(t["op"], unit),
            money(t["ni_parent"] if t["ni_parent"] is not None else t["ni"], unit),
            "-" if t["eps"] is None else format(int(round(t["eps"])), ",")))
    if r.get("oneoff"):
        L.append("  ! " + NOTE_ONEOFF)
    if any(q["derived"] for q in qs):
        L.append("  " + NOTE_Q4)
    for nt in r.get("notes") or []:
        L.append("  참고: " + nt)
    for p in r.get("provisional") or []:
        L.append("  ! %s 에 정기보고서보다 새 공시가 있습니다: %s %s  접수번호 %s (news.py read 로 읽으세요)" % (
            p["date"], p["report"], p["url"], p["rcept_no"]))
    nx = r.get("next")
    if nx:
        L.append("  다음 정기보고서: %d %s, 법정 기한 %s (기간 끝 %s 뒤 %d일, 토·일이면 다음 평일. 공휴일은 반영 안 함). "
                 "실제 발표일이 아니고 잠정실적은 더 일찍 나올 수 있습니다." % (
                     nx["year"], nx["report"], nx["due"], nx["period_end"], nx["days"]))
    elif r.get("acc_mt") and r["acc_mt"] != "12":
        L.append("  결산월이 %s월이라 분기 이름은 사업연도 기준이고, 다음 보고서 기한은 계산하지 않았습니다." % r["acc_mt"])
    return L


# ---------------------------------------------------------------- 명령

def main(argv=None, transport=None, list_transport=None, codes=None, sleep=time.sleep):
    krx.utf8_stdio()
    ap = argparse.ArgumentParser(description="DART 분기·반기·사업보고서로 실적 요약 (trader-kr-earnings).")
    ap.add_argument("symbols", nargs="*", help="종목코드 (없으면 trader-kr/ 의 관심 종목)")
    ap.add_argument("--quarters", type=int, default=8, help="보일 분기 수 (기본 8, 최대 20)")
    ap.add_argument("--root", default="trader-kr")
    ap.add_argument("--cache", default=CACHE)
    ap.add_argument("--refresh", action="store_true", help="캐시를 무시하고 다시 받는다 (정정 보고서가 나왔을 때)")
    ap.add_argument("--now", help='이 한국 시간인 것처럼, 예: "2026-11-20 08:30"')
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        if not 1 <= a.quarters <= 20:
            raise EarningsError("--quarters 는 1 ~ 20 입니다")
        key = dart.api_key()
        if not key:
            raise EarningsError("DART_API_KEY 가 없습니다. 터미널에서 python krcommon/keys.py dart 로 등록하세요 "
                                "(키를 채팅에 붙여 넣지 않습니다)")
        if a.symbols:
            syms = [watch._sym(x) for x in a.symbols]
            if not all(syms):
                raise EarningsError("종목코드는 숫자 6자리입니다: %s" % " ".join(a.symbols))
        else:
            syms = [s for s, _, _ in watch.watched(a.root)]
            if not syms:
                raise EarningsError("종목을 주세요 (예: earnings.py 005930) 또는 /trader-kr-scanner 를 먼저 돌리세요")
        now = krx.parse_now(a.now)
        out = [analyze(s, key, now, a.quarters, codes, transport, list_transport, a.cache, a.refresh, sleep)
               for s in syms]
    except (EarningsError, fin.FinError, dart.DartError, krx.KrxError) as e:
        print("오류: %s" % e, file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        L = ["실적 요약: %s 기준, DART 분기·반기·사업보고서 (회사가 낸 숫자)." % now.strftime("%Y-%m-%d %H:%M")]
        for r in out:
            L.append("")
            L.extend(text_company(r))
        L.append("")
        L.append("전년비는 같은 보고서에 적힌 전년 같은 분기와 견준 것입니다. 순이익은 지배주주 몫이 있으면 그것입니다.")
        L.append(SAFETY)
        print("\n".join(L))
    return 0


if __name__ == "__main__":
    sys.exit(main())
