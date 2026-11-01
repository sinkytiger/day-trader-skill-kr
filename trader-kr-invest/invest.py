#!/usr/bin/env python3
"""장기 투자 실험실 (trader-kr-invest): 한 달에 한 번 사고파는 장기 전략을 KRX 일별매매정보(2010년~)로
시험하고, 시장 기준 B1 (시가총액 상위 300 보통주를 시가총액 비중대로)과 비교한다. 표준 라이브러리만 쓴다.
주문을 내지 않는다. 지금 살 종목은 내지 않는다 (과거 성적만 낸다).

    python invest.py data --from 2010-01-04 --to 2026-10-30   # 캐시에 없는 날만 받는다 (KRX 키, 하루 한도 안에서)
    python invest.py data --check                             # 받지 않고 받은 범위만 센다
    python invest.py rules                                    # 시험할 수 있는 규칙 이름
    python invest.py run --rule "모멘텀12-1 30종목 매달" --from 2011-02 --to 2023-10
    python invest.py run --rule "저변동성 50종목 분기" --delist B --cost-x 2 --json
    python invest.py dca --months 36                          # B1 적립식과 한 번에 넣기 비교

자료: KRX Open API 유가증권·코스닥 일별매매정보. 그날 상장된 전 종목이 오므로 그 뒤 상장폐지·합병된 종목도
들어 있다 (생존 편향 없음). 하루 수익률은 종가 / (종가 - 전일 대비) - 1. 전일 대비는 KRX 기준가와의 차이고,
기준가에는 분할·병합·감자·권리락 조정이 들어 있다. 현금 배당은 빠진다 (가격 수익률). 보통주만 쓴다
(종목 코드 끝자리 0, 이름에 스팩 없음). 캐시: trader-kr/cache/invest/days/YYYYMMDD.json.gz.

종목군 U (신호일마다): 그날 거래대금 > 0, 시가총액 > 0, 첫 거래일이 12개월 전 월말 이전인 보통주 가운데
시가총액 상위 300. 신호는 매달 마지막 거래일 종가, 체결은 다음 거래일 종가. 보유 기간 안에는 사 둔 그대로
두고, 리밸런싱 달에만 목표 비중으로 맞춘다. 비용은 매매한 비중에만: 수수료(양쪽), 매도 거래세, 밀림(양쪽).

상장폐지 두 판: A 는 보유 기간 끝에 행이 없으면 마지막 행 가격으로 판다 (합병, 정리매매 가격까지 반영).
B 는 그 뒤로 다시는 행이 없는 종목을 -100% 로 친다 (비관적인 한계). 거래정지 종목은 KRX 표에 거래대금 0 인
행으로 남아 종목군에서 빠지고, 들고 있었으면 두 판 모두 멈춘 가격에 판 것으로 친다 (낙관적이다). 같은 코드가
1년 넘게 없다가 다시 나오면 다시 상장한 새 종목으로 친다 (코드 재사용).

과거 성적은 미래를 약속하지 않는다.
"""

import argparse
import collections
import datetime
import gzip
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
from krcommon import krx, krxdata  # noqa: E402

STORE = os.path.join("trader-kr", "cache", "invest")
FIRST_DAY = datetime.date(2010, 1, 4)        # KRX Open API 일별매매정보가 있는 첫 날
UNIVERSE = 300
WINDOW = 252                                 # 저변동성, 52주 고가의 거래일 창
MIN_RETS = 200
NW_LAG = 3
YEAR_MONTH = 4                               # 연 1회 리밸런싱의 신호 달 (4월 말. 사업보고서 제출 기한 3월 31일 한 달 뒤)
COST = {"commission": 0.00015, "tax": 0.002, "slip": 0.001}
MAX_CALLS = 9000                             # 한 번에 부르는 상한 (키당 하루 10,000건)
EMPTY_RUN_STOP = 7                           # 평일이 이만큼 이어서 비면 (한도 등) 멈춘다
REUSE_GAP = 365                              # 같은 코드가 이 날수 넘게 없다가 나오면 다른 회사 (코드 재사용)
NOTE = "과거 성적은 미래를 약속하지 않습니다. 배당이 빠진 가격 수익률이고, 지금 살 종목은 내지 않습니다."


class InvestError(Exception):
    pass


# ---------------------------------------------------------------- 자료 (KRX 일별매매정보)

def is_common(sym):
    return len(sym) == 6 and sym[-1] == "0"


def is_spac(name):
    return "스팩" in (name or "")


def day_file(store, d):
    return os.path.join(store, "days", d.strftime("%Y%m%d") + ".json.gz")


def compact(rows):
    """KRX 표 {종목: 행} -> 보통주만 [종목, 시장(K/Q), 이름, 종가, 전일대비, 시가총액, 거래대금]."""
    out = []
    for s in sorted(rows):
        r = rows[s]
        if is_common(s):
            out.append([s, "K" if r.get("market") == "KOSPI" else "Q", r.get("name") or "", r["c"], r.get("chg"),
                        r.get("mktcap"), r.get("value")])
    return out


def write_day(store, d, rows):
    path = day_file(store, d)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    n = collections.Counter(r.get("market") for r in rows.values())
    doc = {"date": d.isoformat(), "source": "KRX Open API 일별매매정보",
           "n": {"KOSPI": n.get("KOSPI", 0), "KOSDAQ": n.get("KOSDAQ", 0)}, "rows": compact(rows)}
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def read_day(store, d):
    path = day_file(store, d)
    if not os.path.exists(path):
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def weekdays(start, end):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += datetime.timedelta(days=1)


def download(start, end, key, store=STORE, now_kst=None, transport=None, max_calls=MAX_CALLS, workers=6,
             progress=None):
    """캐시에 없는 평일을 날짜 순으로 받는다. 키 오류나 한도에 걸리면 그 자리에서 멈춘다 (다음에 이어 받음).
    평일이 EMPTY_RUN_STOP 개 넘게 이어서 비면 한도 등으로 빈 답이 오는 것으로 보고 그 빈 날들을 지우고 멈춘다."""
    from concurrent.futures import ThreadPoolExecutor
    now_kst = now_kst or krx.kst_now()
    want = [d for d in weekdays(start, end) if krxdata.published(d, now_kst) and read_day(store, d) is None]
    out = {"wanted": len(want), "fetched": 0, "calls": 0, "stopped": None}
    empty_run = []
    i = 0
    while i < len(want):
        room = (max_calls - out["calls"]) // 2
        if room <= 0:
            out["stopped"] = "호출 상한 %d건에 닿아 멈췄습니다. 다음에 다시 부르면 이어 받습니다" % max_calls
            break
        batch = want[i:i + min(room, workers * 4)]
        i += len(batch)
        got, err = {}, None
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(batch)))) as pool:
            futs = [(d, pool.submit(krxdata.fetch_day, d, key, transport)) for d in batch]
            for d, f in futs:
                try:
                    got[d] = f.result()
                except krxdata.KrxDataError as e:
                    err = err or e
        out["calls"] += 2 * len(batch)
        for d in batch:                      # 날짜 순으로 쓴다. 오류 난 날 뒤는 쓰지 않는다
            if d not in got:
                break
            write_day(store, d, got[d])
            out["fetched"] += 1
            if got[d]:
                empty_run = []
            else:
                empty_run.append(d)
                if len(empty_run) > EMPTY_RUN_STOP:
                    for x in empty_run:
                        os.remove(day_file(store, x))
                    out["fetched"] -= len(empty_run)
                    out["stopped"] = ("%s 부터 평일 %d일이 이어서 비었습니다. 휴장이 이렇게 길지 않으니 한도 등으로 빈 답이 "
                                      "온 것으로 보고 멈췄습니다" % (empty_run[0].isoformat(), len(empty_run)))
                    return out
        if err:
            out["stopped"] = "KRX 오류로 멈췄습니다: %s" % err
            break
        if progress:
            progress("  %s 까지 %d / %d일, 호출 %d건" % (batch[-1].isoformat(), out["fetched"], len(want), out["calls"]))
    return out


def coverage(store=STORE, start=FIRST_DAY, end=None):
    """받은 범위: 해마다 거래일, 휴장 평일, 안 받은 평일, 한 시장만 빈 날, 전일대비 없는 행."""
    end = end or krx.kst_now().date()
    years = collections.OrderedDict()
    odd, no_chg, last = [], 0, None
    for d in weekdays(start, end):
        y = years.setdefault(d.year, {"open": 0, "closed": 0, "missing": 0, "symbols": set()})
        doc = read_day(store, d)
        if doc is None:
            y["missing"] += 1
            continue
        if not doc["rows"]:
            y["closed"] += 1
            continue
        y["open"] += 1
        last = d
        if not doc["n"].get("KOSPI") or not doc["n"].get("KOSDAQ"):
            odd.append(d.isoformat())
        for r in doc["rows"]:
            y["symbols"].add(r[0])
            if r[4] is None:
                no_chg += 1
    out = []
    prev = None
    for yr, y in years.items():
        gone = len(prev - y["symbols"]) if prev is not None else None
        out.append({"year": yr, "open": y["open"], "closed": y["closed"], "missing": y["missing"],
                    "symbols": len(y["symbols"]), "gone_since_last_year": gone})
        prev = y["symbols"] if y["symbols"] else prev
    return {"years": out, "one_market_empty": odd, "rows_without_chg": no_chg, "last_open": last}


def trading_days(store=STORE, start=FIRST_DAY, end=None):
    end = end or krx.kst_now().date()
    out = []
    for d in weekdays(start, end):
        doc = read_day(store, d)
        if doc and doc["rows"]:
            out.append((d, doc["rows"]))
    return out


# ---------------------------------------------------------------- 패널 (월말 신호일과 체결일의 값)

class Snap(object):
    __slots__ = ("mkt", "cap", "value", "spac", "vol", "hi")

    def __init__(self, mkt, cap, value, spac, vol, hi):
        self.mkt, self.cap, self.value, self.spac, self.vol, self.hi = mkt, cap, value, spac, vol, hi


class _State(object):
    __slots__ = ("idx", "c", "first", "last", "di", "w_idx", "w_r", "s1", "s2", "nr")

    def __init__(self, d):
        self.idx, self.c, self.first, self.last = 1.0, None, d, d
        self.di = collections.deque()          # 창 안 행의 거래일 번호
        self.w_idx = collections.deque()       # 그 행의 조정 가격
        self.w_r = collections.deque()         # 그 행의 하루 수익률 (첫 행은 None)
        self.s1 = self.s2 = 0.0
        self.nr = 0


def month_anchors(dates):
    """[(월, T, E)]. T 는 그달 마지막 거래일, E 는 그다음 거래일 (자료 끝이면 None).
    자료가 달 중간에 끝나면 그달은 넣지 않는다 (그 뒤 평일이 그달에 남아 있으면)."""
    out = []
    for i, d in enumerate(dates):
        nxt = dates[i + 1] if i + 1 < len(dates) else None
        if nxt is not None and (nxt.year, nxt.month) == (d.year, d.month):
            continue
        if nxt is None and any(x.month == d.month for x in weekdays(d + datetime.timedelta(days=1),
                                                                     d + datetime.timedelta(days=7))):
            continue
        out.append((d.strftime("%Y-%m"), d, nxt))
    return out


class Panel(object):
    """신호일(T)·체결일(E)의 조정 가격과 신호일의 종목 정보. 자료 하루하루를 한 번 훑어 만든다."""

    def __init__(self, days):
        self.dates = [d for d, _ in days]
        self.months = month_anchors(self.dates)
        self.mi = {m: i for i, (m, _, _) in enumerate(self.months)}
        self.snap, self.idx, self.lk = {}, {}, {}
        self.no_chg = 0
        t_of = {T: m for m, T, _ in self.months}
        anchors = set(t_of) | {E for _, _, E in self.months if E}
        if self.dates:
            anchors.add(self.dates[-1])
        st, alias = {}, {}
        self.reused = []
        for di, (d, rows) in enumerate(days):
            today = {}
            for code, mkt, name, c, chg, cap, val in rows:
                if not c or c <= 0:
                    continue
                s = alias.get(code, code)
                x = st.get(s)
                if x is not None and (d - x.last).days > REUSE_GAP:
                    # 1년 넘게 없다가 다시 나온 코드는 다른 회사로 친다 (코드 재사용). 앞 회사는 그날 사라진 것
                    s = alias[code] = s + "*"
                    self.reused.append((code, x.last, d))
                    x = None
                r = None
                if x is None:
                    x = st[s] = _State(d)
                else:
                    base = (c - chg) if chg is not None else None
                    if base and base > 0:
                        r = c / base - 1.0
                    elif x.c:
                        r = c / x.c - 1.0
                        self.no_chg += 1
                    if r is not None:
                        x.idx *= 1.0 + r
                        x.s1 += r
                        x.s2 += r * r
                        x.nr += 1
                x.di.append(di)
                x.w_idx.append(x.idx)
                x.w_r.append(r)
                x.c, x.last = c, d
                today[s] = (mkt, name, cap, val)
            if d in anchors:
                self.idx[d] = {s: st[s].idx for s in today}
                self.lk[d] = {s: x.idx for s, x in st.items() if s not in today}
            if d in t_of:
                cut = di - WINDOW
                snap = {}
                for s, (mkt, name, cap, val) in today.items():
                    x = st[s]
                    while x.di and x.di[0] <= cut:
                        x.di.popleft()
                        x.w_idx.popleft()
                        r = x.w_r.popleft()
                        if r is not None:
                            x.s1 -= r
                            x.s2 -= r * r
                            x.nr -= 1
                    vol = None
                    if x.nr >= MIN_RETS:
                        mean = x.s1 / x.nr
                        vol = math.sqrt(max(0.0, (x.s2 - x.nr * mean * mean) / (x.nr - 1)))
                    snap[s] = Snap(mkt, cap or 0.0, val or 0.0, is_spac(name), vol, x.idx / max(x.w_idx))
                self.snap[t_of[d]] = snap
        self.final = {s: (x.last, x.idx) for s, x in st.items()}
        self.first = {s: x.first for s, x in st.items()}
        for a in self.lk:                       # 잠시 멈춘 종목만 남긴다 (영영 사라진 종목은 final 로 충분)
            self.lk[a] = {s: v for s, v in self.lk[a].items() if self.final[s][0] > a}

    def price(self, s, a):
        """a 날의 조정 가격. 그날 행이 없으면 그 전 마지막 행의 값. 아직 상장 전이면 None."""
        v = self.idx.get(a, {}).get(s)
        if v is None:
            v = self.lk.get(a, {}).get(s)
        if v is None and s in self.final and self.final[s][0] <= a:
            v = self.final[s][1]
        return v

    def growth(self, s, a, b, delist="A"):
        if delist == "B" and self.final[s][0] < b:
            return 0.0
        pa, pb = self.price(s, a), self.price(s, b)
        return pb / pa if pa and pb else 1.0

    def period_end(self, i):
        """신호 월 i 의 보유가 끝나는 날: 다음 달 체결일, 자료 끝이면 다음 달 신호일."""
        m, T, E = self.months[i + 1]
        return E or T


def load_panel(store=STORE, end=None):
    return Panel(trading_days(store, FIRST_DAY, end))


# ---------------------------------------------------------------- 규칙

def rules():
    """r2 에 사전 등록한 16개 (순서 고정)."""
    out = [{"fam": "ew", "reb": "M"}]
    for fam in ("mom", "lowvol"):
        for n in (30, 50):
            for reb in ("M", "Q"):
                out.append({"fam": fam, "n": n, "reb": reb})
    for n in (30, 50):
        out.append({"fam": "small", "n": n, "reb": "Q"})
    for n in (30, 50):
        out.append({"fam": "hi52", "n": n, "reb": "M"})
    out.append({"fam": "rev", "n": 30, "reb": "M"})
    for ma in (10, 12):
        out.append({"fam": "trend", "ma": ma, "reb": "M"})
    return out


BENCH = {"fam": "cap", "reb": "M"}
FAM_NAMES = {"ew": "동일가중", "mom": "모멘텀12-1", "lowvol": "저변동성", "small": "소형", "hi52": "52주고가근접",
             "rev": "1개월반전"}


def name(v):
    if "label" in v:                         # 다른 스크립트(value.py)의 규칙은 이름을 들고 온다
        return v["label"]
    f = v["fam"]
    if f == "cap":
        return "B1 시가총액가중 %d" % UNIVERSE
    if f == "trend":
        return "추세타이밍 %d개월" % v["ma"]
    reb = "매달" if v["reb"] == "M" else "분기"
    if f == "ew":
        return "동일가중 %d %s" % (UNIVERSE, reb)
    return "%s %d종목 %s" % (FAM_NAMES[f], v["n"], reb)


def find_rule(text):
    for v in rules() + [BENCH]:
        if name(v) == text.strip():
            return v
    raise InvestError("규칙 '%s' 이 없습니다. python invest.py rules 로 이름을 보세요" % text)


def universe(P, i):
    """신호 월 i 의 U: [(종목, Snap)] 시가총액 큰 순 UNIVERSE 개. 12개월 전 신호일이 없으면 None."""
    if i < 12:
        return None
    m, T, _ = P.months[i]
    t12 = P.months[i - 12][1]
    cands = [(s, x) for s, x in P.snap[m].items()
             if x.value > 0 and x.cap > 0 and not x.spac and P.first[s] <= t12]
    cands.sort(key=lambda sx: (-sx[1].cap, sx[0]))
    return cands[:UNIVERSE]


def market_index(P, market=None):
    """신호일마다 {월: 값}. 그날 거래된 보통주(스팩 제외, 이력 조건 없음) 전부를 시가총액 비중대로 묶은 가격 지수.
    사라진 종목은 마지막 가격 (판 A). market 이 'K' 면 코스피만."""
    out, level, prev = collections.OrderedDict(), 1.0, None
    for i, (m, T, _) in enumerate(P.months):
        if prev is not None:
            pm, pT, w = prev
            tot = sum(w.values())
            level *= sum(c * P.growth(s, pT, T) for s, c in w.items()) / tot if tot else 1.0
        out[m] = level
        w = {s: x.cap for s, x in P.snap[m].items()
             if x.value > 0 and x.cap > 0 and not x.spac and (market is None or x.mkt == market)}
        prev = (m, T, w)
    return out


def targets(P, i, v, mkt_idx=None):
    """신호 월 i 의 목표 비중 {종목: 비중}. 빈 사전은 현금."""
    U = universe(P, i)
    if not U:
        return None
    f = v["fam"]
    if f in ("cap", "trend"):
        if f == "trend":
            if i - v["ma"] + 1 < 0:
                return None
            ms = [P.months[k][0] for k in range(i - v["ma"] + 1, i + 1)]
            if any(m not in mkt_idx for m in ms):
                return None
            vals = [mkt_idx[m] for m in ms]
            if not vals[-1] > sum(vals) / len(vals):
                return {}
        tot = sum(x.cap for _, x in U)
        return {s: x.cap / tot for s, x in U}
    if f == "ew":
        return {s: 1.0 / len(U) for s, _ in U}
    T, T1, T12 = P.months[i][1], P.months[i - 1][1], P.months[i - 12][1]
    scored = []
    for s, x in U:
        if f == "mom":
            a, b = P.price(s, T12), P.price(s, T1)
            if a and b:
                scored.append((-(b / a - 1.0), s))
        elif f == "rev":
            a, b = P.price(s, T1), P.price(s, T)
            if a and b:
                scored.append((b / a - 1.0, s))
        elif f == "lowvol":
            if x.vol is not None:
                scored.append((x.vol, s))
        elif f == "small":
            scored.append((x.cap, s))
        elif f == "hi52":
            scored.append((-x.hi, s))
    scored.sort()
    pick = [s for _, s in scored[:v["n"]]]
    return {s: 1.0 / len(pick) for s in pick} if pick else {}


def rebalances(v, i, P, first):
    if i == first or v["reb"] == "M":
        return True
    if v["reb"] == "Y":
        return P.months[i][1].month == YEAR_MONTH
    return P.months[i][1].month in (3, 6, 9, 12)


# ---------------------------------------------------------------- 시험

def backtest(P, v, start, end, delist="A", cost=COST, cost_x=1.0, targets_fn=None, income=None):
    """수익률 월 start ~ end (YYYY-MM). [{"m", "r", "turnover", "gone"}] 비용 뒤 월 수익률.
    신호 월 i 의 E 에 리밸런싱하고 다음 달 E 까지 보유. 그 수익률은 다음 달 이름으로 적는다.
    targets_fn(P, i, v) 를 주면 그 목표 비중을 쓴다. income(s, a, b) 를 주면 a ~ b 사이에 받은 배당을 그 종목에 다시
    넣은 배수를 곱한다 (value.py). 둘 다 없으면 r2 와 같다."""
    ms = [m for m, _, _ in P.months]
    if start not in ms or end not in ms:
        raise InvestError("자료에 %s ~ %s 가 없습니다 (있는 달: %s ~ %s)" % (start, end, ms[0] if ms else "-",
                                                                        ms[-1] if ms else "-"))
    first, last = ms.index(start) - 1, ms.index(end) - 1
    if first < 12:
        raise InvestError("%s 수익률에는 12개월 앞 자료가 필요합니다 (자료 첫 달 %s)" % (start, ms[0]))
    mkt_idx = market_index(P) if v["fam"] == "trend" else None
    buy = cost_x * (cost["commission"] + cost["slip"])
    sell = cost_x * (cost["commission"] + cost["slip"] + cost["tax"])
    hold, cash, out = {}, 1.0, []
    for i in range(first, last + 1):
        E = P.months[i][2]
        total = cash + sum(hold.values())
        turnover = 0.0
        if rebalances(v, i, P, first):
            tg = targets_fn(P, i, v) if targets_fn else targets(P, i, v, mkt_idx)
            if tg is None:
                raise InvestError("%s 신호를 만들 자료가 모자랍니다 (%s)" % (P.months[i][0], name(v)))
            cur = {s: h / total for s, h in hold.items()} if total > 0 else {}
            sold = sum(max(0.0, w - tg.get(s, 0.0)) for s, w in cur.items())
            bought = sum(max(0.0, w - cur.get(s, 0.0)) for s, w in tg.items())
            after = total * (1.0 - sold * sell - bought * buy)
            hold = {s: w * after for s, w in tg.items()}
            cash = after * (1.0 - sum(tg.values()))
            turnover = (sold + bought) / 2.0
        b = P.period_end(i)
        gone = 0
        for s in list(hold):
            g = P.growth(s, E, b, delist)
            if income and g:
                g *= income(s, E, b)
            if E <= P.final[s][0] < b:         # 이 보유 기간에 사라짐 (상장폐지·합병)
                gone += 1
            hold[s] *= g
            if hold[s] == 0.0:
                del hold[s]
        out.append({"m": P.months[i + 1][0], "r": (cash + sum(hold.values())) / total - 1.0, "turnover": turnover,
                    "gone": gone})
    return out


def holm(ps):
    """홀름 보정 p 값 (입력 순서 그대로)."""
    m = len(ps)
    order = sorted(range(m), key=lambda i: ps[i])
    adj, run = [0.0] * m, 0.0
    for rank, i in enumerate(order):
        run = max(run, min(1.0, (m - rank) * ps[i]))
        adj[i] = run
    return adj


def nw(xs, lag=NW_LAG):
    """평균, Newey-West 표준오차 (Bartlett, lag), 평균 > 0 의 한쪽 p (정규 근사)."""
    n = len(xs)
    if n < 3:
        return {"n": n, "mean": (sum(xs) / n) if n else None, "se": None, "p": 1.0}
    m = sum(xs) / n
    d = [x - m for x in xs]
    s = sum(x * x for x in d) / n
    for k in range(1, min(lag, n - 1) + 1):
        s += 2.0 * (1.0 - k / (lag + 1.0)) * sum(d[t] * d[t - k] for t in range(k, n)) / n
    se = math.sqrt(max(s, 0.0) / n)
    p = 0.5 * math.erfc((m / se) / math.sqrt(2)) if se > 0 else (0.0 if m > 0 else 1.0)
    return {"n": n, "mean": m, "se": se, "p": p}


def perf(rs):
    """연복리, 연 변동성, 최대 낙폭 (월 수익률 목록)."""
    if not rs:
        return {"cagr": None, "vol": None, "mdd": None}
    level, peak, mdd = 1.0, 1.0, 0.0
    for r in rs:
        level *= 1.0 + r
        peak = max(peak, level)
        mdd = min(mdd, level / peak - 1.0)
    mean = sum(rs) / len(rs)
    sd = math.sqrt(sum((r - mean) ** 2 for r in rs) / (len(rs) - 1)) if len(rs) > 1 else 0.0
    return {"cagr": level ** (12.0 / len(rs)) - 1.0, "vol": sd * math.sqrt(12), "mdd": mdd}


def compare(strat, bench):
    """같은 달끼리 초과수익 (규칙 - 기준)."""
    b = {x["m"]: x["r"] for x in bench}
    ex = [x["r"] - b[x["m"]] for x in strat if x["m"] in b]
    st = nw(ex)
    st["strategy"] = perf([x["r"] for x in strat])
    st["bench"] = perf([x["r"] for x in bench])
    st["turnover"] = sum(x["turnover"] for x in strat) / len(strat) if strat else None
    st["gone"] = sum(x["gone"] for x in strat)
    return st


def dca(rs, months=36):
    """창마다 한 번에 넣기와 매달 같은 금액 넣기의 끝 가치. 한 번에 넣기가 나은 창 비율, 끝 가치 비율 중앙값."""
    out = []
    for i in range(0, len(rs) - months + 1):
        w = rs[i:i + months]
        lump = months * math.prod(1.0 + r for r in w)
        drip = sum(math.prod(1.0 + r for r in w[j:]) for j in range(months))
        out.append(lump / drip)
    if not out:
        return {"windows": 0, "lump_wins": None, "median_ratio": None}
    s = sorted(out)
    return {"windows": len(out), "lump_wins": sum(1 for x in out if x > 1.0) / len(out),
            "median_ratio": s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2.0}


# ---------------------------------------------------------------- 명령

def cmd_data(a):
    store = a.store
    if not a.check:
        key = krxdata.api_key()
        if not key:
            raise InvestError("KRX 키가 없습니다. 터미널에서 python krcommon/keys.py krx 로 넣으세요 (채팅에 붙여 넣지 마세요)")
        start = datetime.date.fromisoformat(a.start)
        end = datetime.date.fromisoformat(a.end) if a.end else krx.kst_now().date()
        print("KRX 일별매매정보를 받습니다: %s ~ %s (캐시에 없는 평일만, 호출 상한 %d건)" % (start, end, a.max_calls),
              flush=True)
        res = download(start, end, key, store, max_calls=a.max_calls,
                       progress=lambda m: print(m, flush=True))
        print("받은 날 %d / %d, 호출 %d건" % (res["fetched"], res["wanted"], res["calls"]), flush=True)
        if res["stopped"]:
            print(res["stopped"], flush=True)
    cov = coverage(store)
    print("해마다 (거래일 / 휴장 평일 / 안 받은 평일 / 종목 수 / 전해에 있다 사라진 종목):")
    for y in cov["years"]:
        print("  %d: %3d / %2d / %3d / %5d / %s" % (y["year"], y["open"], y["closed"], y["missing"], y["symbols"],
                                                 "-" if y["gone_since_last_year"] is None else y["gone_since_last_year"]))
    print("한 시장만 빈 날 %d일, 전일대비 없는 행 %d개, 마지막 거래일 %s" % (
        len(cov["one_market_empty"]), cov["rows_without_chg"], cov["last_open"] or "-"))
    return 0


def month_end(m):
    """'2023-10' -> 그달 다음 달 10일 (보유 기간 끝 체결일까지 읽을 자료 끝)."""
    y, mo = int(m[:4]), int(m[5:7])
    return datetime.date(y + (mo == 12), mo % 12 + 1, 10)


def pct(x, nd=2):
    return "-" if x is None else "%+.*f%%" % (nd, 100.0 * x)


def run_one(P, v, start, end, delist="A", cost=COST, cost_x=1.0):
    strat = backtest(P, v, start, end, delist, cost, cost_x)
    bench = backtest(P, BENCH, start, end, delist, cost, cost_x)
    st = compare(strat, bench)
    sizes = [len(universe(P, P.mi[x["m"]] - 1) or []) for x in strat]
    st.update({"rule": name(v), "from": start, "to": end, "delist": delist, "cost_x": cost_x, "months": len(strat),
               "universe_min": min(sizes) if sizes else 0})
    return st


def panel_for(a):
    """--to 달의 보유 기간 끝까지 읽는다 (그 뒤 자료는 읽지 않는다). --to 가 없으면 받은 자료 끝 달."""
    end_day = min(month_end(a.end), krx.kst_now().date()) if a.end else krx.kst_now().date()
    if a.progress:
        print("자료를 읽습니다 (%s 까지)..." % end_day, file=sys.stderr, flush=True)
    P = load_panel(a.store, end_day)
    if not P.months:
        raise InvestError("받은 자료가 없습니다. python invest.py data 로 먼저 받으세요")
    return P, a.end or P.months[-1][0]


def cmd_run(a):
    v = find_rule(a.rule)
    P, end = panel_for(a)
    st = run_one(P, v, a.start, end, a.delist, COST, a.cost_x)
    if st["universe_min"] < UNIVERSE:
        raise InvestError("종목군이 %d개보다 작은 달이 있습니다 (최소 %d). 자료가 빠졌는지 data --check 로 보세요"
                          % (UNIVERSE, st["universe_min"]))
    if a.json:
        print(json.dumps(st, ensure_ascii=False, indent=1))
        return 0
    s, b = st["strategy"], st["bench"]
    print("%s 대 %s, 수익률 %s ~ %s (%d개월), 상장폐지 판 %s, 비용 x%g" % (
        st["rule"], name(BENCH), st["from"], st["to"], st["months"], st["delist"], st["cost_x"]))
    for nm, x in ((st["rule"], s), (name(BENCH), b)):
        print("  %s 연복리 %8s  연변동성 %6.1f%%  최대낙폭 %8s" % (krx.pad(nm, 26), pct(x["cagr"]), 100.0 * (x["vol"] or 0),
                                                         pct(x["mdd"], 1)))
    print("  월 초과수익 평균 %s (Newey-West 표준오차 %s), 0보다 크다는 한쪽 p %.3f" % (
        pct(st["mean"], 3), pct(st["se"], 3), st["p"]))
    print("  달마다 바꾼 비중 평균 %.0f%%, 보유 중 사라진 종목 %d번" % (100.0 * (st["turnover"] or 0), st["gone"]))
    print(NOTE)
    return 0


def cmd_dca(a):
    P, end = panel_for(a)
    rs = [x["r"] for x in backtest(P, BENCH, a.start, end, a.delist)]
    res = dca(rs, a.months)
    if a.json:
        print(json.dumps(res, ensure_ascii=False))
        return 0
    print("%s, %s ~ %s, %d개월 창 %d개 (한 달씩 밀며)" % (name(BENCH), a.start, end, a.months, res["windows"]))
    if res["windows"]:
        print("  처음에 한 번에 넣기가 매달 나눠 넣기보다 끝 가치가 컸던 창 %.0f%%, 끝 가치 비율(한 번에 / 나눠) 중앙값 %.3f"
              % (100.0 * res["lump_wins"], res["median_ratio"]))
    print(NOTE)
    return 0


def main(argv=None):
    krx.utf8_stdio()
    ap = argparse.ArgumentParser(description="장기 투자 실험실 (trader-kr-invest).")
    sub = ap.add_subparsers(dest="cmd")
    d = sub.add_parser("data", help="KRX 일별매매정보 받기 (캐시에 없는 날만), 받은 범위 세기")
    d.add_argument("--from", dest="start", default=FIRST_DAY.isoformat())
    d.add_argument("--to", dest="end")
    d.add_argument("--check", action="store_true", help="받지 않고 범위만 센다")
    d.add_argument("--max-calls", type=int, default=MAX_CALLS)
    sub.add_parser("rules", help="규칙 이름")
    r = sub.add_parser("run", help="규칙 하나를 B1 과 비교")
    r.add_argument("--rule", required=True, help='python invest.py rules 의 이름 그대로 (예: "모멘텀12-1 30종목 매달")')
    w = sub.add_parser("dca", help="B1 을 한 번에 넣기와 매달 나눠 넣기 비교")
    w.add_argument("--months", type=int, default=36)
    for x in (r, w):
        x.add_argument("--from", dest="start", default="2011-02", help="첫 수익률 달 (기본 2011-02)")
        x.add_argument("--to", dest="end", help="마지막 수익률 달 (기본: 받은 자료 끝 달)")
        x.add_argument("--delist", choices=("A", "B"), default="A", help="A 마지막 가격, B 사라지면 -100%%")
        x.add_argument("--json", action="store_true")
    r.add_argument("--cost-x", type=float, default=1.0, help="비용 배수 (민감도, 기본 1)")
    for x in (d, r, w):
        x.add_argument("--store", default=STORE)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        if a.cmd == "data":
            return cmd_data(a)
        if a.cmd == "rules":
            for v in [BENCH] + rules():
                print(name(v))
            return 0
        a.progress = not a.json
        if a.cmd == "run":
            return cmd_run(a)
        if a.cmd == "dca":
            return cmd_dca(a)
    except (InvestError, krxdata.KrxDataError, krx.KrxError) as e:
        print("멈춤: %s" % e, file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
