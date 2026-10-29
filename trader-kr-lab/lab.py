#!/usr/bin/env python3
"""전략 검증 실험실 (trader-kr-lab): 여러 단타 규칙을 앞 기간에서 고르고 뒤 기간에서 확인한다.
표준 라이브러리만 쓴다. 주문을 내지 않는다.

    python lab.py run --symbols 005930,000660,035420              # 250 거래일, 학습 120일 -> 시험 20일
    python lab.py run --top 10 --sessions 250 --write             # KRX 거래대금 상위 10 보통주
    python lab.py run --symbols 005930 --train 60 --test 20 --slip-ticks 2 --json

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

자료: 토스증권 1분봉 (2022-11-23 이후, trader-kr/cache 캐시)을 5분봉으로 묶은 온전한 거래일.
처음 받을 때는 종목당 몇 분 걸린다. 과거 성적은 미래를 약속하지 않는다.
"""

import argparse
import datetime
import json
import math
import os
import random
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

def default_grid():
    out = []
    for orm in (15, 30):
        for stop in ("or_mid", "or_low"):
            for r in (1.0, 1.5, 2.0):
                for vx in (None, 1.5):
                    for cf in (False, True):
                        out.append({"or_minutes": orm, "stop": stop, "target_r": r, "vol_x": vx, "cost_filter": cf})
    return out


def name(v):
    return "ORB%d %s %gR%s%s" % (v["or_minutes"], {"or_mid": "중간손절", "or_low": "저가손절"}[v["stop"]], v["target_r"],
                                 " 거래량%gx" % v["vol_x"] if v["vol_x"] else "", " 비용필터" if v["cost_filter"] else "")


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
    s = krx.floor_tick((oh + ol) / 2.0 if v["stop"] == "or_mid" else ol)
    risk = e - s
    if risk < max(2 * krx.tick_size(s), e * 0.0005):
        return None
    cr, tr, slip = cost["commission_rate"], cost["sell_tax_rate"], cost["slip_ticks"]
    cost_stop = e * cr + s * (cr + tr)
    if v["cost_filter"] and cost_stop > cost["max_cost_to_risk"] * risk:
        return None
    t = krx.round_tick(e + v["target_r"] * risk)
    exitp, outcome = None, None
    for b in Dd[k + 1:FLAT_SLOT + 1]:
        if b["o"] <= s:
            exitp, outcome = krx.floor_tick(b["o"]) - slip * krx.tick_size(b["o"]), "gap"
            break
        if b["l"] <= s:
            exitp, outcome = s - slip * krx.tick_size(s), "stop"
            break
        if b["h"] >= t:
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
        res["sessions"], res["dates"][0], res["dates"][1]),
         "비용: 수수료 %s, 매도세 %s, 손절·시간 청산 %d칸 밀림, 갭은 시가 체결." % (
             krx.pct_str(c["commission_rate"], 3), krx.pct_str(c["sell_tax_rate"], 2), c["slip_ticks"]), ""]
    top = sorted(res["table"], key=lambda r: -(r["avg_R_net"] if r["avg_R_net"] is not None else -9))[:8]
    L.append("1. 전 기간 (고르기 전, 참고). 비용 뺀 평균 R 상위 8개:")
    L.append("   %-36s %6s %9s %9s %6s %8s" % ("규칙", "거래", "비용 전 R", "비용 뒤 R", "승률", "보정 p"))
    for r in top:
        L.append("   %-36s %6d %+9.3f %+9.3f %5.0f%% %8.3f" % (
            r["rule"], r["trades"], r["avg_R"] or 0, r["avg_R_net"] or 0, 100 * (r["win_rate"] or 0), r["p_holm"]))
    L.append("")
    L.append("2. 워크포워드 (앞 %d 거래일에서 고르고 뒤 %d 거래일에서 확인, %d번):" % (st["train"], st["test"], len(res["folds"])))
    for f in res["folds"]:
        if f["rule"]:
            L.append("   %s~%s 고름: %-34s 학습 %+.3f -> 시험 %s (%d건)" % (
                f["test"][0], f["test"][1], f["rule"], f["train_avg_R_net"],
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
    q.add_argument("--config", default="trader-kr/config.json")
    q.add_argument("--offline", metavar="DIR", help="저장된 1분봉 파일 폴더")
    q.add_argument("--now")
    q.add_argument("--json", action="store_true")
    q.add_argument("--write", nargs="?", const="trader-kr/lab.json", metavar="PATH")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
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
        cost = {"commission_rate": float(cfg.get("commission_rate") or 0), "sell_tax_rate": float(cfg.get("sell_tax_rate") or 0),
                "slip_ticks": a.slip_ticks, "max_cost_to_risk": float(cfg.get("max_cost_to_risk") or 0.3)}
        res = experiment(by, default_grid(), cost, a.train, a.test, a.min_trades)
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
