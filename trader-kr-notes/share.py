#!/usr/bin/env python3
"""공유용 공부 노트 자료 묶기 (trader-kr-notes/share.py). 공개 자료만 담는다.

    python trader-kr-notes/notes.py share

먼저 돌아 있어야 하는 것 (2일 안, 공부 자료는 1일 안):
  notes.py markets  -> trader-kr/notes/markets/out/{markets,domestic,basis,report}.json, raw/screener.json
  notes.py study    -> trader-kr/notes/study/out/{study_firms,study_years,study_check}.json
새로 받는 것: Yahoo 수정 종가 QUAL·IVV (해마다 수익률 비교).
넣지 않는 것: 개인 연구, 규칙이 고른 종목, 토스증권 자료.
결과: trader-kr/notes/share/{markets,etfs,firms,years,qual_years,basis}.json 과 report.json.
하나라도 빠지거나 오래됐으면 report.json 에 ok:false, 종료 코드 2 (자료 파일은 그대로 둔다).
"""
import datetime as dt
import io
import json
import os
import sys
import time
import urllib.request

NOTES = os.path.join("trader-kr", "notes")
MK, STUDY, SHARE = os.path.join(NOTES, "markets"), os.path.join(NOTES, "study"), os.path.join(NOTES, "share")
UA = {"User-Agent": "Mozilla/5.0"}
ETF_CODES = ["360750", "379800", "449180", "133690", "069500", "294400"]   # 국내 상장 S&P500·나스닥100·코스피200
problems = []


def load(*p):
    try:
        return json.load(io.open(os.path.join(*p), encoding="utf-8"))
    except Exception as e:
        problems.append("읽기 실패 %s (%s)" % (os.path.join(*p), e))
        return None


def age_ok(rep, name, days, now):
    if not rep or not rep.get("run_at"):
        problems.append("%s 실행 기록 없음" % name)
        return
    age = now - dt.datetime.strptime(rep["run_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    if age > dt.timedelta(days=days):
        problems.append("%s 결과가 %s 에 만든 것이라 오래됨 (먼저 다시 돌릴 것)" % (name, rep["run_at"]))


def yahoo_year_ends(sym, raw_dir):
    p = os.path.join(raw_dir, "yh_%s.json" % sym)
    if not (os.path.exists(p) and time.time() - os.path.getmtime(p) < 12 * 3600):
        try:
            raw = urllib.request.urlopen(urllib.request.Request(
                "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=15y&interval=1d" % sym, headers=UA), timeout=60).read()
            open(p, "wb").write(raw)
        except Exception as e:
            problems.append("Yahoo %s 받기 실패 (%s)" % (sym, e))
            return None
    c = json.load(open(p, encoding="utf-8"))["chart"]["result"][0]
    return year_ends(c["timestamp"], c["indicators"]["adjclose"][0]["adjclose"])


def year_ends(timestamps, adj):
    """{연도: 그해 마지막 수정 종가} (미국 동부 날짜 기준)."""
    ye = {}
    for a, q in zip(timestamps, adj):
        if q:
            ye[dt.datetime.fromtimestamp(a - 5 * 3600, dt.timezone.utc).year] = q
    return ye


def yearly_vs(qy, iy, this_year):
    out = []
    for y in range(2014, this_year):
        if all(k in qy and k in iy for k in (y, y - 1)):
            q, i = qy[y] / qy[y - 1] - 1, iy[y] / iy[y - 1] - 1
            out.append({"year": str(y), "qual": round(q * 100, 1), "sp500": round(i * 100, 1), "diff": round((q - i) * 100, 1)})
    return out


def main(argv=None):
    os.makedirs(os.path.join(SHARE, "raw"), exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    rep = {"run_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "problems": problems}
    mrep, mk, dom, mbasis = load(MK, "out", "report.json"), load(MK, "out", "markets.json"), load(MK, "out", "domestic.json"), load(MK, "out", "basis.json")
    scr = load(MK, "raw", "screener.json")
    firms, years, chk = load(STUDY, "out", "study_firms.json"), load(STUDY, "out", "study_years.json"), load(STUDY, "out", "study_check.json")
    if problems:
        return stop(rep)
    age_ok(mrep, "notes.py markets", 2, now)
    age_ok(chk, "notes.py study", 1, now)
    if not mrep.get("ok"):
        problems.append("notes.py markets 마지막 실행이 ok 가 아님")
    if chk.get("counts", {}).get("check_mismatch") or chk.get("problems"):
        problems.append("study_check 에 불일치나 문제가 남아 있음")

    by_t = {r["ticker"]: r for r in mk}
    markets = []
    for t, label, corr_key in (("IVV", "미국 (S&P500)", "corr_korea"), ("EWY", "한국", "corr_us")):
        r = by_t.get(t)
        if not r:
            problems.append("markets 에 %s 없음" % t)
            continue
        markets.append({"market": label, "ticker": t, "pe": r["pe"], "pb": r["pb"], "div_yield": r["div_yield"],
                        "ytd_usd": r["ytd_usd"], "ytd_krw": r["ytd_krw"], "y5_krw": r["y5_krw"], "y10_krw": r["y10_krw"],
                        "corr_other": r[corr_key], "fee": r["fee"]})
    by_c = {r["code"]: r for r in dom}
    etfs = [{k: by_c[c][k] for k in ("market", "name", "code", "fee_pct", "aum_eok", "ytd_nav_pct", "y1_nav_pct", "asof")}
            for c in ETF_CODES if c in by_c]
    if len(etfs) != len(ETF_CODES):
        problems.append("국내 ETF %d / %d 개" % (len(etfs), len(ETF_CODES)))

    qy, iy = yahoo_year_ends("QUAL", os.path.join(SHARE, "raw")), yahoo_year_ends("IVV", os.path.join(SHARE, "raw"))
    if problems or not (qy and iy):
        return stop(rep)
    qual = yearly_vs(qy, iy, dt.date.today().year)
    s = {r.get("localExchangeTicker"): r for r in scr.values()}
    g = lambda r, k: (r.get(k) or {}).get("r") if isinstance(r.get(k), dict) else r.get(k)
    q, i = s.get("QUAL"), s.get("IVV")
    if not (q and i):
        problems.append("iShares 목록에 QUAL 또는 IVV 없음")
        return stop(rep)
    mb = {r["item"]: r["value"] for r in mbasis}
    asof = str(g(q, "navAnnualisedAsOf"))
    restated = sorted(set(y["name"] for y in years if y.get("restated")))
    basis = [
        {"item": "갱신일", "value": dt.date.today().isoformat()},
        {"item": "주가 기준일", "value": max(f["price_date"] for f in firms)},
        {"item": "원·달러 작년 말", "value": mb.get("원·달러 작년 말")},
        {"item": "원·달러 최근", "value": mb.get("원·달러 최근")},
        {"item": "원화 가치 변화 (달러 대비, %)", "value": mb.get("원화 변화 (달러 대비, %)")},
        {"item": "시장 PER 기준일", "value": mb.get("PER 기준일")},
        {"item": "올해 수익률 기준일", "value": mb.get("올해 수익률 기준일")},
        {"item": "국내 ETF 기준일", "value": mb.get("국내 ETF 기준일")},
        {"item": "QUAL 10년 연평균 (%)", "value": round(g(q, "navTenYearAnnualized"), 1)},
        {"item": "S&P500 (IVV) 10년 연평균 (%)", "value": round(g(i, "navTenYearAnnualized"), 1)},
        {"item": "10년 수익률 기준일", "value": "%s-%s-%s" % (asof[:4], asof[4:6], asof[6:])},
        {"item": "QUAL 이긴 해 수", "value": sum(1 for r in qual if r["diff"] > 0)},
        {"item": "비교한 해 수", "value": len(qual)},
        {"item": "비교 첫 해", "value": qual[0]["year"] if qual else None},
        {"item": "비교 끝 해", "value": qual[-1]["year"] if qual else None},
        {"item": "고쳐 쓴 연도가 있는 회사 수", "value": len(restated)},
        {"item": "국내 시가총액 순위 기준일 (KRX)", "value": max((f.get("krx_day") or "") for f in firms if f["market"] == "한국") or None},
    ]
    counts = {"markets": len(markets), "etfs": len(etfs), "firms": len(firms), "years": len(years), "qual_years": len(qual)}
    rep["counts"] = counts
    if counts["markets"] != 2 or counts["years"] != counts["firms"] * 5 or counts["qual_years"] < 10:
        problems.append("건수 이상 %s" % counts)
    if problems:
        return stop(rep)
    keep_firm = ("market", "code", "name", "rule", "krx_day", "price", "price_date", "currency", "ttm_eps", "per", "fy_eps", "per_fy",
                 "ttm_ni", "equity_q", "q_label", "roe", "debt_ratio", "op_margin", "last_fy", "fy_link", "q_link")
    keep_year = ("market", "code", "name", "fy", "period_end", "basis", "revenue", "op", "ni", "assets", "liabilities", "equity",
                 "ocf", "capex", "eps", "source_report", "restated", "restated_items", "orig_revenue", "orig_op", "orig_ni", "link", "orig_link")
    data = {"markets": markets, "etfs": etfs,
            "firms": [{k: f.get(k) for k in keep_firm} for f in firms],
            "years": [dict({k: y.get(k) for k in keep_year}, key="%s|%s" % (y["code"], y["fy"])) for y in years],
            "qual_years": qual, "basis": basis}
    for k, v in data.items():
        io.open(os.path.join(SHARE, k + ".json"), "w", encoding="utf-8").write(json.dumps(v, ensure_ascii=False, indent=1))
    rep["ok"] = True
    json.dump(rep, io.open(os.path.join(SHARE, "report.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(counts, ensure_ascii=False), "고쳐 쓴 연도가 있는 회사:", ", ".join(restated) or "없음")
    return 0


def stop(rep):
    rep["ok"] = False
    os.makedirs(SHARE, exist_ok=True)
    json.dump(rep, io.open(os.path.join(SHARE, "report.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("멈춤:", file=sys.stderr)
    for p in problems:
        print(" - " + p, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
