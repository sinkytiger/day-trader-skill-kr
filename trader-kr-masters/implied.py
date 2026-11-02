#!/usr/bin/env python3
"""가격이 깔고 있는 가정 (trader-kr-masters): 한 회사의 지금 시가총액이 앞으로의 숫자에 대해 무엇을 전제하는지
거꾸로 푼다. 표준 라이브러리만 쓴다. 싸다·비싸다, 매수·매도를 말하지 않고 목표가를 내지 않는다.

    python implied.py --symbol 005930                    # 밴드, 역DCF, 필요한 ROE
    python implied.py --symbol 005930 --r 0.07 --r 0.09 --terminal 0.01 --horizon 10 --json
    python implied.py --symbol 005930 --no-band          # 월말 KRX 표를 받지 않고 지금 값만

1. 밴드. 첫 사업보고서가 나온 달부터 달마다 마지막 거래일의 시가총액(보통주 + 우선주)을, 그날 알던 마지막
   사업보고서(정정 전 첫 접수일 기준)의 지배주주 순이익·지배주주 자본·현금배당금총액과 견준다 (PER, PBR,
   배당수익률). 지금 값이 지난 달들 가운데 어디쯤인지 본다.
2. 역DCF. 시가총액이 앞으로 잉여현금흐름(영업활동현금흐름 - 유형자산 취득, 최근 3년 평균에서 출발)의 현재가치라면
   앞 N년 동안 연 몇 % 늘어야 하나. N년 뒤에는 연 terminal 로 늘어난다고 둔다.
3. 필요한 ROE (잔여이익모형). 시가총액 = 지배주주 자본 + 앞으로 초과이익((ROE - r) x 자본)의 현재가치가 되려면
   ROE 가 영원히 얼마여야 하나. 이익을 모두 배당할 때 (자본이 그대로: ROE = r x PBR) 와, 지난 3년 배당성향대로
   남겨 자본을 키울 때 (성장 g = ROE x 남기는 몫: PBR = (ROE - g) / (r - g)) 두 판.
할인율 r, N, terminal 은 가정이다. 기본은 r 6·8·10%, N 10년, terminal 2%.
"""

import argparse
import datetime
import importlib.util
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir))
from krcommon import financials as fin, krx, krxdata  # noqa: E402

_spec = importlib.util.spec_from_file_location("trader_masters_for_implied", os.path.join(HERE, "masters.py"))
ms = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ms)

RS = (0.06, 0.08, 0.10)
HORIZON = 10
TERMINAL = 0.02
BASE_YEARS = 3
LOW, HIGH = -0.5, 1.0                # 역DCF 성장률을 찾는 범위
HISTORY_YEARS = 11                   # FY2015 를 넣어 2016년 봄부터 밴드를 그린다
NOTE = ("할인율·성장률은 가정입니다. 이 표는 싸다·비싸다나 매수·매도를 말하지 않고, 투자 권유가 아닙니다. "
        "사업보고서(연간) 숫자만 쓰고, 빚과 현금은 따로 조정하지 않았습니다.")


class ImpliedError(Exception):
    pass


# ---------------------------------------------------------------- 그날 알던 숫자

def first_filed(filings, row):
    """row 의 숫자를 처음 알 수 있던 날 (YYYYMMDD). 숫자를 준 보고서(src)의 정정 전 첫 접수일, 접수 기록이 없으면
    그 보고서의 접수번호 앞 8자리 (정정본이면 늦은 날이라 보수적이다)."""
    x = (filings or {}).get("%d.12" % row.get("src", row["year"]))
    return x["first"] if x and x.get("first") else (row.get("filed") or None)


def known_at(rows, filings, day):
    """day 에 알던 가장 최근 사업연도의 행 (없으면 None)."""
    cut = day.strftime("%Y%m%d")
    got = [r for r in rows if (first_filed(filings, r) or "99999999") <= cut]
    return max(got, key=lambda r: r["year"]) if got else None


def multiples(row, cap):
    ni, eq, dv = row.get("ni_parent"), row.get("equity_parent"), row.get("div_total")
    return {"per": cap / ni if cap and ni and ni > 0 else None,
            "pbr": cap / eq if cap and eq and eq > 0 else None,
            "dy": dv / cap if cap and dv is not None else None}


def months_between(first, today):
    """first 가 든 달부터 today 의 앞 달까지 [(연, 월)]."""
    out, y, m = [], first.year, first.month
    while (y, m) < (today.year, today.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def month_end(y, m):
    return datetime.date(y + (m == 12), m % 12 + 1, 1) - datetime.timedelta(days=1)


def month_markets(sym, months, key, now_kst, transport, cache_dir, workers=None):
    """달마다 마지막 거래일의 fin.market (상장 전·폐지 뒤는 None). 캐시에 없는 KRX 표는 여러 개씩 동시에 받는다."""
    def one(ym):
        return fin.market(sym, key, month_end(*ym), now_kst, transport, cache_dir, lookback=4)
    with ThreadPoolExecutor(max_workers=workers or krxdata.WORKERS) as ex:
        return list(ex.map(one, months))


def quantile(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def band_stats(points, key, now):
    xs = [p[key] for p in points if p[key] is not None]
    out = {"n": len(xs), "skipped": len(points) - len(xs), "now": now}
    if xs:
        out.update({"min": min(xs), "p25": quantile(xs, 0.25), "median": quantile(xs, 0.5),
                    "p75": quantile(xs, 0.75), "max": max(xs),
                    "below": None if now is None else sum(1 for x in xs if x < now) / len(xs)})
    return out


# ---------------------------------------------------------------- 역DCF, 잔여이익

def dcf_value(f0, g, r, n, gt):
    """앞 n년 연 g, 그 뒤 연 gt 로 느는 잉여현금흐름 (출발 f0) 의 현재가치."""
    v, f = 0.0, f0
    for t in range(1, n + 1):
        f *= 1.0 + g
        v += f / (1.0 + r) ** t
    return v + f * (1.0 + gt) / (r - gt) / (1.0 + r) ** n


def solve(fn, target, lo=LOW, hi=HIGH, steps=200):
    """커지는 함수 fn 에서 fn(x) = target 인 x 와 표시. 범위 밖이면 (None, "below"|"above")."""
    if target <= fn(lo):
        return None, "below"
    if target >= fn(hi):
        return None, "above"
    for _ in range(steps):
        mid = (lo + hi) / 2.0
        if fn(mid) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0, ""


def implied_growth(cap, f0, r, n=HORIZON, gt=TERMINAL):
    return solve(lambda g: dcf_value(f0, g, r, n, gt), cap)


def roe_all_paid(pbr, r):
    """이익을 모두 배당해 자본이 그대로일 때 PBR 을 설명하는 영원한 ROE: PBR = 1 + (ROE - r) / r."""
    return r * pbr


def roe_retained(pbr, r, keep):
    """해마다 이익의 keep 만큼 남겨 자본이 ROE x keep 로 늘 때: PBR = (ROE - g) / (r - g), g = ROE x keep."""
    return pbr * r / (1.0 - keep + pbr * keep)


# ---------------------------------------------------------------- 실행

def analyze(symbol, now_kst=None, rs=RS, horizon=HORIZON, terminal=TERMINAL, band=True, progress=None,
            dart_transport=None, krx_transport=None, dart_key=None, krx_key=None, dart_cache=fin.CACHE,
            krx_cache=krxdata.CACHE_DIR, workers=None):
    now_kst = now_kst or krx.kst_now()
    rs = sorted(set(rs))
    if not rs or rs[0] <= terminal or rs[-1] >= 0.5:
        raise ImpliedError("할인율은 terminal (%.1f%%) 보다 크고 50%% 보다 작아야 합니다" % (100 * terminal))
    if not 1 <= horizon <= 30:
        raise ImpliedError("--horizon 은 1 ~ 30 년입니다")
    info, rows = fin.history(symbol, HISTORY_YEARS, dart_key, dart_cache, now_kst, dart_transport, progress)
    if not rows:
        raise ImpliedError("%s 의 사업보고서 재무제표를 찾지 못했습니다 (2015년 이후)" % info["name"])
    rows = ms.derive(rows)
    filings = fin.annual_filings(info["corp_code"], dart_key or fin.dart.api_key(), dart_cache, now_kst,
                                 dart_transport)
    mk = fin.market(info["symbol"], krx_key, None, now_kst, krx_transport, krx_cache)
    if not mk:
        raise ImpliedError("KRX 시가총액이 없습니다. KRX 키를 본인 터미널에서 python krcommon/keys.py krx 로 넣으세요 "
                           "(채팅에 붙여 넣지 마세요)")
    today = datetime.date.fromisoformat(mk["date"])
    cur = known_at(rows, filings, today)
    if cur is None:
        raise ImpliedError("%s 기준으로 접수된 사업보고서가 없습니다" % mk["date"])
    seen = [r for r in rows if r["year"] <= cur["year"]]
    now = multiples(cur, mk["mktcap"])

    points = []
    if band:
        firsts = sorted(f for f in (first_filed(filings, r) for r in rows) if f)
        start = datetime.datetime.strptime(firsts[0], "%Y%m%d").date()
        months = months_between(start, today)
        if progress:
            progress("  KRX 월말 시가총액 %d달 (캐시에 없는 표는 받음, 처음이면 몇 분)..." % len(months))
        for x in month_markets(info["symbol"], months, krx_key, now_kst, krx_transport, krx_cache, workers):
            r = known_at(rows, filings, datetime.date.fromisoformat(x["date"])) if x else None
            if r:
                points.append(dict(multiples(r, x["mktcap"]), date=x["date"], fy=r["year"], cap=x["mktcap"]))
    bands = {k: band_stats(points, k, now[k]) for k in ("per", "pbr", "dy")}

    tail = seen[-BASE_YEARS:]
    fcf = [r.get("fcf") for r in tail]
    dcf = {"base_years": [r["year"] for r in tail], "horizon": horizon, "terminal": terminal, "grid": [],
           "base": None, "skip": ""}
    if cur.get("current_assets") is None:
        dcf["skip"] = "유동·비유동 구분이 없는 재무제표 (금융업 등) 는 영업현금흐름에 예금·대출이 섞여 역산하지 않습니다"
    elif len(tail) < BASE_YEARS or None in fcf or tail[0]["year"] != cur["year"] - BASE_YEARS + 1:
        dcf["skip"] = "최근 %d년 잉여현금흐름이 다 있지 않습니다" % BASE_YEARS
    else:
        dcf["base"] = sum(fcf) / len(fcf)
        if dcf["base"] <= 0:
            dcf["skip"] = ("최근 %d년 평균 잉여현금흐름이 0 이하라 역산하지 않습니다 (연결에 할부·카드 같은 금융 자회사가 있으면 "
                           "대출이 영업현금흐름에 섞여 이렇게 나올 수 있습니다)" % BASE_YEARS)
        else:
            for r in rs:
                g, flag = implied_growth(mk["mktcap"], dcf["base"], r, horizon, terminal)
                dcf["grid"].append({"r": r, "g": g, "flag": flag})

    ni = [r.get("ni_parent") for r in tail]
    dv = [r.get("div_total") for r in tail]
    keep = None
    if len(tail) == BASE_YEARS and None not in ni and None not in dv and sum(ni) > 0:
        keep = min(1.0, max(0.0, 1.0 - sum(dv) / sum(ni)))
    rim = {"pbr": now["pbr"], "keep": keep, "grid": [], "skip": ""}
    if now["pbr"] is None:
        rim["skip"] = "지배주주 자본이 0 이하이거나 없습니다"
    else:
        for r in rs:
            rim["grid"].append({"r": r, "all_paid": roe_all_paid(now["pbr"], r),
                                "retained": None if keep is None else roe_retained(now["pbr"], r, keep)})

    roes = [r.get("roe") for r in seen if r.get("roe") is not None]
    span = seen[-1]["year"] - seen[0]["year"]
    f_first = [r.get("fcf") for r in seen[:BASE_YEARS]]
    fcf_g = None
    if len(seen) >= 2 * BASE_YEARS and None not in f_first and dcf["base"] is not None:
        fcf_g = ms.cagr(sum(f_first) / BASE_YEARS, dcf["base"], span - (BASE_YEARS - 1))
    hist = {"from": seen[0]["year"], "to": seen[-1]["year"],
            "rev_cagr": ms.cagr(seen[0].get("revenue"), seen[-1].get("revenue"), span),
            "ni_cagr": ms.cagr(seen[0].get("ni_parent"), seen[-1].get("ni_parent"), span), "fcf_cagr": fcf_g,
            "roe_last": cur.get("roe"), "roe_avg3": ms.avg([r.get("roe") for r in tail]),
            "roe_avg": ms.avg(roes), "roe_n": len(roes)}
    return {"symbol": info["symbol"], "name": info["name"], "market": mk,
            "report": {"year": cur["year"], "fs_div": cur["fs_div"], "filed": first_filed(filings, cur)},
            "now": now, "band": bands, "points": points, "dcf": dcf, "rim": rim, "history": hist,
            "assumptions": {"r": rs, "horizon": horizon, "terminal": terminal}, "note": NOTE}


def num(x, nd=1):
    return "-" if x is None else "%.*f" % (nd, x)


def g_text(row):
    if row["flag"] == "below":
        return "%s 보다 낮음" % ms.pct(LOW, 0)
    if row["flag"] == "above":
        return "%s 보다 높음" % ms.pct(HIGH, 0)
    return ms.pct(row["g"])


def print_doc(doc):
    mk, rp, b = doc["market"], doc["report"], doc["band"]
    f = rp["filed"] or "--------"
    print("%s (%s) 가격이 깔고 있는 가정" % (doc["name"], doc["symbol"]))
    print("자료: DART FY%d 사업보고서 (%s, 첫 접수 %s-%s-%s), KRX %s 시가총액 %s 원 (보통주+우선주)" % (
        rp["year"], "연결" if rp["fs_div"] == "CFS" else "별도", f[:4], f[4:6], f[6:8], mk["date"], ms.won(mk["mktcap"])))
    print()
    pts = doc["points"]
    if pts:
        print("1. 지금 값과 지난 밴드 (월말 %d달, %s ~ %s, 그달 알던 사업보고서 기준)" % (
            len(pts), pts[0]["date"][:7], pts[-1]["date"][:7]))
    else:
        print("1. 지금 값 (밴드 없음)")
    print("   %-10s %8s %8s %8s %8s %8s %8s  %s" % ("", "지금", "가장낮음", "25%", "가운데", "75%", "가장높음",
                                               "지금보다 낮았던 달"))
    for k, label, fmt in (("per", "PER", lambda x: num(x)), ("pbr", "PBR", lambda x: num(x, 2)),
                          ("dy", "배당수익률", lambda x: ms.pct(x))):
        s = b[k]
        print("   %s %8s %8s %8s %8s %8s %8s  %s" % (
            krx.pad(label, 10), fmt(s["now"]), fmt(s.get("min")), fmt(s.get("p25")), fmt(s.get("median")),
            fmt(s.get("p75")), fmt(s.get("max")), "-" if s.get("below") is None else ms.pct(s["below"], 0)))
    if b["per"]["skipped"]:
        print("   (PER 은 적자이던 달 %d개를 뺌)" % b["per"]["skipped"])
    print("   PER = 시가총액 / 지배주주 순이익, PBR = 시가총액 / 지배주주 자본, 배당수익률 = 현금배당금총액 / 시가총액")
    d = doc["dcf"]
    print()
    print("2. 역DCF: 지금 시가총액이 앞으로 잉여현금흐름의 현재가치라면")
    if d["skip"]:
        print("   %s" % d["skip"])
    else:
        print("   출발 잉여현금 %s 원 (FY%d ~ %d 평균), %d년 뒤부터 연 %s 성장" % (
            ms.won(d["base"]), d["base_years"][0], d["base_years"][-1], d["horizon"], ms.pct(d["terminal"])))
        for row in d["grid"]:
            print("   할인율 %s: 앞 %d년 동안 연 %s 늘어야 함" % (ms.pct(row["r"], 0), d["horizon"], g_text(row)))
    h = doc["history"]
    print("   지난 숫자 (FY%d ~ %d): 매출 연 %s, 지배주주 순이익 연 %s, 잉여현금 3년 평균 연 %s" % (
        h["from"], h["to"], ms.pct(h["rev_cagr"]), ms.pct(h["ni_cagr"]), ms.pct(h["fcf_cagr"])))
    r = doc["rim"]
    print()
    print("3. 필요한 ROE (잔여이익모형): 지금 PBR %s 가 영원히 이어질 ROE 라면" % num(r["pbr"], 2))
    if r["skip"]:
        print("   %s" % r["skip"])
    for row in r["grid"]:
        print("   할인율 %s: 이익을 모두 배당할 때 ROE %s, 지난 3년 배당성향대로 남길 때 ROE %s%s" % (
            ms.pct(row["r"], 0), ms.pct(row["all_paid"]), ms.pct(row["retained"]),
            "" if row["retained"] is None else " (자본 연 %s 성장)" % ms.pct(row["retained"] * r["keep"])))
    if r["keep"] is not None:
        print("   (남기는 몫 %s = 1 - 최근 3년 현금배당금총액 / 지배주주 순이익. 자본 성장이 할인율에 가까울수록 "
              "작은 차이에 크게 흔들린다)" % ms.pct(r["keep"], 0))
    print("   지난 ROE: 최근 %s, 최근 3년 평균 %s, %d년 평균 %s" % (
        ms.pct(h["roe_last"]), ms.pct(h["roe_avg3"]), h["roe_n"], ms.pct(h["roe_avg"])))
    print()
    print(doc["note"])


def main(argv=None):
    krx.utf8_stdio()
    ap = argparse.ArgumentParser(description="가격이 깔고 있는 가정: PER·PBR 밴드, 역DCF, 필요한 ROE (trader-kr-masters).")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--r", type=float, action="append", help="할인율 (여러 번 줄 수 있음, 기본 0.06 0.08 0.10)")
    ap.add_argument("--horizon", type=int, default=HORIZON, help="역DCF 앞 기간 (년, 기본 10)")
    ap.add_argument("--terminal", type=float, default=TERMINAL, help="그 뒤 연 성장률 (기본 0.02)")
    ap.add_argument("--no-band", action="store_true", help="월말 KRX 표를 받지 않고 지금 값만")
    ap.add_argument("--now")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        prog = None if a.json else (lambda m: print(m, file=sys.stderr, flush=True))
        doc = analyze(a.symbol, krx.parse_now(a.now), tuple(a.r or RS), a.horizon, a.terminal, not a.no_band, prog)
    except (ImpliedError, ms.MastersError, fin.FinError, fin.dart.DartError, krxdata.KrxDataError, krx.KrxError) as e:
        print("멈춤: %s" % e, file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(doc, ensure_ascii=False, indent=1))
    else:
        print_doc(doc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
