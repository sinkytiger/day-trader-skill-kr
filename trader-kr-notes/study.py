#!/usr/bin/env python3
"""「미장·국장 공부 노트」(공유판) 재무제표 공부 자료. 숫자만 모은다.

    python trader-kr-notes/notes.py study

예시 회사
  미국: trader-kr/notes/us-watchlist.json 의 종목 (예시 파일 trader-kr-notes/us-watchlist.example.json)
  국내 10곳: KRX 일별 자료의 가장 최근 날, 코스피 보통주 시가총액 순으로, DART 업종 코드 64~66(금융·보험·지주회사)을 뺀 위 10곳
자료
  미국: SEC companyfacts (us-watch/raw 에 받아 둔 것) 의 연간(10-K) 5년, 최근 4개 분기 주당순이익은 trader-kr/notes/us-watch/out/watch.json (notes.py us-watch 를 먼저 돌린다)
  국내: DART 사업보고서 전체 재무제표 (trader-kr/cache/dart, 없으면 받음) 5년, 최근 분기 보고서로 최근 4개 분기, 네이버 종가
점검 (하나라도 걸리면 종료 코드 2, 자료 파일을 바꾸지 않음)
  20곳 x 5년의 매출·순이익·자산·부채·자본·영업현금흐름이 모두 있는지, 자산 = 부채 + 자본 (0.5% 안)
  국내 2023~2025 매출·영업이익·순이익·지배주주순이익을 네이버 연간 재무 요약과 대조 (1억원 또는 0.2% 안)
  국내 PER·주당순이익을 네이버 값과 대조 (주당순이익 1% 안)
결과: trader-kr/notes/study/out/study_firms.json, study_years.json, study_check.json
"""
import datetime as dt
import io
import json
import os
import re
import sys
import time
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))   # 저장소 폴더
sys.path.insert(0, ROOT)
from krcommon import dart, financials as fin  # noqa: E402

HERE = os.path.join("trader-kr", "notes", "study")   # 프로젝트 폴더 기준
OUT, RAW = os.path.join(HERE, "out"), os.path.join(HERE, "raw")
WATCH = os.path.join("trader-kr", "notes", "us-watch")
UA = {"User-Agent": "Mozilla/5.0"}
FIN_PREFIX = ("64", "65", "66")
problems, checks = [], []


def d(s):
    return dt.date.fromisoformat(s)


def get(url, name, max_age_h=6):
    p = os.path.join(RAW, name)
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < max_age_h * 3600:
        return open(p, "rb").read()
    for i in range(3):
        try:
            raw = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60).read()
            open(p, "wb").write(raw)
            time.sleep(0.15)
            return raw
        except Exception as e:
            last = e
            time.sleep(2 + 2 * i)
    problems.append("받기 실패 %s (%s)" % (url, last))
    return None


def num(s):
    if s in (None, "", "-"):
        return None
    try:
        return float(str(s).replace(",", ""))
    except ValueError:  # 네이버의 'N/A' 등 (적자 회사의 PER)
        return None


def jo_eok(text):
    """'1,057조 5,918억' -> 원"""
    t = text.replace(",", "")
    jo = re.search(r"([\d.]+)조", t)
    ok = re.search(r"([\d.]+)억", t)
    return ((float(jo.group(1)) * 1e12) if jo else 0) + ((float(ok.group(1)) * 1e8) if ok else 0)


# ------------------------------------------------------------------ 국내

def kr_pick10(key):
    import importlib.util
    spec = importlib.util.spec_from_file_location("inv_ro", os.path.join(ROOT, "trader-kr-invest", "invest.py"))
    inv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inv)
    day, doc = dt.date.today(), None
    for _ in range(45):
        doc = inv.read_day(inv.STORE, day)
        if doc and doc.get("rows"):
            break
        day -= dt.timedelta(days=1)
    if not doc or not doc.get("rows"):
        raise SystemExit("멈춤: 최근 45일 안에 KRX 일별 자료가 없습니다 (python trader-kr-invest/invest.py data)")
    rows = sorted([r for r in doc["rows"] if r[1] == "K" and r[0].endswith("0") and r[5]], key=lambda r: -r[5])
    codes = fin.corp_codes(key)
    picked, skipped = [], []
    for r in rows:
        corp = (codes.get(r[0]) or {}).get("corp_code")
        if not corp:
            skipped.append((r[2], "고유번호 없음"))
            continue
        try:
            co = fin.company(corp, key)
        except fin.FinError as e:
            skipped.append((r[2], "기업개황 없음 (%s)" % e))
            continue
        if co.get("induty_code", "").startswith(FIN_PREFIX):
            skipped.append((r[2], "금융·지주 (업종 %s)" % co.get("induty_code")))
            continue
        if co.get("acc_mt") != "12":
            skipped.append((r[2], "12월 결산 아님"))
            continue
        picked.append({"code": r[0], "corp": corp, "name": r[2], "cap_krx": r[5], "krx_day": str(day), "induty": co.get("induty_code")})
        if len(picked) == 10:
            break
    return picked, skipped


def kr_quarter(corp, key, year):
    """가장 최근 분기 보고서 (3분기 > 반기 > 1분기) 전체 재무제표 list 와 보고서 코드."""
    for code, label in (("11014", "3분기"), ("11012", "반기"), ("11013", "1분기")):
        for fs in ("CFS", "OFS"):
            p = os.path.join(RAW, "dartq_%s_%d_%s_%s.json" % (corp, year, code, fs))
            if os.path.exists(p):
                res = json.load(open(p, encoding="utf-8"))
            else:
                res = fin._json(fin._get("fnlttSinglAcntAll.json", {"corp_code": corp, "bsns_year": year, "reprt_code": code, "fs_div": fs}, key, None))
                if str(res.get("status")) == "000":
                    json.dump(res, open(p, "w", encoding="utf-8"), ensure_ascii=False)
                time.sleep(0.15)
            if str(res.get("status")) == "000" and res.get("list"):
                return res["list"], label, fs
    return None, None, None


OCF_NAMES = ("영업활동순현금흐름합계", "영업활동순현금흐름", "영업활동으로인한순현금흐름", "영업활동으로부터의현금흐름",
             "영업활동으로인한현금흐름", "영업활동현금흐름")
EPS_PARTS = ("BasicEarningsLossPerShareFromContinuingOperations", "BasicEarningsLossPerShareFromDiscontinuedOperations")


def kr_eps(rows, col):
    """기본주당이익. 합계 줄이 없고 계속영업·중단영업으로만 나눠 공시하면 둘을 더한다 (보통주 표준 계정만)."""
    v = fin.pick(rows, "eps", col)
    if v is not None:
        return v, "합계"
    parts = {}
    for r in rows:
        if r.get("sj_div") in ("IS", "CIS") and fin.base_id(r.get("account_id")) in EPS_PARTS:
            x = num(r.get(col))
            if x is not None:
                parts.setdefault(fin.base_id(r.get("account_id")), x)
    if EPS_PARTS[0] in parts:
        return parts[EPS_PARTS[0]] + parts.get(EPS_PARTS[1], 0.0), "계속+중단영업"
    return None, None


def kr_row(rows, col):
    r = {k: fin.pick(rows, k, col) for k in ("revenue", "op", "ni_parent", "ni", "assets", "liabilities", "equity",
                                               "equity_parent", "ocf", "capex")}
    if r["ni_parent"] is None:
        r["ni_parent"] = r["ni"]
    if r["equity_parent"] is None:
        r["equity_parent"] = r["equity"]
    if r["capex"] is not None:
        r["capex"] = abs(r["capex"])
    if r["ocf"] is None:
        for x in rows:
            if x.get("sj_div") == "CF" and str(x.get("account_nm") or "").replace(" ", "") in OCF_NAMES and num(x.get(col)) is not None:
                r["ocf"] = num(x.get(col))
                break
    r["eps"], r["eps_how"] = kr_eps(rows, col)
    return r


def source_plan(years):
    """{연도: (읽을 사업보고서 연도, 칸)}. 그 연도를 당기·전기·전전기로 담은 보고서 중 가장 최근 것."""
    cols = ["thstrm_amount", "frmtrm_amount", "bfefrmtrm_amount"]
    last = years[-1]
    return {y: (min(last, y + 2), cols[min(last, y + 2) - y]) for y in years}


def kr_firm(f, key, years):
    """각 연도는 그 연도를 담은 가장 최근 사업보고서 값 (마지막 해 보고서의 당기·전기·전전기, 그 앞은 두 해 전 보고서의 전기·전전기).
    그 연도 자신의 보고서 값과 다르면 restated 로 표시한다 (분할·사업 매각 등으로 나중에 고쳐 쓴 숫자)."""
    out_years = []
    cache = fin.CACHE
    docs = {}
    for y in years:
        docs[y] = fin.statements(f["corp"], y, key, cache)
        if not docs[y]:
            problems.append("%s FY%d 사업보고서 재무제표 없음" % (f["name"], y))
    if any(v is None for v in docs.values()):
        return out_years, None
    plan = source_plan(years)
    for y in years:
        src, col = plan[y]
        r = kr_row(docs[src]["list"], col)
        orig = kr_row(docs[y]["list"], "thstrm_amount")
        diffs = [k for k in ("revenue", "op", "ni_parent", "assets")
                 if r[k] is not None and orig[k] is not None and abs(r[k] - orig[k]) > max(1e8, abs(orig[k]) * 0.002)]
        out_years.append({"market": "한국", "code": f["code"], "name": f["name"], "fy": "FY%d" % y, "period_end": "%d-12-31" % y,
                          "basis": "연결" if docs[src].get("fs_div") == "CFS" else "별도",
                          "revenue": r["revenue"], "op": r["op"], "ni": r["ni_parent"], "ni_total": r["ni"],
                          "assets": r["assets"], "liabilities": r["liabilities"], "equity": r["equity"], "equity_parent": r["equity_parent"],
                          "ocf": r["ocf"], "capex": r["capex"], "eps": r["eps"], "eps_how": r["eps_how"],
                          "source_report": "FY%d 사업보고서" % src, "restated": bool(diffs),
                          "restated_items": diffs, "orig_revenue": orig["revenue"], "orig_op": orig["op"], "orig_ni": orig["ni_parent"], "orig_ni_total": orig["ni"],
                          "link": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=%s" % docs[src].get("rcept_no"),
                          "orig_link": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=%s" % docs[y].get("rcept_no")})
    q, qlabel, qfs = kr_quarter(f["corp"], key, years[-1] + 1)
    if q is None:
        problems.append("%s %d년 분기 보고서 없음" % (f["name"], years[-1] + 1))
        return out_years, None
    last = out_years[-1] if out_years else {}
    ni_now = fin.pick(q, "ni_parent", "thstrm_add_amount")
    ni_prev = fin.pick(q, "ni_parent", "frmtrm_add_amount")
    eps_now = kr_eps(q, "thstrm_add_amount")[0]
    eps_prev = kr_eps(q, "frmtrm_add_amount")[0]
    eq_q = fin.pick(q, "equity_parent", "thstrm_amount") or fin.pick(q, "equity", "thstrm_amount")
    if None in (ni_now, ni_prev, eps_now, eps_prev, eq_q, last.get("ni"), last.get("eps")):
        problems.append("%s 최근 4개 분기 계산에 필요한 값 빠짐" % f["name"])
        return out_years, None
    cur = {"ttm_ni": last["ni"] + ni_now - ni_prev, "ttm_eps": last["eps"] + eps_now - eps_prev, "equity_q": eq_q,
           "q_label": "%d년 %s" % (years[-1] + 1, qlabel), "q_basis": "연결" if qfs == "CFS" else "별도",
           "q_link": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=%s" % q[0].get("rcept_no")}
    return out_years, cur


def naver_checks(f, yrs, cur):
    raw = get("https://m.stock.naver.com/api/stock/%s/basic" % f["code"], "nvb_%s.json" % f["code"], 3)
    integ = get("https://m.stock.naver.com/api/stock/%s/integration" % f["code"], "nvi_%s.json" % f["code"], 3)
    ann = get("https://m.stock.naver.com/api/stock/%s/finance/annual" % f["code"], "nva_%s.json" % f["code"], 24)
    if not (raw and integ and ann):
        return None
    b, it = json.loads(raw), json.loads(integ)
    info = {x.get("code"): x.get("value") for x in it.get("totalInfos", [])}
    price, day = num(b["closePrice"]), b["localTradedAt"][:10]
    nv_eps, nv_per = num(str(info.get("eps", "")).replace("원", "")), num(str(info.get("per", "")).replace("배", ""))
    cap = jo_eok(info.get("marketValue", "")) if info.get("marketValue") else None
    if nv_eps and cur:  # 네이버의 최근 4개 분기 기준 시점이 달라 참고로만 남긴다 (판정에 넣지 않음)
        checks.append({"what": "%s 최근 4개 분기 주당순이익 (참고)" % f["name"], "ours": round(cur["ttm_eps"]), "other": nv_eps,
                       "source": "네이버", "ok": True, "info_only": True, "diff_pct": round(abs(cur["ttm_eps"] / nv_eps - 1) * 100, 2)})
    fi = json.loads(ann).get("financeInfo", {})
    names = {"매출액": "revenue", "영업이익": "op", "당기순이익": "ni_total", "지배주주순이익": "ni"}
    by_fy = {y["fy"]: y for y in yrs}
    for row in fi.get("rowList", []):
        k = names.get(row.get("title"))
        if not k:
            continue
        for col, v in row.get("columns", {}).items():
            fy = "FY" + col[:4]
            if col[4:] != "12" or fy not in by_fy or num(v.get("value")) is None:
                continue
            ours = by_fy[fy][k]
            orig = by_fy[fy].get("orig_" + k)
            other = num(v["value"]) * 1e8
            same = lambda a: a is not None and (abs(a - other) <= 1.01e8 or (other and abs(a / other - 1) <= 0.002))
            ok = same(ours) or same(orig)
            checks.append({"what": "%s %s %s" % (f["name"], fy, row["title"]), "ours": None if ours is None else round(ours / 1e8),
                           "other": num(v["value"]), "source": "네이버 (억원)", "ok": ok,
                           "note": "" if same(ours) else ("네이버는 그 해 원래 보고서 값, 여기는 나중에 고쳐 쓴 값" if ok else "")})
    return {"price": price, "price_date": day, "cap": cap, "naver_per": nv_per, "naver_eps": nv_eps}


# ------------------------------------------------------------------ 미국

US_REV = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax"]
US_CAPEX = ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireOtherPropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
            "PaymentsToAcquirePropertyPlantAndEquipmentAndIntangibleAssets", "PaymentsForCapitalImprovements"]


def us_load(t, n_ciks):
    g = {}
    for i in range(n_ciks):
        p = os.path.join(WATCH, "raw", "cf_%s_%d.json" % (t, i))
        f = json.load(open(p, encoding="utf-8"))
        for tag, body in f["facts"].get("us-gaap", {}).items():
            for unit, xs in body.get("units", {}).items():
                g.setdefault(tag, {}).setdefault(unit, []).extend(xs)
    return g


def us_one_tag(g, tags, unit="USD"):
    best, end = None, ""
    for tag in tags:
        xs = g.get(tag, {}).get(unit, [])
        ann = [x for x in xs if x.get("form", "").startswith("10-K")]
        if ann:
            e = max(x["end"] for x in ann)
            if e > end:
                best, end = tag, e
    if not best:
        return None, {}
    per = {}
    for x in g[best][unit]:
        if not x.get("form", "").startswith("10-K"):
            continue
        k = (x.get("start"), x["end"])
        if k not in per or x["filed"] > per[k]["filed"]:
            per[k] = x
    return best, per


def us_annual(per):
    """{기간 끝: (값, accn)} 1년짜리 기간만."""
    return {e: (x["val"], x.get("accn")) for (s, e), x in per.items() if s and 340 <= (d(e) - d(s)).days <= 380}


def us_instant(per):
    return {e: (x["val"], x.get("accn")) for (s, e), x in per.items() if not s}


def us_all_forms(g, tags, unit="USD"):
    """10-K·10-Q 모두. 가장 늦게까지 있는 태그 하나, 기간마다 나중 공시가 이긴다."""
    best, end = None, ""
    for tag in tags:
        xs = g.get(tag, {}).get(unit, [])
        if xs and max(x["end"] for x in xs) > end:
            best, end = tag, max(x["end"] for x in xs)
    per = {}
    for x in (g.get(best, {}).get(unit, []) if best else []):
        k = (x.get("start"), x["end"])
        if k not in per or x["filed"] > per[k]["filed"]:
            per[k] = x
    return list(per.values())


def us_ttm(facts):
    dur = [dict(x, days=(d(x["end"]) - d(x["start"])).days) for x in facts if x.get("start")]
    ann = [x for x in dur if 340 <= x["days"] <= 380]
    if not ann:
        return None
    a = max(ann, key=lambda x: x["end"])
    ytd = [x for x in dur if 80 <= x["days"] <= 290 and x["end"] > a["end"] and abs((d(x["start"]) - d(a["end"])).days - 1) <= 7]
    if not ytd:
        return a["val"], a["end"]
    y = max(ytd, key=lambda x: x["end"])
    prior = [x for x in dur if abs((d(y["end"]) - d(x["end"])).days - 365) <= 8 and abs(x["days"] - y["days"]) <= 8]
    if not prior:
        return None
    p = min(prior, key=lambda x: abs((d(y["end"]) - d(x["end"])).days - 365))
    return a["val"] + y["val"] - p["val"], y["end"]


def us_ttm_roe(g, eps_end):
    """최근 4개 분기 순이익(지배주주)과 최근 분기말 자기자본. 기간 끝이 주당순이익과 같아야 쓴다."""
    ni = us_ttm(us_all_forms(g, ["NetIncomeLoss"]))
    if not ni or ni[1] != eps_end:
        pl, nci = us_ttm(us_all_forms(g, ["ProfitLoss"])), us_ttm(us_all_forms(g, ["NetIncomeLossAttributableToNoncontrollingInterest"]))
        ni = (pl[0] - nci[0], pl[1]) if pl and nci and pl[1] == nci[1] == eps_end else None
    eq = [x for x in us_all_forms(g, ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]) if not x.get("start")]
    eq = max(eq, key=lambda x: (x["end"], x["filed"])) if eq else None
    if not ni or not eq or eq["end"] != eps_end:
        return None
    return {"ttm_ni": ni[0], "equity_q": eq["val"], "equity_q_end": eq["end"]}


def us_firm(t, name, cik, n_ciks):
    g = us_load(t, n_ciks)
    _, rev = us_one_tag(g, US_REV)
    _, op = us_one_tag(g, ["OperatingIncomeLoss"])
    _, ni = us_one_tag(g, ["NetIncomeLoss"])
    _, pl = us_one_tag(g, ["ProfitLoss"])
    _, nci = us_one_tag(g, ["NetIncomeLossAttributableToNoncontrollingInterest"])
    _, assets = us_one_tag(g, ["Assets"])
    _, liab = us_one_tag(g, ["Liabilities"])
    _, le = us_one_tag(g, ["LiabilitiesAndStockholdersEquity"])
    _, eqp = us_one_tag(g, ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"])
    _, eqt = us_one_tag(g, ["StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", "StockholdersEquity"])
    _, ocf = us_one_tag(g, ["NetCashProvidedByUsedInOperatingActivities"])
    _, capex = us_one_tag(g, US_CAPEX)
    _, eps = us_one_tag(g, ["EarningsPerShareDiluted"], "USD/shares")
    R, OP, NI, PL, NCI = us_annual(rev), us_annual(op), us_annual(ni), us_annual(pl), us_annual(nci)
    A, L, LE, EP, ET = us_instant(assets), us_instant(liab), us_instant(le), us_instant(eqp), us_instant(eqt)
    CF, CX, EPS = us_annual(ocf), us_annual(capex), us_annual(eps)
    ends = sorted(R)[-5:]
    rows = []
    for e in ends:
        n = NI.get(e, (None,))[0]
        if n is None and e in PL and e in NCI:
            n = PL[e][0] - NCI[e][0]
        lia = L.get(e, (None,))[0]
        if lia is None and e in LE and e in ET:
            lia = LE[e][0] - ET[e][0]
        accn = R[e][1]
        rows.append({"market": "미국", "code": t, "name": name, "fy": "FY" + e[:4] + (" (%s월 결산)" % (d(e) - dt.timedelta(days=7)).month if (d(e) - dt.timedelta(days=7)).month != 12 else ""),
                     "period_end": e, "basis": "연결", "revenue": R[e][0], "op": OP.get(e, (None,))[0], "ni": n, "ni_total": None,
                     "assets": A.get(e, (None,))[0], "liabilities": lia, "equity": ET.get(e, (None,))[0], "equity_parent": EP.get(e, (None,))[0],
                     "ocf": CF.get(e, (None,))[0], "capex": CX.get(e, (None,))[0], "eps": EPS.get(e, (None,))[0],
                     "link": "https://www.sec.gov/Archives/edgar/data/%d/%s/" % (int(cik), (accn or "").replace("-", ""))})
    return rows, g


# ------------------------------------------------------------------ 본체

def main():
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(RAW, exist_ok=True)
    key = dart.api_key()
    if not key:
        print("멈춤: OpenDART 키가 없습니다 (python krcommon/keys.py dart)", file=sys.stderr)
        return 2
    firms, years = [], []
    picked, skipped = kr_pick10(key)
    fy_last = dt.date.today().year - 1
    kr_years = list(range(fy_last - 4, fy_last + 1))
    for f in picked:
        yrs, cur = kr_firm(f, key, kr_years)
        nv = naver_checks(f, yrs, cur)
        years += yrs
        if not (cur and nv and yrs):
            problems.append("%s 현재 지표를 만들지 못함" % f["name"])
            continue
        last = yrs[-1]
        firms.append({"market": "한국", "code": f["code"], "name": f["name"], "rule": "코스피 시가총액 상위 (금융·지주 제외)", "krx_day": f["krx_day"],
                      "price": nv["price"], "price_date": nv["price_date"], "currency": "원",
                      "ttm_ni": cur["ttm_ni"], "ttm_eps": round(cur["ttm_eps"], 1), "per": round(nv["price"] / cur["ttm_eps"], 1) if cur["ttm_eps"] > 0 else None,
                      "fy_eps": last["eps"], "per_fy": round(nv["price"] / last["eps"], 1) if last.get("eps") and last["eps"] > 0 else None,
                      "naver_per": nv["naver_per"], "equity_q": cur["equity_q"], "q_label": cur["q_label"],
                      "roe": round(cur["ttm_ni"] / cur["equity_q"] * 100, 1) if cur["equity_q"] else None,
                      "debt_ratio": round(last["liabilities"] / last["equity"] * 100) if last.get("equity") else None,
                      "op_margin": round(last["op"] / last["revenue"] * 100, 1) if last.get("op") is not None and last.get("revenue") else None,
                      "last_fy": last["fy"], "q_link": cur["q_link"], "fy_link": last["link"]})
    wl = json.load(open(os.path.join("trader-kr", "notes", "us-watchlist.json"), encoding="utf-8"))
    watch = {r["ticker"]: r for r in json.load(open(os.path.join(WATCH, "out", "watch.json"), encoding="utf-8"))}
    tick = json.load(open(os.path.join(WATCH, "raw", "company_tickers.json"), encoding="utf-8"))
    cikmap = {v["ticker"]: str(v["cik_str"]).zfill(10) for v in tick.values()}
    for t in wl["tickers"]:
        w = watch.get(t)
        if not w:
            problems.append("watch.json 에 %s 없음" % t)
            continue
        rows, g = us_firm(t, wl["names"].get(t, t), cikmap[t], 1 + len(wl.get("extra_cik", {}).get(t, [])))
        years += rows
        last = rows[-1]
        cur = us_ttm_roe(g, w["eps_end"])
        if cur is None:
            problems.append("%s 최근 4개 분기 순이익·자기자본을 주당순이익과 같은 기간으로 맞추지 못함" % t)
            cur = {"ttm_ni": None, "equity_q": None}
        firms.append({"market": "미국", "code": t, "name": wl["names"].get(t, t), "rule": "미국 관심 종목 (us-watchlist.json)",
                      "price": w["close"], "price_date": w["date"], "currency": "달러",
                      "ttm_ni": cur["ttm_ni"], "ttm_eps": w["eps_ttm"], "per": w["per"], "naver_per": None,
                      "fy_eps": last["eps"], "per_fy": round(w["close"] / last["eps"], 1) if last.get("eps") and last["eps"] > 0 else None,
                      "equity_q": cur["equity_q"], "q_label": "%s 분기까지" % w["eps_end"],
                      "roe": round(cur["ttm_ni"] / cur["equity_q"] * 100, 1) if cur["ttm_ni"] is not None and cur["equity_q"] else None,
                      "debt_ratio": round(last["liabilities"] / last["equity"] * 100) if last.get("equity") and last.get("liabilities") else None,
                      "op_margin": round(last["op"] / last["revenue"] * 100, 1) if last.get("op") is not None and last.get("revenue") else None,
                      "last_fy": last["fy"], "q_link": None, "fy_link": last["link"]})

    # 완전성·항등식 점검
    need = ("revenue", "ni", "assets", "liabilities", "equity", "ocf")
    per_firm = {}
    for y in years:
        per_firm.setdefault(y["name"], 0)
        per_firm[y["name"]] += 1
        miss = [k for k in need if y.get(k) is None]
        if miss:
            problems.append("%s %s 빠짐: %s" % (y["name"], y["fy"], ", ".join(miss)))
            continue
        gap = abs(y["assets"] - (y["liabilities"] + y["equity"])) / y["assets"]
        y["identity_gap_pct"] = round(gap * 100, 3)
        if gap > 0.005:
            problems.append("%s %s 자산 ≠ 부채 + 자본 (차이 %.2f%%)" % (y["name"], y["fy"], gap * 100))
    for name, n in per_firm.items():
        if n != 5:
            problems.append("%s 연도 %d개 (5개 기대)" % (name, n))
    bad = [c for c in checks if not c["ok"]]
    report = {"run_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "kr_skipped_above": skipped,
              "counts": {"firms": len(firms), "years": len(years), "checks": len(checks), "check_mismatch": len(bad)},
              "mismatches": bad, "problems": problems}
    json.dump(report, io.open(os.path.join(OUT, "study_check.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if problems or len(firms) != 20:
        print("멈춤:", file=sys.stderr)
        for p in problems:
            print(" - " + p, file=sys.stderr)
        return 2
    json.dump(firms, io.open(os.path.join(OUT, "study_firms.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(years, io.open(os.path.join(OUT, "study_years.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(report["counts"], ensure_ascii=False))
    for c in bad:
        print(" 대조 불일치:", c)
    return 0


if __name__ == "__main__":
    sys.exit(main())
