#!/usr/bin/env python3
"""거래 일지 (trader-kr-journal): 실제 진입과 청산을 적고, 계획을 얼마나 지켰는지 숫자로 본다.
표준 라이브러리만 쓴다. 주문을 내지 않고 증권사를 부르지 않는다. 체결 가격은 사용자가 알려 준 대로 적는다.

    python journal.py open --entry-price 30450 --qty 119            # trader-kr/plan.json 의 계획으로 진입 기록
    python journal.py open --symbol 005930 --entry-price 72300 --qty 10 --stop 71700 --entry-time 10:20
    python journal.py close last --exit-price 30100 --reason stop   # 가장 최근 열린 거래 청산
    python journal.py close 20261028-SYN001-1 --exit-price 31150 --exit-time 11:05 --reason target
    python journal.py list                                         # 기록 목록
    python journal.py report                                       # 성적과 계획 준수
    python journal.py report --since 2026-10-01 --json

거래마다 재는 것:
  R          (청산가 - 진입가) / 계획 위험 (계획 진입가 - 손절가). 비용 뺀 R 도 같이
  손익       수량 x (청산가 - 진입가) - 수수료 - 매도 거래세 (원 미만 버림 추정)
  늦은 진입  실제 진입 시각 - 신호 시각 (계획의 entry_time). 15분 넘으면 늦은 진입
  진입 밀림  실제 진입가 - 계획 진입가 (호가 칸 수)
  손절 어김  손절가보다 아래에서 나왔는데 이유가 손절이 아니거나, 손절이라도 호가 2칸 넘게 아래
  청산 어김  계획 청산 시각(flat_by) 뒤에 나옴, 또는 다음 날로 넘김
  수량 초과  수량 x 계획 위험 + 비용 이 설정 max_loss_per_trade_krw 를 넘음

모아 보는 것: 승률, 평균 R(비용 뺀), 손익 합, 낸 비용 합, 늦은 진입과 제때 진입의 평균 R 비교,
손절을 어겨 더 잃은 돈, 청산 어김과 수량 초과 횟수, 셋업별 평균 R.
기록은 trader-kr/journal.jsonl 에 한 줄씩 남는다 (저장소에 올리지 않는다).
"""

import argparse
import datetime
import json
import os
import re
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
from krcommon import krx  # noqa: E402

PATH = os.path.join("trader-kr", "journal.jsonl")
LATE_MINUTES = 15
REASONS = {"stop": "손절", "target": "목표", "flat": "시간 청산", "manual": "직접 청산"}


class JournalError(Exception):
    pass


def p(x):
    return krx.price_str(x)


def load(path=PATH):
    rows = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    raise JournalError("%s 의 %d번째 줄이 JSON 이 아닙니다" % (path, i))
    return rows


def save(rows, path=PATH):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def hhmm(text, what):
    if text is None:
        return None
    try:
        return krx.hm(krx.parse_hhmm(text, what))
    except krx.KrxError as e:
        raise JournalError(str(e))


def minutes_between(a, b):
    """'HH:MM' 두 개의 차이 (b - a, 분)."""
    ta, tb = krx.parse_hhmm(a, "a"), krx.parse_hhmm(b, "b")
    return krx.minutes_of(tb) - krx.minutes_of(ta)


def new_id(rows, day, sym):
    n = 1 + sum(1 for r in rows if r["date"] == day and r["symbol"] == sym)
    return "%s-%s-%d" % (day.replace("-", ""), sym, n)


# ---------------------------------------------------------------- 거래 하나

def evaluate(r, cfg):
    """닫힌 거래 하나의 숫자와 어긴 것들."""
    c = dict(krx.CONFIG_DEFAULTS)
    c.update(cfg or {})
    cr, tr = float(c.get("commission_rate") or 0), float(c.get("sell_tax_rate") or 0)
    entry, qty = float(r["entry_price"]), int(r["qty"])
    out = {"id": r["id"], "symbol": r["symbol"], "date": r["date"], "setup": r.get("setup") or "기타",
           "flags": [], "closed": r.get("exit_price") is not None}
    plan_entry = r.get("plan_entry")
    stop = r.get("stop")
    risk = (float(plan_entry) - float(stop)) if (plan_entry and stop) else ((entry - float(stop)) if stop else None)
    if risk is not None and risk <= 0:
        risk = None
    out["risk_per_share"] = risk
    if r.get("signal_time") and r.get("entry_time"):
        late = minutes_between(r["signal_time"], r["entry_time"])
        out["late_minutes"] = late
        if late > LATE_MINUTES:
            out["flags"].append("늦은 진입 %d분" % late)
    if plan_entry:
        t = krx.tick_size(plan_entry)
        out["entry_slip_ticks"] = round((entry - float(plan_entry)) / t, 1)
    if risk and c.get("max_loss_per_trade_krw"):
        planned_loss = qty * risk + krx.trade_costs(entry, float(stop), qty, cr, tr)["total"]
        if planned_loss > float(c["max_loss_per_trade_krw"]):
            out["flags"].append("수량 초과 (손절 때 %s, 한도 %s)" % (krx.won(planned_loss), krx.won(c["max_loss_per_trade_krw"])))
    if not out["closed"]:
        return out
    exitp = float(r["exit_price"])
    costs = krx.trade_costs(entry, exitp, qty, cr, tr)
    gross = qty * (exitp - entry)
    out.update(pnl_gross=gross, costs=costs["total"], pnl_net=gross - costs["total"], reason=r.get("reason"))
    if risk:
        out["R"] = round((exitp - entry) / risk, 3)
        out["R_net"] = round((gross - costs["total"]) / (qty * risk), 3)
    if stop:
        s = float(stop)
        two = 2 * krx.tick_size(s)
        if exitp < s and (r.get("reason") != "stop" or exitp < s - two):
            out["flags"].append("손절 어김 (손절가 %s 보다 %s원 아래에서 나옴)" % (p(s), p(s - exitp)))
            out["stop_overrun"] = qty * (s - exitp)
    if r.get("flat_by") and r.get("exit_time"):
        exit_day = r.get("exit_date") or r["date"]
        if exit_day != r["date"]:
            out["flags"].append("다음 날로 넘김 (%s 청산)" % exit_day)
        elif minutes_between(r["flat_by"], r["exit_time"]) > 0:
            out["flags"].append("청산 시각 %s 를 넘김 (%s 청산)" % (r["flat_by"], r["exit_time"]))
    return out


# ---------------------------------------------------------------- 모아 보기

def summarize(rows, cfg, since=None):
    ev = [evaluate(r, cfg) for r in rows if not since or r["date"] >= since]
    closed = [e for e in ev if e["closed"]]
    s = {"trades": len(ev), "closed": len(closed), "open": len(ev) - len(closed)}
    if not closed:
        return s, ev
    nets = [e["pnl_net"] for e in closed]
    rs = [e["R_net"] for e in closed if e.get("R_net") is not None]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    s.update(win_rate=round(len(wins) / float(len(closed)), 3), pnl_net=sum(nets),
             pnl_gross=sum(e["pnl_gross"] for e in closed), costs=sum(e["costs"] for e in closed),
             avg_win=round(statistics.mean(wins)) if wins else None,
             avg_loss=round(statistics.mean(losses)) if losses else None,
             avg_R_net=round(statistics.mean(rs), 3) if rs else None, R_count=len(rs))
    late = [e["R_net"] for e in closed if e.get("late_minutes") is not None and e["late_minutes"] > LATE_MINUTES
            and e.get("R_net") is not None]
    ontime = [e["R_net"] for e in closed if e.get("late_minutes") is not None and e["late_minutes"] <= LATE_MINUTES
              and e.get("R_net") is not None]
    s["late"] = {"count": len(late), "avg_R_net": round(statistics.mean(late), 3) if late else None}
    s["on_time"] = {"count": len(ontime), "avg_R_net": round(statistics.mean(ontime), 3) if ontime else None}
    over = [e for e in closed if e.get("stop_overrun")]
    s["stop_breaches"] = {"count": len(over), "extra_loss": sum(e["stop_overrun"] for e in over)}
    s["flat_violations"] = sum(1 for e in closed if any(f.startswith(("청산 시각", "다음 날")) for f in e["flags"]))
    s["oversize"] = sum(1 for e in ev if any(f.startswith("수량 초과") for f in e["flags"]))
    by = {}
    for e in closed:
        if e.get("R_net") is not None:
            by.setdefault(e["setup"], []).append(e["R_net"])
    s["by_setup"] = {k: {"count": len(v), "avg_R_net": round(statistics.mean(v), 3)} for k, v in sorted(by.items())}
    return s, ev


def report_lines(s, ev):
    L = []
    if not s["trades"]:
        return ["기록이 없습니다. 진입하면 journal.py open 으로 적으세요."]
    L.append("거래 %d건 (청산 %d, 열림 %d)." % (s["trades"], s["closed"], s["open"]))
    if s["closed"]:
        L.append("승률 %.0f%%, 손익 %s (비용 전 %s, 낸 비용 %s), 평균 이익 %s, 평균 손실 %s." % (
            100 * s["win_rate"], krx.won(s["pnl_net"]), krx.won(s["pnl_gross"]), krx.won(s["costs"]),
            krx.won(s["avg_win"]) if s["avg_win"] is not None else "-",
            krx.won(s["avg_loss"]) if s["avg_loss"] is not None else "-"))
        if s.get("avg_R_net") is not None:
            L.append("비용 뺀 평균 R %+.3f (계획 위험이 있는 %d건)." % (s["avg_R_net"], s["R_count"]))
        if s["pnl_gross"] > 0 and s["costs"]:
            L.append("비용이 비용 전 이익의 %.0f%% 를 가져갔습니다." % (100.0 * s["costs"] / s["pnl_gross"]))
        L.append("")
        L.append("계획을 지켰나:")
        lt, ot = s["late"], s["on_time"]
        if lt["count"] or ot["count"]:
            L.append("  늦은 진입(신호 뒤 %d분 넘음) %d건%s, 제때 진입 %d건%s." % (
                LATE_MINUTES, lt["count"], (" 평균 R %+.3f" % lt["avg_R_net"]) if lt["avg_R_net"] is not None else "",
                ot["count"], (" 평균 R %+.3f" % ot["avg_R_net"]) if ot["avg_R_net"] is not None else ""))
        sb = s["stop_breaches"]
        L.append("  손절 어김 %d건%s." % (sb["count"], (", 손절가에서 나왔다면 덜 잃었을 돈 %s" % krx.won(sb["extra_loss"]))
                                     if sb["count"] else ""))
        L.append("  청산 시각 어김 %d건, 한도를 넘는 수량 %d건." % (s["flat_violations"], s["oversize"]))
        if s.get("by_setup"):
            L.append("  셋업별: " + ", ".join("%s %d건 평균 R %+.3f" % (k, v["count"], v["avg_R_net"])
                                          for k, v in s["by_setup"].items()))
    flagged = [e for e in ev if e["flags"]]
    if flagged:
        L.append("")
        L.append("어긴 기록:")
        for e in flagged[-10:]:
            L.append("  %s  %s" % (e["id"], "; ".join(e["flags"])))
    if s["closed"] < 20:
        L.append("")
        L.append("청산 %d건은 작은 표본입니다. 20건은 넘어야 습관이 보이기 시작합니다." % s["closed"])
    return L


# ---------------------------------------------------------------- 명령

def cmd_open(a):
    rows = load(a.journal)
    now = krx.parse_now(a.now)
    plan = None
    if not a.symbol:
        plan = krx.read_json(a.plan, "계획 (trader-kr-exit 를 먼저 돌리거나 --symbol 을 주세요)")
    sym = krx.norm_symbol(a.symbol or plan.get("symbol"))
    day = a.date or (plan.get("date") if plan else None) or now.date().isoformat()
    if a.qty is None:
        orders = krx.read_json(a.orders, "주문 제안", required=False) or {}
        o = [x for x in orders.get("orders", []) if x.get("symbol") == sym]
        if not o:
            raise JournalError("--qty 를 주세요 (trader-kr/orders.json 에 %s 주문 제안이 없습니다)" % sym)
        a.qty = int(o[0]["qty"])
    if a.qty <= 0 or a.entry_price <= 0:
        raise JournalError("수량과 진입가는 0보다 커야 합니다")
    rec = {"id": new_id(rows, day, sym), "symbol": sym, "name": (plan or {}).get("name"), "date": day,
           "entry_time": hhmm(a.entry_time, "--entry-time") or now.strftime("%H:%M"),
           "entry_price": float(a.entry_price), "qty": int(a.qty),
           "setup": a.setup or (plan or {}).get("setup"), "signal_time": (plan or {}).get("entry_time"),
           "plan_entry": (plan or {}).get("entry"), "stop": a.stop if a.stop is not None else (plan or {}).get("stop"),
           "target": (plan or {}).get("target"), "flat_by": (plan or {}).get("flat_by") or a.flat_by,
           "note": a.note, "recorded_at": now.strftime("%Y-%m-%d %H:%M:%S")}
    if plan and plan.get("date") and plan["date"] != day:
        raise JournalError("계획은 %s 것인데 기록 날짜는 %s 입니다. --symbol 로 따로 적으세요" % (plan["date"], day))
    rows.append(rec)
    save(rows, a.journal)
    ev = evaluate(rec, krx.read_config(a.config))
    print("기록했습니다: %s  %s %d주, 진입 %s (%s)%s." % (rec["id"], sym, rec["qty"], p(rec["entry_price"]), rec["entry_time"],
                                                 (", 손절 %s" % p(rec["stop"])) if rec.get("stop") else ""))
    for f in ev["flags"]:
        print("  주의: " + f)
    print("나오면: journal.py close %s --exit-price 가격 --reason stop|target|flat|manual" % rec["id"])
    return 0


def cmd_close(a):
    rows = load(a.journal)
    now = krx.parse_now(a.now)
    open_rows = [r for r in rows if r.get("exit_price") is None]
    if a.id == "last":
        if not open_rows:
            raise JournalError("열린 거래가 없습니다")
        rec = open_rows[-1]
    else:
        found = [r for r in rows if r["id"] == a.id]
        if not found:
            raise JournalError("%s 기록이 없습니다 (journal.py list)" % a.id)
        rec = found[0]
        if rec.get("exit_price") is not None and not a.force:
            raise JournalError("%s 는 이미 청산했습니다. 고치려면 --force" % a.id)
    if a.exit_price <= 0:
        raise JournalError("청산가는 0보다 커야 합니다")
    rec.update(exit_price=float(a.exit_price), exit_time=hhmm(a.exit_time, "--exit-time") or now.strftime("%H:%M"),
               exit_date=a.exit_date or rec["date"], reason=a.reason,
               note=((rec.get("note") or "") + (" / " + a.note if a.note else "")).strip(" /") or None)
    save(rows, a.journal)
    ev = evaluate(rec, krx.read_config(a.config))
    print("청산했습니다: %s  %s %s (%s). 손익 %s (비용 %s 뺌)%s." % (
        rec["id"], p(rec["exit_price"]), rec["exit_time"], REASONS.get(a.reason, a.reason), krx.won(ev["pnl_net"]),
        krx.won(ev["costs"]), (", 비용 뺀 R %+.2f" % ev["R_net"]) if ev.get("R_net") is not None else ""))
    for f in ev["flags"]:
        print("  주의: " + f)
    return 0


def cmd_list(a):
    rows = load(a.journal)
    if a.open:
        rows = [r for r in rows if r.get("exit_price") is None]
    if not rows:
        print("기록이 없습니다.")
        return 0
    for r in rows:
        print("%-22s %s %-7s %4d주 진입 %s %s  %s" % (
            r["id"], r["date"], r["symbol"], r["qty"], p(r["entry_price"]), r["entry_time"],
            ("청산 %s %s (%s)" % (p(r["exit_price"]), r.get("exit_time") or "", REASONS.get(r.get("reason"), r.get("reason"))))
            if r.get("exit_price") is not None else "열림"))
    return 0


def cmd_report(a):
    if a.since and not re.match(r"^\d{4}-\d{2}-\d{2}$", a.since):
        raise JournalError("--since 는 2026-10-01 같은 날짜입니다")
    s, ev = summarize(load(a.journal), krx.read_config(a.config), a.since)
    if a.json:
        print(json.dumps({"summary": s, "trades": ev}, ensure_ascii=False, indent=2))
    else:
        print("\n".join(report_lines(s, ev)))
        print("교육과 도구일 뿐 투자 권유가 아닙니다 (not financial advice).")
    return 0


def main(argv=None):
    krx.utf8_stdio()
    ap = argparse.ArgumentParser(description="거래 일지 (trader-kr-journal).")
    ap.add_argument("--journal", default=PATH)
    ap.add_argument("--config", default="trader-kr/config.json")
    sub = ap.add_subparsers(dest="cmd")
    q = sub.add_parser("open", help="진입 기록")
    q.add_argument("--plan", default="trader-kr/plan.json")
    q.add_argument("--orders", default="trader-kr/orders.json")
    q.add_argument("--symbol", help="계획 없이 적을 때 종목코드")
    q.add_argument("--entry-price", type=float, required=True)
    q.add_argument("--entry-time", help="HH:MM (기본 지금)")
    q.add_argument("--qty", type=int, help="수량 (기본 trader-kr/orders.json 의 수량)")
    q.add_argument("--stop", type=float, help="손절가 (기본 계획의 손절가)")
    q.add_argument("--setup")
    q.add_argument("--flat-by", help="청산 시각 (기본 계획의 flat_by)")
    q.add_argument("--date")
    q.add_argument("--note")
    q.add_argument("--now")
    q = sub.add_parser("close", help="청산 기록")
    q.add_argument("id", help="기록 번호 또는 last")
    q.add_argument("--exit-price", type=float, required=True)
    q.add_argument("--exit-time", help="HH:MM (기본 지금)")
    q.add_argument("--exit-date", help="다음 날 청산했으면 그 날짜")
    q.add_argument("--reason", choices=sorted(REASONS), required=True)
    q.add_argument("--note")
    q.add_argument("--force", action="store_true")
    q.add_argument("--now")
    q = sub.add_parser("list", help="기록 목록")
    q.add_argument("--open", action="store_true")
    q = sub.add_parser("report", help="성적과 계획 준수")
    q.add_argument("--since")
    q.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"open": cmd_open, "close": cmd_close, "list": cmd_list, "report": cmd_report}[a.cmd](a)
    except (JournalError, krx.KrxError) as e:
        print("journal.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
