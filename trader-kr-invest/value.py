#!/usr/bin/env python3
"""가치 규칙 실험실 (trader-kr-invest/value.py): PBR, PER, 배당수익률과 대가 점검표(그레이엄·버핏·린치)로 고른 30종목을
해마다 4월 말에 바꿔 들고 가는 규칙을, 시가총액 상위 300 을 시가총액 비중대로 1년씩 들고 가는 기준(BV)과 비교한다.
배당을 넣은 수익률이다. 표준 라이브러리만 쓴다. 주문을 내지 않는다. 지금 살 종목은 내지 않는다 (과거 성적만 낸다).

    python value.py data                  # 4월 말 종목군 회사의 DART 재무·배당·접수 기록·기업개황과 KRX 표 (캐시에 없는 것만)
    python value.py check                 # 받은 범위와 자료 점검. 하나라도 걸리면 "멈춤" 으로 끝난다
    python value.py rules
    python value.py run --rule "저PBR 30종목 연1회"
    python value.py run --rule "그레이엄 점검표 30종목 연1회" --delist B --cost-x 2 --json
    python value.py forward lock --name value-2026-11 --judge "저PBR+고ROE 30종목 연1회" --judge "..."
    python value.py forward update --name value-2026-11      # 잠근 뒤 끝난 달만 채점 (매달 invest.py data 뒤)

가격, 종목군 U, 비용, 상장폐지 두 판은 invest.py 와 같다 (KRX 일별매매정보를 먼저 python invest.py data 로 받는다).
신호일 T 는 해마다 4월 마지막 거래일, 체결은 다음 거래일 종가, 다음 해 체결일까지 산 그대로 들고 간다.

가치 규칙의 대상 V(T): U(T) 에서 금융(업종 코드 64·65·66, 다만 재무상태표에 유동자산이 있는 지주회사 64992 는 남김),
리츠(회사 이름에 "부동산투자회사"), 12월 결산이 아닌 회사, 코드 재사용 종목, FY(T-1) 사업보고서가 T 까지 접수되지 않은
회사를 뺀다.

시점: T 에는 정정 전 첫 접수일이 T 이하인 사업보고서만 쓴다 (OpenDART 공시 목록). 재무제표 API 는 정정된 값을 주므로
숫자는 T 뒤에 고쳐진 값일 수 있다 (--strict 는 T 에 쓰는 최근 보고서가 T 뒤에 정정된 회사를 뺀다).
배당: FY y 보통주 주당 현금배당금을 y 년 12월 마지막 거래일 종가로 그 종목에 다시 넣는다 (기준일 근사).
대가 점검표 기준은 trader-kr-masters/masters.py 의 graham, buffett, lynch 를 그대로 부른다.
r4 규칙 4개: 피오트로스키 F-score (유상증자 신호를 뺀 8개), 노비-마크스 매출총이익/자산, 둘을 PBR 과 섞은 것,
시가총액가중 기준에서 F-score 낮은 종목을 뺀 것.
과거 성적은 미래를 약속하지 않는다.
"""

import argparse
import collections
import datetime
import hashlib
import importlib.util
import inspect
import json
import os
import re
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir))
from krcommon import dart, financials as fin, krx, krxdata  # noqa: E402


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


inv = _load(os.path.join(HERE, "invest.py"), "trader_invest_engine")
ms = _load(os.path.join(HERE, os.pardir, "trader-kr-masters", "masters.py"), "trader_masters_rules")

FIRST_SIGNAL = "2016-04"          # FY2015 (DART 전체 재무제표 첫 사업연도) 사업보고서 뒤 첫 4월 말
N = 30
FIN_PREFIX = ("64", "65", "66")   # 한국표준산업분류 금융 및 보험업
HOLDING = "64992"                 # 지주회사 (금융지주와 비금융 지주가 같은 코드)
REIT_WORD = "부동산투자회사"
MIN_V = 150                       # 자료 점검 2: T 마다 V(T) 가 이보다 작으면 멈춤
FILED_MIN = 0.90                  # 자료 점검 1
GAP_MAX = 0.03                    # 자료 점검 3, 4
YIELD_GAP_MAX = 0.005             # 자료 점검 5 (배당수익률 차이 중앙값)
MAX_CALLS = 18000                 # 한 번에 부르는 DART 상한 (OpenDART 는 하루 2만 건쯤에서 요청 제한 020)
RETRIES = 4                       # 연결이 끊기면 다시 부르는 횟수 (2026-11-01 첫 받기가 2,308건째에 끊겼다)
RETRY_WAIT = 2.0                  # 초. 다시 부를 때마다 두 배
NOTE = "과거 성적은 미래를 약속하지 않습니다. 배당을 넣은 수익률(12월 근사)이고, 지금 살 종목은 내지 않습니다."

FAMS = (("pbr", "저PBR"), ("per", "저PER"), ("dy", "고배당"), ("pbr_roe", "저PBR+고ROE"), ("pbr_mom", "저PBR+모멘텀"),
        ("graham", "그레이엄 점검표"), ("buffett", "버핏 점검표"), ("lynch", "린치 점검표"))
FAMS4 = (("f_pbr", "저PBR 3분의1 중 F-score 상위"), ("gpa", "매출총이익/자산 상위"), ("gpa_pbr", "매출총이익/자산+저PBR"))
MAX_F = 2                         # r4: 시가총액가중에서 뺄 F-score (8개 중 2 이하)
BENCH = {"fam": "bv", "reb": "Y", "label": "BV 시가총액가중 %d 연1회" % inv.UNIVERSE}
BV_F = {"fam": "bv_f", "reb": "Y", "max_f": MAX_F, "label": "시가총액가중 F-score %d 이하 제외 연1회" % MAX_F}
EWV = {"fam": "ewv", "reb": "Y", "label": "EWV 가치 대상 동일가중 연1회"}


class ValueLabError(Exception):
    pass


def rules():
    """r3 에 사전 등록한 8개 (순서 고정)."""
    return [{"fam": f, "n": N, "reb": "Y", "label": "%s %d종목 연1회" % (t, N)} for f, t in FAMS]


def rules_r4():
    """r4 에 사전 등록한 4개 (순서 고정)."""
    return [{"fam": f, "n": N, "reb": "Y", "label": "%s %d종목 연1회" % (t, N)} for f, t in FAMS4] + [dict(BV_F)]


def find_rule(text):
    for v in rules() + rules_r4() + [BENCH, EWV]:
        if v["label"] == text.strip():
            return v
    raise ValueLabError("규칙 '%s' 이 없습니다. python value.py rules 로 이름을 보세요" % text)


def signals(P):
    """[(i, T)] 4월 말 신호 (FIRST_SIGNAL 부터, 종목군을 만들 수 있는 달)."""
    return [(i, T) for i, (m, T, _) in enumerate(P.months)
            if m >= FIRST_SIGNAL and T.month == inv.YEAR_MONTH and i >= 12]


def dec_days(P):
    """해마다 12월 마지막 거래일 (배당을 넣는 날)."""
    return [T for m, T, _ in P.months if T.month == 12]


def is_financial(company, has_current_assets):
    code = (company or {}).get("induty_code") or ""
    if not code.startswith(FIN_PREFIX):
        return False
    return not (code == HOLDING and has_current_assets)


def rank(vals, reverse=False):
    """{종목: 값} -> {종목: 순위 (1부터)}. 값이 같으면 종목 코드 순."""
    order = sorted(vals, key=lambda s: ((-vals[s]) if reverse else vals[s], s))
    return {s: k + 1 for k, s in enumerate(order)}


# ---------------------------------------------------------------- 캐시 읽기 (받기는 data 명령)

class Store(object):
    """DART·KRX 캐시만 읽는다. 없는 파일은 missing 에 센다."""

    def __init__(self, P, dart_cache=fin.CACHE, krx_cache=krxdata.CACHE_DIR, store=inv.STORE):
        self.P, self.dart_cache, self.krx_cache, self.store = P, dart_cache, krx_cache, store
        self.codes = (fin._read(os.path.join(dart_cache, "corpcode.json")) or {}).get("codes") or {}
        self.reused = {c for c, _, _ in P.reused}
        self.want = firm_years(P)
        self.missing = collections.Counter()
        self._tables, self._feats, self._close, self._fs = {}, {}, {}, {}

    def corp(self, s):
        if s.endswith("*") or s in self.reused:
            return None
        return (self.codes.get(s) or {}).get("corp_code")

    def _doc(self, *parts):
        return fin._read(os.path.join(self.dart_cache, *parts))

    def company(self, corp):
        doc = self._doc("company", corp + ".json")
        if doc is None:
            self.missing["company"] += 1
        return doc

    def filings(self, corp):
        doc = self._doc("filings", corp + ".json")
        if doc is None:
            self.missing["filings"] += 1
            return None
        return doc["periods"]

    def statement(self, corp, y):
        """그 해 재무제표 (slim 꼴, 메모리에 둔다). 캐시에 없으면 None."""
        k = (corp, y)
        if k not in self._fs:
            doc = self._doc("fs", "%s_%d.json" % (corp, y))
            if doc is None:
                self.missing["fs"] += 1
            self._fs[k] = fin.slim((doc or {}).get("data"))
        return self._fs[k]

    def alot(self, corp, y):
        k = ("alot", corp, y)
        if k not in self._fs:
            doc = self._doc("alot", "%s_%d.json" % (corp, y))
            if doc is None:
                self.missing["alot"] += 1
            self._fs[k] = (doc or {}).get("data")
        return self._fs[k]

    def table(self, d):
        """KRX 그날 표 {종목: 행} (캐시만). 없으면 None."""
        if d not in self._tables:
            doc = fin._read(os.path.join(self.krx_cache, d.strftime("%Y%m%d") + ".json"))
            self._tables[d] = doc["rows"] if doc else None
        return self._tables[d]

    def cap(self, s, d):
        """d 의 {"mktcap": 보통주+우선주, "price": 보통주 종가, "date"}. 그날 행이 없으면 None."""
        rows = self.table(d)
        r = (rows or {}).get(s)
        if not r:
            return None
        caps = sum((x.get("mktcap") or 0) for c, x in rows.items() if c[:5] == s[:5])
        return {"mktcap": caps, "price": r["c"], "date": d.isoformat()}

    def start_cap(self, s, rows):
        """대가 점검표 B7 의 시작 시가총액: 최근 5 사업연도 첫 해의 4월 1일 이후 첫 거래일 (masters.start_market 과 같음)."""
        if len(rows) < 5:
            return None
        first = datetime.date(rows[-5]["year"], 4, 1)
        for k in range(14):
            d = first + datetime.timedelta(days=k)
            if d.weekday() < 5 and self.table(d):
                m = self.cap(s, d)
                if m:
                    return m
        return None

    def close(self, d):
        """그날 보통주 종가 {종목: 원} (invest 캐시)."""
        if d not in self._close:
            doc = inv.read_day(self.store, d)
            self._close[d] = {r[0]: r[3] for r in (doc or {}).get("rows", [])}
        return self._close[d]

    # ------------------------------------------------------------ 신호일 T 의 숫자

    def features(self, i):
        """신호 월 i 의 ({종목: 숫자}, 뺀 이유 Counter). V(T) 에 든 종목만 숫자가 있다."""
        if i in self._feats:
            return self._feats[i]
        P = self.P
        m, T, _ = P.months[i]
        U = inv.universe(P, i) or []
        T1, T12 = P.months[i - 1][1], P.months[i - 12][1]
        out, why = {}, collections.Counter()
        for s, _x in U:
            corp = self.corp(s)
            if not corp:
                why["코드 재사용·고유번호 없음"] += 1
                continue
            co = self.company(corp)
            if co is None:
                why["기업개황 없음"] += 1
                continue
            if co.get("acc_mt") != "12":
                why["12월 결산 아님"] += 1
                continue
            if REIT_WORD in co.get("name", ""):
                why["리츠"] += 1
                continue
            code = co.get("induty_code") or ""
            if code.startswith(FIN_PREFIX) and code != HOLDING:
                why["금융"] += 1
                continue
            lo = self.want.get(s, (fin.FIRST_YEAR, 0))[0]
            docs = {y: self.statement(corp, y) for y in range(lo, T.year)}
            divs = {y: self.alot(corp, y) for y in range(lo, T.year)}
            filings = self.filings(corp) or {}
            rows = fin.pit_rows(docs, divs, filings, T)
            if not rows or rows[-1]["year"] != T.year - 1:
                ent = filings.get("%d.12" % (T.year - 1)) or {}
                if docs.get(T.year - 1) is None and ent.get("first") and ent["first"] <= T.strftime("%Y%m%d"):
                    why["재무제표 자료 없음 (FY%d 보고서는 T 까지 접수)" % (T.year - 1)] += 1   # OpenDART 가 빈 응답
                else:
                    why["FY%d 사업보고서가 T 까지 없음" % (T.year - 1)] += 1
                continue
            if is_financial(co, rows[-1].get("current_assets") is not None):
                why["금융"] += 1
                continue
            mk = self.cap(s, T)
            if not mk or not mk["mktcap"]:
                why["T 시가총액 없음"] += 1
                continue
            rows = ms.derive(rows)
            for r in rows:
                r["_sym"] = s
            last = rows[-1]
            eq, ni, cap = last.get("equity_parent"), last.get("ni_parent"), mk["mktcap"]
            a, b = P.price(s, T12), P.price(s, T1)
            g = ms.graham(rows, mk)
            bu = ms.buffett(rows, mk, self.start_cap(s, rows))
            _kind, ly = ms.lynch(rows, mk)
            roes = [r["roe"] for r in rows if r.get("roe") is not None]
            peg = [c for c in ly if c["id"] == "L1"][0]
            out[s] = {
                "pbr": cap / eq if eq and eq > 0 else None,
                "ey": ni / cap if ni is not None and ni > 0 else None,
                "dy": last["dps"] / mk["price"] if last.get("dps") and mk["price"] else None,
                "roe": last.get("roe"),
                "mom": (b / a - 1.0) if a and b else None,
                "graham": sum(1 for c in g if c["ok"] is True),
                "g7": _per3(rows, cap),
                "buffett": sum(1 for c in bu if c["ok"] is True),
                "b1": sum(roes) / len(roes) if len(roes) >= 3 else None,
                "lynch": sum(1 for c in ly if c["ok"] is True),
                "peg": _peg(peg),
                "fscore": fscore(rows),
                "gpa": gpa(rows),
                "revised_after": bool(last.get("revised_after")),
                "cap": cap,
            }
        self._feats[i] = (out, why)
        return out, why

    def income(self):
        """invest.backtest 의 income(s, a, b): a < X <= b 인 12월 마지막 거래일 X 마다 (1 + 주당배당금 / X 종가)."""
        P = self.P
        days = dec_days(P)

        def f(s, a, b):
            mult = 1.0
            for X in days:
                if not (a < X <= b) or P.final[s][0] < X:
                    continue
                corp = self.corp(s)
                c = self.close(X).get(s)
                if not corp or not c:
                    continue
                t = self.alot(corp, X.year)
                dps = ((t or {}).get("thstrm") or {}).get("dps") or 0.0
                if dps > 0:
                    mult *= 1.0 + dps / c
            return mult
        return f

    def targets(self, strict=False):
        """invest.backtest 의 targets_fn(P, i, v)."""
        def f(P, i, v):
            if v["fam"] == "bv":
                return inv.targets(P, i, {"fam": "cap", "reb": "Y"})
            F, _ = self.features(i)
            if strict:
                F = {s: x for s, x in F.items() if not x["revised_after"]}
            if v["fam"] == "bv_f":
                return drop_low_f(inv.targets(P, i, {"fam": "cap", "reb": "Y"}), F, v["max_f"])
            return pick(F, v)
        return f


def _per3(rows, cap):
    e3 = ms.avg([r.get("ni_parent") for r in rows[-3:]])
    return cap / e3 if e3 and e3 > 0 else None


def _peg(c):
    v = c.get("value") or ""
    try:
        return float(v.split()[0]) if v != "-" else None
    except (ValueError, IndexError):
        return None


def _div(x, y):
    return x / y if x is not None and y not in (None, 0) else None


def fscore(rows):
    """피오트로스키 (2000) F-score 의 8개 신호 (유상증자 신호 뺌: 한국 상장주식수에는 무상증자·액면분할이 섞인다).
    t = 마지막 해. 1 ROA_t > 0, 2 영업현금흐름 > 0, 3 ROA 가 전년보다 높음, 4 영업현금흐름 / 기초자산 > ROA,
    5 비유동부채 / 자산이 전년보다 낮음, 6 유동비율이 전년보다 높음, 7 매출총이익률이 전년보다 높음 (두 해 중 매출총이익이
    없으면 영업이익률), 8 매출 / 기초자산이 전년보다 높음. ROA = 당기순이익 / 기초자산. 숫자가 없는 신호는 0.
    마지막 세 해가 이어져 있지 않으면 None."""
    if len(rows) < 3 or rows[-1]["year"] - rows[-3]["year"] != 2:
        return None
    a, b, c = rows[-3], rows[-2], rows[-1]

    def ni(r):
        return r.get("ni") if r.get("ni") is not None else r.get("ni_parent")

    def gt(x, y):
        return x is not None and y is not None and x > y

    roa, roa0 = _div(ni(c), b.get("assets")), _div(ni(b), a.get("assets"))
    cfo = c.get("ocf")
    gm, gm0 = _div(c.get("gross_profit"), c.get("revenue")), _div(b.get("gross_profit"), b.get("revenue"))
    if gm is None or gm0 is None:
        gm, gm0 = _div(c.get("op"), c.get("revenue")), _div(b.get("op"), b.get("revenue"))
    sig = (gt(roa, 0.0), gt(cfo, 0.0), gt(roa, roa0), gt(_div(cfo, b.get("assets")), roa),
           gt(_div(b.get("noncurrent_liabilities"), b.get("assets")), _div(c.get("noncurrent_liabilities"), c.get("assets"))),
           gt(_div(c.get("current_assets"), c.get("current_liabilities")),
              _div(b.get("current_assets"), b.get("current_liabilities"))),
           gt(gm, gm0), gt(_div(c.get("revenue"), b.get("assets")), _div(b.get("revenue"), a.get("assets"))))
    return sum(1 for x in sig if x)


def gpa(rows):
    """노비-마크스 (2013) 매출총이익 / 자산총계 (마지막 해). 매출총이익이 없으면 None."""
    last = rows[-1] if rows else {}
    return _div(last.get("gross_profit"), last.get("assets"))


def drop_low_f(weights, F, max_f):
    """시가총액 비중에서 V(T) 의 F-score 가 max_f 이하인 종목을 빼고 다시 1 로 맞춘다 (V 밖 종목과 F-score 없는 종목은 둔다)."""
    keep = {s: w for s, w in weights.items() if not (s in F and F[s]["fscore"] is not None and F[s]["fscore"] <= max_f)}
    tot = sum(keep.values())
    return {s: w / tot for s, w in keep.items()} if tot else {}


def pick(F, v):
    """V(T) 숫자 {종목: 숫자} -> 목표 비중 {종목: 1/n}. 규칙 정의는 r3·r4 계획서와 같다."""
    f, n = v["fam"], v.get("n", N)
    if f == "ewv":
        return {s: 1.0 / len(F) for s in F} if F else {}
    if f in ("pbr", "per", "dy"):
        key = {"pbr": "pbr", "per": "ey", "dy": "dy"}[f]
        vals = {s: x[key] for s, x in F.items() if x[key] is not None}
        order = sorted(vals, key=lambda s: (vals[s] if f == "pbr" else -vals[s], s))
    elif f in ("pbr_roe", "pbr_mom"):
        other = "roe" if f == "pbr_roe" else "mom"
        both = {s: x for s, x in F.items() if x["pbr"] is not None and x[other] is not None}
        r1 = rank({s: x["pbr"] for s, x in both.items()})
        r2 = rank({s: x[other] for s, x in both.items()}, reverse=True)
        order = sorted(both, key=lambda s: (r1[s] + r2[s], s))
    elif f == "f_pbr":
        cheap = {s: x["pbr"] for s, x in F.items() if x["pbr"] is not None}
        third = sorted(cheap, key=lambda s: (cheap[s], s))[:-(-len(cheap) // 3)]
        ok = [s for s in third if F[s]["fscore"] is not None]
        order = sorted(ok, key=lambda s: (-F[s]["fscore"], cheap[s], s))
    elif f == "gpa":
        vals = {s: x["gpa"] for s, x in F.items() if x["gpa"] is not None}
        order = sorted(vals, key=lambda s: (-vals[s], s))
    elif f == "gpa_pbr":
        both = {s: x for s, x in F.items() if x["pbr"] is not None and x["gpa"] is not None}
        r1 = rank({s: x["pbr"] for s, x in both.items()})
        r2 = rank({s: x["gpa"] for s, x in both.items()}, reverse=True)
        order = sorted(both, key=lambda s: (r1[s] + r2[s], s))
    elif f in ("graham", "buffett", "lynch"):
        tie = {"graham": ("g7", 1), "buffett": ("b1", -1), "lynch": ("peg", 1)}[f]
        big = float("inf")

        def k(s):
            t = F[s][tie[0]]
            return (-F[s][f], big if t is None else tie[1] * t, s)
        order = sorted(F, key=k)
    else:
        raise ValueLabError("모르는 규칙 %s" % f)
    chosen = order[:n]
    return {s: 1.0 / len(chosen) for s in chosen} if chosen else {}


# ---------------------------------------------------------------- 받기

class Calls(object):
    """DART 호출을 세고 상한에서 멈춘다. 연결이 끊긴 호출만 RETRIES 번까지 쉬었다가 다시 부른다 (다시 부른 것도 센다)."""

    def __init__(self, transport, limit, wait=RETRY_WAIT, sleep=time.sleep):
        self.transport, self.limit, self.n, self.lock = transport or fin._bytes, limit, 0, threading.Lock()
        self.wait, self.sleep, self.retried = wait, sleep, 0

    def __call__(self, url):
        for k in range(RETRIES + 1):
            with self.lock:
                if self.n >= self.limit:
                    raise fin.FinError("호출 상한 %d건에 닿아 멈췄습니다. 다음에 다시 부르면 이어 받습니다" % self.limit)
                self.n += 1
            try:
                return self.transport(url)
            except fin.FinError as e:
                if "연결하지 못했습니다" not in str(e) or k == RETRIES:
                    raise
                with self.lock:
                    self.retried += 1
                self.sleep(self.wait * 2 ** k)


def firm_years(P):
    """4월 말 U 에 든 종목마다 (첫 해, 끝 해): 첫 거래 해 - 1 (2015 부터) ~ 마지막으로 U 에 든 4월의 해 (2025 까지)."""
    last = {}
    for i, T in signals(P):
        for s, _ in inv.universe(P, i) or []:
            last[s] = max(last.get(s, 0), T.year)
    cap_year = max([T.year for _, T in signals(P)] or [fin.FIRST_YEAR]) - 1
    return {s: (max(fin.FIRST_YEAR, P.first[s].year - 1), min(y, cap_year)) for s, y in last.items()}


def download(P, key, krx_key=None, dart_cache=fin.CACHE, krx_cache=krxdata.CACHE_DIR, now_kst=None, transport=None,
             krx_transport=None, max_calls=MAX_CALLS, workers=2, progress=None, sleep=time.sleep):
    """firm_years 의 회사마다 기업개황, 접수 기록, 사업보고서 재무제표(금융·리츠 빼고), 배당. 그리고 신호일과 B7 시작일의
    KRX 표. 오류나 상한에 걸리면 그 자리에서 멈춘다 (캐시에 있는 것은 다시 받지 않는다)."""
    from concurrent.futures import ThreadPoolExecutor
    now_kst = now_kst or krx.kst_now()
    call = Calls(transport, max_calls, sleep=sleep)
    codes = fin.corp_codes(key, dart_cache, now_kst, call)
    want = firm_years(P)
    need = max(T for _, T in signals(P))
    out = {"firms": len(want), "done": 0, "no_corp": [], "stopped": None, "krx_days": 0}
    stop = threading.Event()

    def one(s):
        if stop.is_set():
            return False
        info = codes.get(s)
        if s.endswith("*") or s in {c for c, _, _ in P.reused} or not info:
            out["no_corp"].append(s)
            return True
        corp = info["corp_code"]
        co = fin.company(corp, key, dart_cache, call)
        fin.annual_filings(corp, key, dart_cache, now_kst, call, need=need)
        lo, hi = want[s]
        skip_fs = (co.get("induty_code", "").startswith(FIN_PREFIX) and co.get("induty_code") != HOLDING) \
            or REIT_WORD in co.get("name", "") or co.get("acc_mt") != "12"
        for y in range(lo, hi + 1):
            if stop.is_set():
                return False
            if not skip_fs:
                fin.statements(corp, y, key, dart_cache, now_kst, call, sleep=None)
            fin.dividend_table(corp, y, key, dart_cache, now_kst, call)
        return True

    syms = sorted(want)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [(s, pool.submit(one, s)) for s in syms]
        for k, (s, f) in enumerate(futs):
            try:
                if f.result():
                    out["done"] += 1
            except fin.FinError as e:
                if not stop.is_set():
                    out["stopped"] = str(e)
                stop.set()
            if progress and (k + 1) % 25 == 0:
                progress("  회사 %d / %d, DART 호출 %d건" % (k + 1, len(syms), call.n))
    out["calls"], out["retried"] = call.n, call.retried
    if out["stopped"]:
        return out
    days = [T for _, T in signals(P)]
    for d in days:
        krxdata.day_table(d, krx_key, krx_cache, now_kst, krx_transport)
        out["krx_days"] += 1
    for y in range(min(T.year for T in days) - 5, max(T.year for T in days) - 4):
        for k in range(14):                  # B7 시작일: 4월 1일 이후 첫 거래일까지
            d = datetime.date(y, 4, 1) + datetime.timedelta(days=k)
            if d.weekday() < 5:
                out["krx_days"] += 1
                if krxdata.day_table(d, krx_key, krx_cache, now_kst, krx_transport):
                    break
    return out


# ---------------------------------------------------------------- 자료 점검

def check(P, S):
    """r3 계획서의 자료 점검 1~7. {"by_signal": [...], "gaps": {...}, "yield": {...}, "fails": [문장]}."""
    fails, rows = [], []
    fs_gap = fs_n = div_gap = div_n = 0
    diffs = []
    want = firm_years(P)
    for i, T in signals(P):
        F, why = S.features(i)
        base = len(F) + sum(n for w, n in why.items() if w.startswith("FY"))
        filed = len(F) / base if base else 0.0
        rows.append({"signal": T.isoformat(), "U": len(inv.universe(P, i) or []), "V": len(F), "why": dict(why),
                     "filed_ratio": filed, "revised_after": sum(1 for x in F.values() if x["revised_after"])})
        if filed < FILED_MIN:
            fails.append("%s: FY%d 사업보고서가 T 까지 접수된 비율 %.0f%% (기준 %.0f%%)" % (
                T, T.year - 1, 100 * filed, 100 * FILED_MIN))
        if len(F) < MIN_V:
            fails.append("%s: V(T) %d개 (기준 %d)" % (T, len(F), MIN_V))
        if S.table(T) is None:
            fails.append("%s: KRX 표가 캐시에 없음" % T)
    for s, (lo, hi) in sorted(want.items()):
        corp = S.corp(s)
        if not corp:
            continue
        co = S.company(corp)
        filings = S.filings(corp)
        if co is None or filings is None:
            fails.append("%s: 기업개황 또는 접수 기록이 없음 (value.py data)" % s)
            continue
        fin_like = co.get("induty_code", "").startswith(FIN_PREFIX) and co.get("induty_code") != HOLDING
        skip = fin_like or REIT_WORD in co.get("name", "") or co.get("acc_mt") != "12"
        for y in range(lo, hi + 1):
            if "%d.12" % y not in filings:
                continue
            div_n += 1
            t = S.alot(corp, y)
            if t is None:
                div_gap += 1
            if not skip:
                fs_n += 1
                if S.statement(corp, y) is None:
                    fs_gap += 1
            X = [d for d in dec_days(P) if d.year == y]
            th = (t or {}).get("thstrm") or {}
            if X and th.get("dps") and th.get("yield") is not None and co.get("acc_mt") == "12":
                c = S.close(X[0]).get(s)
                if c:
                    diffs.append(abs(th["dps"] / c - th["yield"]))
    gaps = {"fs": [fs_gap, fs_n], "div": [div_gap, div_n]}
    if fs_n and fs_gap / fs_n > GAP_MAX:
        fails.append("접수된 사업보고서인데 재무제표가 빈 회사-연도 %d / %d" % (fs_gap, fs_n))
    if div_n and div_gap / div_n > GAP_MAX:
        fails.append("접수된 사업보고서인데 배당이 빈 회사-연도 %d / %d" % (div_gap, div_n))
    diffs.sort()
    med = diffs[len(diffs) // 2] if diffs else None
    if med is None or med > YIELD_GAP_MAX:
        fails.append("배당수익률 차이 중앙값 %s (기준 %.1f%%p)" % ("-" if med is None else "%.2f%%p" % (100 * med),
                                                         100 * YIELD_GAP_MAX))
    big = sum(1 for x in diffs if x > 0.01)
    return {"by_signal": rows, "gaps": gaps, "yield": {"n": len(diffs), "median": med, "over_1pp": big},
            "missing_files": dict(S.missing), "fails": fails}


# ---------------------------------------------------------------- 시험

def run_one(P, S, v, start, end, delist="A", cost=inv.COST, cost_x=1.0, dividends=True, strict=False, lag=inv.NW_LAG):
    if not start.endswith("-%02d" % (inv.YEAR_MONTH + 1)):
        raise ValueLabError("첫 수익률 달은 %d월이어야 합니다 (4월 말 신호 다음 달)" % (inv.YEAR_MONTH + 1))
    inc = S.income() if dividends else None
    strat = inv.backtest(P, v, start, end, delist, cost, cost_x, S.targets(strict), inc)
    bench = inv.backtest(P, BENCH, start, end, delist, cost, cost_x, S.targets(strict), inc)
    small = [(P.months[i][0], len(S.features(i)[0])) for i, T in signals(P)
             if start <= P.months[i + 1][0] <= end and len(S.features(i)[0]) < MIN_V]
    if small:
        raise ValueLabError("가치 대상 V(T) 가 %d개보다 작은 신호가 있습니다 (%s). python value.py check 로 자료를 보세요"
                            % (MIN_V, ", ".join("%s %d개" % x for x in small)))
    st = inv.compare(strat, bench)
    if lag != inv.NW_LAG:
        b = {x["m"]: x["r"] for x in bench}
        st.update(inv.nw([x["r"] - b[x["m"]] for x in strat], lag))
    st.update({"rule": v["label"], "from": start, "to": end, "delist": delist, "cost_x": cost_x, "months": len(strat),
               "dividends": dividends, "strict": strict, "nw_lag": lag,
               "monthly": [[x["m"], x["r"]] for x in strat], "bench_monthly": [[x["m"], x["r"]] for x in bench]})
    return st


# ---------------------------------------------------------------- 전진 시험 (forward)
#
# r3·r4 규칙과 EWV 를 잠그고 잠근 뒤의 달만 채점한다. 판정 대상, 판정에 쓰는 첫 몇 달, 홀름 보정은 잠글 때 정하고,
# 그 달 수가 차기 전에는 "판정 전" 만 말한다 (자주 들여다보다 좋을 때 멈추는 일을 막는다). 계산 코드나 규칙 정의가
# 잠근 뒤 바뀌면 멈춘다. 종목 목록은 내지 않는다.

FORWARD_DIR = os.path.join("trader-kr", "forward", "value")
JUDGE_MONTHS = 36
MAX_JUDGED = 6
LOCK_KEYS = ("rules", "judged", "first_month", "judge_months", "cost", "bench", "engine")
LOCK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CLOSE_TIME = datetime.time(15, 30)
FORWARD_NOTE = "잠근 뒤의 성적도 한 구간의 과거입니다. 미래를 약속하지 않고, 종목 목록은 내지 않습니다."


def tracked():
    return rules() + rules_r4() + [EWV]


def _source(o):
    """함수의 원본 (줄 끝 공백 뺌). 클래스는 메서드마다 (파일에서 바로 불러온 모듈은 클래스 원본을 못 찾는다)."""
    if inspect.isclass(o):
        return "\n".join(_source(f) for _, f in sorted(vars(o).items()) if inspect.isfunction(f))
    return "\n".join(x.rstrip() for x in inspect.getsource(o).splitlines()).strip()


def engine_print():
    """결과를 바꾸는 계산 (종목군, 그때 알던 숫자, 점검표, 고르기, 백테스트) 원본과 상수의 지문."""
    objs = (Store, pick, fscore, gpa, drop_low_f, _div, _per3, _peg, is_financial, rank, run_one, signals, dec_days,
            firm_years, inv.backtest, inv.targets, inv.universe, inv.Panel, inv.month_anchors, inv.compare, inv.nw,
            inv.perf, inv.rebalances, fin.pit_rows, fin.filed_on, fin.year_row, fin.pick, fin.base_id, fin.slim,
            ms.derive, ms.graham, ms.buffett, ms.lynch, ms.lynch_kind, ms.avg, ms.ratio, ms.cagr)
    src = "\n".join(_source(o) for o in objs)
    consts = repr((N, MAX_F, FIN_PREFIX, HOLDING, REIT_WORD, MIN_V, FIRST_SIGNAL, inv.UNIVERSE, inv.YEAR_MONTH,
                   inv.REUSE_GAP, inv.WINDOW, inv.MIN_RETS, inv.NW_LAG, sorted(fin.ITEMS.items())))
    return hashlib.sha256((src + consts).encode("utf-8")).hexdigest()[:16]


def lock_hash(doc):
    body = json.dumps({k: doc[k] for k in LOCK_KEYS}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def first_month(now):
    """채점할 첫 달 (YYYY-MM). 그달 수익률은 그달 첫 거래일 종가부터 세니, 잠근 때가 이번 달 첫 평일 장 마감(15:30)
    전이면 이번 달, 아니면 다음 달 (첫 평일이 휴장이면 실제 시작은 더 늦어 더 안전하다)."""
    d = now.date().replace(day=1)
    while d.weekday() >= 5:
        d += datetime.timedelta(days=1)
    if now.date() < d or (now.date() == d and now.time() < CLOSE_TIME):
        return now.strftime("%Y-%m")
    return "%d-%02d" % (now.year + (now.month == 12), now.month % 12 + 1)


def forward_paths(dirname, nm):
    return os.path.join(dirname, nm + ".lock.json"), os.path.join(dirname, nm + ".json")


def forward_lock(judged, now, nm, dirname=FORWARD_DIR, judge_months=JUDGE_MONTHS):
    """r3·r4 규칙 12개와 EWV 를 잠근다. judged 는 판정 대상 (홀름 보정). 같은 이름이 있으면 덮어쓰지 않는다."""
    if not LOCK_NAME.match(nm or ""):
        raise ValueLabError("--name 은 영문·숫자·.-_ 64자까지입니다 (받은 값 %r)" % nm)
    if judge_months < 12:
        raise ValueLabError("--judge-months 는 12 이상입니다")
    rs = tracked()
    labels = [v["label"] for v in rs]
    judged = [j.strip() for j in judged or []]
    bad = [j for j in judged if j not in labels]
    if bad:
        raise ValueLabError("판정 대상 %s 이 잠글 규칙에 없습니다. python value.py rules 의 이름 그대로 주세요" % bad)
    if not judged or len(set(judged)) != len(judged) or len(judged) > MAX_JUDGED:
        raise ValueLabError("판정 대상은 서로 다른 1 ~ %d개입니다 (많을수록 홀름 보정이 커진다)" % MAX_JUDGED)
    doc = {"name": nm, "rules": {v["label"]: v for v in rs}, "judged": judged, "first_month": first_month(now),
           "judge_months": int(judge_months), "cost": dict(inv.COST), "bench": BENCH["label"], "engine": engine_print(),
           "locked_at": now.strftime("%Y-%m-%d %H:%M")}
    doc["hash"] = lock_hash(doc)
    path = forward_paths(dirname, nm)[0]
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    try:
        with open(path, "x", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    except FileExistsError:
        raise ValueLabError("%s 가 이미 있습니다. 잠근 것은 바꾸지 않습니다. 다르게 잠그려면 새 --name 으로" % path)
    return doc, path


def read_lock(path):
    """잠근 파일을 읽고 지문, 규칙 정의, 계산 코드가 잠근 때와 같은지 본다. 다르면 멈춘다."""
    doc = krx.read_json(path, "잠근 규칙 파일")
    if not isinstance(doc, dict) or any(k not in doc for k in LOCK_KEYS + ("name", "hash", "locked_at")):
        raise ValueLabError("%s 는 잠근 규칙 파일 형식이 아닙니다" % path)
    if lock_hash(doc) != doc["hash"]:
        raise ValueLabError("%s 가 잠근 뒤 바뀌었습니다 (지문이 다릅니다). 바꾸려면 새 --name 으로 다시 잠그세요" % path)
    for label, v in doc["rules"].items():
        try:
            same = find_rule(label) == v
        except ValueLabError:
            same = False
        if not same:
            raise ValueLabError("잠근 규칙 %r 의 정의가 지금 value.py 와 다릅니다. 새 --name 으로 다시 잠그세요" % label)
    if doc["engine"] != engine_print():
        raise ValueLabError("계산 코드가 잠근 뒤 바뀌었습니다 (지문 %s -> %s). 같은 규칙이라도 결과가 달라지므로 새 --name "
                            "으로 다시 잠그세요" % (doc["engine"], engine_print()))
    return doc


def pick_lock(dirname, nm):
    if nm:
        return nm
    names = sorted(f[:-len(".lock.json")] for f in os.listdir(dirname) if f.endswith(".lock.json")) \
        if os.path.isdir(dirname) else []
    if len(names) == 1:
        return names[0]
    if not names:
        raise ValueLabError("잠근 규칙이 없습니다. 먼저 value.py forward lock 을 돌리세요")
    raise ValueLabError("잠근 것이 여럿입니다 (%s). --name 으로 고르세요" % ", ".join(names))


def _prod(xs):
    out = 1.0
    for x in xs:
        out *= x
    return out


def forward_update(lock, P, S):
    """잠근 첫 달부터, 다음 달 첫 거래일까지 자료가 있는 (끝난) 달만 채점한다."""
    fm, J = lock["first_month"], lock["judge_months"]
    done = [m for m, _, E in P.months if E is not None and m >= fm]
    res = {"name": lock["name"], "locked_at": lock["locked_at"], "first_month": fm, "judge_months": J,
           "judged": lock["judged"], "engine": lock["engine"], "months": done, "rules": []}
    if not done:
        res["verdict"] = {"state": "none", "passed": [],
                          "text": "아직 채점할 달이 없습니다 (%s 이 끝나고 다음 달 첫 거래일 자료를 받은 뒤)" % fm}
        return res
    y, mo = int(fm[:4]), int(fm[5:7])
    start = "%d-%02d" % (y if mo > inv.YEAR_MONTH else y - 1, inv.YEAR_MONTH + 1)
    keep = set(done)
    for label, v in lock["rules"].items():
        a = run_one(P, S, v, start, done[-1], "A", lock["cost"])
        b = dict(a["bench_monthly"])
        rs = [(m, r) for m, r in a["monthly"] if m in keep]
        ex = [r - b[m] for m, r in rs]
        row = {"rule": label, "judged": label in lock["judged"], "n": len(rs),
               "cum": _prod(1.0 + r for _, r in rs) - 1.0, "bench_cum": _prod(1.0 + b[m] for m, _ in rs) - 1.0,
               "excess": ex, "mean": sum(ex) / len(ex) if ex else None}
        if row["judged"]:
            bb = run_one(P, S, v, start, done[-1], "B", lock["cost"])
            bm = dict(bb["bench_monthly"])
            row["excess_b"] = [r - bm[m] for m, r in bb["monthly"] if m in keep]
        res["rules"].append(row)
    res["rules"].sort(key=lambda r: lock["judged"].index(r["rule"]) if r["judged"] else len(lock["judged"]))
    n = len(done)
    if n < J:
        res["verdict"] = {"state": "before", "passed": [],
                          "text": "판정 전 (%d / %d달). 그 전에는 결과로 규칙을 바꾸거나 멈추지 않습니다" % (n, J)}
        return res
    judged = [r for r in res["rules"] if r["judged"]]
    for r in judged:
        st = inv.nw(r["excess"][:J])
        r["judge"] = {"mean": st["mean"], "se": st["se"], "p": st["p"], "mean_b": sum(r["excess_b"][:J]) / J}
    for r, adj in zip(judged, inv.holm([r["judge"]["p"] for r in judged])):
        r["judge"]["p_holm"] = adj
        r["judge"]["pass"] = adj < 0.05 and r["judge"]["mean"] > 0 and r["judge"]["mean_b"] > 0
    passed = [r["rule"] for r in judged if r["judge"]["pass"]]
    res["verdict"] = {"state": "done", "passed": passed,
                      "text": "첫 %d달 (%s ~ %s) 판정: %s" % (J, done[0], done[J - 1],
                                                         ", ".join(passed) if passed else "기준을 넘은 규칙 없음")}
    return res


def lock_report(doc, path):
    return ["%s 에 잠갔습니다 (%s)." % (path, doc["locked_at"]),
            "규칙 %d개 (r3 8, r4 4, EWV) 를 %s 부터의 달만 채점합니다. 기준 %s." % (
                len(doc["rules"]), doc["first_month"], doc["bench"]),
            "판정 대상 %d개 (홀름 보정): %s" % (len(doc["judged"]), ", ".join(doc["judged"])),
            "판정은 첫 %d달로 한 번 (보정 p < 0.05, A·B 판 평균 초과수익 플러스). 그 뒤 달은 참고만." % doc["judge_months"],
            "계산 지문 %s. 계산 코드나 규칙 정의가 바뀌면 update 가 멈춥니다." % doc["engine"],
            "매달 초: python invest.py data 로 KRX 를 받은 뒤 python value.py forward update --name %s" % doc["name"],
            "해마다 5월 초: python value.py data 로 새 사업보고서를 받습니다 (4월 말 신호용)."]


def forward_report(res):
    L = ["전진 시험 %s (잠금 %s, %s 부터 채점, 판정은 첫 %d달)" % (res["name"], res["locked_at"], res["first_month"],
                                                         res["judge_months"])]
    if not res["months"]:
        return L + [res["verdict"]["text"], FORWARD_NOTE]
    L.append("채점한 달 %d개: %s ~ %s (다음 달 첫 거래일까지 자료가 있는 달만)" % (
        len(res["months"]), res["months"][0], res["months"][-1]))
    L.append("  %s %8s %10s %12s" % (krx.pad("규칙", 36), "누적", "기준 누적", "월 초과 평균"))
    for title, flag in (("판정 대상", True), ("참고", False)):
        L.append(title)
        for r in [x for x in res["rules"] if x["judged"] == flag]:
            extra = "" if "judge" not in r else "  (홀름 p %.3f, B 판 %s)" % (r["judge"]["p_holm"],
                                                                        inv.pct(r["judge"]["mean_b"], 3))
            L.append("  %s %8s %10s %12s%s" % (krx.pad(r["rule"], 36), inv.pct(r["cum"]), inv.pct(r["bench_cum"]),
                                             inv.pct(r["mean"], 3), extra))
    L.append("판정: %s" % res["verdict"]["text"])
    L.append(FORWARD_NOTE)
    return L


def forward_main(a):
    now = krx.parse_now(a.now)
    if a.fcmd == "lock":
        doc, path = forward_lock(a.judge, now, a.name, a.dir, a.judge_months)
        print(json.dumps(doc, ensure_ascii=False, indent=2) if a.json else "\n".join(lock_report(doc, path)))
        return 0
    if a.fcmd == "update":
        nm = pick_lock(a.dir, a.name)
        lock = read_lock(forward_paths(a.dir, nm)[0])
        P = panel_for(a)
        res = forward_update(lock, P, Store(P))
        krx.write_json(forward_paths(a.dir, nm)[1], res)
        print(json.dumps(res, ensure_ascii=False, indent=1) if a.json else "\n".join(forward_report(res)))
        return 0
    return 1


def panel_for(a):
    end_day = min(inv.month_end(a.end), krx.kst_now().date()) if getattr(a, "end", None) else krx.kst_now().date()
    print("자료를 읽습니다 (%s 까지)..." % end_day, file=sys.stderr, flush=True)
    P = inv.load_panel(a.store, end_day)
    if not P.months:
        raise ValueLabError("받은 KRX 자료가 없습니다. python invest.py data 로 먼저 받으세요")
    return P


def cmd_data(a):
    key = dart.api_key()
    if not key:
        raise ValueLabError("DART 키가 없습니다. 터미널에서 python krcommon/keys.py dart 로 넣으세요 (채팅에 붙여 넣지 마세요)")
    krx_key = krxdata.api_key()
    if not krx_key:
        raise ValueLabError("KRX 키가 없습니다. 터미널에서 python krcommon/keys.py krx 로 넣으세요 (채팅에 붙여 넣지 마세요)")
    P = panel_for(a)
    print("4월 말 신호 %d번, 회사 %d곳의 DART 자료를 받습니다 (캐시에 없는 것만, 호출 상한 %d건)" % (
        len(signals(P)), len(firm_years(P)), a.max_calls), flush=True)
    res = download(P, key, krx_key, max_calls=a.max_calls, progress=lambda m: print(m, flush=True))
    print("회사 %d / %d, DART 호출 %d건 (끊겨서 다시 부름 %d), KRX 표 %d일, 고유번호 없음 %d" % (
        res["done"], res["firms"], res["calls"], res["retried"], res["krx_days"], len(res["no_corp"])), flush=True)
    if res["stopped"]:
        print("멈춤: %s" % res["stopped"], flush=True)
        return 2
    return 0


def cmd_check(a):
    P = panel_for(a)
    res = check(P, Store(P))
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        print("신호일: U / V / FY 접수 비율 / 정정(T 뒤) / 뺀 이유")
        for r in res["by_signal"]:
            print("  %s: %d / %d / %.0f%% / %d / %s" % (r["signal"], r["U"], r["V"], 100 * r["filed_ratio"],
                                                    r["revised_after"],
                                                    ", ".join("%s %d" % kv for kv in sorted(r["why"].items()))))
        g = res["gaps"]
        print("접수됐는데 빈 회사-연도: 재무제표 %d / %d, 배당 %d / %d" % (g["fs"][0], g["fs"][1], g["div"][0], g["div"][1]))
        y = res["yield"]
        print("배당수익률 확인 (주당배당금 / 12월 마지막 거래일 종가 대 회사가 적은 값): %d건, 차이 중앙값 %s, 1%%p 넘음 %d건" % (
            y["n"], "-" if y["median"] is None else "%.2f%%p" % (100 * y["median"]), y["over_1pp"]))
        if res["missing_files"]:
            print("캐시에 없는 파일: %s" % ", ".join("%s %d" % kv for kv in sorted(res["missing_files"].items())))
    for f in res["fails"]:
        print("멈춤: %s" % f, file=sys.stderr)
    return 2 if res["fails"] else 0


def cmd_run(a):
    v = find_rule(a.rule)
    P = panel_for(a)
    end = a.end or P.months[-1][0]
    st = run_one(P, Store(P), v, a.start, end, a.delist, inv.COST, a.cost_x, not a.no_div, a.strict)
    if a.json:
        print(json.dumps(st, ensure_ascii=False, indent=1))
        return 0
    s, b = st["strategy"], st["bench"]
    print("%s 대 %s, 수익률 %s ~ %s (%d개월), 상장폐지 판 %s, 비용 x%g, 배당 %s%s" % (
        st["rule"], BENCH["label"], st["from"], st["to"], st["months"], st["delist"], st["cost_x"],
        "포함" if st["dividends"] else "뺌", ", 정정 뺀 판" if st["strict"] else ""))
    for nm, x in ((st["rule"], s), (BENCH["label"], b)):
        print("  %s 연복리 %8s  연변동성 %6.1f%%  최대낙폭 %8s" % (krx.pad(nm, 28), inv.pct(x["cagr"]), 100.0 * (x["vol"] or 0),
                                                         inv.pct(x["mdd"], 1)))
    print("  월 초과수익 평균 %s (Newey-West 표준오차 %s), 0보다 크다는 한쪽 p %.3f" % (
        inv.pct(st["mean"], 3), inv.pct(st["se"], 3), st["p"]))
    print("  해마다 바꾼 비중 평균 (달로 나눔) %.1f%%, 보유 중 사라진 종목 %d번" % (100.0 * (st["turnover"] or 0), st["gone"]))
    print(NOTE)
    return 0


def main(argv=None):
    krx.utf8_stdio()
    ap = argparse.ArgumentParser(description="가치 규칙 실험실 (trader-kr-invest/value.py).")
    sub = ap.add_subparsers(dest="cmd")
    d = sub.add_parser("data", help="4월 말 종목군 회사의 DART·KRX 자료 받기 (캐시에 없는 것만)")
    d.add_argument("--max-calls", type=int, default=MAX_CALLS)
    c = sub.add_parser("check", help="받은 범위와 자료 점검")
    c.add_argument("--json", action="store_true")
    sub.add_parser("rules", help="규칙 이름")
    r = sub.add_parser("run", help="규칙 하나를 BV 와 비교")
    r.add_argument("--rule", required=True, help='python value.py rules 의 이름 그대로 (예: "저PBR 30종목 연1회")')
    r.add_argument("--from", dest="start", default="2016-05", help="첫 수익률 달 (5월, 기본 2016-05)")
    r.add_argument("--to", dest="end", help="마지막 수익률 달 (기본: 받은 자료 끝 달)")
    r.add_argument("--delist", choices=("A", "B"), default="A", help="A 마지막 가격, B 사라지면 -100%%")
    r.add_argument("--cost-x", type=float, default=1.0, help="비용 배수 (민감도, 기본 1)")
    r.add_argument("--no-div", action="store_true", help="배당을 넣지 않은 판 (민감도)")
    r.add_argument("--strict", action="store_true", help="T 에 쓰는 최근 사업보고서가 T 뒤에 정정된 회사를 뺀 판 (민감도)")
    r.add_argument("--json", action="store_true")
    f = sub.add_parser("forward", help="r3·r4 규칙을 잠그고 잠근 뒤의 달만 채점 (전진 시험)")
    fs = f.add_subparsers(dest="fcmd")
    fl = fs.add_parser("lock", help="규칙 12개와 EWV, 판정 대상, 판정 달 수를 잠근다 (덮어쓰지 않음)")
    fl.add_argument("--name", required=True)
    fl.add_argument("--judge", action="append", help="판정 대상 규칙 이름 (여러 번, 1 ~ %d개)" % MAX_JUDGED)
    fl.add_argument("--judge-months", type=int, default=JUDGE_MONTHS, help="판정에 쓰는 첫 달 수 (기본 %d)" % JUDGE_MONTHS)
    fu = fs.add_parser("update", help="잠근 첫 달부터 끝난 달을 채점")
    fu.add_argument("--name")
    for x in (fl, fu):
        x.add_argument("--dir", default=FORWARD_DIR, help="잠근 파일 폴더 (기본 trader-kr/forward/value)")
        x.add_argument("--now")
        x.add_argument("--json", action="store_true")
    for x in (d, c, r, fu):
        x.add_argument("--store", default=inv.STORE)
    a = ap.parse_args(argv)
    if not a.cmd or (a.cmd == "forward" and not a.fcmd):
        (f if a.cmd else ap).print_help()
        return 1
    try:
        if a.cmd == "rules":
            for v in [BENCH, EWV] + rules() + rules_r4():
                print(v["label"])
            return 0
        if a.cmd == "data":
            return cmd_data(a)
        if a.cmd == "check":
            return cmd_check(a)
        if a.cmd == "run":
            return cmd_run(a)
        if a.cmd == "forward":
            return forward_main(a)
    except (ValueLabError, inv.InvestError, fin.FinError, krxdata.KrxDataError, krx.KrxError) as e:
        print("멈춤: %s" % e, file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
