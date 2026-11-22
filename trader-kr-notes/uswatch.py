#!/usr/bin/env python3
"""미국 관심 종목 주간 점검. 숫자만 모으고 의견은 내지 않는다.

    python trader-kr-notes/notes.py us-watch      # 종목은 trader-kr/notes/us-watchlist.json

종목마다 (1) 종가와 지난 1주·올해·52주 고점 대비 변화 (Yahoo 수정 종가 기준), (2) 지난 7일 SEC 새 공시
(10-K, 10-Q, 8-K 항목, 위임장, Form 4 건수), (3) 최근 4개 분기 주당순이익(SEC 공시 원자료)으로 계산한 PER 을 모은다.

하나라도 못 읽으면 trader-kr/notes/us-watch/out/report.json 에 ok:false 와 빠진 것을 적고 종료 코드 2 로 멈춘다 (message.txt 는 쓰지 않는다).
다 읽으면 같은 폴더에 watch.json, message.txt(텔레그램용 글), report.json 을 쓰고, 지난주 값은 last.json 에 남긴다.
"""
import datetime as dt
import io
import json
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join("trader-kr", "notes", "us-watch")   # 프로젝트 폴더 기준
RAW, OUT = os.path.join(BASE, "raw"), os.path.join(BASE, "out")
WATCHLIST = os.path.join("trader-kr", "notes", "us-watchlist.json")
SEC_UA = {"User-Agent": "trader-kr-research/1.0", "Accept-Encoding": "identity"}
YH_UA = {"User-Agent": "Mozilla/5.0"}
ITEMS = {"1.01": "중요 계약", "1.02": "계약 종료", "1.05": "사이버보안 사고", "2.01": "인수·처분 완료", "2.02": "실적 발표",
         "2.03": "채무 발생", "2.05": "구조조정 비용", "2.06": "손상", "3.01": "상장 관련", "3.02": "미등록 주식 발행",
         "3.03": "주주 권리 변경", "4.01": "감사인 변경", "4.02": "재무제표 신뢰 불가", "5.01": "지배권 변경",
         "5.02": "임원·이사 변동", "5.03": "정관 변경", "5.07": "주총 결과", "7.01": "공정공시", "8.01": "기타 사건",
         "9.01": "첨부 자료"}
problems = []


def get(url, ua, name, max_age_h=6):
    p = os.path.join(RAW, name)
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < max_age_h * 3600:
        return open(p, "rb").read()
    last = None
    for i in range(3):
        try:
            raw = urllib.request.urlopen(urllib.request.Request(url, headers=ua), timeout=120).read()
            open(p, "wb").write(raw)
            time.sleep(0.25)
            return raw
        except Exception as e:
            last = e
            time.sleep(2 + 2 * i)
    problems.append("받기 실패 %s (%s)" % (url, last))
    return None


def d(s):
    return dt.date.fromisoformat(s)


def per_period(facts):
    per = {}
    for x in facts:
        k = (x.get("start"), x["end"])
        if k not in per or x.get("filed", "") > per[k].get("filed", ""):
            per[k] = x
    return list(per.values())


def ttm(facts):
    """최근 연간 + 그 뒤 누적 분기 - 1년 전 같은 누적 분기. (값, 기간 끝, 'TTM'|'FY')"""
    dur = [dict(x, days=(d(x["end"]) - d(x["start"])).days) for x in facts if x.get("start")]
    ann = [x for x in dur if 340 <= x["days"] <= 380]
    if not ann:
        return None
    a = max(ann, key=lambda x: x["end"])
    ytd = [x for x in dur if 80 <= x["days"] <= 290 and x["end"] > a["end"] and abs((d(x["start"]) - d(a["end"])).days - 1) <= 7]
    if not ytd:
        return a["val"], a["end"], "FY"
    y = max(ytd, key=lambda x: x["end"])
    prior = [x for x in dur if abs((d(y["end"]) - d(x["end"])).days - 365) <= 8 and abs(x["days"] - y["days"]) <= 8]
    if not prior:
        return None
    p = min(prior, key=lambda x: abs((d(y["end"]) - d(x["end"])).days - 365))
    return a["val"] + y["val"] - p["val"], y["end"], "TTM"


def eps_facts(ciks, t):
    """us-gaap:EarningsPerShareDiluted (주식 종류 구분 없는 값). 여러 CIK 를 합친다."""
    xs = []
    for i, c in enumerate(ciks):
        raw = get("https://data.sec.gov/api/xbrl/companyfacts/CIK%s.json" % c, SEC_UA, "cf_%s_%d.json" % (t, i), max_age_h=20)
        if raw is None:
            return None
        f = json.loads(raw)
        xs += f["facts"].get("us-gaap", {}).get("EarningsPerShareDiluted", {}).get("units", {}).get("USD/shares", [])
    return per_period(xs)


def class_a_eps_from_filings(cik, t, subs):
    """주식 종류별로만 주당순이익을 내는 회사(비자): 최근 10-K 와 그 뒤 10-Q 원문(inline XBRL)에서 A주 희석 주당순이익을 읽는다."""
    r = subs["filings"]["recent"]
    idx = [i for i, f in enumerate(r["form"]) if f in ("10-K", "10-Q")]
    k = next((i for i in idx if r["form"][i] == "10-K"), None)
    if k is None:
        problems.append("%s 10-K 를 찾지 못함" % t)
        return None
    use = [k] + [i for i in idx if r["form"][i] == "10-Q" and r["filingDate"][i] > r["filingDate"][k]]
    facts = []
    for i in use:
        acc, doc = r["accessionNumber"][i].replace("-", ""), r["primaryDocument"][i]
        raw = get("https://www.sec.gov/Archives/edgar/data/%d/%s/%s" % (int(cik), acc, doc), SEC_UA, "%s_%s.htm" % (t, acc), max_age_h=24 * 30)
        if raw is None:
            return None
        page = raw.decode("utf-8", "replace")
        ctx = {}
        for m in re.finditer(r'<xbrli:context id="([^"]+)">(.*?)</xbrli:context>', page, re.S):
            body = m.group(2)
            mem = re.findall(r'<xbrldi:explicitMember dimension="([^"]+)">([^<]+)</xbrldi:explicitMember>', body)
            sd = re.search(r"<xbrli:startDate>([^<]+)</xbrli:startDate>", body)
            ed = re.search(r"<xbrli:endDate>([^<]+)</xbrli:endDate>", body)
            if sd and ed:
                ctx[m.group(1)] = (mem, sd.group(1), ed.group(1))
        n = 0
        for m in re.finditer(r'<ix:nonFraction([^>]*name="us-gaap:EarningsPerShareDiluted"[^>]*)>([^<]*)</ix:nonFraction>', page):
            attrs, txt = m.group(1), m.group(2)
            cref = re.search(r'contextRef="([^"]+)"', attrs)
            c = ctx.get(cref.group(1)) if cref else None
            if not c or c[0] != [("us-gaap:StatementClassOfStockAxis", "us-gaap:CommonClassAMember")]:
                continue
            v = float(txt.replace(",", ""))
            if 'sign="-"' in attrs:
                v = -v
            facts.append({"start": c[1], "end": c[2], "val": v, "filed": r["filingDate"][i]})
            n += 1
        if n == 0:
            problems.append("%s %s 원문에서 A주 주당순이익을 읽지 못함" % (t, r["form"][i]))
            return None
    return per_period(facts)


def price_stats(t):
    raw = get("https://query1.finance.yahoo.com/v8/finance/chart/%s?range=2y&interval=1d" % t, YH_UA, "yh_%s.json" % t, max_age_h=3)
    if raw is None:
        return None
    try:
        c = json.loads(raw)["chart"]["result"][0]
        pts = [(dt.datetime.fromtimestamp(a - 5 * 3600, dt.timezone.utc).date(), p, q)
               for a, p, q in zip(c["timestamp"], c["indicators"]["quote"][0]["close"], c["indicators"]["adjclose"][0]["adjclose"]) if p and q]
    except Exception as e:
        problems.append("%s 시세 읽기 실패 (%s)" % (t, e))
        return None
    ld, lp, la = pts[-1]

    def at(day):
        cand = [x for x in pts if x[0] <= day]
        return cand[-1] if cand else None
    wk = at(ld - dt.timedelta(days=7))
    ye = at(dt.date(ld.year - 1, 12, 31))
    yr = [x for x in pts if x[0] > ld - dt.timedelta(days=365)]
    hi = max(yr, key=lambda x: x[2])
    if not wk or not ye:
        problems.append("%s 1주 전·작년 말 시세 없음" % t)
        return None
    return {"close": round(lp, 2), "date": str(ld), "wk": round((la / wk[2] - 1) * 100, 1), "ytd": round((la / ye[2] - 1) * 100, 1),
            "from_high": round((la / hi[2] - 1) * 100, 1), "high_date": str(hi[0])}


def new_filings(subs_list, since, until):
    out, form4 = [], 0
    for subs in subs_list:
        r = subs["filings"]["recent"]
        for i, f in enumerate(r["form"]):
            fd = r["filingDate"][i]
            if not (since <= fd <= until):
                continue
            if f == "4":
                form4 += 1
            elif f in ("10-K", "10-Q", "8-K", "DEF 14A", "10-K/A", "10-Q/A", "8-K/A"):
                items = [x.strip() for x in (r.get("items", [""] * len(r["form"]))[i] or "").split(",") if x.strip()]
                out.append({"date": fd, "form": f, "items": [ITEMS.get(x, x) for x in items if x != "9.01"]})
    return sorted(out, key=lambda x: (x["date"], x["form"])), form4


def short(day):
    return "%d/%d" % (d(day).month, d(day).day)


def main():
    os.makedirs(RAW, exist_ok=True)
    os.makedirs(OUT, exist_ok=True)
    if not os.path.exists(WATCHLIST):
        print("관심 종목 파일 %s 이 없습니다. 예시를 복사해 고쳐 쓰세요: %s" % (WATCHLIST, os.path.join(HERE, "us-watchlist.example.json")),
              file=sys.stderr)
        return 2
    wl = json.load(open(WATCHLIST, encoding="utf-8"))
    tickers, names, extra = wl["tickers"], wl.get("names", {}), wl.get("extra_cik", {})
    today = dt.date.today()
    since, until = (today - dt.timedelta(days=6)).isoformat(), today.isoformat()
    report = {"run_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "problems": problems, "window": [since, until]}

    tick_raw = get("https://www.sec.gov/files/company_tickers.json", SEC_UA, "company_tickers.json", max_age_h=24 * 7)
    cikmap = {v["ticker"]: str(v["cik_str"]).zfill(10) for v in json.loads(tick_raw).values()} if tick_raw else {}
    rows = []
    for t in tickers:
        if t not in cikmap:
            problems.append("%s SEC 회사 번호를 찾지 못함" % t)
            continue
        ciks = [cikmap[t]] + extra.get(t, [])
        px = price_stats(t)
        subs_list = []
        for i, c in enumerate(ciks):
            raw = get("https://data.sec.gov/submissions/CIK%s.json" % c, SEC_UA, "sub_%s_%d.json" % (t, i), max_age_h=3)
            if raw is not None:
                subs_list.append(json.loads(raw))
        if len(subs_list) != len(ciks):
            continue
        facts = eps_facts(ciks, t)
        if facts is not None and not facts:  # 주식 종류별로만 내는 회사
            facts = class_a_eps_from_filings(cikmap[t], t, subs_list[0])
        e = ttm(facts) if facts else None
        if e is None:
            problems.append("%s 최근 4개 분기 주당순이익을 계산하지 못함" % t)
        elif (today - d(e[1])).days > 200:
            problems.append("%s 주당순이익 기간 끝 %s 이 너무 오래됨" % (t, e[1]))
        fl, f4 = new_filings(subs_list, since, until)
        if px is None or e is None:
            continue
        rows.append({"ticker": t, "name": names.get(t, t), **px, "eps_ttm": round(e[0], 2), "eps_end": e[1], "eps_basis": e[2],
                     "per": round(px["close"] / e[0], 1) if e[0] > 0 else None, "filings": fl, "form4": f4})

    report["counts"] = {"tickers": len(tickers), "rows": len(rows)}
    if problems or len(rows) != len(tickers):
        if len(rows) != len(tickers):
            problems.append("건수가 모자람: %d / %d" % (len(rows), len(tickers)))
        report["ok"] = False
        json.dump(report, io.open(os.path.join(OUT, "report.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("멈춤: 못 읽은 것이 있어 message.txt 를 쓰지 않았습니다.", file=sys.stderr)
        for p in problems:
            print(" - " + p, file=sys.stderr)
        return 2

    last_p = os.path.join(OUT, "last.json")
    last = json.load(open(last_p, encoding="utf-8")) if os.path.exists(last_p) else {}
    price_day = max(r["date"] for r in rows)
    lines = ["[미국 관심 종목 주간 점검] %s 종가 기준" % short(price_day),
             "지난 1주 / 올해 / 52주 종가 고점 대비 / PER (최근 4개 분기 이익)"]
    for r in rows:
        per = "PER %.1f" % r["per"] if r["per"] is not None else "적자"
        lines.append("%s %s달러  %+.1f%% / %+.1f%% / %+.1f%% / %s" % (r["name"], format(r["close"], ",.2f"), r["wk"], r["ytd"], r["from_high"], per))
    lines.append("")
    lines.append("이번 주 새 공시 (SEC, %s~%s)" % (short(since), short(until)))
    any_f = False
    for r in rows:
        if r["filings"]:
            any_f = True
            fs = ", ".join("%s%s %s" % (f["form"], " " + "·".join(f["items"]) if f["items"] else "", short(f["date"])) for f in r["filings"])
            results = any(f["form"] == "8-K" and "실적 발표" in f["items"] for f in r["filings"])
            report_filed = any(f["form"] in ("10-Q", "10-K", "10-Q/A", "10-K/A") for f in r["filings"])
            if results and not report_filed:
                fs += " (분기 보고서 공시 전이라 PER은 %s 분기까지 이익 기준)" % short(r["eps_end"])
            lines.append("- %s: %s" % (r["name"], fs))
    if not any_f:
        lines.append("- 없음")
    f4 = [(r["name"], r["form4"]) for r in rows if r["form4"]]
    if f4:
        lines.append("내부자 거래 신고(Form 4): " + ", ".join("%s %d건" % x for x in f4))
    changed = [r for r in rows if r["ticker"] in last and last[r["ticker"]].get("eps_end") != r["eps_end"]]
    if changed:
        lines.append("")
        lines.append("새 분기 실적이 반영된 회사")
        for r in changed:
            o = last[r["ticker"]]
            lines.append("- %s: %s → %s 분기까지, 주당순이익 %.2f → %.2f달러" % (r["name"], short(o["eps_end"]), short(r["eps_end"]), o["eps_ttm"], r["eps_ttm"]))
    lines.append("")
    lines.append("주가: Yahoo 수정 종가(배당 포함). 이익: SEC 공시 원자료.")
    msg = "\n".join(lines)

    json.dump(rows, io.open(os.path.join(OUT, "watch.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    io.open(os.path.join(OUT, "message.txt"), "w", encoding="utf-8").write(msg + "\n")
    json.dump({r["ticker"]: {"eps_end": r["eps_end"], "eps_ttm": r["eps_ttm"], "per": r["per"], "date": r["date"]} for r in rows},
              io.open(last_p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    report["ok"] = True
    report["counts"]["filings"] = sum(len(r["filings"]) for r in rows)
    report["counts"]["form4"] = sum(r["form4"] for r in rows)
    json.dump(report, io.open(os.path.join(OUT, "report.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(report["counts"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
