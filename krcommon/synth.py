"""시험과 데모용 합성 1분봉. 실제 시세가 아니다 (토스증권 자료는 공유할 수 없어서 만든다).

make_history() 는 합성 종목 SYN001 의 62 거래일 1분봉을 만든다. 마지막 날
(2026-10-28, 수) 은 정해진 모양이다:
  09:00~09:15 시가 범위: 고가 30,300, 저가 29,900
  09:15~10:10 범위 안에서 움직임 (종가 30,000 ~ 30,250)
  10:10~10:15 5분봉이 30,450 에 끝나 범위 고가를 넘음 -> 10:15 신호, 30,450
그 앞 61 거래일은 시드 고정 무작위 걸음이다. 호가 단위(50원)를 지킨다.
"""

import datetime
import random

from krcommon import krx

SYMBOL = "SYN001"
PLAN_DAY = datetime.date(2026, 10, 28)
TICK = 50


def business_days(end, n):
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= datetime.timedelta(days=1)
    return sorted(out)


def _tick(x):
    return int(round(x / TICK)) * TICK


def _bar(day, minute, o, h, l, c, v):
    start = datetime.datetime.combine(day, datetime.time(9, 0)) + datetime.timedelta(minutes=minute)
    return {"t": krx.ts_of(start), "o": float(o), "h": float(max(h, o, c)), "l": float(min(l, o, c)), "c": float(c),
            "v": float(v)}


def random_day(day, start_price, rng, vol=0.0011):
    """09:00~15:30 1분봉 390개: 접속매매 380개, 15:20~15:28 거래 없음, 15:29 종가 단일가."""
    out = []
    px = _tick(start_price)
    for m in range(380):
        o = px
        c = _tick(o * (1 + rng.gauss(0, vol)))
        c = min(max(c, 26000), 36000)
        h = max(o, c) + TICK * rng.choice((0, 0, 1))
        l = min(o, c) - TICK * rng.choice((0, 0, 1))
        v = 60000 if m == 0 else rng.randint(1000, 6000)
        out.append(_bar(day, m, o, h, l, c, v))
        px = c
    for m in range(380, 389):
        out.append(_bar(day, m, px, px, px, px, 0))
    close = _tick(px * (1 + rng.gauss(0, 0.001)))
    out.append(_bar(day, 389, close, close, close, close, 40000))
    return out, close


def plan_day(day, prev_close):
    """정해진 모양의 계획일."""
    out = []
    path = [30000, 30100, 30200, 30300, 30250, 30150, 30050, 29950, 29900, 30000, 30100, 30150, 30200, 30150, 30100]
    prev = prev_close
    for m, c in enumerate(path):          # 09:00 ~ 09:14
        o = prev if m else 30000
        h = max(o, c)
        l = min(o, c)
        out.append(_bar(day, m, o, h, l, c, 60000 if m == 0 else 3000))
        prev = c
    mid = [30000, 30050, 30100, 30150, 30200, 30250, 30200, 30150, 30100, 30050, 30000, 30050, 30100]
    for m in range(15, 70):               # 09:15 ~ 10:09
        c = mid[(m - 15) % len(mid)]
        out.append(_bar(day, m, prev, max(prev, c), min(prev, c), c, 2500))
        prev = c
    for m, c in zip(range(70, 75), (30150, 30250, 30300, 30400, 30450)):   # 10:10 ~ 10:14
        out.append(_bar(day, m, prev, max(prev, c), min(prev, c), c, 5000))
        prev = c
    rest, _ = random_day(day, prev, random.Random(7))
    out.extend(rest[75:])                 # 10:15 이후는 무작위 걸음
    return out


def make_history(sessions=61, seed=11):
    rng = random.Random(seed)
    days = business_days(PLAN_DAY, sessions + 1)
    bars, px = [], 30000
    for d in days[:-1]:
        b, px = random_day(d, _tick(px * (1 + rng.gauss(0, 0.004))), rng)
        bars.extend(b)
    bars.extend(plan_day(days[-1], px))
    bars.sort(key=lambda x: x["t"])
    return {"symbol": SYMBOL, "name": "합성종목", "interval": "1m", "source": "synthetic", "bars": bars}


def flat_day(day=PLAN_DAY, price=30000):
    """범위를 한 번도 넘지 않는 날 (만료 시험용)."""
    out = []
    for m in range(390):
        c = price + (TICK if m % 2 else 0)
        out.append(_bar(day, m, price, c, price, c, 60000 if m == 0 else 1000))
    return {"symbol": SYMBOL, "interval": "1m", "source": "synthetic", "bars": out}
