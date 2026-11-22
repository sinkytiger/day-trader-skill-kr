"""세계 시장 비교 노트 자료 (trader-kr-notes/markets.py). 표준 라이브러리만 쓴다.

    python trader-kr-notes/notes.py markets   # 받아서 trader-kr/notes/markets/out/ 에 markets, commodities, domestic, basis 를 쓴다

받는 곳: iShares 펀드 목록(수익률·보수·배당)과 상품 페이지(PER·PBR), Yahoo 시세(원·달러,
ETF, 원자재 선물), 네이버 증권 ETF 분석. 하나라도 못 읽으면 out/ 을 건드리지 않고
out/report.json 에 빠진 것을 적은 뒤 종료 코드 2 로 멈춘다. 대시보드에 올리는 일은
이 스크립트가 하지 않는다.
"""
import datetime as dt
import html
import io
import json
import math
import os
import re
import sys
import time
import urllib.request

BASE = os.path.join("trader-kr", "notes", "markets")   # 프로젝트 폴더 기준 (저장소에 올리지 않는 trader-kr/)
RAW = os.path.join(BASE, "raw")
OUT = os.path.join(BASE, "out")
UA = {"User-Agent": "Mozilla/5.0"}

MARKETS = [
    ("미국 S&P500", "기준", "IVV", "TIGER·KODEX 미국S&P500"),
    ("전세계", "기준", "ACWI", "선진국만: KODEX MSCI선진국"),
    ("신흥국 전체", "기준", "EEM", "PLUS 신흥국MSCI(합성 H)"),
    ("한국", "반도체", "EWY", "KODEX 200 등"),
    ("대만", "반도체", "EWT", "없음"),
    ("일본", "아시아", "EWJ", "TIGER 일본니케이225 등"),
    ("인도", "아시아", "INDA", "KODEX 인도Nifty50 등"),
    ("태국", "아시아", "THD", "없음"),
    ("유럽 전체", "유럽", "IEUR", "유로존 대형주만: RISE 유로스탁스50(H)"),
    ("영국", "유럽", "EWU", "없음"),
    ("스페인", "유럽 은행", "EWP", "없음"),
    ("이탈리아", "유럽 은행", "EWI", "없음"),
    ("폴란드", "유럽 은행", "EPOL", "없음"),
    ("유럽 금융", "유럽 은행", "EUFN", "없음"),
    ("노르웨이", "원자재", "ENOR", "없음"),
    ("페루", "원자재", "EPU", "없음"),
    ("남아공", "원자재", "EZA", "없음"),
    ("호주", "원자재", "EWA", "없음"),
    ("캐나다", "원자재", "EWC", "없음"),
    ("멕시코", "중남미", "EWW", "ACE MSCI멕시코(합성)"),
    ("브라질", "신흥국 기타", "EWZ", "중남미 전체: TIGER 라틴35"),
    ("인도네시아", "신흥국 기타", "EIDO", "ACE MSCI인도네시아(합성)"),
    ("사우디", "신흥국 기타", "KSA", "없음"),
    ("중국", "신흥국 기타", "MCHI", "본토 A주: KODEX 차이나CSI300"),
]
COMMODITIES = [("BZ=F", "브렌트유", "달러/배럴"), ("CL=F", "WTI", "달러/배럴"), ("HG=F", "구리", "달러/파운드"),
               ("GC=F", "금", "달러/온스"), ("SI=F", "은", "달러/온스")]
DOMESTIC = [("미국 S&P500", "360750"), ("미국 S&P500", "379800"), ("미국 S&P500 (환헤지)", "449180"), ("미국 나스닥100", "133690"),
            ("한국 코스피200", "069500"), ("한국 코스피200", "294400"), ("인도", "453810"), ("일본", "241180"), ("일본", "101280"),
            ("중국", "283580"), ("유럽", "379790"), ("베트남", "245710"), ("신흥국", "195980"), ("선진국", "251350"),
            ("멕시코", "291130"), ("인도네시아", "256440"), ("중남미", "105010"), ("독일", "411860"), ("금", "411060"),
            ("금 (환헤지)", "132030"), ("구리", "160580"), ("원유 (환헤지)", "261220")]

problems = []


def get(url, path, timeout=60, tries=3):
    last = None
    for i in range(tries):
        try:
            raw = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read()
            with open(path, "wb") as f:
                f.write(raw)
            return raw
        except Exception as e:  # 네트워크 오류는 몇 번 다시 시도한 뒤 문제로 적는다
            last = e
            time.sleep(2 + 2 * i)
    problems.append("받기 실패 %s (%s)" % (url, last))
    return None


def yahoo(sym, adj=True, tz=0):
    raw = get("https://query1.finance.yahoo.com/v8/finance/chart/%s?range=11y&interval=1d" % urllib.request.quote(sym),
              os.path.join(RAW, "yh_%s.json" % re.sub(r"[^A-Za-z0-9]", "_", sym)))
    if raw is None:
        return None
    try:
        d = json.loads(raw.decode("utf-8"))["chart"]["result"][0]
        a = d["indicators"].get("adjclose", [{}])[0].get("adjclose") if adj else None
        c = a or d["indicators"]["quote"][0]["close"]
        rows = [(dt.datetime.fromtimestamp(t + tz, dt.timezone.utc).strftime("%Y-%m-%d"), v)
                for t, v in zip(d["timestamp"], c) if v is not None]
    except Exception as e:
        problems.append("Yahoo %s 읽기 실패 (%s)" % (sym, e))
        return None
    if not rows:
        problems.append("Yahoo %s 시세 0건" % sym)
        return None
    return rows


def near(rows, day):
    r = [x for x in rows if x[0] <= day]
    if not r:
        raise ValueError("%s 이전 시세 없음" % day)
    return r[-1]


def shift_years(day, n):
    d = dt.date.fromisoformat(day)
    try:
        return d.replace(year=d.year - n).isoformat()
    except ValueError:
        return d.replace(year=d.year - n, day=28).isoformat()


def monthly(rows):
    m = {}
    for d, v in rows:
        m[d[:7]] = v
    ks = sorted(m)
    return {ks[i]: m[ks[i]] / m[ks[i - 1]] - 1 for i in range(1, len(ks))}


def corr(a, b, lo, hi):
    ks = [x for x in a if x in b and lo <= x <= hi]
    if len(ks) < 55:
        raise ValueError("겹치는 달 %d개" % len(ks))
    xa = [a[x] for x in ks]
    xb = [b[x] for x in ks]
    ma, mb = sum(xa) / len(xa), sum(xb) / len(xb)
    return round(sum((p - ma) * (q - mb) for p, q in zip(xa, xb)) / math.sqrt(sum((p - ma) ** 2 for p in xa) * sum((q - mb) ** 2 for q in xb)), 2)


def us_date(s):
    return dt.datetime.strptime(s, "%b %d, %Y").date().isoformat()


def ishares_ratio(page, key):
    # 상품 페이지는 값을 JSON 조각으로 품고 있다. 따옴표가 &quot; 로 바뀌어 올 때가 있어 먼저 푼다.
    text = html.unescape(page)
    for block in re.findall(r'"%s":\{([^{}]*)\}' % key, text):
        v = re.search(r'"formattedValue":"([0-9.,]+)"', block)
        d = re.search(r'"formattedAsOfDate":"([A-Z][a-z]{2} \d{2}, \d{4})"', block)
        if v and d:
            return float(v.group(1).replace(",", "")), us_date(d.group(1))
    return None


def eok(s):
    if not s:
        return None
    s = s.replace(",", "")
    jo = re.search(r"([\d.]+)조", s)
    ok = re.search(r"([\d.]+)억", s)
    if not jo and not ok:
        return None
    return (float(jo.group(1)) * 10000 if jo else 0) + (float(ok.group(1)) if ok else 0)


def main():
    os.makedirs(RAW, exist_ok=True)
    os.makedirs(OUT, exist_ok=True)
    today = dt.date.today()
    report = {"run_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "counts": {}, "problems": problems}

    # 1. iShares 펀드 목록
    raw = get("https://www.ishares.com/us/product-screener/product-screener-v3.1.jsn?dcrPath=/templatedata/config/product-screener-v3/data/en/us-ishares/ishares-product-screener-backend-config&siteEntryPassthrough=true",
              os.path.join(RAW, "screener.json"), timeout=120)
    scr = {}
    if raw:
        try:
            scr = {r.get("localExchangeTicker"): r for r in json.loads(raw.decode("utf-8")).values()}
        except Exception as e:
            problems.append("iShares 펀드 목록 읽기 실패 (%s)" % e)

    def g(r, k):
        v = r.get(k)
        return v.get("r") if isinstance(v, dict) else None

    # 2. 상품 페이지 PER·PBR
    ratios = {}
    for name, grp, t, dom in MARKETS:
        r = scr.get(t)
        if not r or not r.get("productPageUrl"):
            problems.append("iShares 목록에 %s 없음" % t)
            continue
        page = get("https://www.ishares.com" + r["productPageUrl"], os.path.join(RAW, "page_%s.html" % t))
        time.sleep(0.5)
        if page is None:
            continue
        text = page.decode("utf-8", "replace")
        pe, pb = ishares_ratio(text, "priceEarnings"), ishares_ratio(text, "priceBook")
        if not pe or not pb:
            problems.append("%s 상품 페이지에서 PER·PBR 못 읽음" % t)
            continue
        ratios[t] = (pe, pb)

    # 3. Yahoo
    krw = yahoo("KRW=X")
    ys = {t: yahoo(t, tz=-5 * 3600) for _, _, t, _ in MARKETS}
    ys["SPY"] = yahoo("SPY", tz=-5 * 3600)
    cms = {sym: yahoo(sym, adj=False, tz=-5 * 3600) for sym, _, _ in COMMODITIES}
    for k, rows in list(ys.items()) + list(cms.items()) + [("KRW=X", krw)]:
        if rows and (today - dt.date.fromisoformat(rows[-1][0])).days > 10:
            problems.append("Yahoo %s 마지막 시세가 %s 로 오래됨" % (k, rows[-1][0]))

    # 4. 네이버 국내 ETF
    domestic = []
    for market, code in DOMESTIC:
        raw = get("https://m.stock.naver.com/api/stock/%s/etfAnalysis" % code, os.path.join(RAW, "nv_%s.json" % code))
        time.sleep(0.35)
        if raw is None:
            continue
        try:
            a = json.loads(raw.decode("utf-8"))
            nav = {x["periodTypeCode"]: x["value"] for x in a.get("navPerformanceList", [])}
            row = {"market": market, "name": a["itemName"], "code": code, "fee_pct": a.get("totalFee"), "aum_eok": eok(a.get("totalNav")),
                   "ytd_nav_pct": nav.get("YTD"), "y1_nav_pct": nav.get("Y1"), "asof": a.get("navPerformanceReferenceDate")}
        except Exception as e:
            problems.append("네이버 %s 읽기 실패 (%s)" % (code, e))
            continue
        if None in (row["fee_pct"], row["aum_eok"], row["ytd_nav_pct"], row["asof"]):
            problems.append("네이버 %s 값 빠짐" % code)
            continue
        domestic.append(row)

    if problems or not krw:
        return stop(report)

    # 5. 시장 표
    markets = []
    try:
        mon_kr, mon_us = monthly(ys["EWY"]), monthly(ys["SPY"])
        first = today.replace(day=1)
        hi_m = (first - dt.timedelta(days=1)).strftime("%Y-%m")
        y, mth = int(hi_m[:4]), int(hi_m[5:])
        lo_idx = y * 12 + mth - 1 - 59
        lo_m = "%04d-%02d" % (lo_idx // 12, lo_idx % 12 + 1)
        dates = {}
        for name, grp, t, dom in MARKETS:
            r = scr[t]
            ytd, y5, y10 = g(r, "dailyPerformanceYearToDate"), g(r, "navFiveYearAnnualized"), g(r, "navTenYearAnnualized")
            if None in (ytd, y5, y10, g(r, "fees"), g(r, "twelveMonTrlYield"), g(r, "totalNetAssets")):
                problems.append("%s 수익률·보수·배당 값 빠짐" % t)
                continue
            ytd_at = str(r["navAmountAsOf"]["r"])
            ytd_at = "%s-%s-%s" % (ytd_at[:4], ytd_at[4:6], ytd_at[6:])
            ann_at = str(r["navAnnualisedAsOf"]["r"])
            ann_at = "%s-%s-%s" % (ann_at[:4], ann_at[4:6], ann_at[6:])
            dates.setdefault("ytd", set()).add(ytd_at)
            dates.setdefault("ann", set()).add(ann_at)
            dates.setdefault("pe", set()).add(ratios[t][0][1])
            fx_now, fx_ye = near(krw, ytd_at)[1], near(krw, "%d-12-31" % (int(ytd_at[:4]) - 1))[1]
            fx_end, fx_5, fx_10 = near(krw, ann_at)[1], near(krw, shift_years(ann_at, 5))[1], near(krw, shift_years(ann_at, 10))[1]
            m = monthly(ys[t])
            markets.append({"market": name, "group": grp, "ticker": t, "pe": ratios[t][0][0], "pb": ratios[t][1][0],
                            "div_yield": round(g(r, "twelveMonTrlYield"), 2),
                            "ytd_usd": round(ytd, 1), "ytd_krw": round(((1 + ytd / 100) * fx_now / fx_ye - 1) * 100, 1),
                            "y5_usd": round(y5, 1), "y10_usd": round(y10, 1),
                            "y5_krw": round(((1 + y5 / 100) * (fx_end / fx_5) ** (1 / 5) - 1) * 100, 1),
                            "y10_krw": round(((1 + y10 / 100) * (fx_end / fx_10) ** (1 / 10) - 1) * 100, 1),
                            "corr_korea": None if t == "EWY" else corr(m, mon_kr, lo_m, hi_m),
                            "corr_us": None if t == "IVV" else corr(m, mon_us, lo_m, hi_m),
                            "fee": g(r, "fees"), "aum_usd_bn": round(g(r, "totalNetAssets") / 1e9, 2), "domestic_etf": dom})
    except Exception as e:
        problems.append("시장 표 계산 실패 (%s)" % e)
    if problems:
        return stop(report)

    # 6. 원자재 표
    commodities = []
    try:
        for sym, name, unit in COMMODITIES:
            s = cms[sym]
            last = s[-1]
            ye = near(s, "%d-12-31" % (int(last[0][:4]) - 1))
            wk = near(s, (dt.date.fromisoformat(last[0]) - dt.timedelta(days=7)).isoformat())
            hi = max([x for x in s if x[0] >= last[0][:4] + "-01-01"], key=lambda x: x[1])
            d10, p10 = near(s, shift_years(last[0], 10))
            yrs = (dt.date.fromisoformat(last[0]) - dt.date.fromisoformat(d10)).days / 365.25
            fx_last, fx_ye = near(krw, last[0])[1], near(krw, ye[0])[1]
            commodities.append({"commodity": name, "unit": unit, "price": round(last[1], 2), "date": last[0],
                                "ytd_usd": round((last[1] / ye[1] - 1) * 100, 1),
                                "ytd_krw": round((last[1] * fx_last / (ye[1] * fx_ye) - 1) * 100, 1),
                                "wk_change": round((last[1] / wk[1] - 1) * 100, 1), "high_ytd": round(hi[1], 2), "high_date": hi[0],
                                "from_high": round((last[1] / hi[1] - 1) * 100, 1), "y10_cagr": round(((last[1] / p10) ** (1 / yrs) - 1) * 100, 1)})
            dates.setdefault("cm", set()).add(last[0])
    except Exception as e:
        problems.append("원자재 표 계산 실패 (%s)" % e)
    if problems:
        return stop(report)

    # 7. 기준 표
    ytd_at = max(dates["ytd"])
    fx_ye = near(krw, "%d-12-31" % (int(ytd_at[:4]) - 1))
    fx_now = near(krw, ytd_at)
    basis = [
        {"item": "갱신일", "value": today.isoformat()},
        {"item": "원·달러 작년 말", "value": round(fx_ye[1], 1)},
        {"item": "원·달러 최근", "value": round(fx_now[1], 1)},
        {"item": "원·달러 최근 날짜", "value": fx_now[0]},
        {"item": "원화 변화 (달러 대비, %)", "value": round((fx_ye[1] / fx_now[1] - 1) * 100, 1)},
        {"item": "PER 기준일", "value": max(dates["pe"])},
        {"item": "올해 수익률 기준일", "value": ytd_at},
        {"item": "5·10년 기준일", "value": max(dates["ann"])},
        {"item": "국내 ETF 기준일", "value": max(r["asof"] for r in domestic).replace(".", "-")},
        {"item": "원자재 기준일", "value": max(dates["cm"])},
    ]

    report["counts"] = {"markets": len(markets), "markets_expected": len(MARKETS), "pe_read": len(ratios),
                        "corr_korea": sum(1 for r in markets if r["corr_korea"] is not None), "corr_us": sum(1 for r in markets if r["corr_us"] is not None),
                        "commodities": len(commodities), "commodities_expected": len(COMMODITIES),
                        "domestic": len(domestic), "domestic_expected": len(DOMESTIC),
                        "dates": {k: sorted(v) for k, v in dates.items()}}
    if len(markets) != len(MARKETS) or len(commodities) != len(COMMODITIES) or len(domestic) != len(DOMESTIC):
        problems.append("건수가 모자람: %s" % json.dumps(report["counts"], ensure_ascii=False))
        return stop(report)

    for name, data in [("markets", markets), ("commodities", commodities), ("domestic", domestic), ("basis", basis)]:
        with io.open(os.path.join(OUT, name + ".json"), "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=1))
    with io.open(os.path.join(OUT, "message.txt"), "w", encoding="utf-8") as f:
        f.write(message(markets, commodities, basis) + "\n")
    report["ok"] = True
    write_report(report)
    print(json.dumps(report["counts"], ensure_ascii=False))
    return 0


def message(markets, commodities, basis):
    """텔레그램용 짧은 글. 숫자는 위에서 쓴 파일 값 그대로. 대시보드에 올린 뒤에만 보낸다."""
    b = {r["item"]: r["value"] for r in basis}
    md = lambda s: "%d/%d" % (int(s[5:7]), int(s[8:10]))
    chg = b["원화 변화 (달러 대비, %)"]
    rank = sorted(markets, key=lambda r: -r["ytd_krw"])
    fmt = lambda rs: ", ".join("%s %+.1f%%" % (r["market"], r["ytd_krw"]) for r in rs)
    return "\n".join([
        "[세계 시장 비교 노트] 갱신일 %s" % b["갱신일"],
        "원·달러 %s원 (%s), 작년 말보다 원화 %.1f%% %s" % (format(b["원·달러 최근"], ",.1f"), md(b["원·달러 최근 날짜"]), abs(chg), "강세" if chg >= 0 else "약세"),
        "올해 원화 기준 수익률 (%s 기준)" % md(b["올해 수익률 기준일"]),
        "상위: " + fmt(rank[:3]),
        "하위: " + fmt(rank[-3:][::-1]),
        "원자재 지난 1주 (%s 기준): " % md(b["원자재 기준일"]) + ", ".join("%s %+.1f%%" % (c["commodity"], c["wk_change"]) for c in commodities),
    ])


def write_report(report):
    with io.open(os.path.join(OUT, "report.json"), "w", encoding="utf-8") as f:
        f.write(json.dumps(report, ensure_ascii=False, indent=1))


def stop(report):
    report["ok"] = False
    write_report(report)
    print("멈춤: 못 읽은 것이 있어 out/ 의 자료를 바꾸지 않았습니다.", file=sys.stderr)
    for p in problems:
        print(" - " + p, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
