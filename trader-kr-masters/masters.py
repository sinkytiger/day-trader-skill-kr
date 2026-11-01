#!/usr/bin/env python3
"""투자 대가 점검표 (trader-kr-masters): 한 회사의 DART 사업보고서 숫자(2015년~)와 KRX 시가총액으로 그레이엄·버핏·
린치의 기준을 하나씩 재고, 기준마다 숫자와 충족 여부를 보여 준다. 표준 라이브러리만 쓴다. 주문을 내지 않고,
매수·매도·강세·약세 신호를 내지 않는다. 숫자는 모두 이 스크립트가 공시 원값에서 낸다.

    python masters.py check --symbol 005930              # 최근 10 사업연도 (2015년부터 있음)
    python masters.py check --symbol 005930 --years 6 --json
    python masters.py thesis new --symbol 005930 --statement "..." --pillar "설명 | 매출성장 >= 5%" --risk "설명 | 부채비율 > 100%"
    python masters.py thesis add --symbol 005930 --data "무엇이 바뀌었나" --impact P1:강화 --conviction 보통
    python masters.py thesis check --symbol 005930       # 숫자 근거·위험 판정, 지난 점검 뒤 공시, 촉매
    python masters.py thesis show --symbol 005930 | thesis list | thesis metrics
    python masters.py find --name 삼성전자                 # 회사 이름으로 종목코드

기준값은 대가들이 쓴 기준을 한국 자료에 맞춰 이 스크립트가 정한 것이다 (예: 그레이엄의 '매출 1억 달러' 는
'매출 1조 원'). 기준마다 원래 기준과 바꾼 점을 같이 적는다. 대가별 관점의 틀은 그레이엄 『현명한 투자자』의
방어적 투자자 기준과, virattt/ai-hedge-fund (MIT, Copyright (c) 2024 Virat Singh) 의 hedge_fund/signals 프롬프트를
참고해 다시 썼다.

논지 추적(thesis)은 anthropics/financial-services 의 thesis-tracker (Anthropic, Apache License 2.0) 를 한국 주식용으로
옮기고 고친 것이다. 그 부분 머리의 설명, THIRD_PARTY_NOTICES.md, LICENSES/Apache-2.0.txt 를 보라.
"""

import argparse
import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
from krcommon import financials as fin, krx, krxdata  # noqa: E402

NOTE = ("이 점검표는 매수·매도 신호가 아니고 투자 권유가 아닙니다. 기준값은 한국 자료에 맞춰 정한 것이고, "
        "사업보고서(연간) 숫자만 씁니다.")


class MastersError(Exception):
    pass


# ---------------------------------------------------------------- 숫자

def won(x):
    if x is None:
        return "-"
    a = abs(x)
    if a >= 1e12:
        return "%.1f조" % (x / 1e12)
    if a >= 1e8:
        return "%.0f억" % (x / 1e8)
    return "{:,.0f}".format(x)


def pct(x, nd=1):
    return "-" if x is None else "%.*f%%" % (nd, 100.0 * x)


def ratio(a, b):
    return a / b if a is not None and b not in (None, 0) else None


def cagr(a, b, years):
    if a is None or b is None or a <= 0 or b <= 0 or years <= 0:
        return None
    return (b / a) ** (1.0 / years) - 1.0


def avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def derive(rows):
    """연도별 숫자에 ROE, 이익률, 부채비율, 유동비율, FCF 를 더한다."""
    out = []
    for i, r in enumerate(rows):
        d = dict(r)
        prev = rows[i - 1] if i and rows[i - 1]["year"] == r["year"] - 1 else None
        eq = r.get("equity_parent")
        base = avg([eq, prev.get("equity_parent")]) if prev else eq
        d["roe"] = ratio(r.get("ni_parent"), base) if base and base > 0 else None
        d["op_margin"] = ratio(r.get("op"), r.get("revenue"))
        d["net_margin"] = ratio(r.get("ni_parent"), r.get("revenue"))
        d["debt_ratio"] = ratio(r.get("liabilities"), r.get("equity")) if (r.get("equity") or 0) > 0 else None
        d["current_ratio"] = ratio(r.get("current_assets"), r.get("current_liabilities"))
        d["fcf"] = (r["ocf"] - r["capex"]) if r.get("ocf") is not None and r.get("capex") is not None else None
        out.append(d)
    return out


def crit(cid, label, value, rule, ok, note=""):
    return {"id": cid, "label": label, "value": value, "rule": rule, "ok": ok, "note": note}


# ---------------------------------------------------------------- 그레이엄 (방어적 투자자)

def graham(rows, mk):
    last = rows[-1]
    n = len(rows)
    ni = [r.get("ni_parent") for r in rows]
    cap = (mk or {}).get("mktcap")
    out = []
    rev = last.get("revenue")
    out.append(crit("G1", "규모", "매출 %s 원" % won(rev), "매출 1조 원 이상",
                    None if rev is None else rev >= 1e12, "원래 기준은 1971년 미국 매출 1억 달러"))
    cr = last.get("current_ratio")
    fin_note = "유동·비유동 구분이 없는 재무제표 (금융업 등)" if last.get("current_assets") is None else ""
    out.append(crit("G2", "유동비율", "-" if cr is None else "%.2f" % cr, "2.0 이상", None if cr is None else cr >= 2.0,
                    fin_note))
    wc = (last["current_assets"] - last["current_liabilities"]) if cr is not None else None
    ncl = last.get("noncurrent_liabilities")
    out.append(crit("G3", "장기 부채", "비유동부채 %s / 순유동자산 %s" % (won(ncl), won(wc)), "비유동부채 <= 순유동자산",
                    None if wc is None or ncl is None else ncl <= wc,
                    fin_note or "원래는 장기 부채. 여기서는 비유동부채 전체로 봐서 더 엄격하다"))
    known = [x for x in ni if x is not None]
    losses = sum(1 for x in known if x <= 0)
    out.append(crit("G4", "이익 안정", "%d년 중 적자 %d년" % (len(known), losses), "자료 있는 모든 해 흑자",
                    None if len(known) < 5 else losses == 0, "원래 10년. DART 자료는 2015년부터"))
    dps = [r.get("dps") for r in rows if r.get("dps") is not None]
    out.append(crit("G5", "배당 기록", "%d년 중 배당 %d년" % (len(dps), sum(1 for x in dps if x > 0)),
                    "자료 있는 모든 해 현금배당", None if len(dps) < 5 else all(x > 0 for x in dps), "원래 20년"))
    g6 = None
    val = "-"
    if n >= 6 and all(x is not None for x in ni[:3] + ni[-3:]):
        a, b = avg(ni[:3]), avg(ni[-3:])
        val = "앞 3년 평균 %s -> 최근 3년 평균 %s" % (won(a), won(b))
        g6 = a > 0 and b >= a * 4.0 / 3.0
    out.append(crit("G6", "이익 성장", val, "최근 3년 평균이 첫 3년 평균의 4/3 이상", g6,
                    "원래 10년 사이 1/3 이상. 여기서는 자료 기간 (%d년)" % n))
    e3 = avg(ni[-3:])
    per3 = ratio(cap, e3) if e3 and e3 > 0 else None
    out.append(crit("G7", "PER (3년 평균 이익)", "-" if per3 is None else "%.1f" % per3, "15 이하",
                    None if per3 is None else per3 <= 15, "" if cap else "시가총액 없음 (KRX 키)"))
    pbr = ratio(cap, last.get("equity_parent")) if (last.get("equity_parent") or 0) > 0 else None
    ok8 = None if pbr is None else (pbr <= 1.5 or (per3 is not None and per3 * pbr <= 22.5))
    out.append(crit("G8", "PBR", "-" if pbr is None else "%.2f (PER x PBR %s)" % (
        pbr, "-" if per3 is None else "%.1f" % (per3 * pbr)), "1.5 이하, 또는 PER x PBR 22.5 이하", ok8))
    return out


# ---------------------------------------------------------------- 버핏

def buffett(rows, mk, mk_start):
    last = rows[-1]
    roe = [r.get("roe") for r in rows if r.get("roe") is not None]
    out = []
    a = avg(roe)
    out.append(crit("B1", "자기자본이익률 (평균)", "%s (%d년)" % (pct(a), len(roe)), "15% 이상",
                    None if len(roe) < 3 else a >= 0.15, "지배주주 순이익 / 평균 지배주주 자본"))
    lo = min(roe) if roe else None
    out.append(crit("B2", "자기자본이익률 (가장 낮은 해)", pct(lo), "10% 아래로 내려간 해 없음",
                    None if len(roe) < 3 else lo >= 0.10))
    om = [r.get("op_margin") for r in rows]
    ok3, val = None, "-"
    if len(om) >= 6 and None not in om[:3] + om[-3:]:
        x, y = avg(om[:3]), avg(om[-3:])
        val = "첫 3년 %s -> 최근 3년 %s" % (pct(x), pct(y))
        ok3 = y >= x
    out.append(crit("B3", "영업이익률", val, "최근 3년 평균 >= 첫 3년 평균 (유지·개선)", ok3))
    dr = last.get("debt_ratio")
    out.append(crit("B4", "부채비율", pct(dr, 0), "100% 이하", None if dr is None else dr <= 1.0,
                    "부채총계 / 자본총계. 금융업은 높게 나온다"))
    fcf = [r.get("fcf") for r in rows if r.get("fcf") is not None]
    pos = sum(1 for x in fcf if x > 0)
    out.append(crit("B5", "잉여현금흐름", "%d년 중 플러스 %d년" % (len(fcf), pos), "80% 이상의 해에서 플러스",
                    None if len(fcf) < 3 else pos >= 0.8 * len(fcf), "영업활동현금흐름 - 유형자산 취득"))
    eqs = [(r["year"], r.get("equity_parent")) for r in rows if (r.get("equity_parent") or 0) > 0]
    g = cagr(eqs[0][1], eqs[-1][1], eqs[-1][0] - eqs[0][0]) if len(eqs) >= 2 else None
    out.append(crit("B6", "장부가치 성장", "지배주주 자본 연 %s" % pct(g), "연 10% 이상", None if g is None else g >= 0.10,
                    "증자·자사주 매입도 섞여 있다"))
    tail = rows[-5:]
    ok7, val = None, "-"
    if mk and mk_start and len(tail) == 5 and all(r.get("ni_parent") is not None for r in tail):
        kept = sum(r["ni_parent"] - (r.get("div_total") or 0.0) for r in tail)
        rise = mk["mktcap"] - mk_start["mktcap"]
        val = "유보이익 %s, 시가총액 증가 %s (%s -> %s)" % (won(kept), won(rise), mk_start["date"], mk["date"])
        ok7 = kept > 0 and rise >= kept
    out.append(crit("B7", "유보이익 1원당 시가총액", val, "최근 5년 유보이익보다 시가총액이 더 늘었음", ok7,
                    "유보이익 = 지배주주 순이익 - 현금배당금총액"))
    return out


def buffett_info(rows, mk):
    cap = (mk or {}).get("mktcap")
    f3 = avg([r.get("fcf") for r in rows[-3:]])
    e = rows[-1].get("ni_parent")
    return {"fcf_yield": ratio(f3, cap), "earnings_yield": ratio(e, cap)}


# ---------------------------------------------------------------- 린치

def lynch_kind(rows):
    """(유형, 순이익 연복리 성장률, 그 햇수). 최근 5년 (자료가 짧으면 자료 기간)."""
    ni = [r.get("ni_parent") for r in rows]
    known = [x for x in ni if x is not None]
    flips = sum(1 for a, b in zip(known, known[1:]) if (a > 0) != (b > 0))
    if len(known) >= 2 and known[-2] <= 0 < known[-1]:
        return "회생 (적자에서 흑자로)", None, 0
    if flips >= 2:
        return "경기순환·불규칙 (흑자와 적자가 %d번 바뀜)" % flips, None, 0
    a = rows[-6] if len(rows) >= 6 else rows[0]
    n = rows[-1]["year"] - a["year"]
    g = cagr(a.get("ni_parent"), rows[-1].get("ni_parent"), n)
    if g is None:
        return "알 수 없음 (적자가 섞임)", None, n
    for lo, word in ((0.20, "고성장"), (0.10, "우량 성장"), (0.0, "저성장")):
        if g >= lo:
            return "%s (순이익 %d년 연 %s)" % (word, n, pct(g)), g, n
    return "이익 감소 (순이익 %d년 연 %s)" % (n, pct(g)), g, n


def lynch(rows, mk):
    kind, g, gy = lynch_kind(rows)
    last = rows[-1]
    cap = (mk or {}).get("mktcap")
    per = ratio(cap, last.get("ni_parent")) if (last.get("ni_parent") or 0) > 0 else None
    out = []
    peg = per / (100.0 * g) if per is not None and g and g > 0 else None
    out.append(crit("L1", "PEG", "-" if peg is None else "%.2f (PER %.1f / 성장 %.1f)" % (peg, per, 100 * g), "1 이하",
                    None if peg is None else peg <= 1.0, "성장은 순이익 %d년 연복리" % gy if gy else ""))
    dy = ratio(last.get("dps"), (mk or {}).get("price"))
    dpeg = (100.0 * (g + (dy or 0.0))) / per if per is not None and g is not None and g > 0 else None
    out.append(crit("L2", "배당 더한 성장 / PER", "-" if dpeg is None else "%.2f (배당수익률 %s)" % (dpeg, pct(dy)),
                    "1 이상 (린치: 2 면 아주 좋음)", None if dpeg is None else dpeg >= 1.0))
    t = rows[-6:] if len(rows) >= 6 else rows
    yrs = t[-1]["year"] - t[0]["year"]
    rg = cagr(t[0].get("revenue"), t[-1].get("revenue"), yrs)
    m0, m1 = t[0].get("op_margin"), t[-1].get("op_margin")
    ok3 = None if rg is None or m0 is None or m1 is None else (rg > 0 and m1 >= m0)
    out.append(crit("L3", "매출이 이익으로", "매출 연 %s, 영업이익률 %s -> %s" % (pct(rg), pct(m0), pct(m1)),
                    "매출이 늘고 영업이익률이 %d년 전보다 낮지 않음" % yrs, ok3))
    ok4, val = None, "재고 없음"
    if len(rows) >= 2 and rows[-2].get("inventories") and last.get("inventories") is not None:
        ig = last["inventories"] / rows[-2]["inventories"] - 1
        sg = ratio(last.get("revenue"), rows[-2].get("revenue"))
        sg = sg - 1 if sg is not None else None
        val = "재고 %s, 매출 %s (전년 대비)" % (pct(ig), pct(sg))
        ok4 = None if sg is None else ig <= sg
    out.append(crit("L4", "재고", val, "재고 증가율 <= 매출 증가율", ok4, "재고가 매출보다 빨리 늘면 경고"))
    nl = ratio(last.get("noncurrent_liabilities"), last.get("equity")) if (last.get("equity") or 0) > 0 else None
    out.append(crit("L5", "부채", "비유동부채 / 자본 %s" % pct(nl, 0), "33% 이하", None if nl is None else nl <= 1 / 3.0,
                    "린치의 '자본 75 : 부채 25'. 장기 차입 대신 비유동부채 전체로 봄"))
    ni = [r.get("ni_parent") for r in rows[-6:]]
    ups = [b > a for a, b in zip(ni, ni[1:]) if a is not None and b is not None]
    out.append(crit("L6", "이익 꾸준히 증가", "%d번 중 %d번 증가" % (len(ups), sum(ups)), "최근 5번 중 4번 이상 전년보다 증가",
                    None if len(ups) < 5 else sum(ups) >= 4))
    return kind, out


def summary(cs):
    ok = sum(1 for c in cs if c["ok"] is True)
    no = sum(1 for c in cs if c["ok"] is False)
    na = sum(1 for c in cs if c["ok"] is None)
    return {"total": len(cs), "met": ok, "not_met": no, "no_data": na}


# ---------------------------------------------------------------- 실행

def start_market(rows, now_kst, transport=None, key=None, cache_dir=None):
    """최근 5 사업연도 직전 사업보고서가 나온 뒤 (그해 4월 1일 이후 첫 거래일)의 시가총액."""
    if len(rows) < 5:
        return None
    first = datetime.date(rows[-5]["year"], 4, 1)
    for k in range(14):
        d = first + datetime.timedelta(days=k)
        if d.weekday() >= 5:
            continue
        m = fin.market(rows[-1]["_sym"], key, d, now_kst, transport, cache_dir or krxdata.CACHE_DIR, lookback=1)
        if m and m["date"] >= first.isoformat():
            return m
    return None


def check(symbol, years=10, now_kst=None, progress=None, dart_transport=None, krx_transport=None, dart_key=None,
          krx_key=None, dart_cache=fin.CACHE, krx_cache=krxdata.CACHE_DIR):
    now_kst = now_kst or krx.kst_now()
    info, rows = fin.history(symbol, years, dart_key, dart_cache, now_kst, dart_transport, progress)
    if not rows:
        raise MastersError("%s 의 사업보고서 재무제표를 찾지 못했습니다 (2015년 이후)" % info["name"])
    rows = derive(rows)
    for r in rows:
        r["_sym"] = info["symbol"]
    mk = fin.market(info["symbol"], krx_key, None, now_kst, krx_transport, krx_cache)
    mk0 = start_market(rows, now_kst, krx_transport, krx_key, krx_cache) if mk else None
    kind, ly = lynch(rows, mk)
    doc = {"symbol": info["symbol"], "name": info["name"], "years": [r["year"] for r in rows],
           "fs_div": rows[-1]["fs_div"], "filed": rows[-1]["filed"], "market": mk, "market_start": mk0,
           "rows": [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows],
           "graham": graham(rows, mk), "buffett": buffett(rows, mk, mk0), "buffett_info": buffett_info(rows, mk),
           "lynch_kind": kind, "lynch": ly}
    for k in ("graham", "buffett", "lynch"):
        doc[k + "_summary"] = summary(doc[k])
    return doc


def find(name, key=None, cache=fin.CACHE, now_kst=None, transport=None):
    """회사 이름(일부)으로 상장사 [(종목코드, 이름)]. 이름이 똑같은 회사가 먼저."""
    key = key or fin.dart.api_key()
    if not key:
        raise fin.FinError("DART_API_KEY 가 없습니다. 터미널에서 python krcommon/keys.py dart 로 넣으세요")
    want = "".join(name.split())
    codes = fin.corp_codes(key, cache, now_kst or krx.kst_now(), transport)
    hits = [(s, v["name"]) for s, v in codes.items() if want in "".join(v["name"].split())]
    return sorted(hits, key=lambda h: ("".join(h[1].split()) != want, len(h[1]), h[0]))


def mark(ok):
    return {True: "O", False: "X", None: "-"}[ok]


def print_doc(doc):
    mk = doc["market"]
    fs = "연결" if doc["fs_div"] == "CFS" else "별도"
    f = doc["filed"]
    print("%s (%s) 대가 점검표" % (doc["name"], doc["symbol"]))
    print("자료: DART 사업보고서 %d ~ %d (%s, 최근 접수 %s-%s-%s)%s" % (
        doc["years"][0], doc["years"][-1], fs, f[:4], f[4:6], f[6:8],
        ", 시가총액 KRX %s %s 원 (보통주+우선주)" % (mk["date"], won(mk["mktcap"])) if mk else ", 시가총액 없음 (KRX 키 없음)"))
    print()
    print("%-6s %10s %8s %10s %8s %8s %8s %10s %8s" % ("연도", "매출", "영업이익률", "순이익", "ROE", "부채비율", "유동비율",
                                                    "잉여현금", "주당배당"))
    for r in doc["rows"]:
        print("%-6d %10s %8s %10s %8s %8s %8s %10s %8s" % (
            r["year"], won(r.get("revenue")), pct(r.get("op_margin")), won(r.get("ni_parent")), pct(r.get("roe")),
            pct(r.get("debt_ratio"), 0), "-" if r.get("current_ratio") is None else "%.2f" % r["current_ratio"],
            won(r.get("fcf")), "-" if r.get("dps") is None else "{:,.0f}".format(r["dps"])))
    print("(순이익은 지배주주 몫, 주당배당은 그 해 보고서 금액이라 액면분할 전 해는 분할 전 금액)")
    titles = (("graham", "그레이엄 (방어적 투자자)"), ("buffett", "버핏"), ("lynch", "린치"))
    for k, t in titles:
        s = doc[k + "_summary"]
        print()
        extra = " / 유형: %s" % doc["lynch_kind"] if k == "lynch" else ""
        print("%s: %d개 중 %d개 충족, %d개 못 미침, %d개 자료 없음%s" % (t, s["total"], s["met"], s["not_met"], s["no_data"],
                                                             extra))
        for c in doc[k]:
            print("  [%s] %s: %s (기준 %s)%s" % (mark(c["ok"]), c["label"], c["value"], c["rule"],
                                              " - " + c["note"] if c["note"] else ""))
        if k == "buffett":
            bi = doc["buffett_info"]
            print("  참고: 이익수익률(순이익 / 시가총액) %s, 잉여현금 수익률(최근 3년 평균) %s. 국고채 금리와 견줘 보는 숫자다"
                  % (pct(bi["earnings_yield"]), pct(bi["fcf_yield"])))
    print()
    print(NOTE)


# ---------------------------------------------------------------- 논지 추적 (thesis)
#
# 이 부분과 SKILL.md 의 "논지 추적" 은 anthropics/financial-services 의
# plugins/vertical-plugins/equity-research/skills/thesis-tracker/SKILL.md (Anthropic, Apache License 2.0,
# 커밋 ef165e4160f2) 를 한국 주식용으로 옮기고 고친 것이다. 원본은 Claude 가 따르는 절차 문서이고, 여기서는
# 그 절차를 코드로 만들었다. 바꾼 것:
#   - 한국어로 옮김.
#   - 숫자로 쓸 수 있는 근거와 위험은 DART 사업보고서 값과 KRX 시가총액으로 자동 판정한다.
#   - 지난 점검 뒤 나온 DART 공시를 보여 준다.
#   - 매수 논지만 다룬다 (공매도 논지 뺌).
#   - 조치, 확신도, 목표가, 손절 조건은 사용자가 적는 칸으로만 둔다. 코드와 Claude 는 제안하지 않는다.
#   - 아침 회의, 위원회용 출력 형식은 뺐다.
# 라이선스 전문은 LICENSES/Apache-2.0.txt, 출처는 THIRD_PARTY_NOTICES.md.

THESIS_DIR = os.path.join("trader-kr", "thesis")
REVIEW_DAYS = 92                     # 원본: 아무 일이 없어도 최소 분기마다 본다
IMPACTS = ("강화", "약화", "중립")
CONVICTIONS = ("높음", "보통", "낮음")
COND_RE = re.compile(r"^\s*(\S+)\s*(>=|<=|>|<)\s*(-?[\d.,]+)\s*(%|조|억|원|배)?\s*$")


def _growth(rows, k):
    if len(rows) < 2 or rows[-2]["year"] != rows[-1]["year"] - 1:
        return None
    a, b = rows[-2].get(k), rows[-1].get(k)
    return b / a - 1.0 if a and a > 0 and b is not None else None


def _per(rows, mk):
    ni = rows[-1].get("ni_parent")
    return ratio((mk or {}).get("mktcap"), ni) if (ni or 0) > 0 else None


def _pbr(rows, mk):
    eq = rows[-1].get("equity_parent")
    return ratio((mk or {}).get("mktcap"), eq) if (eq or 0) > 0 else None


METRICS = {
    "매출성장": ("%", "최근 사업연도 매출 전년 대비", lambda rows, mk: _growth(rows, "revenue")),
    "순이익성장": ("%", "지배주주 순이익 전년 대비", lambda rows, mk: _growth(rows, "ni_parent")),
    "영업이익률": ("%", "영업이익 / 매출", lambda rows, mk: rows[-1].get("op_margin")),
    "순이익률": ("%", "지배주주 순이익 / 매출", lambda rows, mk: rows[-1].get("net_margin")),
    "ROE": ("%", "지배주주 순이익 / 평균 지배주주 자본", lambda rows, mk: rows[-1].get("roe")),
    "부채비율": ("%", "부채총계 / 자본총계", lambda rows, mk: rows[-1].get("debt_ratio")),
    "유동비율": ("배", "유동자산 / 유동부채", lambda rows, mk: rows[-1].get("current_ratio")),
    "잉여현금": ("원", "영업활동현금흐름 - 유형자산 취득", lambda rows, mk: rows[-1].get("fcf")),
    "주당배당": ("원", "보통주 주당 현금배당금", lambda rows, mk: rows[-1].get("dps")),
    "PER": ("배", "시가총액 / 지배주주 순이익", _per),
    "PBR": ("배", "시가총액 / 지배주주 자본", _pbr),
    "배당수익률": ("%", "주당배당 / 주가", lambda rows, mk: ratio(rows[-1].get("dps"), (mk or {}).get("price"))),
}
UNITS = {"%": 0.01, "조": 1e12, "억": 1e8, "원": 1.0, "배": 1.0, "": 1.0}


def parse_cond(text):
    """'매출성장 >= 5%' -> {"metric", "op", "value"} (값은 비율이면 0.05)."""
    m = COND_RE.match(text or "")
    if not m or m.group(1) not in METRICS:
        raise MastersError("조건 '%s' 을 읽지 못했습니다. 예: \"매출성장 >= 5%%\", \"부채비율 > 100%%\", \"PER <= 15\". "
                           "지표: %s" % (text, ", ".join(METRICS)))
    name, op, num, unit = m.group(1), m.group(2), float(m.group(3).replace(",", "")), m.group(4) or ""
    kind = METRICS[name][0]
    if (kind == "%") != (unit == "%"):
        raise MastersError("%s 는 %s 로 적습니다 (받은 값 '%s')" % (name, "퍼센트(%)" if kind == "%" else "숫자", text))
    if (kind == "배" and unit not in ("", "배")) or (kind == "원" and unit not in ("", "원", "억", "조")):
        raise MastersError("%s 에 단위 '%s' 를 쓸 수 없습니다" % (name, unit))
    return {"metric": name, "op": op, "value": num * UNITS[unit]}


def cond_text(c):
    kind = METRICS[c["metric"]][0]
    v = c["value"]
    shown = ("%g%%" % (100 * v)) if kind == "%" else (won(v) if kind == "원" else "%g" % v)
    return "%s %s %s" % (c["metric"], c["op"], shown)


def metric_text(name, v):
    if v is None:
        return "-"
    kind = METRICS[name][0]
    return pct(v) if kind == "%" else (won(v) + " 원" if kind == "원" else "%.2f" % v)


def holds(c, v):
    if v is None:
        return None
    return {">=": v >= c["value"], "<=": v <= c["value"], ">": v > c["value"], "<": v < c["value"]}[c["op"]]


def parse_item(text, kind, n):
    """'설명 | 조건' 또는 '설명'. kind: P (근거) / R (위험)."""
    desc, _, cond = (text or "").partition("|")
    if not desc.strip():
        raise MastersError("%s 설명이 비었습니다" % ("근거" if kind == "P" else "위험"))
    item = {"id": "%s%d" % (kind, n), "text": desc.strip()}
    if cond.strip():
        item["cond"] = parse_cond(cond)
    return item


def parse_catalyst(text, n):
    parts = [x.strip() for x in (text or "").split("|")]
    try:
        d = datetime.date.fromisoformat(parts[0])
    except ValueError:
        raise MastersError("촉매는 '2027-01-30 | 4분기 실적 발표 | 기대 영향' 처럼 날짜부터 적습니다 (받은 값 '%s')" % text)
    if len(parts) < 2 or not parts[1]:
        raise MastersError("촉매 '%s' 에 사건이 없습니다" % text)
    return {"id": "C%d" % n, "date": d.isoformat(), "event": parts[1], "expect": parts[2] if len(parts) > 2 else "",
            "done": None}


def thesis_path(sym, folder=THESIS_DIR):
    return os.path.join(folder, "%s.json" % krx.norm_symbol(sym))


def load_thesis(sym, folder=THESIS_DIR):
    p = thesis_path(sym, folder)
    if not os.path.exists(p):
        raise MastersError("%s 논지가 없습니다. thesis new 로 먼저 적으세요" % krx.norm_symbol(sym))
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def save_thesis(doc, folder=THESIS_DIR):
    krx.write_json(thesis_path(doc["symbol"], folder), doc)


def next_id(items, kind):
    return 1 + max([int(x["id"][1:]) for x in items if x["id"].startswith(kind)] or [0])


def thesis_new(a, now, folder=THESIS_DIR):
    sym = krx.norm_symbol(a.symbol)
    if os.path.exists(thesis_path(sym, folder)):
        raise MastersError("%s 논지가 이미 있습니다. 덮어쓰지 않습니다. 고치려면 thesis add 로 기록을 더하세요" % sym)
    if not (a.statement or "").strip():
        raise MastersError("--statement 에 논지를 한두 문장으로 적으세요")
    if not a.pillar:
        raise MastersError("--pillar 로 근거를 하나 이상 적으세요 (원본은 3 ~ 5개)")
    if not a.risk:
        raise MastersError("--risk 로 논지를 깨뜨릴 위험을 하나 이상 적으세요. 반증할 수 없는 논지는 논지가 아닙니다")
    doc = {"symbol": sym, "name": a.name or "", "position": "매수", "created": now.date().isoformat(),
           "statement": a.statement.strip(),
           "pillars": [parse_item(t, "P", i + 1) for i, t in enumerate(a.pillar)],
           "risks": [parse_item(t, "R", i + 1) for i, t in enumerate(a.risk)],
           "catalysts": [parse_catalyst(t, i + 1) for i, t in enumerate(a.catalyst or [])],
           "target": a.target or "", "exit": a.exit or "", "log": [], "checks": [],
           "source": "anthropics/financial-services thesis-tracker (Apache-2.0) 를 옮김. THIRD_PARTY_NOTICES.md"}
    save_thesis(doc, folder)
    return doc


def thesis_add(a, now, folder=THESIS_DIR):
    doc = load_thesis(a.symbol, folder)
    for t in a.pillar or []:
        doc["pillars"].append(parse_item(t, "P", next_id(doc["pillars"], "P")))
    for t in a.risk or []:
        doc["risks"].append(parse_item(t, "R", next_id(doc["risks"], "R")))
    for t in a.catalyst or []:
        doc["catalysts"].append(parse_catalyst(t, next_id(doc["catalysts"], "C")))
    ids = {x["id"] for x in doc["pillars"] + doc["risks"] + doc["catalysts"]}
    impacts = []
    for t in a.impact or []:
        k, _, d = t.partition(":")
        if k.strip() not in ids or d.strip() not in IMPACTS:
            raise MastersError("--impact 는 'P1:강화' 처럼 있는 번호와 강화·약화·중립 중 하나 (받은 값 '%s')" % t)
        impacts.append({"id": k.strip(), "dir": d.strip()})
    for t in a.done or []:
        k, _, res = t.partition(":")
        c = [x for x in doc["catalysts"] if x["id"] == k.strip()]
        if not c:
            raise MastersError("--done 의 촉매 번호 '%s' 가 없습니다" % k)
        c[0]["done"] = {"date": a.date or now.date().isoformat(), "result": res.strip()}
    if a.conviction and a.conviction not in CONVICTIONS:
        raise MastersError("--conviction 은 높음·보통·낮음 중 하나입니다")
    if a.data or impacts or a.action or a.conviction:
        if not a.data:
            raise MastersError("--data 에 무엇이 바뀌었는지 적으세요")
        doc["log"].append({"date": a.date or now.date().isoformat(), "data": a.data, "impact": impacts,
                           "action": a.action or "", "conviction": a.conviction or ""})
    for k in ("target", "exit"):
        if getattr(a, k):
            doc[k] = getattr(a, k)
    save_thesis(doc, folder)
    return doc


def last_review(doc):
    return max([doc["created"]] + [c["date"] for c in doc["checks"]] + [x["date"] for x in doc["log"]])


def trend(it, v, pv, first):
    if first:
        return "처음"
    if v is None or pv is None:
        return "-"
    if v == pv:
        return "같음"
    c = it["cond"]
    good = (v > pv) == (c["op"] in (">=", ">"))
    if it["id"].startswith("R"):
        good = not good
    return "%s (%s -> %s)" % ("좋아짐" if good else "나빠짐", metric_text(c["metric"], pv), metric_text(c["metric"], v))


def thesis_check(sym, now, folder=THESIS_DIR, dart_transport=None, krx_transport=None, dart_key=None, krx_key=None,
                 dart_cache=fin.CACHE, krx_cache=krxdata.CACHE_DIR, save=True):
    """논지의 숫자 근거·위험을 최근 사업보고서와 시가총액으로 판정하고, 지난 점검 뒤 공시와 촉매를 모은다."""
    doc = load_thesis(sym, folder)
    info, rows = fin.history(sym, 3, dart_key, dart_cache, now, dart_transport)
    if not rows:
        raise MastersError("%s 의 사업보고서 재무제표를 찾지 못했습니다" % sym)
    rows = derive(rows)
    mk = fin.market(sym, krx_key, None, now, krx_transport, krx_cache)
    prev = doc["checks"][-1] if doc["checks"] else None
    values, oks, card = {}, {}, []
    for it in doc["pillars"] + doc["risks"]:
        row = {"id": it["id"], "text": it["text"], "cond": cond_text(it["cond"]) if it.get("cond") else "-",
               "now": "-", "ok": None, "trend": "", "note": ""}
        if it.get("cond"):
            v = METRICS[it["cond"]["metric"]][2](rows, mk)
            values[it["id"]], oks[it["id"]] = v, holds(it["cond"], v)
            row["now"], row["ok"] = metric_text(it["cond"]["metric"], v), oks[it["id"]]
            row["trend"] = trend(it, v, (prev or {}).get("values", {}).get(it["id"]), prev is None)
        logs = [(x["date"], i["dir"]) for x in doc["log"] for i in x["impact"] if i["id"] == it["id"]]
        if logs:
            row["note"] = "기록 %d건, 마지막 %s %s" % (len(logs), logs[-1][1], logs[-1][0])
        card.append(row)
    if prev:
        since = datetime.date.fromisoformat(prev["filings_until"]) + datetime.timedelta(days=1)
    else:
        since = datetime.date.fromisoformat(doc["created"])
    filings = []
    if since <= now.date():
        filings = fin.dart.corp_list(info["corp_code"], since, now.date(), dart_key or fin.dart.api_key(), dart_transport)
    today = now.date().isoformat()
    soon = (now.date() + datetime.timedelta(days=120)).isoformat()
    out = {"thesis": doc, "name": info["name"], "card": card, "filings": filings, "since": since.isoformat(),
           "catalysts_due": [c for c in doc["catalysts"] if c["date"] < today and not c.get("done")],
           "catalysts_next": [c for c in doc["catalysts"] if today <= c["date"] <= soon and not c.get("done")],
           "previous": prev["date"] if prev else None,
           "report": {"year": rows[-1]["year"], "fs_div": rows[-1]["fs_div"], "filed": rows[-1]["filed"]},
           "market": mk, "date": today}
    if save:
        doc["name"] = doc["name"] or info["name"]
        doc["checks"].append({"date": today, "values": values, "ok": oks, "filings_until": today,
                              "report_year": rows[-1]["year"], "market_date": (mk or {}).get("date")})
        save_thesis(doc, folder)
    return out


THESIS_NOTE = ("조치, 확신도, 목표가, 손절 조건은 사용자가 적는 칸입니다. 이 점검은 매수·매도나 조치를 제안하지 않고, "
               "투자 권유가 아닙니다.")


def item_state(it, ok):
    if not it.get("cond"):
        return "숫자 조건 없음"
    words = ("충족", "못 미침") if it["id"].startswith("P") else ("일어남", "아직 아님")
    return "자료 없음" if ok is None else words[0] if ok else words[1]


def print_check(res):
    doc = res["thesis"]
    r, mk, f = res["report"], res["market"], res["report"]["filed"]
    print("%s (%s) 논지 점검 %s (지난 점검 %s)" % (res["name"], doc["symbol"], res["date"], res["previous"] or "없음"))
    print("논지: %s" % doc["statement"])
    print("자료: DART %d 사업보고서 (%s, 접수 %s-%s-%s)%s" % (
        r["year"], "연결" if r["fs_div"] == "CFS" else "별도", f[:4], f[4:6], f[6:8],
        ", KRX %s 시가총액 %s 원" % (mk["date"], won(mk["mktcap"])) if mk else ", 시가총액 없음 (KRX 키)"))
    items = {x["id"]: x for x in doc["pillars"] + doc["risks"]}
    for title, kind in (("근거 점수표 (근거 / 처음 기대 / 지금 / 추세)", "P"), ("위험 (조건이 맞으면 위험이 일어난 것)", "R")):
        print()
        print(title)
        for c in [x for x in res["card"] if x["id"].startswith(kind)]:
            print("  %s %s | %s | %s %s | %s%s" % (
                c["id"], c["text"], c["cond"], c["now"], item_state(items[c["id"]], c["ok"]), c["trend"] or "-",
                " | " + c["note"] if c["note"] else ""))
    print()
    if res["catalysts_due"]:
        print("지난 촉매 (결과를 thesis add --done 으로 적으세요): " + "; ".join(
            "%s %s %s" % (c["id"], c["date"], c["event"]) for c in res["catalysts_due"]))
    if res["catalysts_next"]:
        print("다가올 촉매 (120일 안): " + "; ".join("%s %s %s" % (c["id"], c["date"], c["event"])
                                              for c in res["catalysts_next"]))
    fl = res["filings"]
    print("%s 부터 DART 공시 %d건%s" % (res["since"], len(fl), ":" if fl else ""))
    for x in fl[-12:]:
        d = str(x.get("rcept_dt") or x.get("rcept_no") or "")[:8]
        print("  %s-%s-%s %s%s" % (d[:4], d[4:6], d[6:8], " ".join(str(x.get("report_nm") or "").split()),
                                   " [!]" if fin.dart.flagged(x) else ""))
    if len(fl) > 12:
        print("  (앞 %d건 생략)" % (len(fl) - 12))
    if doc["log"]:
        x = doc["log"][-1]
        print("마지막 기록 %s: %s (조치: %s, 확신도: %s)" % (x["date"], x["data"], x["action"] or "적지 않음",
                                                    x["conviction"] or "적지 않음"))
    if doc.get("target") or doc.get("exit"):
        print("사용자가 적은 목표: %s / 손절 조건: %s" % (doc.get("target") or "-", doc.get("exit") or "-"))
    print()
    print(THESIS_NOTE)


def print_show(doc, now):
    print("%s (%s) 논지, %s 에 적음" % (doc.get("name") or "", doc["symbol"], doc["created"]))
    print("논지: %s" % doc["statement"])
    days = (now.date() - datetime.date.fromisoformat(last_review(doc))).days
    print("마지막으로 본 날 %s (%d일 전)%s" % (last_review(doc), days,
                                       ". 분기마다 보라는 원칙에 따라 점검할 때입니다" if days > REVIEW_DAYS else ""))
    last = doc["checks"][-1] if doc["checks"] else None
    for it in doc["pillars"] + doc["risks"]:
        st = "점검 전"
        if last and it.get("cond"):
            st = "%s (%s 점검)" % (item_state(it, last["ok"].get(it["id"])), last["date"])
        elif not it.get("cond"):
            st = item_state(it, None)
        print("  %s %s | %s | %s" % (it["id"], it["text"], cond_text(it["cond"]) if it.get("cond") else "-", st))
    for c in doc["catalysts"]:
        print("  %s %s %s%s" % (c["id"], c["date"], c["event"],
                                " -> %s (%s)" % (c["done"]["result"], c["done"]["date"]) if c.get("done") else ""))
    for x in doc["log"][-5:]:
        imp = ", ".join("%s %s" % (i["id"], i["dir"]) for i in x["impact"])
        print("  기록 %s: %s%s (조치: %s, 확신도: %s)" % (x["date"], x["data"], " [%s]" % imp if imp else "",
                                                 x["action"] or "-", x["conviction"] or "-"))
    print(THESIS_NOTE)


def thesis_list(folder=THESIS_DIR, now=None):
    out = []
    if os.path.isdir(folder):
        for f in sorted(os.listdir(folder)):
            if f.endswith(".json"):
                with open(os.path.join(folder, f), encoding="utf-8") as fh:
                    doc = json.load(fh)
                days = (now.date() - datetime.date.fromisoformat(last_review(doc))).days
                out.append({"symbol": doc["symbol"], "name": doc.get("name"), "last": last_review(doc), "days": days,
                            "due": days > REVIEW_DAYS, "statement": doc["statement"]})
    return out


def thesis_main(a):
    now = krx.parse_now(a.now)
    folder = a.dir
    if a.tcmd == "metrics":
        for k, (unit, desc, _) in METRICS.items():
            print("  %s %s (%s)" % (krx.pad(k, 10), desc, unit))
        return 0
    if a.tcmd == "list":
        rows = thesis_list(folder, now)
        if not rows:
            print("적어 둔 논지가 없습니다 (%s)" % folder)
        for r in rows:
            print("  %s %s, 마지막으로 본 날 %s (%d일 전)%s: %s" % (r["symbol"], r["name"] or "", r["last"], r["days"],
                                                             " [점검할 때]" if r["due"] else "", r["statement"]))
        return 0
    if a.tcmd == "new":
        doc = thesis_new(a, now, folder)
        print("%s 논지를 적었습니다 (%s). 근거 %d개, 위험 %d개, 촉매 %d개. thesis check 로 숫자를 재세요" % (
            doc["symbol"], thesis_path(doc["symbol"], folder), len(doc["pillars"]), len(doc["risks"]),
            len(doc["catalysts"])))
        if len(doc["pillars"]) < 3 or len(doc["risks"]) < 3:
            print("원본은 근거와 위험을 3 ~ 5개씩 적으라고 합니다. thesis add 로 더할 수 있습니다")
        return 0
    if a.tcmd == "add":
        doc = thesis_add(a, now, folder)
        print("%s 논지를 고쳤습니다. 근거 %d개, 위험 %d개, 촉매 %d개, 기록 %d건" % (
            doc["symbol"], len(doc["pillars"]), len(doc["risks"]), len(doc["catalysts"]), len(doc["log"])))
        return 0
    if a.tcmd == "show":
        print_show(load_thesis(a.symbol, folder), now)
        return 0
    if a.tcmd == "check":
        res = thesis_check(a.symbol, now, folder, save=not a.dry_run)
        if a.json:
            print(json.dumps({k: v for k, v in res.items() if k != "thesis"}, ensure_ascii=False, indent=1))
        else:
            print_check(res)
        return 0
    return 1


def add_thesis_parser(sub):
    t = sub.add_parser("thesis", help="매수 논지를 적고 숫자·공시로 다시 본다 (논지 추적)")
    ts = t.add_subparsers(dest="tcmd")
    n = ts.add_parser("new", help="논지 적기 (덮어쓰지 않음)")
    n.add_argument("--statement", help="논지 한두 문장")
    n.add_argument("--name", help="회사 이름 (없으면 check 때 DART 이름)")
    ad = ts.add_parser("add", help="기록, 근거·위험·촉매 더하기, 촉매 결과")
    ad.add_argument("--data", help="무엇이 바뀌었나")
    ad.add_argument("--impact", action="append", help="P1:강화, R2:약화, P3:중립 (여러 번)")
    ad.add_argument("--action", help="사용자가 정한 조치 (그대로 적기만 함)")
    ad.add_argument("--conviction", help="높음 / 보통 / 낮음")
    ad.add_argument("--done", action="append", help="C1:결과 (촉매가 지나간 뒤)")
    ad.add_argument("--date", help="기록 날짜 (기본 오늘)")
    for x in (n, ad):
        x.add_argument("--pillar", action="append", help='근거 "설명 | 매출성장 >= 5%%" (조건은 없어도 됨)')
        x.add_argument("--risk", action="append", help='위험 "설명 | 부채비율 > 100%%" (조건이 맞으면 위험이 일어난 것)')
        x.add_argument("--catalyst", action="append", help='촉매 "2027-01-30 | 4분기 실적 | 기대 영향"')
        x.add_argument("--target", help="사용자가 정한 목표 (그대로 적기만 함)")
        x.add_argument("--exit", help="사용자가 정한 손절·정리 조건 (그대로 적기만 함)")
    s = ts.add_parser("show", help="적어 둔 논지와 기록 (네트워크 없음)")
    c = ts.add_parser("check", help="숫자 근거·위험 판정, 지난 점검 뒤 공시, 촉매")
    c.add_argument("--dry-run", action="store_true", help="점검 기록을 남기지 않음")
    c.add_argument("--json", action="store_true")
    ts.add_parser("list", help="적어 둔 논지들과 마지막으로 본 날")
    ts.add_parser("metrics", help="조건에 쓸 수 있는 지표")
    for x in (n, ad, s, c):
        x.add_argument("--symbol", required=True)
    for x in ts.choices.values():
        x.add_argument("--dir", default=THESIS_DIR, help="논지 폴더 (기본 trader-kr/thesis)")
        x.add_argument("--now")
    return t


def main(argv=None):
    krx.utf8_stdio()
    ap = argparse.ArgumentParser(description="투자 대가 점검표와 논지 추적 (trader-kr-masters).")
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("check", help="한 회사의 그레이엄·버핏·린치 기준")
    c.add_argument("--symbol", required=True)
    c.add_argument("--years", type=int, default=10, help="최근 사업연도 수 (기본 10, 2015년부터)")
    c.add_argument("--now")
    c.add_argument("--json", action="store_true")
    fd = sub.add_parser("find", help="회사 이름으로 종목코드 찾기 (DART 회사 고유번호 목록)")
    fd.add_argument("--name", required=True)
    t = add_thesis_parser(sub)
    a = ap.parse_args(argv)
    if not a.cmd or (a.cmd == "thesis" and not a.tcmd):
        (t if a.cmd else ap).print_help()
        return 1
    try:
        if a.cmd == "thesis":
            return thesis_main(a)
        if a.cmd == "find":
            hits = find(a.name)
            if not hits:
                print("'%s' 이 이름에 들어간 상장사가 없습니다" % a.name)
            for sym, nm in hits[:20]:
                print("  %s %s" % (sym, nm))
            return 0 if hits else 2
        now = krx.parse_now(a.now)
        if a.years < 3:
            raise MastersError("--years 는 3 이상입니다")
        prog = None if a.json else (lambda m: print(m, file=sys.stderr, flush=True))
        doc = check(a.symbol, a.years, now, prog)
    except (MastersError, fin.FinError, fin.dart.DartError, krxdata.KrxDataError, krx.KrxError) as e:
        print("멈춤: %s" % e, file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(doc, ensure_ascii=False, indent=1))
    else:
        print_doc(doc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
