"""회사 재무 (DART 사업보고서 전체 재무제표)와 시가총액 (KRX 일별매매정보). 표준 라이브러리만 쓴다.

대가 점검표(trader-kr-masters)가 쓰고, 나중에 재무 기준을 사전 등록해 시험할 때도 쓰려고 공통 모듈로 둔다.
숫자는 모두 공시 원값에서 코드가 낸다. 해마다 그 해 사업보고서의 당기 값을 쓰고, 그 해 보고서가 없으면 다음 해
보고서의 전기 값을 쓴다. 접수번호 앞 8자리가 접수일이라, 그날 이전에는 그 숫자를 알 수 없었다는 기준으로 쓸 수 있다.

호출 (OpenDART, 키 DART_API_KEY 또는 ~/.dart/api_key, 키 값은 찍지 않음):
  corpCode.xml           회사 고유번호 (zip). 종목코드 -> corp_code. 30일마다 다시 받는다
  fnlttSinglAcntAll.json 단일회사 전체 재무제표. 연결(CFS)이 없으면 별도(OFS). 2015 사업연도부터
  alotMatter.json        배당에 관한 사항 (주당 현금배당금, 현금배당금총액, 현금배당수익률. 당기·전기·전전기)
  company.json           기업개황 (업종 코드, 결산월)
  list.json              사업보고서 접수 기록 (정정 전 첫 접수일. 그날 전에는 그 숫자를 알 수 없었다)
캐시: trader-kr/cache/dart/corpcode.json, .../fs/<corp>_<연도>.json, .../alot/<corp>_<연도>.json,
      .../company/<corp>.json, .../filings/<corp>.json

시점 (pit_rows): 어느 날에 알 수 있던 사업보고서만으로 해마다의 숫자를 만든다. 재무제표 API 는 정정된 보고서의 값을
주므로, 숫자 자체는 그날 뒤에 고쳐진 값일 수 있다 (row["revised_after"]).
"""

import datetime
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

from . import dart, krx, krxdata

BASE = "https://opendart.fss.or.kr/api/"
CACHE = os.path.join("trader-kr", "cache", "dart")
FIRST_YEAR = 2015                    # DART 전체 재무제표가 있는 첫 사업연도
CORP_REFRESH_DAYS = 30
ANNUAL = "11011"                     # 사업보고서
PERIOD_RE = re.compile(r"\((\d{4})\.(\d{2})\)")   # "사업보고서 (2019.12)" 의 결산 연월

# 표준 계정 (접두어 ifrs-full_, ifrs_, dart_ 를 뗀 이름) -> 항목. 앞에 있는 표가 먼저
ITEMS = {
    "revenue": (("IS", "CIS"), ("Revenue",), ("매출액", "영업수익", "수익(매출액)", "매출")),
    "op": (("IS", "CIS"), ("OperatingIncomeLoss",), ("영업이익", "영업이익(손실)")),
    "ni_parent": (("IS", "CIS"), ("ProfitLossAttributableToOwnersOfParent",), ()),
    "ni": (("IS", "CIS"), ("ProfitLoss",), ("당기순이익", "당기순이익(손실)")),
    "equity_parent": (("BS",), ("EquityAttributableToOwnersOfParent",), ()),
    "equity": (("BS",), ("Equity",), ("자본총계",)),
    "assets": (("BS",), ("Assets",), ("자산총계",)),
    "liabilities": (("BS",), ("Liabilities",), ("부채총계",)),
    "current_assets": (("BS",), ("CurrentAssets",), ("유동자산",)),
    "current_liabilities": (("BS",), ("CurrentLiabilities",), ("유동부채",)),
    "noncurrent_liabilities": (("BS",), ("NoncurrentLiabilities",), ("비유동부채",)),
    "inventories": (("BS",), ("Inventories",), ("재고자산",)),
    "cash": (("BS",), ("CashAndCashEquivalents",), ("현금및현금성자산",)),
    "ocf": (("CF",), ("CashFlowsFromUsedInOperatingActivities",), ("영업활동현금흐름", "영업활동으로인한현금흐름")),
    "capex": (("CF",), ("PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
                        "PurchaseOfPropertyPlantAndEquipment"), ("유형자산의취득",)),
    "eps": (("IS", "CIS"), ("BasicEarningsLossPerShare",), ("기본주당이익", "기본주당이익(손실)")),
}


class FinError(Exception):
    pass


def _bytes(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "day-trader-skill-kr/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except (urllib.error.URLError, OSError) as e:
        raise FinError("DART 에 연결하지 못했습니다: %s" % getattr(e, "reason", e))


def _get(path, params, key, transport):
    q = dict(params, crtfc_key=key)
    return (transport or _bytes)(BASE + path + "?" + urllib.parse.urlencode(q))


def _json(raw):
    try:
        return json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except ValueError:
        raise FinError("DART 응답이 JSON 이 아닙니다")


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


# ---------------------------------------------------------------- 회사 고유번호

def parse_corp_zip(raw):
    """corpCode.xml zip -> {종목코드: {"corp_code", "name"}} (상장사만)."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            xml = z.read(z.namelist()[0])
    except (zipfile.BadZipFile, IndexError, KeyError):
        doc = None
        try:
            doc = _json(raw)
        except FinError:
            pass
        msg = (doc or {}).get("message") if isinstance(doc, dict) else None
        raise FinError("DART 회사 고유번호 파일을 받지 못했습니다%s" % (": " + msg if msg else ""))
    out = {}
    for el in ET.fromstring(xml).iter("list"):
        sym = (el.findtext("stock_code") or "").strip()
        if sym:
            out[sym] = {"corp_code": (el.findtext("corp_code") or "").strip(), "name": (el.findtext("corp_name") or "").strip()}
    return out


def corp_codes(key, cache=CACHE, now_kst=None, transport=None):
    now_kst = now_kst or krx.kst_now()
    path = os.path.join(cache, "corpcode.json")
    doc = _read(path)
    if doc:
        age = (now_kst.date() - datetime.date.fromisoformat(doc["fetched"])).days
        if age < CORP_REFRESH_DAYS:
            return doc["codes"]
    codes = parse_corp_zip(_get("corpCode.xml", {}, key, transport))
    _write(path, {"fetched": now_kst.date().isoformat(), "codes": codes})
    return codes


# ---------------------------------------------------------------- 재무제표

def base_id(account_id):
    a = str(account_id or "")
    for p in ("ifrs-full_", "ifrs_", "dart_"):
        if a.startswith(p):
            return a[len(p):]
    return a


def pick(rows, item, col="thstrm_amount"):
    """rows (fnlttSinglAcntAll 의 list) 에서 항목 하나. 표준 계정을 먼저, 없으면 계정 이름으로."""
    tables, ids, names = ITEMS[item]
    for sj in tables:
        for r in rows:
            if r.get("sj_div") == sj and base_id(r.get("account_id")) in ids:
                v = krxdata.num(r.get(col))
                if v is not None:
                    return v
        for r in rows:
            if r.get("sj_div") == sj and str(r.get("account_nm") or "").replace(" ", "") in names:
                v = krxdata.num(r.get(col))
                if v is not None:
                    return v
    return None


def statements(corp, year, key, cache=CACHE, now_kst=None, transport=None, sleep=time.sleep):
    """그 해 사업보고서의 전체 재무제표 {"fs_div", "rcept_no", "list"}. 없으면 None.
    지난 해(올해 -2 이전)의 '없음' 은 캐시해 다시 묻지 않는다."""
    now_kst = now_kst or krx.kst_now()
    path = os.path.join(cache, "fs", "%s_%d.json" % (corp, year))
    doc = _read(path)
    if doc is not None:
        return doc.get("data")
    got = None
    for fs in ("CFS", "OFS"):
        res = _json(_get("fnlttSinglAcntAll.json", {"corp_code": corp, "bsns_year": year, "reprt_code": ANNUAL,
                                                    "fs_div": fs}, key, transport))
        st = str(res.get("status"))
        if st == "000" and res.get("list"):
            L = res["list"]
            got = {"fs_div": fs, "rcept_no": L[0].get("rcept_no"), "list": L}
            break
        if st not in ("013",):
            raise FinError("DART 재무제표 응답 %s: %s" % (st, res.get("message") or ""))
        if sleep:
            sleep(0.1)
    if got is not None or year < now_kst.year - 1:
        _write(path, {"data": got})
    return got


DIV_COLS = ("thstrm", "frmtrm", "lwfr")   # 당기, 전기, 전전기


def dividend_table(corp, year, key, cache=CACHE, now_kst=None, transport=None):
    """그 해 사업보고서의 배당에 관한 사항 {"thstrm"|"frmtrm"|"lwfr": {"dps", "total", "yield"}}. 없으면 None.
    dps 는 보통주 주당 현금배당금(원), total 은 현금배당금총액(원), yield 는 회사가 적은 보통주 현금배당수익률 (0.017 꼴)."""
    now_kst = now_kst or krx.kst_now()
    path = os.path.join(cache, "alot", "%s_%d.json" % (corp, year))
    doc = _read(path)
    if doc is not None:
        return doc.get("data")
    res = _json(_get("alotMatter.json", {"corp_code": corp, "bsns_year": year, "reprt_code": ANNUAL}, key, transport))
    st = str(res.get("status"))
    got = None
    if st == "000":
        got = {}
        for col in DIV_COLS:
            dps = total = yld = None
            for r in res.get("list") or []:
                se = str(r.get("se") or "").replace(" ", "")
                common = str(r.get("stock_knd") or "") == "보통주"
                v = krxdata.num(r.get(col))
                if se.startswith("주당현금배당금") and common:
                    dps = v
                elif se.startswith("현금배당금총액"):
                    total = v * 1e6 if v is not None else None
                elif se.startswith("현금배당수익률") and common:
                    yld = v / 100.0 if v is not None else None
            got[col] = {"dps": dps or 0.0, "total": total or 0.0, "yield": yld}
    elif st != "013":
        raise FinError("DART 배당 응답 %s: %s" % (st, res.get("message") or ""))
    if got is not None or year < now_kst.year - 1:
        _write(path, {"data": got})
    return got


def dividends(corp, year, key, cache=CACHE, now_kst=None, transport=None):
    """그 해 보통주 주당 현금배당금(원)과 현금배당금총액(원). 없으면 None."""
    t = dividend_table(corp, year, key, cache, now_kst, transport)
    return {"dps": t["thstrm"]["dps"], "total": t["thstrm"]["total"]} if t else None


# ---------------------------------------------------------------- 기업개황, 사업보고서 접수 기록

def company(corp, key, cache=CACHE, transport=None):
    """기업개황 {"name", "induty_code", "acc_mt"}. 한 번 받으면 캐시를 쓴다 (업종 코드와 결산월은 받은 날의 값)."""
    path = os.path.join(cache, "company", "%s.json" % corp)
    doc = _read(path)
    if doc is not None:
        return doc
    res = _json(_get("company.json", {"corp_code": corp}, key, transport))
    if str(res.get("status")) != "000":
        raise FinError("DART 기업개황 응답 %s: %s" % (res.get("status"), res.get("message") or ""))
    doc = {"name": res.get("corp_name") or "", "induty_code": str(res.get("induty_code") or ""),
           "acc_mt": str(res.get("acc_mt") or "")}
    _write(path, doc)
    return doc


def annual_filings(corp, key, cache=CACHE, now_kst=None, transport=None, need=None, sleep=time.sleep):
    """사업보고서 접수 기록 {"YYYY.MM": {"first": 정정 전 첫 접수일, "first_no": 그 접수번호, "n": 정정 포함 건수}}.
    FIRST_YEAR 이후 접수분. 캐시를 need (날짜, 기본 오늘) 이후에 받았으면 그것을 쓴다."""
    now_kst = now_kst or krx.kst_now()
    path = os.path.join(cache, "filings", "%s.json" % corp)
    doc = _read(path)
    if doc is not None and doc.get("fetched", "") >= (need or now_kst.date()).isoformat():
        return doc["periods"]
    rows, page, total = [], 1, 1
    while page <= total:
        res = _json(_get("list.json", {"corp_code": corp, "bgn_de": "%d0101" % FIRST_YEAR,
                                       "end_de": now_kst.strftime("%Y%m%d"), "pblntf_detail_ty": "A001",
                                       "page_no": page, "page_count": 100}, key, transport))
        st = str(res.get("status"))
        if st == "013":
            break
        if st != "000":
            raise FinError("DART 공시 목록 응답 %s: %s" % (st, res.get("message") or ""))
        rows.extend(res.get("list") or [])
        total = int(res.get("total_page") or 1)
        page += 1
        if page <= total and sleep:
            sleep(0.1)
    periods = {}
    for r in rows:
        mt = PERIOD_RE.search(str(r.get("report_nm") or ""))
        if not mt:
            continue
        d, no = str(r.get("rcept_dt") or ""), str(r.get("rcept_no") or "")
        x = periods.setdefault("%s.%s" % mt.groups(), {"first": d, "first_no": no, "n": 0})
        if (d, no) < (x["first"], x["first_no"]):
            x["first"], x["first_no"] = d, no
        x["n"] += 1
    _write(path, {"fetched": now_kst.date().isoformat(), "periods": periods})
    return periods


def year_row(doc, col="thstrm_amount"):
    rows = doc["list"]
    out = {k: pick(rows, k, col) for k in ITEMS}
    if out["ni_parent"] is None:
        out["ni_parent"] = out["ni"]
    if out["equity_parent"] is None:
        out["equity_parent"] = out["equity"]
    if out["capex"] is not None:
        out["capex"] = abs(out["capex"])
    return out


def history(symbol, years=10, key=None, cache=CACHE, now_kst=None, transport=None, progress=None):
    """최근 사업연도부터 years 년 (2015 이전은 없음). (회사 정보, [{year, fs_div, filed, src, ...항목, dps, div_total}]).
    해마다 그 해 보고서의 당기 값, 그 해 보고서가 없으면 다음 해 보고서의 전기 값. src 는 숫자를 준 보고서의 사업연도."""
    key = key or dart.api_key()
    if not key:
        raise FinError("DART_API_KEY 가 없습니다. 터미널에서 python krcommon/keys.py dart 로 넣으세요 (채팅에 붙여 넣지 마세요)")
    now_kst = now_kst or krx.kst_now()
    sym = krx.norm_symbol(symbol)
    info = corp_codes(key, cache, now_kst, transport).get(sym)
    if not info:
        raise FinError("DART 에 %s 상장사가 없습니다" % sym)
    corp = info["corp_code"]
    last = now_kst.year - 1
    docs = {}
    for y in range(last, max(FIRST_YEAR, last - years) - 1, -1):
        if progress:
            progress("  DART %s %d 사업보고서..." % (info["name"], y))
        docs[y] = statements(corp, y, key, cache, now_kst, transport)
    if docs.get(last) is None:            # 아직 올해 보고서가 안 나왔으면 한 해 앞부터
        last -= 1
    span = [y for y in range(last, max(FIRST_YEAR, last - years + 1) - 1, -1)]
    out = []
    for y in span:
        if docs.get(y):
            row, sy = year_row(docs[y]), y
        elif docs.get(y + 1):
            row, sy = year_row(docs[y + 1], "frmtrm_amount"), y + 1
        else:
            continue
        src = docs[sy]
        div = dividends(corp, y, key, cache, now_kst, transport)
        row.update({"year": y, "fs_div": src["fs_div"], "filed": str(src.get("rcept_no") or "")[:8], "src": sy,
                    "dps": (div or {}).get("dps"), "div_total": (div or {}).get("total")})
        out.append(row)
    out.sort(key=lambda r: r["year"])
    return dict(info, symbol=sym), out


def slim(doc):
    """statements() 결과를 세 열(당기·전기·전전기)의 year_row 만 남긴 작은 꼴로 (여러 해를 메모리에 둘 때)."""
    if not doc:
        return doc
    return {"fs_div": doc["fs_div"], "rcept_no": doc.get("rcept_no"),
            "rows": {c: year_row(doc, c) for c in ("thstrm_amount", "frmtrm_amount", "bfefrmtrm_amount")}}


def filed_on(filings, year, doc, month="12"):
    """FY year 사업보고서를 처음 알 수 있던 날 (YYYYMMDD). 접수 기록의 정정 전 첫 접수일, 없으면 재무제표 접수번호 앞
    8자리 (정정본이면 늦은 날이라 보수적이다)."""
    x = (filings or {}).get("%d.%s" % (year, month))
    if x and x.get("first"):
        return x["first"]
    return str((doc or {}).get("rcept_no") or "")[:8] or None


def pit_rows(docs, divs, filings, day, years=10):
    """day 에 알 수 있던 사업보고서만으로 해마다의 숫자 (history 와 같은 꼴, 12월 결산). docs·divs 는 {사업연도:
    statements()·dividend_table() 결과 또는 None. statements 대신 slim() 결과도 된다}. 해마다 그 해 보고서의 당기 값, 그 보고서를 못 쓰면 다음 해 보고서의
    전기 값, 그것도 없으면 그다음 해 보고서의 전전기 값. 최근 years 개.
    row["revised_after"]: 그 숫자를 준 재무제표의 접수번호가 day 뒤다 (정정된 값일 수 있음)."""
    cut = day.strftime("%Y%m%d")
    ok = {}
    for y, doc in (docs or {}).items():
        if doc:
            f = filed_on(filings, y, doc)
            if f and f <= cut:
                ok[y] = (doc, f)
    out = []
    if not ok:
        return out
    for y in range(min(ok) - 2, max(ok) + 1):
        for src, col, dcol in ((y, "thstrm_amount", "thstrm"), (y + 1, "frmtrm_amount", "frmtrm"),
                               (y + 2, "bfefrmtrm_amount", "lwfr")):
            if src not in ok:
                continue
            doc, f = ok[src]
            row = dict(doc["rows"][col]) if "rows" in doc else year_row(doc, col)
            if row["assets"] is None and row["revenue"] is None and row["ni"] is None:
                continue                      # 그 열이 빈 보고서 (전전기가 없는 표 등)
            div = (divs or {}).get(src)
            dv = (div or {}).get(dcol) or {}
            row.update({"year": y, "fs_div": doc["fs_div"], "filed": f, "src": src,
                        "revised_after": str(doc.get("rcept_no") or "")[:8] > cut,
                        "dps": dv.get("dps") if div else None, "div_total": dv.get("total") if div else None})
            out.append(row)
            break
    return out[-years:]


# ---------------------------------------------------------------- 시가총액 (KRX)

def market(symbol, key=None, day=None, now_kst=None, transport=None, cache_dir=krxdata.CACHE_DIR, lookback=10):
    """가장 최근(또는 day 이전) KRX 거래일의 {date, price, shares, mktcap (보통주+우선주), mktcap_common}.
    키가 없거나 못 찾으면 None. 우선주는 종목 코드 앞 5자리가 같은 행."""
    key = key or krxdata.api_key()
    if not key:
        return None
    now_kst = now_kst or krx.kst_now()
    sym = krx.norm_symbol(symbol)
    d = day or now_kst.date()
    for _ in range(lookback * 2):
        if d.weekday() < 5 and krxdata.published(d, now_kst):
            rows = krxdata.day_table(d, key, cache_dir, now_kst, transport)
            if rows:
                r = rows.get(sym)
                if not r:
                    return None
                caps = sum((x.get("mktcap") or 0) for s, x in rows.items() if s[:5] == sym[:5])
                return {"date": d.isoformat(), "price": r["c"], "shares": r.get("shares"), "mktcap": caps,
                        "mktcap_common": r.get("mktcap"), "name": r.get("name")}
        d -= datetime.timedelta(days=1)
    return None
