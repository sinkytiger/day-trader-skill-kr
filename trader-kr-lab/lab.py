#!/usr/bin/env python3
"""전략 검증 실험실 (trader-kr-lab): 여러 단타 규칙을 앞 기간에서 고르고 뒤 기간에서 확인한다.
표준 라이브러리만 쓴다. 주문을 내지 않는다.

    python lab.py run --symbols 005930,000660,035420              # 250 거래일, 학습 120일 -> 시험 20일
    python lab.py run --top 10 --sessions 250 --write             # KRX 거래대금 상위 10 보통주
    python lab.py run --symbols 005930 --train 60 --test 20 --slip-ticks 2 --json
    python lab.py forward lock --rule "ORB30 저가손절 2R 거래량1.5x" --symbols 005930,000660 --name orb30
    python lab.py forward update --name orb30                    # 잠근 날 뒤 거래일에만 적용 (매주)

규칙 (기본 48가지, 시가 범위 돌파의 변형):
  시가 범위      15분, 30분
  손절           범위 중간값, 범위 저가 (호가 단위로 내림)
  목표           1R, 1.5R, 2R (호가 단위로 반올림)
  거래량 조건    없음, 신호 봉 거래량이 범위 봉 평균의 1.5배 이상
  비용 필터      없음, 손절 때 비용(수수료+매도세)이 주당 위험의 30% 를 넘으면 건너뜀
진입은 09:15 ~ 14:50 에 끝난 첫 돌파 봉 종가를 매수 호가로 올린 가격, 청산은 손절 먼저 그다음 목표,
둘 다 아니면 15:15 봉 종가. 손절과 시간 청산은 --slip-ticks 칸(기본 1) 아래에서, 봉 시가가 이미
손절가 아래면 그 시가에서 체결된다고 친다 (갭). 목표는 지정가라 목표가에 체결된다고 친다.
비용은 설정의 수수료와 매도 거래세. R = (청산가 - 진입가) / (진입가 - 손절가).

검증:
  1. 전 기간 (고르기 전, 참고): 규칙마다 거래 수, 비용 전·후 평균 R, 승률, 평균 R 이 0보다 큰지의
     한쪽 검정 p 값과 홀름 보정 p 값. 많이 시험할수록 우연히 좋아 보이는 규칙이 섞이는 것을 보정한다.
  2. 워크포워드: 앞 --train 거래일에서 비용 뺀 평균 R 이 가장 큰 규칙(거래 --min-trades 건 이상)을
     고르고, 바로 뒤 --test 거래일에 그 규칙만 적용한다. --test 만큼 밀며 되풀이한다. 시험 기간
     거래만 모은 평균 R 과 95% 부트스트랩 구간이 이 실험실의 답이다.
  3. 비용: 비용 전·후 평균 R, 비용이 비용 전 이익에서 가져간 몫, 비용 필터를 켠 규칙과 끈 규칙 비교.
  4. 미래 검증 (forward): 규칙 하나와 종목·비용을 잠그고, 잠근 날 뒤의 거래일에만 같은 체결 계산을
     적용한다. 판정은 처음 --judge-trades 건(기본 200)으로 한 번만: 그 비용 뺀 평균 R 의 95% 구간
     아래쪽이 0보다 크면 넘음. 잠근 파일이나 체결 계산(run_rule)이 바뀌면 update 를 멈춘다.

자료: 토스증권 1분봉 (2022-11-23 이후, trader-kr/cache 캐시)을 5분봉으로 묶은 온전한 거래일.
처음 받을 때는 종목당 몇 분 걸린다. 과거 성적은 미래를 약속하지 않는다.
"""

import argparse
import datetime
import hashlib
import inspect
import json
import math
import os
import random
import re
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
from krcommon import bars as kbars, data, krx, krxdata, toss  # noqa: E402

STEP = 300
SESSION_BARS = 76
ENTRY_FIRST_END = 15          # 09:15 (분, 개장 기준) 이후에 끝난 봉
ENTRY_LAST_SLOT = 69          # 14:50 에 끝나는 봉
FLAT_SLOT = 74                # 15:15 에 끝나는 봉
NOTE = "과거 성적은 미래를 약속하지 않습니다. 이 실험실의 답은 고른 뒤의 시험 기간 성적입니다."


class LabError(Exception):
    pass


def p(x):
    return krx.price_str(x)


# ---------------------------------------------------------------- 규칙

def default_grid(stop_pct=None):
    """기본 48개. stop_pct 를 주면 진입가 아래 X% 손절 규칙 32개 (목표 1R, 1.5R, 2R, 목표 없음) 를 더한다."""
    out = []
    for orm in (15, 30):
        for stop in ("or_mid", "or_low"):
            for r in (1.0, 1.5, 2.0):
                for vx in (None, 1.5):
                    for cf in (False, True):
                        out.append({"or_minutes": orm, "stop": stop, "target_r": r, "vol_x": vx, "cost_filter": cf})
    if stop_pct:
        for orm in (15, 30):
            for r in (1.0, 1.5, 2.0, None):
                for vx in (None, 1.5):
                    for cf in (False, True):
                        out.append({"or_minutes": orm, "stop": "pct", "pct": float(stop_pct), "target_r": r, "vol_x": vx,
                                    "cost_filter": cf})
    return out


def name(v):
    stop = "%g%%손절" % v["pct"] if v["stop"] == "pct" else {"or_mid": "중간손절", "or_low": "저가손절"}[v["stop"]]
    target = "%gR" % v["target_r"] if v["target_r"] else "목표없음"
    return "ORB%d %s %s%s%s" % (v["or_minutes"], stop, target, " 거래량%gx" % v["vol_x"] if v["vol_x"] else "",
                                " 비용필터" if v["cost_filter"] else "")


def run_rule(Dd, v, cost):
    """하루(5분봉 76개)에 규칙 하나. 거래가 없으면 None."""
    n = v["or_minutes"] // 5
    rng = Dd[:n]
    oh, ol = max(b["h"] for b in rng), min(b["l"] for b in rng)
    avg_v = sum(b["v"] for b in rng) / float(n)
    first = max(n, ENTRY_FIRST_END // 5 - 1)
    k = None
    for i in range(first, ENTRY_LAST_SLOT + 1):
        b = Dd[i]
        if b["c"] > oh and (not v["vol_x"] or (avg_v and b["v"] >= v["vol_x"] * avg_v)):
            k = i
            break
    if k is None:
        return None
    e = krx.ceil_tick(Dd[k]["c"])
    if v["stop"] == "pct":
        s = krx.floor_tick(e * (1 - v["pct"] / 100.0))
    else:
        s = krx.floor_tick((oh + ol) / 2.0 if v["stop"] == "or_mid" else ol)
    risk = e - s
    if risk < max(2 * krx.tick_size(s), e * 0.0005):
        return None
    cr, tr, slip = cost["commission_rate"], cost["sell_tax_rate"], cost["slip_ticks"]
    cost_stop = e * cr + s * (cr + tr)
    if v["cost_filter"] and cost_stop > cost["max_cost_to_risk"] * risk:
        return None
    t = krx.round_tick(e + v["target_r"] * risk) if v["target_r"] else None      # 목표 없음: 손절 아니면 청산 시각
    exitp, outcome = None, None
    for b in Dd[k + 1:FLAT_SLOT + 1]:
        if b["o"] <= s:
            exitp, outcome = krx.floor_tick(b["o"]) - slip * krx.tick_size(b["o"]), "gap"
            break
        if b["l"] <= s:
            exitp, outcome = s - slip * krx.tick_size(s), "stop"
            break
        if t is not None and b["h"] >= t:
            exitp, outcome = t, "target"
            break
    if exitp is None:
        c = Dd[FLAT_SLOT]["c"]
        exitp, outcome = krx.floor_tick(c) - slip * krx.tick_size(c), "flat"
    gross = exitp - e
    net = gross - (e * cr + exitp * (cr + tr))
    return {"entry": e, "stop": s, "target": t, "exit": exitp, "outcome": outcome, "R": gross / risk,
            "R_net": net / risk, "slot": k}


# ---------------------------------------------------------------- 자료

def full_sessions(chart):
    """{날짜: 5분봉 76개} (온전한 거래일만)."""
    out = {}
    for day, bs in kbars.split_days(chart["bars"]).items():
        slots = [b for b in kbars.session_slots(bs, krx.Session(day), STEP) if b["complete"]]
        if len(slots) == SESSION_BARS and slots[0]["v"] > 0 and all(b["slot"] == i for i, b in enumerate(slots)):
            out[day.isoformat()] = slots
    return out


def load(symbols, end, sessions, offline=None, progress=None):
    start = datetime.datetime.combine(end - datetime.timedelta(days=int(sessions * 1.55) + 12), datetime.time(0, 0))
    stop = datetime.datetime.combine(end, datetime.time(15, 31))
    out = {}
    for i, sym in enumerate(symbols, 1):
        if progress and not offline:
            progress("  %d / %d: %s 1분봉 (캐시에 있는 구간은 건너뜀)..." % (i, len(symbols), sym))
        try:
            ch = data.load_chart(sym, start, stop, offline=offline, progress=progress)
        except data.DataError as e:
            if progress:
                progress("  %s 건너뜀: %s" % (sym, e))
            continue
        days = full_sessions(ch)
        keep = sorted(days)[-sessions:]
        out[sym] = {d: days[d] for d in keep}
    return out


# ---------------------------------------------------------------- 통계

def mean(xs):
    return statistics.mean(xs) if xs else None


def one_sided_p(xs):
    """평균 > 0 의 한쪽 검정 p (정규 근사). 거래가 3건 미만이면 1."""
    if len(xs) < 3:
        return 1.0
    sd = statistics.stdev(xs)
    if sd == 0:
        return 0.0 if statistics.mean(xs) > 0 else 1.0
    z = statistics.mean(xs) / (sd / math.sqrt(len(xs)))
    return 0.5 * math.erfc(z / math.sqrt(2))


def holm(ps):
    """홀름 보정 p 값 (입력 순서 그대로)."""
    m = len(ps)
    order = sorted(range(m), key=lambda i: ps[i])
    adj, run = [0.0] * m, 0.0
    for rank, i in enumerate(order):
        run = max(run, min(1.0, (m - rank) * ps[i]))
        adj[i] = run
    return adj


def bootstrap(xs, seed=19, n=2000):
    if len(xs) < 2:
        return None
    rng = random.Random(seed)
    m = len(xs)
    means = sorted(sum(xs[rng.randrange(m)] for _ in range(m)) / float(m) for _ in range(n))
    return round(means[int(0.025 * n)], 3), round(means[min(n - 1, int(0.975 * n))], 3)


# ---------------------------------------------------------------- 실험

def experiment(sessions_by_symbol, grid, cost, train=120, test=20, min_trades=30, seed=19):
    dates = sorted({d for days in sessions_by_symbol.values() for d in days})
    if len(dates) < train + test:
        raise LabError("온전한 거래일이 %d일뿐입니다. --train %d + --test %d 보다 많아야 합니다" % (len(dates), train, test))
    trades = []
    for vi, v in enumerate(grid):
        rows = []
        for sym, days in sessions_by_symbol.items():
            for d, Dd in days.items():
                t = run_rule(Dd, v, cost)
                if t:
                    rows.append(dict(t, date=d, symbol=sym))
        trades.append(rows)
    # 1. 전 기간
    table = []
    for vi, v in enumerate(grid):
        r = trades[vi]
        nets = [x["R_net"] for x in r]
        table.append({"rule": name(v), "variant": v, "trades": len(r), "avg_R": round(mean([x["R"] for x in r]), 3)
                      if r else None, "avg_R_net": round(mean(nets), 3) if r else None,
                      "win_rate": round(sum(1 for x in nets if x > 0) / float(len(nets)), 3) if r else None,
                      "p": round(one_sided_p(nets), 4)})
    adj = holm([row["p"] for row in table])
    for row, a in zip(table, adj):
        row["p_holm"] = round(a, 4)
    # 2. 워크포워드
    folds, oos = [], []
    i = train
    while i + 1 <= len(dates):
        tr_dates, te_dates = set(dates[i - train:i]), set(dates[i:i + test])
        if not te_dates:
            break
        best, best_m = None, None
        for vi in range(len(grid)):
            tr = [x["R_net"] for x in trades[vi] if x["date"] in tr_dates]
            if len(tr) >= min_trades:
                m = mean(tr)
                if best_m is None or m > best_m:
                    best, best_m = vi, m
        if best is None:
            folds.append({"train": [min(tr_dates), max(tr_dates)], "test": [min(te_dates), max(te_dates)], "rule": None,
                          "train_avg_R_net": None, "test_trades": 0, "test_avg_R_net": None})
        else:
            te = [x for x in trades[best] if x["date"] in te_dates]
            oos.extend(te)
            folds.append({"train": [min(tr_dates), max(tr_dates)], "test": [min(te_dates), max(te_dates)],
                          "rule": name(grid[best]), "train_avg_R_net": round(best_m, 3), "test_trades": len(te),
                          "test_avg_R_net": round(mean([x["R_net"] for x in te]), 3) if te else None})
        i += test
    oos_nets = [x["R_net"] for x in oos]
    chosen = [f["train_avg_R_net"] for f in folds if f["train_avg_R_net"] is not None]
    # 3. 비용
    all_t = [x for r in trades for x in r]
    gross_profit = sum(x["R"] for x in all_t if x["R"] > 0)
    filt_on = [x["R_net"] for vi, v in enumerate(grid) if v["cost_filter"] for x in trades[vi]]
    filt_off = [x["R_net"] for vi, v in enumerate(grid) if not v["cost_filter"] for x in trades[vi]]
    costs = {"avg_R": round(mean([x["R"] for x in all_t]), 3) if all_t else None,
             "avg_R_net": round(mean([x["R_net"] for x in all_t]), 3) if all_t else None,
             "cost_share_of_gross": round(sum(x["R"] - x["R_net"] for x in all_t) / gross_profit, 3)
             if gross_profit > 0 else None,
             "filter_on_avg_R_net": round(mean(filt_on), 3) if filt_on else None, "filter_on_trades": len(filt_on),
             "filter_off_avg_R_net": round(mean(filt_off), 3) if filt_off else None, "filter_off_trades": len(filt_off)}
    return {"dates": [dates[0], dates[-1]], "sessions": len(dates), "symbols": sorted(sessions_by_symbol),
            "rules": len(grid), "table": table, "folds": folds,
            "oos": {"trades": len(oos), "avg_R_net": round(mean(oos_nets), 3) if oos else None,
                    "ci": bootstrap(oos_nets, seed), "win_rate": round(sum(1 for x in oos_nets if x > 0) / float(len(oos)), 3)
                    if oos else None,
                    "chosen_train_avg_R_net": round(mean(chosen), 3) if chosen else None},
            "costs": costs, "settings": {"train": train, "test": test, "min_trades": min_trades, "cost": cost}}


def verdict(res):
    o = res["oos"]
    pos = sum(1 for r in res["table"] if (r["avg_R_net"] or 0) > 0)
    sig = sum(1 for r in res["table"] if r["p_holm"] < 0.05)
    if not o["trades"]:
        return "시험 기간 거래가 없습니다. 거래일이나 종목을 늘리세요."
    lo, hi = o["ci"] if o["ci"] else (None, None)
    head = "고른 규칙의 시험 기간 성적: 거래 %d건, 비용 뺀 평균 R %+.3f" % (o["trades"], o["avg_R_net"])
    if o["ci"]:
        head += " (95%% 구간 %+.3f ~ %+.3f)" % (lo, hi)
    if hi is not None and hi < 0:
        tail = "비용 뺀 기대값이 음수입니다. 이 규칙들로는 남는 게 없습니다."
    elif lo is not None and lo > 0 and sig:
        tail = "시험 기간에도 0보다 컸고 보정 뒤에도 남는 규칙이 있습니다. 그래도 한 번의 실험이니 기간과 종목을 바꿔 다시 확인하세요."
    elif lo is not None and lo > 0:
        tail = "시험 기간에는 0보다 컸지만 전 기간 보정 검정을 통과한 규칙은 없습니다. 우연일 수 있습니다."
    else:
        tail = "0과 구별되지 않습니다. 남는다고 말할 근거가 없습니다."
    extra = " 전 기간 %d개 규칙 중 비용 뺀 평균 R 이 양수인 것 %d개, 홀름 보정 뒤 유의한 것 %d개." % (res["rules"], pos, sig)
    return head + ". " + tail + extra


def report(res):
    st = res["settings"]
    c = st["cost"]
    L = ["전략 검증 실험실: 시가 범위 돌파 %d가지 규칙, 종목 %d개 (%s), 온전한 거래일 %d일 (%s ~ %s)." % (
        res["rules"], len(res["symbols"]), ", ".join(res["symbols"][:8]) + (" 등" if len(res["symbols"]) > 8 else ""),
        res["sessions"], res["dates"][0], res["dates"][1]), cost_line(c), ""]
    top = sorted(res["table"], key=lambda r: -(r["avg_R_net"] if r["avg_R_net"] is not None else -9))[:8]
    L.append("1. 전 기간 (고르기 전, 참고). 비용 뺀 평균 R 상위 8개:")
    L.append("   %s %6s %9s %9s %6s %8s" % (krx.pad("규칙", 36), "거래", "비용 전 R", "비용 뒤 R", "승률", "보정 p"))
    for r in top:
        L.append("   %s %6d %+9.3f %+9.3f %5.0f%% %8.3f" % (
            krx.pad(r["rule"], 36), r["trades"], r["avg_R"] or 0, r["avg_R_net"] or 0, 100 * (r["win_rate"] or 0), r["p_holm"]))
    L.append("")
    L.append("2. 워크포워드 (앞 %d 거래일에서 고르고 뒤 %d 거래일에서 확인, %d번):" % (st["train"], st["test"], len(res["folds"])))
    for f in res["folds"]:
        if f["rule"]:
            L.append("   %s~%s 고름: %s 학습 %+.3f -> 시험 %s (%d건)" % (
                f["test"][0], f["test"][1], krx.pad(f["rule"], 36), f["train_avg_R_net"],
                ("%+.3f" % f["test_avg_R_net"]) if f["test_avg_R_net"] is not None else "-", f["test_trades"]))
        else:
            L.append("   %s~%s 고를 규칙 없음 (학습 기간 거래 %d건 미만)" % (f["test"][0], f["test"][1], st["min_trades"]))
    o = res["oos"]
    if o["chosen_train_avg_R_net"] is not None and o["avg_R_net"] is not None:
        L.append("   고를 때 평균 R %+.3f 이던 규칙들이 시험 기간에는 %+.3f. 이 차이가 고르는 데서 생긴 착시입니다."
                 % (o["chosen_train_avg_R_net"], o["avg_R_net"]))
    L.append("")
    co = res["costs"]
    L.append("3. 비용: 전 거래 평균 R 비용 전 %+.3f, 비용 뒤 %+.3f%s." % (
        co["avg_R"] or 0, co["avg_R_net"] or 0,
        (", 비용이 비용 전 이익의 %.0f%% 를 가져감" % (100 * co["cost_share_of_gross"])) if co["cost_share_of_gross"] else ""))
    if co["filter_on_trades"] and co["filter_off_trades"]:
        L.append("   비용 필터를 켠 규칙 %+.3f (%d건) / 끈 규칙 %+.3f (%d건)." % (
            co["filter_on_avg_R_net"], co["filter_on_trades"], co["filter_off_avg_R_net"], co["filter_off_trades"]))
    L += ["", verdict(res), NOTE]
    return L


# ---------------------------------------------------------------- 미래 검증 (forward)

FORWARD_DIR = os.path.join("trader-kr", "forward")
JUDGE_TRADES = 200
LOCK_KEYS = ("rule", "variant", "symbols", "cost", "after", "judge_trades", "engine")
LOCK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
OUTCOME = {"stop": "손절", "gap": "갭 손절", "target": "목표", "flat": "시간 청산"}
FORWARD_NOTE = "잠근 뒤의 성적도 한 구간의 과거입니다. 미래를 약속하지 않습니다."


def cost_of(cfg, slip_ticks):
    return {"commission_rate": float(cfg.get("commission_rate") or 0), "sell_tax_rate": float(cfg.get("sell_tax_rate") or 0),
            "slip_ticks": slip_ticks, "max_cost_to_risk": float(cfg.get("max_cost_to_risk") or 0.3)}


def engine_print():
    """체결 계산(run_rule) 원본의 지문. 잠근 뒤 계산이 바뀌면 같은 규칙이라도 결과가 달라진다."""
    src = "\n".join(line.rstrip() for line in inspect.getsource(run_rule).splitlines()).strip()
    return hashlib.sha256(src.encode("utf-8")).hexdigest()[:16]


def lock_hash(doc):
    body = json.dumps({k: doc[k] for k in LOCK_KEYS}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def find_rule(rule):
    """이름이 정확히 같은 규칙. 'ORB30 5%손절 ...' 처럼 X% 손절이면 그 X 로 규칙을 만든다."""
    m = re.search(r" ([0-9]+(?:\.[0-9]+)?)%손절", rule or "")
    for v in default_grid(float(m.group(1)) if m else None):
        if name(v) == rule:
            return v
    raise LabError('규칙 %r 이 없습니다. lab.py run 표의 이름 그대로 주세요 (예: "ORB30 저가손절 2R 거래량1.5x")' % rule)


def next_weekday(day):
    d = day + datetime.timedelta(days=1)
    while d.weekday() >= 5:
        d += datetime.timedelta(days=1)
    return d


def forward_paths(dirname, nm):
    return os.path.join(dirname, nm + ".lock.json"), os.path.join(dirname, nm + ".json")


def forward_lock(rule, symbols, cost, now, nm, dirname=FORWARD_DIR, judge=JUDGE_TRADES):
    """규칙 하나와 종목·비용을 잠근다. 같은 이름이 있으면 덮어쓰지 않는다."""
    if not LOCK_NAME.match(nm or ""):
        raise LabError("--name 은 영문·숫자·.-_ 64자까지입니다 (받은 값 %r)" % nm)
    if not symbols:
        raise LabError("--symbols 를 주세요")
    if judge < 30:
        raise LabError("--judge-trades 는 30 이상입니다")
    v = find_rule(rule)
    path = forward_paths(dirname, nm)[0]
    doc = {"name": nm, "rule": name(v), "variant": v, "symbols": sorted(set(symbols)), "cost": cost,
           "locked_at": now.strftime("%Y-%m-%d %H:%M"), "after": now.date().isoformat(),
           "first_session": next_weekday(now.date()).isoformat(), "judge_trades": int(judge), "engine": engine_print()}
    doc["hash"] = lock_hash(doc)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    try:
        with open(path, "x", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    except FileExistsError:
        raise LabError("%s 가 이미 있습니다. 잠근 규칙은 바꾸지 않습니다. 다른 규칙은 새 --name 으로 잠그세요" % path)
    return doc, path


def read_lock(path):
    """잠근 파일을 읽고 지문, 규칙 정의, 체결 계산이 잠근 때와 같은지 본다. 다르면 멈춘다."""
    doc = krx.read_json(path, "잠근 규칙 파일")
    if not isinstance(doc, dict) or any(k not in doc for k in LOCK_KEYS + ("name", "hash", "locked_at", "first_session")):
        raise LabError("%s 는 잠근 규칙 파일 형식이 아닙니다" % path)
    if lock_hash(doc) != doc["hash"]:
        raise LabError("%s 가 잠근 뒤 바뀌었습니다 (지문이 다릅니다). 규칙·종목·비용을 바꾸려면 새 --name 으로 다시 잠그세요" % path)
    try:
        same = find_rule(doc["rule"]) == doc["variant"]
    except LabError:
        same = False
    if not same:
        raise LabError("잠근 규칙 %r 의 정의가 지금 실험실의 규칙과 다릅니다. 새 --name 으로 다시 잠그세요" % doc["rule"])
    if doc["engine"] != engine_print():
        raise LabError("체결 계산(run_rule)이 잠근 뒤 바뀌었습니다. 같은 규칙이라도 결과가 달라지므로 새 --name 으로 다시 잠그세요")
    return doc


def pick_lock(dirname, nm):
    if nm:
        return nm
    names = sorted(f[:-len(".lock.json")] for f in os.listdir(dirname) if f.endswith(".lock.json")) \
        if os.path.isdir(dirname) else []
    if len(names) == 1:
        return names[0]
    if not names:
        raise LabError("잠근 규칙이 없습니다. 먼저 lab.py forward lock 을 돌리세요")
    raise LabError("잠근 규칙이 여럿입니다 (%s). --name 으로 고르세요" % ", ".join(names))


def forward_load(symbols, after, end, offline=None, progress=None):
    """잠근 날 뒤 ~ end 의 온전한 거래일. 못 읽은 종목은 건너뛰지 않고 errors 로 돌려준다."""
    start = datetime.datetime.combine(after + datetime.timedelta(days=1), datetime.time(0, 0))
    stop = datetime.datetime.combine(end, datetime.time(15, 31))
    lo, hi = after.isoformat(), end.isoformat()
    by, errors = {}, {}
    for i, sym in enumerate(symbols, 1):
        if progress and not offline:
            progress("  %d / %d: %s 1분봉 (캐시에 있는 구간은 건너뜀)..." % (i, len(symbols), sym))
        try:
            ch = data.load_chart(sym, start, stop, offline=offline, progress=progress)
        except data.DataError as e:
            errors[sym] = str(e)
            continue
        by[sym] = {d: Dd for d, Dd in full_sessions(ch).items() if lo < d <= hi}
    return by, errors


def forward_trades(by, v, cost, after):
    """잠근 날 뒤 거래일에만 run_rule 을 그대로 적용. 시간 순 (날짜, 신호 봉, 종목)."""
    rows = []
    for sym in sorted(by):
        for d in sorted(by[sym]):
            if d <= after:
                continue                    # 잠근 날과 그 앞은 쓰지 않는다
            t = run_rule(by[sym][d], v, cost)
            if t:
                rows.append(dict(t, date=d, symbol=sym))
    rows.sort(key=lambda x: (x["date"], x["slot"], x["symbol"]))
    return rows


def forward_stats(trades, judge):
    def part(rows):
        nets = [x["R_net"] for x in rows]
        return {"trades": len(rows), "avg_R": round(mean([x["R"] for x in rows]), 3) if rows else None,
                "avg_R_net": round(mean(nets), 3) if rows else None, "ci": bootstrap(nets),
                "win_rate": round(sum(1 for x in nets if x > 0) / float(len(nets)), 3) if rows else None}
    out = part(trades)
    out["judged"] = dict(part(trades[:judge]), through=trades[judge - 1]["date"]) if len(trades) >= judge else None
    return out


def forward_verdict(st, judge):
    """('PASS' | 'FAIL' | 'WAIT', 설명). 판정은 처음 judge 건으로 한 번만 한다 (좋을 때 멈추는 착시를 막으려고)."""
    j = st["judged"]
    if j is None:
        return "WAIT", ("아직 (표본 부족): 거래 %d건 / 판정에 필요한 %d건. 이 건수 전에는 남는다고도 안 남는다고도 "
                        "말할 수 없습니다." % (st["trades"], judge))
    lo, hi = j["ci"]
    head = "처음 %d건 (%s 까지) 비용 뺀 평균 R %+.3f, 95%% 구간 %+.3f ~ %+.3f" % (j["trades"], j["through"], j["avg_R_net"], lo, hi)
    if lo > 0:
        return "PASS", "넘음: %s. 구간 아래쪽이 0보다 큽니다. 그래도 한 구간의 결과입니다." % head
    return "FAIL", "넘지 못함: %s. 잠글 때 정한 조건을 넘지 못했습니다. 이 규칙이 남는다고 말할 근거가 없습니다." % head


def forward_update(lock, now, dirname=FORWARD_DIR, offline=None, progress=None):
    after = krx.parse_day(lock["after"])
    if after is None:
        raise LabError("잠근 파일의 날짜 %r 를 읽지 못했습니다" % lock["after"])
    end = now.date() if now.time() >= datetime.time(15, 31) else now.date() - datetime.timedelta(days=1)
    syms = lock["symbols"]
    by, counts = {}, {s: 0 for s in syms}
    if end > after:
        by, errors = forward_load(syms, after, end, offline, progress)
        if errors:
            raise LabError("자료를 읽지 못한 종목이 있어 멈춥니다 (빠진 채로 성적을 내지 않습니다): %s" % "; ".join(
                "%s: %s" % (s, errors[s]) for s in sorted(errors)))
        counts = {s: len(by.get(s) or {}) for s in syms}
        empty = [s for s in syms if not counts[s]]
        if empty and max(counts.values()) > 0:
            raise LabError("잠근 뒤 거래일이 0일인 종목이 있어 멈춥니다: %s (다른 종목은 최대 %d일). 자료를 확인하세요" % (
                ", ".join(empty), max(counts.values())))
    trades = forward_trades(by, lock["variant"], lock["cost"], lock["after"])
    st = forward_stats(trades, lock["judge_trades"])
    code, text = forward_verdict(st, lock["judge_trades"])
    dates = sorted({d for days in by.values() for d in days})
    res = {"name": lock["name"], "rule": lock["rule"], "symbols": syms, "after": lock["after"],
           "locked_at": lock["locked_at"], "first_session": lock["first_session"], "hash": lock["hash"],
           "updated_at": now.strftime("%Y-%m-%d %H:%M"), "end": end.isoformat(), "sessions": counts,
           "dates": [dates[0], dates[-1]] if dates else None, "cost": lock["cost"], "judge_trades": lock["judge_trades"],
           "trades": [dict(x, R=round(x["R"], 6), R_net=round(x["R_net"], 6)) for x in trades],
           "stats": st, "verdict": code, "verdict_text": text}
    krx.write_json(forward_paths(dirname, lock["name"])[1], res)
    return res


def cost_line(c):
    return "비용: 수수료 %s, 매도세 %s, 손절·시간 청산 %d칸 밀림, 갭은 시가 체결." % (
        krx.pct_str(c["commission_rate"], 3), krx.pct_str(c["sell_tax_rate"], 2), c["slip_ticks"])


def lock_report(doc, path):
    return ["잠갔습니다: %s" % path,
            "  규칙: %s" % doc["rule"],
            "  종목 %d개: %s" % (len(doc["symbols"]), ", ".join(doc["symbols"])),
            "  " + cost_line(doc["cost"]),
            "  %s 에 잠금. 대상은 %s 뒤의 거래일만 (첫 대상 %s, 평일 기준이라 휴장이면 그다음 거래일)." % (
                doc["locked_at"], doc["after"], doc["first_session"]),
            "  판정 (지금 정함): 처음 %d건의 비용 뺀 평균 R 95%% 구간 아래쪽이 0보다 크면 넘음, 아니면 넘지 못함. "
            "판정은 한 번만 합니다." % doc["judge_trades"],
            "  이 파일은 고치지 않습니다. 고치면 update 가 멈춥니다. 다른 규칙은 새 --name 으로 잠그세요.",
            "장 마감 뒤 (매주 한 번이면 충분): python trader-kr-lab/lab.py forward update --name %s" % doc["name"]]


def forward_report(res):
    L = ["미래 검증: %s (잠금 %s, %s 에 잠금, %s 뒤 거래일만)" % (res["rule"], res["name"], res["locked_at"], res["after"]),
         cost_line(res["cost"])]
    if not res["dates"]:
        L.append("아직 잠근 날 뒤에 장이 끝난 온전한 거래일이 없습니다. 첫 대상은 %s (평일 기준), 그날 장 마감 뒤에 update 하세요."
                 % res["first_session"])
    else:
        L.append("거래일 %s ~ %s. 종목별: %s" % (res["dates"][0], res["dates"][1], ", ".join(
            "%s %d일" % (s, n) for s, n in sorted(res["sessions"].items()))))
    st = res["stats"]
    if st["trades"]:
        L.append("거래 %d건, 비용 뺀 평균 R %+.3f%s, 승률 %.0f%%, 비용 전 평균 R %+.3f." % (
            st["trades"], st["avg_R_net"], (" (95%% 구간 %+.3f ~ %+.3f)" % tuple(st["ci"])) if st["ci"] else "",
            100 * st["win_rate"], st["avg_R"]))
        L.append("최근 거래:")
        for x in res["trades"][-5:]:
            end = datetime.datetime(2000, 1, 1, 9, 0) + datetime.timedelta(minutes=5 * (x["slot"] + 1))
            L.append("   %s %s %s 진입 %s -> %s %s, 비용 뺀 R %+.2f" % (
                x["date"], x["symbol"], krx.hm(end.time()), p(x["entry"]), p(x["exit"]),
                OUTCOME.get(x["outcome"], x["outcome"]), x["R_net"]))
    else:
        L.append("거래 0건.")
    L += ["", "판정: " + res["verdict_text"], FORWARD_NOTE]
    return L


def forward_main(a, parser):
    try:
        now = krx.parse_now(a.now)
        if a.fcmd == "lock":
            cfg = krx.read_config(a.config)
            syms = [krx.norm_symbol(s) for s in (a.symbols or "").split(",") if s.strip()]
            doc, path = forward_lock(a.rule, syms, cost_of(cfg, a.slip_ticks), now, a.name, a.dir, a.judge_trades)
            print(json.dumps(doc, ensure_ascii=False, indent=2) if a.json else "\n".join(lock_report(doc, path)))
        elif a.fcmd == "update":
            nm = pick_lock(a.dir, a.name)
            lock = read_lock(forward_paths(a.dir, nm)[0])
            progress = None if a.json else (lambda m: print(m, file=sys.stderr))
            res = forward_update(lock, now, a.dir, a.offline, progress)
            if a.json:
                print(json.dumps(res, ensure_ascii=False, indent=2))
            else:
                print("\n".join(forward_report(res)))
                print("%s 에 썼습니다." % forward_paths(a.dir, nm)[1])
        else:
            parser.print_help()
            return 1
        if not a.json:
            print("교육과 도구일 뿐 투자 권유가 아닙니다 (not financial advice).")
        return 0
    except (LabError, krx.KrxError, data.DataError, toss.TossError) as e:
        print("lab.py: %s" % e, file=sys.stderr)
        return 1


def top_symbols(n, now):
    """KRX 거래대금 상위 n 보통주 (토스 종목 정보로 걸러냄)."""
    key = krxdata.api_key()
    if not key:
        raise LabError("--top 은 KRX Open API 키가 필요합니다 (python krcommon/keys.py krx). 아니면 --symbols 로 고르세요")
    day = now.date() - datetime.timedelta(days=1)
    table = {}
    for _ in range(10):
        if day.weekday() < 5 and krxdata.published(day, now):
            table = krxdata.day_table(day, key)
            if table:
                break
        day -= datetime.timedelta(days=1)
    ranked = sorted(table.values(), key=lambda r: -(r.get("value") or 0))[:n * 3]
    info = {s["symbol"]: s for s in data.client().stocks([r["symbol"] for r in ranked])}
    keep = [r["symbol"] for r in ranked if (info.get(r["symbol"]) or {}).get("securityType") == "STOCK"
            and (info.get(r["symbol"]) or {}).get("isCommonShare") is not False]
    return keep[:n], day


def main(argv=None):
    krx.utf8_stdio()
    ap = argparse.ArgumentParser(description="전략 검증 실험실 (trader-kr-lab).")
    sub = ap.add_subparsers(dest="cmd")
    q = sub.add_parser("run", help="규칙들을 앞에서 고르고 뒤에서 확인")
    q.add_argument("--symbols", help="005930,000660 처럼 쉼표로")
    q.add_argument("--top", type=int, help="KRX 거래대금 상위 N 보통주 (KRX 키 필요)")
    q.add_argument("--sessions", type=int, default=250, help="온전한 거래일 수 (기본 250)")
    q.add_argument("--train", type=int, default=120)
    q.add_argument("--test", type=int, default=20)
    q.add_argument("--min-trades", type=int, default=30, help="학습 기간에 고를 수 있는 최소 거래 수")
    q.add_argument("--slip-ticks", type=int, default=1, help="손절·시간 청산 때 밀리는 호가 칸 (기본 1)")
    q.add_argument("--stop-pct", type=float, help="진입가 아래 X%% 손절 규칙도 시험 (설정 stop_rule 이 pct:X 면 자동)")
    q.add_argument("--config", default="trader-kr/config.json")
    q.add_argument("--offline", metavar="DIR", help="저장된 1분봉 파일 폴더")
    q.add_argument("--now")
    q.add_argument("--json", action="store_true")
    q.add_argument("--write", nargs="?", const="trader-kr/lab.json", metavar="PATH")
    f = sub.add_parser("forward", help="규칙 하나를 잠그고 잠근 날 뒤 거래일에만 적용 (미래 검증)")
    fs = f.add_subparsers(dest="fcmd")
    fl = fs.add_parser("lock", help="규칙·종목·비용을 잠근다 (덮어쓰지 않음)")
    fl.add_argument("--rule", required=True, help='lab.py run 표의 이름 그대로 (예: "ORB30 저가손절 2R 거래량1.5x")')
    fl.add_argument("--symbols", required=True, help="005930,000660 처럼 쉼표로")
    fl.add_argument("--name", default="forward", help="잠금 이름 (파일 이름, 기본 forward)")
    fl.add_argument("--slip-ticks", type=int, default=1, help="손절·시간 청산 때 밀리는 호가 칸 (기본 1)")
    fl.add_argument("--judge-trades", type=int, default=JUDGE_TRADES, help="판정에 쓸 처음 거래 수 (기본 200)")
    fl.add_argument("--config", default="trader-kr/config.json")
    fu = fs.add_parser("update", help="잠근 날 뒤 거래일의 성적을 다시 잰다")
    fu.add_argument("--name", help="잠금 이름 (하나뿐이면 생략)")
    fu.add_argument("--offline", metavar="DIR", help="저장된 1분봉 파일 폴더")
    for x in (fl, fu):
        x.add_argument("--dir", default=FORWARD_DIR, help="잠금·결과 폴더 (기본 trader-kr/forward)")
        x.add_argument("--now")
        x.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    if a.cmd == "forward":
        return forward_main(a, f)
    try:
        now = krx.parse_now(a.now)
        cfg = krx.read_config(a.config)
        if a.symbols:
            syms = [krx.norm_symbol(s) for s in a.symbols.split(",") if s.strip()]
        elif a.top:
            syms, _ = top_symbols(a.top, now)
        else:
            raise LabError("--symbols 나 --top 을 주세요")
        if a.train < 20 or a.test < 5:
            raise LabError("--train 은 20 이상, --test 는 5 이상입니다")
        progress = None if a.json else (lambda m: print(m, file=sys.stderr))
        if progress:
            progress("1분봉을 모읍니다: %d종목, 거래일 %d일..." % (len(syms), a.sessions))
        end = now.date() if now.time() >= datetime.time(15, 31) else now.date() - datetime.timedelta(days=1)
        by = load(syms, end, a.sessions, a.offline, progress)
        if not by:
            raise LabError("자료를 받은 종목이 없습니다")
        cost = cost_of(cfg, a.slip_ticks)
        stop_pct = a.stop_pct
        rule = str(cfg.get("stop_rule") or "").strip().lower()
        if stop_pct is None and rule.startswith("pct:"):
            try:
                stop_pct = float(rule[4:])
            except ValueError:
                raise LabError("설정 stop_rule %r 를 읽지 못했습니다 (예: pct:5)" % cfg.get("stop_rule"))
        if stop_pct is not None and not 0 < stop_pct < 30:
            raise LabError("--stop-pct 는 0 과 30 사이입니다")
        if stop_pct and progress:
            progress("진입가 아래 %g%% 손절 규칙 32개를 더해 80개 규칙을 시험합니다." % stop_pct)
        res = experiment(by, default_grid(stop_pct), cost, a.train, a.test, a.min_trades)
        res["verdict"] = verdict(res)
        if a.write:
            krx.write_json(a.write, res)
        if a.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print("\n".join(report(res)))
            if a.write:
                print("%s 에 썼습니다." % a.write)
            print("교육과 도구일 뿐 투자 권유가 아닙니다 (not financial advice).")
        return 0
    except (LabError, krx.KrxError, data.DataError, toss.TossError, krxdata.KrxDataError) as e:
        print("lab.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
