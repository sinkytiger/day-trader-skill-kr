#!/usr/bin/env python3
"""주문 실행 (trader-kr-broker): 한국투자증권 모의투자 계좌로만, 세 겹 승인 뒤에 주문한다.

    python broker.py show                                   # 주문표: 보낼 수 있는지(READY) 아닌지
    python broker.py approve --index 1 --reply "네"          # 사용자의 답을 글자 그대로 기록
    python broker.py place --index 1                         # 모의투자로 매수, 체결되면 목표가 지정가 매도
    python broker.py watch                                   # 손절가·청산 시각을 지켜보다 시장가로 매도
    python broker.py status                                  # 모의투자 잔고와 오늘 주문 (조회만)
    python broker.py flatten 005930 --reply "네"             # 그 종목 보유분 시장가 매도
    python broker.py log                                     # 오늘 기록

세 겹 승인 (원본 day-trader-skill 과 같다). 주문은 셋이 모두 맞을 때만 나간다:
  1. trader-kr-risk 가 오늘 이 주문 그대로 PASS 했다 (지문이 같다)
  2. 사용자가 채팅에서 이 주문 하나에 "네" / "예" / "yes" 로 답했고, approve 가 그 답을 기록했다 (10분 안, 한 번만)
  3. 사용자가 Claude 의 도구 승인 카드에서 이 명령을 허용했다
그리고 trader-kr/config.json 의 "send_orders" 가 true 여야 한다 (사용자가 직접 바꾼다).

yes 하나가 승인하는 것: 매수 1건과, 체결된 수량에 대한 목표가 지정가 매도 1건, 그리고 watch 가 돌고 있을 때
손절가 이하가 되거나 청산 시각이 되면 내는 시장가 매도. 새로 사는 주문은 늘 새 yes 가 필요하다.

손절은 한국투자증권에 걸리는 주문이 아니다. watch 가 가격을 보다가 손절가 이하면 시장가로 판다. watch 가 돌고
있지 않으면 손절이 지켜지지 않는다. 목표가 지정가 매도는 증권사에 걸려 있다.

모의투자 서버만 부른다 (krcommon/kis.py). 실전 계좌는 지원하지 않는다.
"""

import argparse
import datetime
import importlib.util
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from krcommon import kis, krx  # noqa: E402

STATE = "trader-kr/state/broker.json"
APPROVAL_MINUTES = 10
YES_WORDS = {"yes", "y", "예", "네"}
BATCH_WORDS = {"모두", "전부", "전체", "나머지", "다", "all", "every", "everything", "both", "rest", "batch"}
DOUBT_WORDS = {"no", "not", "dont", "don't", "wait", "but", "maybe", "later", "cancel", "unless", "if"}
DOUBT_PREFIX = ("아니", "말고", "잠깐", "취소", "나중", "근데", "하지만", "만약", "글쎄", "혹시")


class BrokerError(Exception):
    pass


def _module(folder, name):
    key = "trader_kr_broker_" + name
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, os.path.join(ROOT, folder, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.modules[key] = mod
    return mod


def risk_mod():
    return _module("trader-kr-risk", "check")


def journal_mod():
    return _module("trader-kr-journal", "journal")


def p(x):
    return krx.price_str(x) if x is not None else "-"


# ---------------------------------------------------------------- 상태 파일

def load_state(path=STATE):
    doc = krx.read_json(path, "주문 기록", required=False) or {}
    for k in ("orders", "approvals", "positions"):
        doc.setdefault(k, [])
    return doc


def save_state(doc, path=STATE):
    krx.write_json(path, doc)


# ---------------------------------------------------------------- 주문표

def tickets(orders_doc, risk_doc, state, today):
    """주문마다 {index, order, fingerprint, ready, why, risk}."""
    fp = risk_mod().fingerprint
    results = {}
    if risk_doc and risk_doc.get("date") == today:
        for r in risk_doc.get("results") or []:
            results[r.get("fingerprint")] = r
    placed = {r.get("fingerprint") for r in state["orders"] if r.get("date") == today and r.get("kind") == "entry"}
    out = []
    for i, o in enumerate((orders_doc or {}).get("orders") or [], 1):
        f = fp(o)
        r = results.get(f)
        if f in placed:
            ready, why = False, "오늘 이미 보낸 주문입니다"
        elif str(o.get("side", "")).lower() != "buy":
            ready, why = False, "매수 주문만 받습니다 (보유분 매도는 flatten)"
        elif not risk_doc or risk_doc.get("date") != today:
            ready, why = False, "오늘 위험 점검 결과가 없습니다. /trader-kr-risk 를 먼저 돌리세요"
        elif r is None:
            ready, why = False, "위험 점검이 이 주문 그대로를 본 기록이 없습니다 (주문이 바뀌었으면 다시 점검)"
        elif r.get("verdict") != "PASS":
            ready, why = False, "위험 점검 결과가 %s 입니다: %s" % (r.get("verdict"), "; ".join(r.get("reasons") or []))
        else:
            ready, why = True, "위험 점검 PASS (%s)" % risk_doc.get("now_kst")
        out.append({"index": i, "order": o, "fingerprint": f, "ready": ready, "why": why, "risk": r})
    return out


def ticket_lines(t, total):
    o = t["order"]
    ex = o.get("exit") or {}
    pl = o.get("plan") or {}
    how = "시장가" if str(o.get("type", "market")).lower() == "market" else "지정가 %s" % p(o.get("limit_price"))
    L = ["주문 %d / %d  한국투자증권 모의투자" % (t["index"], total),
         "  매수 %s %s %s주, %s, 당일" % (o.get("symbol"), o.get("name") or "", o.get("qty"), how),
         "  체결되면: 목표가 %s 지정가 매도를 증권사에 겁니다" % p(ex.get("take_profit_price")),
         "  손절가 %s, 청산 시각 %s: broker.py watch 가 돌고 있을 때만 시장가로 팝니다" % (
             p(ex.get("stop_price")), ex.get("flat_by") or "-")]
    if t.get("risk") and t["risk"].get("max_loss") is not None:
        L.append("  손절 때 손실 약 %s (빠른 장에서는 더 나쁘게 체결될 수 있음)" % krx.won(t["risk"]["max_loss"]))
    elif pl.get("max_loss") is not None:
        L.append("  손절 때 손실 약 %s" % krx.won(pl["max_loss"]))
    L.append("  %s: %s" % ("READY" if t["ready"] else "NOT READY", t["why"]))
    L.append("  지문 %s" % t["fingerprint"])
    return L


# ---------------------------------------------------------------- yes 확인

def is_yes(reply, order):
    """(맞는지, 이유). 이 주문 하나에 대한 분명한 yes 만 센다."""
    raw = " ".join(str(reply or "").split())
    low = raw.lower()
    words = [w for w in re.split(r"[\s,.!?~]+", low) if w]
    if not words:
        return False, "답이 비었습니다"
    if words[0] not in YES_WORDS:
        return False, "\"네\", \"예\", \"yes\" 로 시작하는 답만 셉니다 (좋아, 해, ok, ㅇㅇ 는 세지 않음)"
    batch = [w for w in words if w in BATCH_WORDS] + (["둘 다"] if "둘 다" in low else [])
    if batch:
        return False, "yes 하나는 주문 하나에만 씁니다 (\"%s\")" % batch[0]
    doubt = [w for w in words[1:] if w in DOUBT_WORDS or w.startswith(DOUBT_PREFIX)]
    if doubt:
        return False, "답에 조건이나 망설임이 있습니다 (\"%s\"). 다시 물으세요" % doubt[0]
    sym = str(order.get("symbol") or "")
    ok_nums = {sym, str(order.get("qty"))}
    for k in ("limit_price",):
        if order.get(k) is not None:
            ok_nums.add(str(int(float(order[k]))))
    ex = order.get("exit") or {}
    pl = order.get("plan") or {}
    for v in (ex.get("stop_price"), ex.get("take_profit_price"), pl.get("entry")):
        if v is not None:
            ok_nums.add(str(int(float(v))))
    for n in re.findall(r"(?<![A-Za-z\d])\d[\d,]*", raw):          # SYN001 처럼 글자에 붙은 숫자는 빼고
        n = n.replace(",", "")
        if n not in ok_nums:
            return False, "답의 숫자 %s 가 이 주문(%s %s주)과 맞지 않습니다" % (n, sym, order.get("qty"))
    return True, "이 주문에 대한 yes"


# ---------------------------------------------------------------- 주문 보내기

def find_approval(state, fingerprint, now):
    for a in reversed(state["approvals"]):
        if a.get("fingerprint") != fingerprint or a.get("used"):
            continue
        at = krx.parse_now(a["at"])
        if (now - at).total_seconds() <= APPROVAL_MINUTES * 60 and at.date() == now.date():
            return a
    return None


def wait_fill(client, day, odno, want, wait, every=2.0):
    """체결 수량이 want 가 되거나 wait 초가 지날 때까지 본다. 주문 기록 한 줄 또는 None."""
    row = None
    spent = 0.0
    while True:
        rows = client.orders(day, odno=odno)
        row = next((r for r in rows if r.get("odno") == odno), row)
        if row and row.get("filled", 0) >= want:
            return row
        if spent >= wait:
            return row
        client.sleep(every)
        spent += every


def plan_path_for(orders_path):
    d = os.path.dirname(os.path.abspath(orders_path))
    cand = os.path.join(d, "plan.json")
    if os.path.exists(cand):
        return cand
    return os.path.join("trader-kr", "plan.json")


def journal_open(plan_path, orders_path, sym, avg, qty, hhmm, now, note):
    j = journal_mod()
    argv = ["open", "--plan", plan_path, "--orders", orders_path, "--entry-price", "%g" % avg,
            "--entry-time", hhmm, "--qty", str(qty), "--now", now.strftime("%Y-%m-%d %H:%M"), "--note", note]
    if not os.path.exists(plan_path):
        argv = ["open", "--symbol", sym, "--entry-price", "%g" % avg, "--entry-time", hhmm, "--qty", str(qty),
                "--now", now.strftime("%Y-%m-%d %H:%M"), "--note", note]
    code = j.main(argv)
    rows = j.load()
    mine = [r for r in rows if r.get("symbol") == sym and r.get("exit_price") is None]
    return mine[-1]["id"] if code == 0 and mine else None


def journal_close(jid, price, hhmm, reason, now, note):
    if not jid:
        return
    journal_mod().main(["close", jid, "--exit-price", "%g" % price, "--exit-time", hhmm, "--reason", reason,
                        "--now", now.strftime("%Y-%m-%d %H:%M"), "--note", note])


def hhmm_of(ord_tmd, now):
    t = str(ord_tmd or "")
    return "%s:%s" % (t[:2], t[2:4]) if len(t) >= 4 and t[:4].isdigit() else now.strftime("%H:%M")


def place(client, cfg, orders_path, risk_path, index, now, show, state_path=STATE, wait=20):
    if cfg.get("send_orders") is not True:
        raise BrokerError("trader-kr/config.json 의 \"send_orders\" 가 true 가 아닙니다. 보내려면 사용자가 직접 true 로 "
                          "바꿉니다 (Claude 가 대신 바꾸지 않습니다).")
    today = now.date().isoformat()
    state = load_state(state_path)
    orders_doc = krx.read_json(orders_path, "주문 제안")
    risk_doc = krx.read_json(risk_path, "위험 점검 결과", required=False)
    ts = tickets(orders_doc, risk_doc, state, today)
    if not 1 <= index <= len(ts):
        raise BrokerError("주문 %d 이 없습니다 (주문 %d개)" % (index, len(ts)))
    t = ts[index - 1]
    if not t["ready"]:
        raise BrokerError("보낼 수 없습니다: %s" % t["why"])
    appr = find_approval(state, t["fingerprint"], now)
    if not appr:
        raise BrokerError("이 주문에 대한 %d분 안의 yes 기록이 없습니다. 사용자에게 이 주문 하나를 보여 주고 묻고, "
                          "broker.py approve --index %d --reply \"<답 그대로>\" 로 기록하세요" % (APPROVAL_MINUTES, index))
    o = t["order"]
    sym = krx.norm_symbol(o["symbol"])
    qty = int(o["qty"])
    price = None if str(o.get("type", "market")).lower() == "market" else float(o["limit_price"])
    ex = o.get("exit") or {}
    appr["used"] = True
    appr["used_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
    sent = client.order("buy", sym, qty, price)
    rec = {"date": today, "at": now.strftime("%Y-%m-%d %H:%M:%S"), "symbol": sym, "side": "buy", "kind": "entry",
           "qty": qty, "type": "market" if price is None else "limit", "status": "placed", "odno": sent["odno"],
           "orgno": sent["orgno"], "fingerprint": t["fingerprint"], "max_loss": (t["risk"] or {}).get("max_loss"),
           "account": "kis-mock"}
    state["orders"].append(rec)
    pos = {"symbol": sym, "name": o.get("name"), "qty": qty, "filled": 0, "status": "pending", "entry_odno": sent["odno"],
           "entry_orgno": sent["orgno"],
           "stop": ex.get("stop_price"), "target": ex.get("take_profit_price"), "flat_by": ex.get("flat_by"),
           "date": today, "plan_path": plan_path_for(orders_path), "orders_path": orders_path, "journal_id": None}
    state["positions"].append(pos)
    save_state(state, state_path)
    show("모의투자로 보냈습니다: 매수 %s %d주 %s, 주문번호 %s." % (sym, qty, "시장가" if price is None else "지정가 %s" % p(price),
                                                        sent["odno"]))
    row = wait_fill(client, now.date(), sent["odno"], qty, wait)
    if row and int(row.get("filled", 0)) >= qty:
        on_entry_fill(client, state, pos, row, now, show)
    elif row and row.get("filled"):
        show("%d주 중 %d주만 체결됐습니다. 나머지는 watch 가 확인합니다." % (qty, row["filled"]))
    else:
        show("아직 체결되지 않았습니다. broker.py watch 가 체결을 확인하고 목표가 주문을 겁니다.")
    save_state(state, state_path)
    return 0


def on_entry_fill(client, state, pos, row, now, show):
    """매수가 체결되면: 일지에 적고, 체결 수량만큼 목표가 지정가 매도를 건다."""
    filled = int(row["filled"])
    avg = float(row.get("avg") or 0)
    pos.update(filled=filled, entry_avg=avg, status="open", entry_time=hhmm_of(row.get("time"), now))
    show("체결: %s %d주, 평균 %s." % (pos["symbol"], filled, p(round(avg, 1))))
    pos["journal_id"] = journal_open(pos["plan_path"], pos["orders_path"], pos["symbol"], avg, filled, pos["entry_time"],
                                     now, "KIS 모의 %s" % pos["entry_odno"])
    if pos.get("target"):
        t = client.order("sell", pos["symbol"], filled, pos["target"])
        pos.update(target_odno=t["odno"], target_orgno=t["orgno"])
        state["orders"].append({"date": pos["date"], "at": now.strftime("%Y-%m-%d %H:%M:%S"), "symbol": pos["symbol"],
                                "side": "sell", "kind": "exit", "qty": filled, "type": "limit", "status": "placed",
                                "odno": t["odno"], "orgno": t["orgno"], "account": "kis-mock"})
        show("목표가 %s 에 %d주 지정가 매도를 걸었습니다 (주문번호 %s)." % (p(pos["target"]), filled, t["odno"]))
    show("손절가 %s, 청산 시각 %s: broker.py watch 가 지킵니다. watch 를 켜 두세요." % (p(pos.get("stop")),
                                                                        pos.get("flat_by") or "-"))


def sell_out(client, state, pos, reason, now, show, wait=20):
    """목표가 주문을 취소하고 남은 수량을 시장가로 판다."""
    remain = int(pos.get("filled", 0)) - int(pos.get("sold", 0))
    if pos.get("target_odno"):
        rows = client.orders(now.date(), odno=pos["target_odno"])
        row = next((r for r in rows if r.get("odno") == pos["target_odno"]), None)
        remain = int(pos.get("filled", 0)) - int((row or {}).get("filled", 0))     # 목표가에 이미 팔린 만큼 뺀다
        if row and int(row.get("remaining", 0)) > 0 and not row.get("cancelled"):
            client.cancel(pos.get("target_orgno"), pos["target_odno"])
    if remain <= 0:
        pos.update(status="closed", exit_reason="target", sold=int(pos.get("filled", 0)))
        return None
    s = client.order("sell", pos["symbol"], remain, None)
    state["orders"].append({"date": pos["date"], "at": now.strftime("%Y-%m-%d %H:%M:%S"), "symbol": pos["symbol"],
                            "side": "sell", "kind": "exit", "qty": remain, "type": "market", "status": "placed",
                            "odno": s["odno"], "orgno": s["orgno"], "account": "kis-mock", "reason": reason})
    row = wait_fill(client, now.date(), s["odno"], remain, wait)
    avg = float((row or {}).get("avg") or 0)
    pos.update(status="closed", exit_reason=reason, exit_avg=avg, sold=int(pos.get("filled", 0)))
    word = {"stop": "손절가 이하", "flat": "청산 시각", "manual": "직접 청산"}[reason]
    show("%s: %s %d주 시장가 매도, 평균 %s." % (word, pos["symbol"], remain, p(round(avg, 1)) if avg else "확인 중"))
    if avg:
        journal_close(pos.get("journal_id"), avg, hhmm_of((row or {}).get("time"), now), reason, now,
                      "KIS 모의 %s" % s["odno"])
    return avg


def watch_step(client, state, now, show):
    """열린 포지션을 한 번 살핀다. 할 일이 남았으면 True."""
    live = False
    for pos in state["positions"]:
        if pos.get("status") not in ("pending", "open") or pos.get("date") != now.date().isoformat():
            continue
        flat = pos.get("flat_by")
        if pos["status"] == "pending":
            rows = client.orders(now.date(), odno=pos["entry_odno"])
            row = next((r for r in rows if r.get("odno") == pos["entry_odno"]), None)
            if row and row.get("filled"):
                if int(row.get("remaining", 0)) > 0 and not row.get("cancelled"):
                    client.cancel(pos.get("entry_orgno"), pos["entry_odno"])      # 일부만 체결: 나머지는 사지 않는다
                on_entry_fill(client, state, pos, row, now, show)
            elif flat and now.strftime("%H:%M") >= flat:
                if row and not row.get("cancelled"):
                    client.cancel(pos.get("entry_orgno"), pos["entry_odno"])
                pos.update(status="cancelled")
                show("%s 매수가 %s 까지 체결되지 않아 취소했습니다." % (pos["symbol"], flat))
                continue
            else:
                live = True
                continue
        if flat and now.strftime("%H:%M") >= flat:
            sell_out(client, state, pos, "flat", now, show)
            continue
        if pos.get("target_odno"):
            rows = client.orders(now.date(), odno=pos["target_odno"])
            row = next((r for r in rows if r.get("odno") == pos["target_odno"]), None)
            if row and int(row.get("filled", 0)) >= int(pos["filled"]):
                pos.update(status="closed", exit_reason="target", exit_avg=float(row.get("avg") or pos["target"]),
                           sold=int(pos["filled"]))
                show("목표가 체결: %s %d주, 평균 %s." % (pos["symbol"], pos["filled"], p(pos["exit_avg"])))
                journal_close(pos.get("journal_id"), pos["exit_avg"], hhmm_of(row.get("time"), now), "target", now,
                              "KIS 모의 %s" % pos["target_odno"])
                continue
        if pos.get("stop") is not None:
            last = client.price(pos["symbol"])["price"]
            pos["last_price"] = last
            if last <= float(pos["stop"]):
                sell_out(client, state, pos, "stop", now, show)
                continue
        live = True
    return live


def flatten(client, state, symbol, reply, now, show):
    sym = krx.norm_symbol(symbol)
    bal = client.balance()
    held = next((x for x in bal["positions"] if x["symbol"] == sym), None)
    if not held or held["qty"] <= 0:
        raise BrokerError("모의투자 계좌에 %s 보유분이 없습니다" % sym)
    ok, why = is_yes(reply, {"symbol": sym, "qty": held["qty"]})
    if not ok:
        raise BrokerError("팔지 않았습니다: %s" % why)
    pos = next((x for x in state["positions"] if x.get("symbol") == sym and x.get("status") in ("open", "pending")), None)
    if pos is None:
        pos = {"symbol": sym, "qty": held["qty"], "filled": held["qty"], "status": "open", "date": now.date().isoformat(),
               "journal_id": None}
        state["positions"].append(pos)
    pos["filled"] = max(int(pos.get("filled", 0)), int(held["qty"]) + int(pos.get("sold", 0)))
    return sell_out(client, state, pos, "manual", now, show)


# ---------------------------------------------------------------- 명령

def cmd_show(a, show):
    now = krx.parse_now(a.now)
    state = load_state(a.state)
    ts = tickets(krx.read_json(a.orders, "주문 제안"), krx.read_json(a.risk, "위험 점검 결과", required=False), state,
                 now.date().isoformat())
    if not ts:
        show("주문 제안이 없습니다. /trader-kr-budget 을 먼저 돌리세요.")
        return 1
    for t in ts:
        for line in ticket_lines(t, len(ts)):
            show(line)
        show("")
    show("READY 인 주문 하나를 사용자에게 보여 주고 \"이 주문을 넣을까요? 네 / 아니오\" 로 묻습니다. 한 번에 하나만.")
    return 0


def cmd_approve(a, show):
    now = krx.parse_now(a.now)
    state = load_state(a.state)
    ts = tickets(krx.read_json(a.orders, "주문 제안"), krx.read_json(a.risk, "위험 점검 결과", required=False), state,
                 now.date().isoformat())
    if not 1 <= a.index <= len(ts):
        raise BrokerError("주문 %d 이 없습니다" % a.index)
    t = ts[a.index - 1]
    if not t["ready"]:
        raise BrokerError("NOT READY 인 주문은 승인할 수 없습니다: %s" % t["why"])
    ok, why = is_yes(a.reply, t["order"])
    state["approvals"].append({"fingerprint": t["fingerprint"], "index": a.index, "reply": a.reply, "accepted": ok,
                               "why": why, "at": now.strftime("%Y-%m-%d %H:%M:%S"), "used": not ok})
    save_state(state, a.state)
    if not ok:
        show("기록하지 않았습니다: %s" % why)
        return 1
    show("yes 를 기록했습니다 (주문 %d, 지문 %s). %d분 안에 broker.py place --index %d 로 보냅니다. 도구 승인 카드가 "
         "한 번 더 묻습니다." % (a.index, t["fingerprint"], APPROVAL_MINUTES, a.index))
    return 0


def cmd_place(a, show, client=None):
    cfg = krx.read_config(a.config)
    return place(client or kis.Client(), cfg, a.orders, a.risk, a.index, krx.parse_now(a.now), show, a.state, a.wait)


def cmd_watch(a, show, client=None, clock=None):
    c = client or kis.Client()
    until = a.until
    while True:
        now = krx.parse_now(a.now) if a.now else krx.kst_now()
        state = load_state(a.state)
        live = watch_step(c, state, now, show)
        save_state(state, a.state)
        if not live or a.once or (until and now.strftime("%H:%M") >= until):
            break
        c.sleep(a.every)
    open_n = sum(1 for x in load_state(a.state)["positions"] if x.get("status") in ("open", "pending"))
    show("watch 끝. 아직 열린 포지션 %d개." % open_n if open_n else "watch 끝. 열린 포지션이 없습니다.")
    return 0


def cmd_status(a, show, client=None):
    c = client or kis.Client()
    now = krx.parse_now(a.now)
    b = c.balance()
    show("한국투자증권 모의투자 잔고: 예수금 %s, 총평가 %s" % (krx.won(b["cash"]), krx.won(b["total"])))
    for x in b["positions"]:
        show("  %s %s %d주 (매도 가능 %d), 평균 %s, 현재 %s" % (x["symbol"], x.get("name") or "", x["qty"], x["sellable"],
                                                     p(x["avg"]), p(x["price"])))
    rows = c.orders(now.date())
    show("오늘 주문 %d건:" % len(rows))
    for r in rows:
        show("  %s %s %s %d주 (체결 %d, 남음 %d%s) 주문번호 %s" % (r.get("time") or "", "매수" if r["side"] == "buy" else "매도",
                                                         r.get("symbol"), r["qty"], r["filled"], r["remaining"],
                                                         ", 취소" if r.get("cancelled") else "", r.get("odno")))
    return 0


def cmd_flatten(a, show, client=None):
    now = krx.parse_now(a.now)
    state = load_state(a.state)
    flatten(client or kis.Client(), state, a.symbol, a.reply, now, show)
    save_state(state, a.state)
    return 0


def cmd_log(a, show):
    now = krx.parse_now(a.now)
    state = load_state(a.state)
    today = now.date().isoformat()
    rows = [r for r in state["orders"] if r.get("date") == today]
    show("오늘 보낸 주문 %d건, 승인 기록 %d건." % (len(rows), sum(1 for x in state["approvals"] if x["at"][:10] == today)))
    for r in rows:
        show("  %s %s %s %s주 %s 주문번호 %s" % (r["at"][11:16], r["kind"], r["side"], r["qty"], r["symbol"], r.get("odno")))
    for x in state["positions"]:
        if x.get("date") == today:
            show("  포지션 %s %s주: %s%s" % (x["symbol"], x.get("filled"), x["status"],
                                        " (%s)" % x["exit_reason"] if x.get("exit_reason") else ""))
    return 0


def main(argv=None, client=None):
    krx.utf8_stdio()
    ap = argparse.ArgumentParser(description="주문 실행 (trader-kr-broker): 한국투자증권 모의투자, 세 겹 승인 뒤에만.")
    sub = ap.add_subparsers(dest="cmd")

    def common(q, orders=True):
        if orders:
            q.add_argument("--orders", default="trader-kr/orders.json")
            q.add_argument("--risk", default="trader-kr/state/risk.json")
        q.add_argument("--state", default=STATE)
        q.add_argument("--config", default="trader-kr/config.json")
        q.add_argument("--now", help='이 한국 시간인 것처럼, 예: "2026-10-28 10:16"')

    common(sub.add_parser("show", help="주문표"))
    q = sub.add_parser("approve", help="사용자의 yes 를 글자 그대로 기록")
    common(q)
    q.add_argument("--index", type=int, required=True)
    q.add_argument("--reply", required=True, help="사용자가 채팅에 쓴 답 그대로")
    q = sub.add_parser("place", help="모의투자로 보내기 (세 겹 승인 뒤에만)")
    common(q)
    q.add_argument("--index", type=int, required=True)
    q.add_argument("--wait", type=float, default=20, help="체결을 기다리는 초 (기본 20)")
    q = sub.add_parser("watch", help="손절가·청산 시각 지키기")
    common(q, orders=False)
    q.add_argument("--every", type=float, default=5, help="몇 초마다 볼지 (기본 5)")
    q.add_argument("--until", help="이 시각(HH:MM)에 멈춤")
    q.add_argument("--once", action="store_true", help="한 번만 보고 끝")
    common(sub.add_parser("status", help="모의투자 잔고와 오늘 주문"), orders=False)
    q = sub.add_parser("flatten", help="보유분 시장가 매도 (사용자의 yes 필요)")
    common(q, orders=False)
    q.add_argument("symbol")
    q.add_argument("--reply", required=True, help="사용자가 채팅에 쓴 답 그대로")
    common(sub.add_parser("log", help="오늘 기록"), orders=False)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1

    def show(text):
        print(text, flush=True)

    try:
        if a.cmd == "show":
            return cmd_show(a, show)
        if a.cmd == "approve":
            return cmd_approve(a, show)
        if a.cmd == "place":
            return cmd_place(a, show, client)
        if a.cmd == "watch":
            return cmd_watch(a, show, client)
        if a.cmd == "status":
            return cmd_status(a, show, client)
        if a.cmd == "flatten":
            return cmd_flatten(a, show, client)
        return cmd_log(a, show)
    except (BrokerError, kis.KisError, krx.KrxError) as e:
        print("broker.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
