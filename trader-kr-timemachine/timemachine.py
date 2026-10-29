#!/usr/bin/env python3
"""타임머신 (trader-kr-timemachine): 계획한 단타를 돈을 걸기 전에 다시 돌려 본다.
표준 라이브러리만 쓴다.

진입 뒤 남은 장을 10,000번 모의로 돌리고, 같은 규칙을 지난 거래일마다 다시 적용한다.

    python timemachine.py run                          # trader-kr/plan.json, 10,000회, 시드 19
    python timemachine.py run --write                  # trader-kr/timemachine.json 에도 저장
    python timemachine.py run --until 15:15            # 청산 시각에서 멈춤
    python timemachine.py run --sessions 120 --seed 7 --replays 20000 --json
    python timemachine.py run --symbol 005930 --date 2026-10-28 --entry 72300 \\
        --stop 71700 --target 73500 --entry-time 10:15
    python timemachine.py chart                        # 경로 부채꼴 그림 (ASCII)

모의 방식 (원본 sim.py 와 같은 알고리즘, 같은 난수 순서):
  1. 토스증권 1분봉으로 계획일 전 --sessions(기본 60)개 거래일을 받아 5분봉으로 묶는다.
     "온전한 거래일"만 쓴다: 09:00~15:20 접속매매 5분봉 76개가 다 있고 첫 봉에
     거래가 있는 날. 개장이 늦은 날(신년 첫날, 수능일), 빈 봉이 있는 날은 뺀다.
  2. 재료: 하루의 봉 자리 1..75 마다, 지난 거래일들의 그 자리 봉을 직전 봉 종가에
     대한 비율 (시가, 고가, 저가, 종가) / 직전 종가 로 모은다.
  3. 한 번의 재생은 진입가에서 시작해 남은 봉을 걷는다. 봉 i 마다 자리 j = i +
     randint(-window, window) (1..75 안)를 고르고, 그 자리 재료에서 하나를 골라
     지금 가격에 곱한다. 가격은 계획일 상·하한가 안으로 자른다. 한 봉 안에서
     손절가를 목표가보다 먼저 본다. 둘 다 닿은 봉은 손절로 센다 (보수적).
  4. 결과: target 또는 stop (먼저 닿은 쪽), 둘 다 아니면 --until 시각(기본 15:20,
     접속매매 끝)까지 열린 채 "neither". green = 목표 먼저, 또는 --until 에 진입가 위.
     R = 목표면 목표 R, 손절이면 -1, 아니면 (--until 가격 - 진입가) / 위험.
     R(비용 뺀) = 수수료와 매도 거래세를 뺀 손익 / 위험. 갭으로 손절가를 건너뛰어도
     여기서는 -1R 이지만 실제 체결은 더 나쁠 수 있다.
  5. --until 가격의 중앙값과 5~95 백분위. 부채꼴 그림은 같은 시드로 2,000개 경로를
     한 번 더 돌린다. 히스토그램은 1~99 백분위 사이 24칸.
  6. 과거: 계획의 규칙 (같은 시가 범위 길이, 손절 규칙, 목표 R 의 시가 범위 돌파)을
     지난 온전한 거래일마다 다시 적용. 범위를 넘어 처음 끝난 봉 종가가 진입,
     같은 손절 먼저 규칙.

원본에 없는 보조 결과 두 가지 (판정은 원본 방식으로만 낸다):
  - 30분 묶음 재생 (--block 6): 봉을 하나씩 따로 고르지 않고, 같은 거래일의 연속 봉 6개씩
    이어 붙인다. 같은 날 안의 변동성 군집과 짧은 추세가 남는다. 원본 방식과 목표·손절 비율이
    5%p 넘게 다르면 알린다.
  - 과거 재적용은 재료보다 긴 기간(--history-sessions, 기본 120 거래일)에 하고, 목표 비율의 95%
    윌슨 구간과 평균 R 의 95% 부트스트랩 구간을 붙인다. 구간이 본전 비율을 포함하면 그렇다고 말한다.

판정: 손절이나 목표에 닿은 재생 중 목표 비율 target / (target + stop) 을 계획 R 의
본전 비율 1 / (1 + R) (1R 이면 50%, 2R 이면 33%) 와 비교한다.
  5%p 미만 "동전 던지기에 가깝다"(1R) / "본전에 가깝다"(그 밖)
  5~15%p "목표 쪽으로 기운다" / "손절 쪽으로 기운다", 15%p 이상 "강하게"
40% 넘게 아무 쪽에도 안 닿았으면 그렇다고 덧붙이고, 평균 R 이 기운 쪽과 반대면 알린다.

온전한 거래일이 20개보다 적거나 진입 봉이 자료에 없으면 이유를 말하고 멈춘다.
같은 계획, 같은 자료, 같은 --seed 면 어느 컴퓨터에서나 같은 숫자가 나온다.
최근 거래일로 만든 모의일 뿐 예측이 아니다. 실적 발표, 공시, 거래정지, VI 는
지난 거래일에 없던 일을 만들 수 있다.

자료: 토스증권 1분봉 (2022-11-23 이후, KRX+NXT 통합 시세, trader-kr/cache 에 캐시).
--offline FILE 은 저장된 1분봉 파일을 읽는다. 시각은 한국 시간.
"""

import argparse
import datetime
import json
import math
import os
import random
import re
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
from krcommon import bars as kbars, dart, data, krx  # noqa: E402

STEP = 300
OPEN_MIN = 9 * 60
AUCTION_MIN = 15 * 60 + 20
SESSION_BARS = (AUCTION_MIN - OPEN_MIN) // 5      # 76
MIN_SESSIONS = 20
SAMPLE_PATHS = 160
BAND_PATHS = 2000
BAND_Q = (0.05, 0.25, 0.5, 0.75, 0.95)
HIST_BINS = 24
COIN_FLIP = 0.05
STRONG = 0.15
MANY_NEITHER = 0.40
WEEKDAYS = ("월", "화", "수", "목", "금", "토", "일")


class TMError(Exception):
    pass


def p(x):
    return krx.price_str(x)


def clock(minutes):
    return "%02d:%02d" % divmod(minutes, 60)


def parse_clock(text, what):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text or ""))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise TMError("%s 는 10:15 같은 한국 시간이어야 합니다 (받은 값 %r)" % (what, text))
    return int(m.group(1)) * 60 + int(m.group(2))


# ---------------------------------------------------------------- 자료

def day_slots(chart):
    """{날짜 문자열: {자리: 5분봉}} (기본 장 시간 기준, 완성 봉만)."""
    out = {}
    for day, bs in kbars.split_days(chart["bars"]).items():
        s = krx.Session(day)
        slots = {b["slot"]: b for b in kbars.session_slots(bs, s, STEP) if b["complete"]}
        if slots:
            out[day.isoformat()] = slots
    return out


def prev_close_of(chart, day, calendar=None):
    """계획일 직전 거래일의 정규장 종가 (자료에 있으면). 그날 장 시간을 캐시된 장 운영
    정보에서 알면 그것으로 (늦게 닫는 날의 종가 단일가를 놓치지 않게), 모르면 기본 장 시간."""
    days = kbars.split_days(chart["bars"])
    older = sorted(d for d in days if d < day)
    if not older:
        return None
    d = older[-1]
    s = calendar.session(d) if calendar is not None and calendar.known(d) else None
    return kbars.day_close(days[d], s or krx.Session(d))


def full_sessions(days, before):
    """계획일 전의 온전한 거래일 (76개 봉 + 첫 봉 거래 있음), 오래된 것부터. 그리고 뺀 날."""
    full, short = [], []
    for d in sorted(days):
        if d >= before:
            continue
        s = days[d]
        if len(s) == SESSION_BARS and s[0]["v"] > 0:
            full.append((d, [s[i] for i in range(SESSION_BARS)]))
        else:
            short.append({"date": d, "bars": len(s)})
    return full, short


# ---------------------------------------------------------------- 모의 (sim.py)

def build_pool(sessions):
    pool = [[] for _ in range(SESSION_BARS)]
    for _, Dd in sessions:
        for i in range(1, SESSION_BARS):
            pc = Dd[i - 1]["c"]
            pool[i].append((Dd[i]["o"] / pc, Dd[i]["h"] / pc, Dd[i]["l"] / pc, Dd[i]["c"] / pc))
    return pool


def pct(a, q):
    return a[min(len(a) - 1, int(q * len(a)))]


def simulate(pool, entry, stop, target, k, last, replays=10000, seed=19, window=2, limits=None,
             commission_rate=0.0, sell_tax_rate=0.0):
    """진입 봉 k 다음부터 last 봉까지 몬테카를로 재생. 매수만.
    limits=(하한가, 상한가) 이면 가격을 그 안으로 자른다."""
    risk = entry - stop
    r_target = round((target - entry) / risk, 4)
    lo_lim, hi_lim = limits if limits else (None, None)

    def clamp(x):
        if hi_lim is not None and x > hi_lim:
            return hi_lim
        if lo_lim is not None and x < lo_lim:
            return lo_lim
        return x

    def slot(rng, i):
        return i if window == 0 else min(SESSION_BARS - 1, max(1, i + rng.randint(-window, window)))

    def net_r(outcome, px):
        exitp = target if outcome == "target" else stop if outcome == "stop" else px
        cost = entry * commission_rate + exitp * (commission_rate + sell_tax_rate)
        return (exitp - entry - cost) / risk

    rng = random.Random(seed)
    choice = rng.choice
    outcomes, closes, paths = [], [], []
    for n in range(replays):
        px = entry
        outcome = None
        keep = n < SAMPLE_PATHS
        path = [entry]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            hi, lo, cl = clamp(px * h), clamp(px * l), clamp(px * c)
            if outcome is None:
                if lo <= stop:
                    outcome = "stop"
                elif hi >= target:
                    outcome = "target"
            px = cl
            if keep:
                path.append(round(px, 1))
        outcomes.append(outcome or "close")
        closes.append(px)
        if keep:
            paths.append(path)
    cnt = {x: outcomes.count(x) for x in ("target", "stop", "close")}
    green = sum(1 for o, c in zip(outcomes, closes) if o == "target" or (o == "close" and c > entry))
    Rs = [r_target if o == "target" else -1.0 if o == "stop" else (c - entry) / risk for o, c in zip(outcomes, closes)]
    Rn = [net_r(o, c) for o, c in zip(outcomes, closes)]
    srt = sorted(closes)
    rng = random.Random(seed)
    choice = rng.choice
    allp = []
    for n in range(BAND_PATHS):
        px = entry
        path = [entry]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            px = clamp(px * c)
            path.append(px)
        allp.append(path)
    bands = []
    for j in range(last - k + 1):
        col = sorted(pp[j] for pp in allp)
        bands.append([round(pct(col, q), 1) for q in BAND_Q])
    lo_, hi_ = pct(srt, 0.01), pct(srt, 0.99)
    W = (hi_ - lo_) / HIST_BINS
    bins = [0] * HIST_BINS
    for c in srt:
        b = int((c - lo_) / W) if W > 0 else 0
        if 0 <= b < HIST_BINS:
            bins[b] += 1
    return {
        "replays": replays, "seed": seed, "window": window, "bars_left": last - k,
        "p_target": round(cnt["target"] / float(replays), 4), "p_stop": round(cnt["stop"] / float(replays), 4),
        "p_neither": round(cnt["close"] / float(replays), 4), "p_green": round(green / float(replays), 4),
        "median_close": round(pct(srt, 0.5), 1), "p5_close": round(pct(srt, 0.05), 1),
        "p95_close": round(pct(srt, 0.95), 1), "avg_R": round(statistics.mean(Rs), 3),
        "avg_R_net": round(statistics.mean(Rn), 3), "r_at_target": r_target,
        "limits": list(limits) if limits else None, "band_paths": BAND_PATHS, "bands": bands,
        "sample_paths": paths, "hist": {"lo": round(lo_, 1), "hi": round(hi_, 1), "bins": bins},
    }


# ---------------------------------------------------------------- 묶음 재생 (원본에 없는 보조 방식)

def simulate_blocks(sessions, entry, stop, target, k, last, replays=10000, seed=19, block=6, limits=None,
                    commission_rate=0.0, sell_tax_rate=0.0):
    """봉을 하나씩 따로 고르는 대신, block 개(기본 6개 = 30분)씩 같은 거래일의 연속 봉을 가져온다.
    시각은 맞춘다 (자리 i 에는 그날의 자리 i 봉). 같은 날 안의 변동성 군집과 짧은 추세가 남는다.
    결과 숫자만 돌려준다 (경로 그림은 원본 방식으로 그린다)."""
    if block < 1:
        raise TMError("--block 은 1 이상입니다")
    ratios = []
    for _, Dd in sessions:
        row = [None] * SESSION_BARS
        for i in range(1, SESSION_BARS):
            pc = Dd[i - 1]["c"]
            row[i] = (Dd[i]["h"] / pc, Dd[i]["l"] / pc, Dd[i]["c"] / pc)
        ratios.append(row)
    risk = entry - stop
    r_target = (target - entry) / risk
    lo_lim, hi_lim = limits if limits else (None, None)

    def clamp(x):
        if hi_lim is not None and x > hi_lim:
            return hi_lim
        if lo_lim is not None and x < lo_lim:
            return lo_lim
        return x

    rng = random.Random(seed)
    n_s = len(ratios)
    cnt = {"target": 0, "stop": 0, "close": 0}
    green, Rs, Rn = 0, [], []
    for _ in range(replays):
        px, outcome, s = entry, None, 0
        for i in range(k + 1, last + 1):
            if (i - k - 1) % block == 0:
                s = rng.randrange(n_s)
            h, l, c = ratios[s][i]
            hi, lo, cl = clamp(px * h), clamp(px * l), clamp(px * c)
            if outcome is None:
                if lo <= stop:
                    outcome = "stop"
                elif hi >= target:
                    outcome = "target"
            px = cl
        o = outcome or "close"
        cnt[o] += 1
        if o == "target" or (o == "close" and px > entry):
            green += 1
        exitp = target if o == "target" else stop if o == "stop" else px
        Rs.append(r_target if o == "target" else -1.0 if o == "stop" else (px - entry) / risk)
        Rn.append((exitp - entry - (entry * commission_rate + exitp * (commission_rate + sell_tax_rate))) / risk)
    return {"block": block, "replays": replays, "seed": seed,
            "p_target": round(cnt["target"] / float(replays), 4), "p_stop": round(cnt["stop"] / float(replays), 4),
            "p_neither": round(cnt["close"] / float(replays), 4), "p_green": round(green / float(replays), 4),
            "avg_R": round(statistics.mean(Rs), 3), "avg_R_net": round(statistics.mean(Rn), 3)}


# ---------------------------------------------------------------- 불확실성

def wilson(k, n, z=1.96):
    """k / n 비율의 95% 윌슨 구간 (n = 0 이면 None)."""
    if n <= 0:
        return None
    p = k / float(n)
    d = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return round(max(0.0, mid - half), 4), round(min(1.0, mid + half), 4)


def bootstrap_mean(xs, seed=19, n=2000):
    """평균의 95% 부트스트랩 구간 (2개 미만이면 None)."""
    if len(xs) < 2:
        return None
    rng = random.Random(seed)
    m = len(xs)
    means = sorted(sum(xs[rng.randrange(m)] for _ in range(m)) / float(m) for _ in range(n))
    return round(means[int(0.025 * n)], 3), round(means[min(n - 1, int(0.975 * n))], 3)


# ---------------------------------------------------------------- 과거 재적용

def opening_range(Dd, n):
    rows = [Dd[i] for i in range(n)]
    return max(b["h"] for b in rows), min(b["l"] for b in rows)


def rule_stop(rule, oh, ol, entry):
    r = rule["stop_rule"]
    if r == "or_mid":
        return krx.floor_tick((oh + ol) / 2.0)
    if r == "or_low":
        return krx.floor_tick(ol)
    return krx.floor_tick(entry * (1 - rule["stop_pct"] / 100.0))


def history_replay(sessions, rule, last, commission_rate=0.0, sell_tax_rate=0.0):
    n = rule["or_minutes"] // 5
    trades = []
    for d, Dd in sessions:
        oh, ol = opening_range(Dd, n)
        kk = next((i for i in range(n, last + 1) if Dd[i]["c"] > oh), None)
        if kk is None:
            continue
        e = krx.ceil_tick(Dd[kk]["c"])         # 호가 사이 체결가는 매수 호가로 올린다
        s = rule_stop(rule, oh, ol, e)
        risk = e - s
        if risk <= 0:
            continue
        t = krx.round_tick(e + rule["target_r"] * risk)
        oc = None
        for b in Dd[kk + 1:last + 1]:
            if b["l"] <= s:
                oc = "stop"
                break
            if b["h"] >= t:
                oc = "target"
                break
        cl = Dd[last]["c"]
        exitp = t if oc == "target" else s if oc == "stop" else cl
        R = (t - e) / float(risk) if oc == "target" else -1.0 if oc == "stop" else (cl - e) / float(risk)
        Rn = (exitp - e - (e * commission_rate + exitp * (commission_rate + sell_tax_rate))) / float(risk)
        trades.append({"date": d, "time": clock(OPEN_MIN + 5 * (kk + 1)), "entry": e, "stop": s, "target": t,
                       "outcome": oc or "close", "R": round(R, 2), "R_net": round(Rn, 2)})
    n_t = sum(1 for x in trades if x["outcome"] == "target")
    n_s = sum(1 for x in trades if x["outcome"] == "stop")
    return {
        "sessions": len(sessions), "triggered": len(trades), "target": n_t, "stop": n_s,
        "close": sum(1 for x in trades if x["outcome"] == "close"),
        "green": sum(1 for x in trades if x["R"] > 0),
        "avg_R": round(statistics.mean(x["R"] for x in trades), 3) if trades else None,
        "avg_R_net": round(statistics.mean(x["R_net"] for x in trades), 3) if trades else None,
        "target_share": round(n_t / float(n_t + n_s), 4) if n_t + n_s else None,
        "target_share_ci": wilson(n_t, n_t + n_s),
        "break_even_share": round(1.0 / (1.0 + rule["target_r"]), 4),
        "avg_R_ci": bootstrap_mean([x["R"] for x in trades]),
        "avg_R_net_ci": bootstrap_mean([x["R_net"] for x in trades]),
        "rule": rule, "trades": trades,
    }


# ---------------------------------------------------------------- 계획

def _num(v, what):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise TMError("계획의 %s 가 숫자가 아닙니다: %r" % (what, v))
    if not x > 0 or math.isinf(x):
        raise TMError("계획의 %s 는 0보다 큰 가격이어야 합니다: %r" % (what, v))
    return x


def load_plan(a, now_kst):
    if a.symbol:
        missing = [f for f in ("entry", "stop", "target", "entry_time") if getattr(a, f) is None]
        if missing:
            raise TMError("--symbol 과 함께 %s 도 주세요" % ", ".join("--" + m.replace("_", "-") for m in missing))
        raw = {"symbol": a.symbol, "side": "buy", "date": a.date or now_kst.date().isoformat(), "entry": a.entry,
               "stop": a.stop, "target": a.target, "entry_time": a.entry_time, "setup": a.setup,
               "opening_range": {"minutes": a.or_minutes}, "stop_rule": a.stop_rule, "interval": "5m"}
        return normalize_plan(raw, "flags")
    if not os.path.exists(a.plan):
        raise TMError("%s 에 계획이 없습니다. /trader-kr-exit 를 먼저 돌리거나 --symbol --date --entry --stop --target "
                      "--entry-time 을 주세요" % a.plan)
    try:
        with open(a.plan, encoding="utf-8") as fh:
            raw = json.load(fh)
    except ValueError as e:
        raise TMError("%s 가 올바른 JSON 이 아닙니다: %s" % (a.plan, e))
    if not isinstance(raw, dict):
        raise TMError("%s 는 계획 형식이 아닙니다" % a.plan)
    return normalize_plan(raw, a.plan)


def normalize_plan(raw, src="flags"):
    try:
        sym = krx.norm_symbol(raw.get("symbol"))
    except krx.KrxError as e:
        raise TMError(str(e))
    if str(raw.get("side") or "buy").lower() != "buy":
        raise TMError("매수 계획만 돌립니다 (계획은 %r)" % raw.get("side"))
    if str(raw.get("interval") or "5m") != "5m":
        raise TMError("5분봉 계획만 돌립니다 (계획은 %s). 1분봉 재료는 봉마다 흔들림이 커서 공정하지 않습니다" % raw.get("interval"))
    try:
        date = datetime.date.fromisoformat(str(raw.get("date") or "")).isoformat()
    except ValueError:
        raise TMError("계획에 2026-10-28 같은 날짜가 필요합니다 (받은 값 %r)" % raw.get("date"))
    entry, stop, target = (_num(raw.get(k), k) for k in ("entry", "stop", "target"))
    if not stop < entry < target:
        raise TMError("매수는 손절가 < 진입가 < 목표가 여야 합니다 (손절 %s, 진입 %s, 목표 %s)" % (p(stop), p(entry), p(target)))
    k = None
    if raw.get("entry_time"):
        close_min = parse_clock(raw["entry_time"], "entry_time")
        if (close_min - OPEN_MIN) % 5:
            raise TMError("entry_time %s 가 5분 격자가 아닙니다 (봉이 끝나는 시각, 예: 10:15)" % raw["entry_time"])
        k = (close_min - OPEN_MIN) // 5 - 1
    if raw.get("entry_bar") is not None:
        try:
            kb = int(raw["entry_bar"])
        except (TypeError, ValueError):
            raise TMError("entry_bar 는 봉 번호여야 합니다: %r" % raw["entry_bar"])
        if k is not None and kb != k:
            raise TMError("계획이 서로 안 맞습니다: entry_bar %d 인데 entry_time %s 는 %d번째 봉입니다" % (kb, raw["entry_time"], k))
        k = kb
    if k is None:
        raise TMError("계획에 entry_time (진입 봉이 끝난 시각, 예: 10:15) 또는 entry_bar 가 필요합니다")
    if not 0 <= k <= SESSION_BARS - 2:
        raise TMError("진입은 09:05 ~ 15:15 사이에 끝난 봉이어야 다시 돌릴 봉이 남습니다 (받은 값 %s)"
                      % clock(OPEN_MIN + 5 * (k + 1)))
    orng = raw.get("opening_range") or {}
    or_min = orng.get("minutes", 15) if isinstance(orng, dict) else 15
    try:
        or_min = int(or_min)
    except (TypeError, ValueError):
        raise TMError("opening_range.minutes 가 숫자가 아닙니다")
    if or_min < 5 or or_min % 5 or or_min >= SESSION_BARS * 5:
        raise TMError("opening_range.minutes 는 5의 배수여야 합니다 (받은 값 %s)" % or_min)
    s = raw.get("session") or {}
    if s and (s.get("open") != "09:00" or s.get("auction") != "15:20"):
        raise TMError("계획일 장 시간이 다릅니다 (%s 개장, %s 접속매매 끝). 타임머신은 09:00~15:20 거래일만 돌립니다"
                      % (s.get("open"), s.get("auction")))
    pl = raw.get("price_limits") or {}
    return {
        "symbol": sym, "name": raw.get("name"), "side": "buy", "date": date,
        "setup": str(raw.get("setup") or "orb"), "entry": entry, "entry_time": clock(OPEN_MIN + 5 * (k + 1)),
        "entry_bar": k, "stop": stop, "target": target, "risk_per_share": round(entry - stop, 2),
        "target_r": round((target - entry) / (entry - stop), 2),
        "opening_range": dict(orng, minutes=or_min) if isinstance(orng, dict) else {"minutes": or_min},
        "stop_rule": str(raw.get("stop_rule") or "auto"), "interval": "5m", "flat_by": raw.get("flat_by"),
        "price_limits": pl if pl.get("lower") and pl.get("upper") else None,
        "commission_rate": raw.get("commission_rate"), "sell_tax_rate": raw.get("sell_tax_rate"), "from": src,
    }


def resolve_rule(plan, entry_day):
    if plan["setup"].lower() != "orb":
        return None
    n = plan["opening_range"]["minutes"] // 5
    rule = {"setup": "orb", "or_minutes": plan["opening_range"]["minutes"], "stop_rule": plan["stop_rule"],
            "target_r": plan["target_r"], "stop_pct": round(100.0 * plan["risk_per_share"] / plan["entry"], 4)}
    if rule["stop_rule"] == "auto":
        rule["stop_rule"] = "pct"
        if all(i in entry_day for i in range(n)):
            oh, ol = opening_range(entry_day, n)
            if krx.floor_tick((oh + ol) / 2.0) == plan["stop"]:
                rule["stop_rule"] = "or_mid"
            elif krx.floor_tick(ol) == plan["stop"]:
                rule["stop_rule"] = "or_low"
    elif rule["stop_rule"] not in ("or_mid", "or_low"):
        rule["stop_rule"] = "pct"
    if rule["stop_rule"] != "pct":
        del rule["stop_pct"]
    return rule


# ---------------------------------------------------------------- 실행

def lean(mc):
    pt, ps = mc["p_target"], mc["p_stop"]
    if pt + ps <= 0:
        return None
    r = mc.get("r_at_target") or 1.0
    share, even = pt / (pt + ps), 1.0 / (1.0 + r)
    return share - even, share, even


def verdict(mc):
    pt, ps = mc["p_target"], mc["p_stop"]
    lv = lean(mc)
    if lv is None:
        return "%s 까지 목표에도 손절에도 닿은 재생이 없습니다. 모두 %s 가격으로 정해집니다." % (mc["until"], mc["until"])
    d, share, even = lv
    r = mc.get("r_at_target") or 1.0
    one_r = abs(r - 1.0) < 0.005
    if abs(d) < COIN_FLIP:
        head = "동전 던지기에 가깝습니다" if one_r else "본전에 가깝습니다"
    elif abs(d) < STRONG:
        head = "목표 쪽으로 기웁니다" if d > 0 else "손절 쪽으로 기웁니다"
    else:
        head = "목표 쪽으로 강하게 기웁니다" if d > 0 else "손절 쪽으로 강하게 기웁니다"
    odds = "목표 먼저 %.1f%%, 손절 먼저 %.1f%%" % (100 * pt, 100 * ps)
    if one_r:
        return "%s: %s." % (head, odds)
    return ("%s: %gR 에서는 한쪽에 닿은 재생 중 목표가 %.0f%% 는 먼저 와야 본전인데 %.0f%% 였습니다 (%s)."
            % (head, round(r, 2), 100 * even, 100 * share, odds))


def verdict_notes(mc):
    notes = []
    lv = lean(mc)
    if mc.get("p_neither", 0) > MANY_NEITHER and lv is not None:
        notes.append("재생의 %.1f%% 는 %s 까지 어느 쪽에도 닿지 않았습니다. 그 결과는 %s 가격이 정합니다."
                     % (100 * mc["p_neither"], mc["until"], mc["until"]))
    avg = mc.get("avg_R")
    if lv and abs(lv[0]) >= COIN_FLIP and avg is not None and avg != 0 and (avg > 0) != (lv[0] > 0):
        notes.append("그래도 평균 R 은 %+.3f 입니다. 어느 쪽에도 안 닿은 재생이 반대로 끌어당깁니다." % avg)
    if mc.get("avg_R_net") is not None and avg is not None and avg > 0 >= mc["avg_R_net"]:
        notes.append("비용을 빼면 평균 R 이 %+.3f 로 0 이하가 됩니다. 수수료와 거래세가 남는 몫을 다 먹습니다."
                     % mc["avg_R_net"])
    return notes


BLOCK_GAP = 0.05     # 묶음 재생과 원본 방식의 목표·손절 비율이 5%p 넘게 다르면 알린다


def time_machine(chart, plan, now_kst, replays=10000, seed=19, window=2, until=None, cfg=None,
                 pool_n=None, history_n=None, block=6):
    """pool_n: 재생 재료로 쓸 최근 거래일 수 (None = 전부), history_n: 과거 재적용에 쓸 수 (None = 전부)."""
    if not 1 <= replays <= 1000000:
        raise TMError("--replays 는 1 ~ 1,000,000 입니다")
    if not 0 <= window <= 10:
        raise TMError("--window 는 0 ~ 10 봉입니다")
    cfg = cfg or {}
    sym = plan["symbol"]
    if chart.get("symbol") and chart["symbol"] != sym:
        raise TMError("자료는 %s 인데 계획은 %s 입니다" % (chart["symbol"], sym))
    now_ts = krx.ts_of(now_kst)
    chart = dict(chart, bars=[b for b in chart["bars"] if b["t"] + 60 <= now_ts])
    if not chart["bars"]:
        raise TMError("%s 의 완성된 1분봉이 자료에 없습니다" % sym)
    k = plan["entry_bar"]
    until_min = AUCTION_MIN if until is None else parse_clock(until, "--until")
    if (until_min - OPEN_MIN) % 5 or until_min > AUCTION_MIN:
        raise TMError("--until 은 5분 격자이고 15:20 (접속매매 끝) 이전이어야 합니다")
    last = (until_min - OPEN_MIN) // 5 - 1
    if last <= k:
        raise TMError("--until %s 이면 %s 진입 뒤에 돌릴 봉이 없습니다" % (clock(until_min), plan["entry_time"]))
    days = day_slots(chart)
    entry_day = days.get(plan["date"], {})
    if k not in entry_day:
        reason = ("그날 봉이 없음" if not entry_day else "자료가 %s 봉에서 끝남"
                  % krx.hm(entry_day[max(entry_day)]["kst"].time()))
        raise TMError("진입 봉이 자료에 없습니다: %s 의 %s 봉 (%s 에 끝남) (%s)"
                      % (plan["date"], clock(OPEN_MIN + 5 * k), plan["entry_time"], reason))
    eb = entry_day[k]
    warn = []
    tick = krx.tick_size(plan["entry"])
    if not eb["l"] - tick <= plan["entry"] <= eb["h"] + tick:
        warn.append("계획 진입가 %s 가 그 봉의 범위 %s ~ %s 밖입니다" % (p(plan["entry"]), p(eb["l"]), p(eb["h"])))
    all_sessions, short = full_sessions(days, plan["date"])
    sessions = all_sessions[-pool_n:] if pool_n else all_sessions
    hist_sessions = all_sessions[-history_n:] if history_n else all_sessions
    if len(sessions) < MIN_SESSIONS:
        raise TMError("%s 의 %s 전 온전한 거래일이 %d개뿐입니다. 타임머신은 %d개 이상이 필요합니다"
                      % (sym, plan["date"], len(sessions), MIN_SESSIONS))
    limits, lim_src = None, None
    if plan.get("price_limits"):
        limits = (float(plan["price_limits"]["lower"]), float(plan["price_limits"]["upper"]))
        lim_src = plan["price_limits"].get("source") or "계획"
    else:
        pc = prev_close_of(chart, datetime.date.fromisoformat(plan["date"]), krx.load_calendar(data.CALENDAR_PATH))
        if pc:
            limits = tuple(float(x) for x in krx.price_limits(pc))
            lim_src = "전일 종가 %s 기준 추정" % p(pc)
    cr = float(plan.get("commission_rate") if plan.get("commission_rate") is not None else
               cfg.get("commission_rate", krx.DEFAULT_COMMISSION))
    tr = float(plan.get("sell_tax_rate") if plan.get("sell_tax_rate") is not None else
               cfg.get("sell_tax_rate", krx.DEFAULT_SELL_TAX))
    pool = build_pool(sessions)
    mc = simulate(pool, plan["entry"], plan["stop"], plan["target"], k, last, replays, seed, window, limits, cr, tr)
    mc = dict(mc, until=clock(until_min), pool_sessions=len(sessions), pool_from=sessions[0][0],
              pool_to=sessions[-1][0], limits_source=lim_src, commission_rate=cr, sell_tax_rate=tr,
              band_times=[clock(OPEN_MIN + 5 * (j + 1)) for j in range(k, last + 1)])
    blocks = simulate_blocks(sessions, plan["entry"], plan["stop"], plan["target"], k, last, replays, seed, block,
                             limits, cr, tr) if block and block > 1 else None
    rule = resolve_rule(plan, entry_day)
    plan_out = {kk: v for kk, v in plan.items() if v is not None}
    if rule:
        plan_out["stop_rule"] = rule["stop_rule"]
        history = history_replay(hist_sessions, rule, last, cr, tr)
    else:
        history = {"sessions": len(hist_sessions), "triggered": 0, "rule": None, "trades": [],
                   "note": "건너뜀: 과거 재적용은 시가 범위 돌파(orb)만 압니다. 이 계획은 %r 셋업입니다" % plan["setup"]}
    lb = chart["bars"][-1]
    return {
        "created_by": "trader-kr-timemachine", "created_at": now_kst.strftime("%Y-%m-%d %H:%M:%S"),
        "source": chart.get("source"),
        "data": {"symbol": sym, "interval": "5m (1분봉을 묶음)",
                 "last_bar": krx.kst_of(lb["t"] + 60).strftime("%Y-%m-%d %H:%M"), "short_sessions_left_out": short},
        "plan": plan_out, "mc": mc, "blocks": blocks, "history": history, "verdict": verdict(mc),
        "verdict_notes": verdict_notes(mc) + block_notes(mc, blocks) + history_notes(history),
        "warnings": warn,
        "note": ("%s 의 최근 온전한 거래일 %d개로 만든 모의입니다. 예측이 아닙니다: 실적, 공시, 거래정지, VI 는 "
                 "그 거래일들에 없던 일을 만들 수 있습니다. 같은 시드면 같은 숫자입니다." % (sym, len(sessions))),
    }


def block_notes(mc, b):
    if not b:
        return []
    d_t, d_s = b["p_target"] - mc["p_target"], b["p_stop"] - mc["p_stop"]
    if max(abs(d_t), abs(d_s)) > BLOCK_GAP:
        return ["30분 묶음 재생은 목표 먼저 %.1f%%, 손절 먼저 %.1f%% 로 원본 방식과 %.0f%%p 넘게 다릅니다. 봉을 따로 고르는 "
                "가정이 결과를 바꾸니 두 숫자를 같이 보세요." % (100 * b["p_target"], 100 * b["p_stop"], 100 * BLOCK_GAP)]
    return []


def history_notes(h):
    if not h.get("rule") or not h.get("target_share_ci"):
        return []
    lo, hi = h["target_share_ci"]
    be = h["break_even_share"]
    if lo <= be <= hi:
        return ["과거 재적용의 목표 비율 %.0f%% 는 95%% 구간 %.0f~%.0f%% 가 본전 %.0f%% 를 포함합니다. 표본으로는 본전과 "
                "구별되지 않습니다." % (100 * h["target_share"], 100 * lo, 100 * hi, 100 * be)]
    side = "위" if lo > be else "아래"
    return ["과거 재적용의 목표 비율 %.0f%% (95%% 구간 %.0f~%.0f%%) 는 본전 %.0f%% %s 입니다."
            % (100 * h["target_share"], 100 * lo, 100 * hi, 100 * be, side)]


def pc(x):
    return "%.1f%%" % (100.0 * x)


def report(doc):
    pl, m, h = doc["plan"], doc["mc"], doc["history"]
    wd = WEEKDAYS[datetime.date.fromisoformat(pl["date"]).weekday()]
    lines = [
        "%s%s 매수, %s(%s): 진입 %s (%s, %d번째 봉), 손절 %s, 목표 %s (%gR, 주당 위험 %s원)."
        % (pl["symbol"], (" " + pl["name"]) if pl.get("name") else "", pl["date"], wd, p(pl["entry"]),
           pl["entry_time"], pl["entry_bar"], p(pl["stop"]), p(pl["target"]), pl["target_r"], p(pl["risk_per_share"])),
        "남은 장 %s ~ %s (5분봉 %d개)를 %s번 재생, 시드 %d."
        % (pl["entry_time"], m["until"], m["bars_left"], "{:,}".format(m["replays"]), m["seed"]),
        "봉마다 %s 의 지난 온전한 거래일 %d개 (%s ~ %s) 에서 같은 시각(+-%d봉) 5분봉을 통째로 골랐습니다."
        % (pl["symbol"], m["pool_sessions"], m["pool_from"], m["pool_to"], m["window"]),
    ]
    if m.get("limits"):
        lines.append("가격은 하한가 %s ~ 상한가 %s 안으로 잘랐습니다 (%s)." % (p(m["limits"][0]), p(m["limits"][1]),
                                                                   m.get("limits_source")))
    lines += [
        "",
        "  목표 먼저     %s" % pc(m["p_target"]),
        "  손절 먼저     %s" % pc(m["p_stop"]),
        "  둘 다 아님    %-7s %s 까지 열려 있음" % (pc(m["p_neither"]), m["until"]),
        "  수익          %-7s 목표 먼저, 또는 %s 에 진입가 위" % (pc(m["p_green"]), m["until"]),
        "",
        "  %s 가격: 중앙값 %s, 재생의 90%% 가 %s ~ %s (호가 단위로 반올림)." % (
            m["until"], p(krx.round_tick(m["median_close"])), p(krx.round_tick(m["p5_close"])),
            p(krx.round_tick(m["p95_close"]))),
        "  평균 R: %+.3f (R = 주당 위험 %s원. 목표 %+gR, 손절 -1R). 비용 뺀 평균 R: %+.3f (수수료 %s, 매도세 %s)."
        % (m["avg_R"], p(pl["risk_per_share"]), m["r_at_target"], m["avg_R_net"],
           krx.pct_str(m["commission_rate"], 3), krx.pct_str(m["sell_tax_rate"], 2)),
        "",
    ]
    b = doc.get("blocks")
    if b:
        lines += ["30분 묶음 재생 (같은 거래일 봉 %d개씩 이어 붙임, 변동성 군집과 짧은 추세를 남김):" % b["block"],
                  "  목표 먼저 %s, 손절 먼저 %s, 둘 다 아님 %s. 평균 R %+.3f, 비용 뺀 평균 R %+.3f."
                  % (pc(b["p_target"]), pc(b["p_stop"]), pc(b["p_neither"]), b["avg_R"], b["avg_R_net"]), ""]
    if h.get("rule"):
        ru = h["rule"]
        sw = {"or_mid": "시가 범위 중간값", "or_low": "시가 범위 저가"}.get(ru["stop_rule"],
                                                                "진입가 아래 %.2f%%" % ru.get("stop_pct", 0))
        if h["triggered"]:
            lines.append("과거: 같은 규칙 (%d분 시가 범위, 손절 %s, 목표 %gR) 을 그 %d 거래일에 적용하면 %d번 신호: "
                         "목표 %d, 손절 %d, 둘 다 아님 %d. 평균 R %+.3f, 비용 뺀 평균 R %+.3f."
                         % (ru["or_minutes"], sw, ru["target_r"], h["sessions"], h["triggered"], h["target"], h["stop"],
                            h["close"], h["avg_R"], h["avg_R_net"]))
            if h.get("avg_R_ci"):
                lines.append("      평균 R 95%% 구간 %+.3f ~ %+.3f, 비용 뺀 평균 R 95%% 구간 %+.3f ~ %+.3f (부트스트랩)."
                             % (h["avg_R_ci"][0], h["avg_R_ci"][1], h["avg_R_net_ci"][0], h["avg_R_net_ci"][1]))
        else:
            lines.append("과거: 같은 규칙은 그 %d 거래일 동안 한 번도 신호가 나지 않았습니다." % h["sessions"])
    else:
        lines.append("과거: " + h.get("note", "건너뜀."))
    lines += ["", doc["verdict"]] + list(doc.get("verdict_notes") or []) + [""]
    for w in doc.get("warnings") or []:
        lines.append("참고: " + w + ".")
    if doc["data"]["short_sessions_left_out"]:
        lines.append("온전하지 않은 거래일 %d개를 뺐습니다: %s." % (
            len(doc["data"]["short_sessions_left_out"]),
            ", ".join(x["date"] for x in doc["data"]["short_sessions_left_out"][:8]) +
            (" 등" if len(doc["data"]["short_sessions_left_out"]) > 8 else "")))
    lines.append(doc["note"] + " 다시 돌리려면 --seed %d." % m["seed"])
    return lines


def render_chart(doc, height=17):
    pl, m = doc["plan"], doc["mc"]
    bands, times = m["bands"], m["band_times"]
    lo = min([b[0] for b in bands] + [pl["stop"], pl["target"]])
    hi = max([b[4] for b in bands] + [pl["stop"], pl["target"]])
    pad = (hi - lo) * 0.04 or 1
    lo, hi = lo - pad, hi + pad
    step = (hi - lo) / (height - 1)

    def row(price):
        return max(0, min(height - 1, int(round((hi - price) / step))))
    grid = [[" "] * len(bands) for _ in range(height)]
    for j, b in enumerate(bands):
        r5, r25, r50, r75, r95 = [row(x) for x in b]
        for r in range(r95, r5 + 1):
            grid[r][j] = "."
        for r in range(r75, r25 + 1):
            grid[r][j] = ":"
        grid[r50][j] = "*"
    marks = {row(pl["target"]): "target", row(pl["stop"]): "stop"}
    if row(pl["entry"]) not in marks:
        marks[row(pl["entry"])] = "entry"
    for r in (row(pl["target"]), row(pl["stop"])):
        grid[r] = ["-" if ch == " " else ch for ch in grid[r]]
    out = ["%s %s, %s 진입부터 재생 경로 %s개 (시드 %d)" % (pl["symbol"], pl["date"], pl["entry_time"],
                                                    "{:,}".format(m.get("band_paths", BAND_PATHS)), m["seed"])]
    for r in range(height):
        label = marks.get(r)
        price = {"target": pl["target"], "stop": pl["stop"], "entry": pl["entry"]}.get(label, hi - r * step)
        left = ("%9s |" % p(round(price))) if (label or r in (0, height - 1)) else "          |"
        out.append(left + "".join(grid[r]) + ("  " + label if label else ""))
    axis = [" "] * len(bands)
    for j in sorted({0, len(bands) // 2, len(bands) - 1}):
        lab = times[j]
        s = max(0, min(j, len(bands) - len(lab)))
        axis[s:s + len(lab)] = list(lab)
    out.append("          +" + "-" * len(bands))
    out.append("           " + "".join(axis))
    out.append("* 중앙값   : 가운데 절반   . 경로 90%   - 손절가와 목표가")
    return out


def compute(a):
    now = krx.parse_now(a.now)
    plan = load_plan(a, now)
    cfg = krx.read_config(a.config)
    day = datetime.date.fromisoformat(plan["date"])
    if a.sessions < MIN_SESSIONS:
        raise TMError("--sessions 는 %d 이상이어야 합니다" % MIN_SESSIONS)
    if a.history_sessions < 1:
        raise TMError("--history-sessions 는 1 이상입니다")
    need = max(a.sessions, a.history_sessions)
    start = datetime.datetime.combine(day - datetime.timedelta(days=int(need * 1.55) + 12), datetime.time(0, 0))
    end = min(now, datetime.datetime.combine(day, datetime.time(15, 31)))
    progress = None if a.json else (lambda msg: print(msg, file=sys.stderr))
    if progress and not a.offline:
        progress("토스증권 1분봉을 받습니다: %s, %s ~ %s (캐시에 있는 구간은 건너뜀)..." % (
            plan["symbol"], start.date().isoformat(), plan["date"]))
    try:
        chart = data.load_chart(plan["symbol"], start, end, offline=a.offline, now_kst=now, progress=progress)
    except data.DataError as e:
        raise TMError(str(e))
    full = full_sessions(day_slots(chart), plan["date"])[0]
    if len(full) > need:
        # 요청보다 많이 받았으면 필요한 만큼만 남긴다
        cut = full[-need][0]
        lo = krx.ts_of(datetime.datetime.combine(datetime.date.fromisoformat(cut), datetime.time(0, 0)))
        chart = dict(chart, bars=[b for b in chart["bars"] if b["t"] >= lo])
    doc = time_machine(chart, plan, now, a.replays, a.seed, a.window, a.until, cfg,
                       pool_n=a.sessions, history_n=a.history_sessions, block=a.block)
    if not a.offline:
        rows, note = dart.check(plan["symbol"], day, now_kst=now)
        if note:
            doc["warnings"].append(note)
        doc["disclosures"] = rows
    return doc


def cmd_run(a):
    doc = compute(a)
    if a.write:
        krx.write_json(a.write, doc, indent=1)
    if a.json:
        slim = dict(doc, mc={k: v for k, v in doc["mc"].items() if k != "sample_paths"})
        print(json.dumps(slim, ensure_ascii=False, indent=2))
    else:
        print("\n".join(report(doc)))
        if a.write:
            print("%s 에 썼습니다." % a.write)
        print("다음: /trader-kr-budget 으로 예산과 거래당 최대 손실에 맞춰 수량을 정합니다.")
    return 0


def cmd_chart(a):
    if a.from_file:
        try:
            doc = krx.read_json(a.from_file, "타임머신 결과 (timemachine.py run --write)")
            doc["mc"]["bands"]
        except (KeyError, TypeError):
            raise TMError("%s 는 타임머신 결과가 아닙니다" % a.from_file)
    else:
        doc = compute(a)
    print("\n".join(render_chart(doc)))
    print("\n".join([doc.get("verdict", "")] + list(doc.get("verdict_notes") or [])))
    return 0


def main(argv=None):
    krx.utf8_stdio()
    ap = argparse.ArgumentParser(description="타임머신: 계획한 단타를 다시 돌려 본다 (trader-kr-timemachine).")
    sub = ap.add_subparsers(dest="cmd")

    def common(q):
        q.add_argument("--plan", default="trader-kr/plan.json")
        q.add_argument("--symbol", help="또는 계획을 옵션으로: 종목코드")
        q.add_argument("--date", help="거래일 YYYY-MM-DD (기본 오늘)")
        q.add_argument("--entry", type=float)
        q.add_argument("--stop", type=float)
        q.add_argument("--target", type=float)
        q.add_argument("--entry-time", help="진입 봉이 끝난 시각, 예: 10:15")
        q.add_argument("--setup", default="orb")
        q.add_argument("--or-minutes", type=int, default=15)
        q.add_argument("--stop-rule", default="auto", choices=["auto", "or_mid", "or_low", "pct"])
        q.add_argument("--replays", type=int, default=10000)
        q.add_argument("--seed", type=int, default=19)
        q.add_argument("--window", type=int, default=2, help="같은 시각 +- 이 봉 수 안에서 고름")
        q.add_argument("--sessions", type=int, default=60, help="재생 재료로 쓸 지난 거래일 수 (기본 60)")
        q.add_argument("--history-sessions", type=int, default=120,
                       help="과거 재적용에 쓸 지난 거래일 수 (기본 120, 국내 1분봉은 2022-11-23 부터)")
        q.add_argument("--block", type=int, default=6, help="묶음 재생의 봉 수 (기본 6 = 30분, 0 이면 끔)")
        q.add_argument("--until", help="재생을 끝낼 시각 (기본 15:20, 접속매매 끝)")
        q.add_argument("--config", default="trader-kr/config.json")
        q.add_argument("--offline", metavar="FILE", help="저장된 1분봉 파일, 네트워크 없이")
        q.add_argument("--now", help='이 한국 시간인 것처럼, 예: "2026-10-28 10:15"')

    q = sub.add_parser("run", help="계획 재생: 10,000번 모의 + 과거 재적용")
    common(q)
    q.add_argument("--json", action="store_true")
    q.add_argument("--write", nargs="?", const="trader-kr/timemachine.json", metavar="PATH")
    q = sub.add_parser("chart", help="재생 경로 부채꼴 그림 (ASCII)")
    common(q)
    q.add_argument("--from", dest="from_file", nargs="?", const="trader-kr/timemachine.json", metavar="PATH")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"run": cmd_run, "chart": cmd_chart}[a.cmd](a)
    except (TMError, krx.KrxError, data.DataError) as e:
        print("timemachine.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
