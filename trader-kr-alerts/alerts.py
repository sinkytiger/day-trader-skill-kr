#!/usr/bin/env python3
"""가격 알림 (trader-kr-alerts). 표준 라이브러리만 쓴다.

손절가, 목표가, 정리 시각, 지정한 가격, 기준가 대비 몇 % 움직임, 개장을 알린다. 알림은 정보일 뿐 주문이 아니다.
모의투자 주문의 손절과 정리는 broker.py watch 가 실제로 지킨다. 이 스킬은 토스증권 앱에서 직접 산 거래나 watch 를
켜지 않은 거래를 사람이 지켜보지 않아도 되게 한다.

    python alerts.py clock                                   # 지금 장 상태, 오늘 장 시간, 다음 개장
    python alerts.py add --from-plan                         # trader-kr/plan.json 의 손절, 목표, 정리 시각
    python alerts.py add --symbol 005930 --below 238000      # --above, --stop, --target 도
    python alerts.py add --symbol 005930 --pct 3 --direction down
    python alerts.py add --at 15:10 --label "정리 준비" --before-close
    python alerts.py add --opening-bell
    python alerts.py check                                   # 울린 것이 있으면 종료 코드 0, 없으면 1
    python alerts.py quote 005930 000660                     # 현재가와 기준가 대비
    python alerts.py list / reset 아이디 / remove 아이디

장 시간은 토스증권 장 운영 정보 (trader-kr/cache/calendar.json) 로 본다. 개장이 늦은 날 (수능일 등) 에는 정리 시각
같은 오후 알림을 --before-close 로 접속매매 끝에 맞춰 옮긴다. 기준가는 토스증권 상·하한가의 가운데 (상·하한가는 기준가
에서 같은 폭만큼 위아래라서) 이고, 보통 전일 종가다.

가격 알림은 정규장 (개장부터 마감까지) 에만 본다 (--any-time 이면 언제든). 시세는 토스증권 현재가이고 점검 사이에
가격이 수준을 지나갔다 돌아올 수 있다. 실시간 감시가 아니다.

파일: 규칙 trader-kr/alerts.json, 울린 기록과 그날 기준가 trader-kr/state/alerts.json.
토스증권 키: ~/.tossinvest/credentials.json 또는 환경변수. 키 값은 찍지 않는다.
"""

import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
from krcommon import data, krx, toss  # noqa: E402

RULES = os.path.join("trader-kr", "alerts.json")
STATE = os.path.join("trader-kr", "state", "alerts.json")
PLAN = os.path.join("trader-kr", "plan.json")
PRICE_TYPES = ("above", "below", "stop", "target")
TYPES = PRICE_TYPES + ("pct_move", "time", "opening_bell")
STALE_MIN = 15
KEEP_DAYS = 10
SAFETY = "교육과 도구일 뿐 투자 권유가 아닙니다 (not financial advice). 알림은 정보이고 이 스킬은 주문을 내지 않습니다."


class AlertError(Exception):
    pass


# ---------------------------------------------------------------- 파일

def load_rules(path):
    doc = krx.read_json(path, "알림 규칙", required=False)
    rules = doc.get("rules", []) if isinstance(doc, dict) else (doc or [])
    if not isinstance(rules, list):
        raise AlertError("%s 의 rules 가 목록이 아닙니다" % path)
    return rules


def save_rules(path, rules):
    krx.write_json(path, {"rules": rules})


def load_state(path):
    doc = krx.read_json(path, "알림 기록", required=False)
    doc = doc if isinstance(doc, dict) else {}
    return {"rang": dict(doc.get("rang") or {}), "base": dict(doc.get("base") or {})}


def save_state(path, state, today):
    cut = (today - datetime.timedelta(days=KEEP_DAYS)).isoformat()
    state["base"] = {d: v for d, v in state["base"].items() if d >= cut}
    krx.write_json(path, state)


def new_id(rules, base):
    have = {r["id"] for r in rules}
    rid, k = base, 2
    while rid in have:
        rid, k = "%s-%d" % (base, k), k + 1
    return rid


# ---------------------------------------------------------------- 장 시간

def status(now, session):
    """(상태, 설명)."""
    if session is None:
        return "closed", "휴장"
    if now < session.open_dt:
        return "pre", "장 전 (%s 개장)" % krx.hm(session.open_t)
    if now < session.auction_dt:
        return "open", "접속매매 중 (%s 까지)" % krx.hm(session.auction_t)
    if now < session.close_dt:
        return "auction", "종가 단일가 (%s 마감)" % krx.hm(session.close_t)
    return "after", "장 마감 (%s)" % krx.hm(session.close_t)


def next_open(now, cal):
    """다음 개장 (오늘 개장 전이면 오늘). 장 운영 정보에 없는 날은 평일 기본값."""
    for i in range(0, 15):
        d = now.date() + datetime.timedelta(days=i)
        s = cal.session(d)
        if s is not None and s.open_dt > now:
            return s
    return None


def at_time(rule, session):
    t = krx.parse_hhmm(rule["at"], "at")
    return session.shift(t) if (rule.get("before_close") and session is not None) else t


# ---------------------------------------------------------------- 시세

def quotes(symbols):
    """{종목: {"price", "ts"}}. 토스증권 현재가."""
    if not symbols:
        return {}
    try:
        rows = data.client().prices(sorted(symbols))
    except toss.TossError as e:
        raise AlertError("토스증권 시세를 받지 못했습니다: %s" % e)
    out = {}
    for q in rows:
        try:
            out[q["symbol"]] = {"price": float(q["lastPrice"]), "ts": q.get("timestamp")}
        except (KeyError, TypeError, ValueError):
            continue
    return out


def bases(symbols, day, state):
    """{종목: 기준가}. 상·하한가의 가운데. 그날 받은 것은 기록에서 다시 쓴다."""
    got = state["base"].setdefault(day.isoformat(), {})
    for s in sorted(symbols):
        if s in got:
            continue
        try:
            pl = data.client().price_limits(s) or {}
            lo, hi = float(pl.get("lowerLimitPrice")), float(pl.get("upperLimitPrice"))
        except (toss.TossError, TypeError, ValueError):
            continue
        if lo > 0 and hi > lo:
            got[s] = (lo + hi) / 2
    return got


def stale(q, now):
    t = krx.parse_iso(q.get("ts")) if q.get("ts") else None
    return t is not None and (now - t).total_seconds() > STALE_MIN * 60


# ---------------------------------------------------------------- 판정

def applies(rule, day):
    return rule.get("date") in (None, day.isoformat())


def crossed(rule, price):
    lv = float(rule["price"])
    return price >= lv if rule["type"] in ("above", "target") else price <= lv


def evaluate(rules, now, session, qts, base, state, any_time=False):
    """울릴 것 [(규칙, 말)]. state["rang"] 를 고친다. 가격 알림은 수준을 지날 때 한 번 (rearm 이면 되돌아간 뒤
    다시), 퍼센트·시각·개장 알림은 하루에 한 번."""
    today = now.date().isoformat()
    st, _ = status(now, session)
    live = any_time or st in ("open", "auction")
    out = []
    for r in rules:
        if not applies(r, now.date()):
            continue
        rid, rang = r["id"], state["rang"].get(r["id"])
        t = r["type"]
        if t in PRICE_TYPES:
            q = qts.get(r["symbol"])
            if not live or not q:
                continue
            p = q["price"]
            if rang and r.get("rearm") and not crossed(r, p):
                del state["rang"][rid]                       # 되돌아갔으니 다시 울릴 수 있다
                continue
            if rang or not crossed(r, p):
                continue
            out.append((r, price_msg(r, q, now)))
        elif t == "pct_move":
            q, b = qts.get(r["symbol"]), base.get(r["symbol"])
            if not live or not q or not b or (rang and rang.get("day") == today):
                continue
            mv = q["price"] / b - 1
            want = r.get("direction")
            if abs(mv) * 100 < float(r["pct"]) or (want == "up" and mv < 0) or (want == "down" and mv > 0):
                continue
            out.append((r, "%s%s 이 기준가 %s원 대비 %s 입니다 (현재가 %s원%s)." % (
                r["symbol"], " %s" % r["name"] if r.get("name") else "", krx.price_str(b), signed_pct(mv),
                krx.price_str(q["price"]), ", %s 시세" % hm_of(q) if q.get("ts") else "")))
        elif t == "time":
            if session is None or (rang and rang.get("day") == today):
                continue
            at = at_time(r, session)
            if now.time() < at:
                continue
            out.append((r, time_msg(r, at, session)))
        elif t == "opening_bell":
            if session is None or (rang and rang.get("day") == today) or not (session.open_dt <= now < session.close_dt):
                continue
            out.append((r, "개장: %s 에 접속매매가 시작됐습니다 (접속매매 %s ~ %s, 마감 %s%s)." % (
                krx.hm(session.open_t), krx.hm(session.open_t), krx.hm(session.auction_t), krx.hm(session.close_t),
                ", 기본 시간 (휴장 여부 미확인)" if session.source == "default" else "")))
    for r, _ in out:
        rec = {"at": now.strftime("%Y-%m-%d %H:%M:%S"), "day": today}
        q = qts.get(r.get("symbol"))
        if q:
            rec["price"] = q["price"]
        state["rang"][r["id"]] = rec
    return out


def signed_pct(x):
    return "%+.2f%%" % (x * 100)


def day_str(now):
    return "%s (%s) %s" % (now.date().isoformat(), "월화수목금토일"[now.weekday()], now.strftime("%H:%M"))


def hm_of(q):
    t = krx.parse_iso(q.get("ts"))
    return t.strftime("%H:%M") if t else "?"


def price_msg(r, q, now):
    who = "%s%s" % (r["symbol"], " %s" % r["name"] if r.get("name") else "")
    lv, p = krx.price_str(float(r["price"])), krx.price_str(q["price"])
    when = " (%s 시세%s)" % (hm_of(q), ", %d분 넘게 지난 시세" % STALE_MIN if stale(q, now) else "") if q.get("ts") else ""
    if r["type"] == "stop":
        return ("손절 %s: 현재가 %s원이 손절가 %s원 이하입니다%s. 계획한 손절가입니다. 모의투자 주문이면 broker.py watch 가 "
                "지킵니다. 직접 산 거래면 계획대로 할지 정하세요." % (who, p, lv, when))
    if r["type"] == "target":
        return ("목표 %s: 현재가 %s원이 목표가 %s원 이상입니다%s. 모의투자 주문이면 목표가 지정가 매도가 체결됐을 수 있으니 "
                "broker.py status 로 확인하세요." % (who, p, lv, when))
    word = "이상" if r["type"] == "above" else "이하"
    return "%s %s: 현재가 %s원이 %s원 %s입니다%s." % (r.get("label") or ("위로" if r["type"] == "above" else "아래로"),
                                                 who, p, lv, word, when)


def time_msg(r, at, session):
    label = r.get("label") or "시각"
    moved = " (개장이 늦은 날이라 %s 에서 옮김)" % r["at"] if krx.hm(at) != r["at"] else ""
    if r.get("flat"):
        return ("정리 시각 %s%s: %s 이 아직 열려 있으면 계획대로 정리할 시각입니다. 접속매매는 %s 에 끝납니다. 모의투자는 "
                "broker.py flatten 이 매도 주문을 만들고, 위험 점검과 사용자의 yes 를 거칩니다." % (
                    krx.hm(at), moved, r.get("symbol") or "단타", krx.hm(session.auction_t)))
    return "%s %s%s (접속매매는 %s 에 끝납니다)." % (label, krx.hm(at), moved, krx.hm(session.auction_t))


# ---------------------------------------------------------------- 규칙 만들기

def from_plan(path, rules):
    plan = krx.read_json(path, "청산 계획 (trader-kr-exit 를 먼저 돌리세요)")
    try:
        sym = krx.norm_symbol(plan.get("symbol"))
        stop, target, day = float(plan["stop"]), float(plan["target"]), plan["date"]
    except (KeyError, TypeError, ValueError, krx.KrxError):
        raise AlertError("%s 에 종목, 손절가, 목표가, 날짜가 없습니다" % path)
    flat = plan.get("flat_by") or krx.CONFIG_DEFAULTS["flat_by"]
    krx.parse_hhmm(flat, "flat_by")
    name = plan.get("name")
    # 같은 계획에서 만든 예전 규칙은 바꾼다 (계획을 다시 짜면 손절가가 바뀐다)
    keep = [r for r in rules if not (r.get("from_plan") and r.get("symbol") == sym and r.get("date") == day)]
    out = []
    for base, rule in (("stop", {"type": "stop", "price": stop}), ("target", {"type": "target", "price": target}),
                       ("flat-by", {"type": "time", "at": krx.hm(krx.parse_hhmm(flat, "flat_by")), "label": "정리 시각",
                                    "flat": True})):
        rule.update({"id": new_id(keep + out, "%s-plan-%s" % (sym, base)), "symbol": sym, "name": name, "date": day,
                     "from_plan": True})
        out.append(rule)
    return keep, out


def build(a, rules, today):
    """add 의 인자 -> (남길 규칙, 새 규칙들)."""
    if a.from_plan:
        return from_plan(a.plan, rules)
    if a.opening_bell:
        if any(r["type"] == "opening_bell" for r in rules):
            raise AlertError("개장 알림은 이미 있습니다")
        return rules, [{"id": "opening-bell", "type": "opening_bell", "label": "개장"}]
    day = today.isoformat() if a.today else None
    if a.at:
        at = krx.hm(krx.parse_hhmm(a.at, "--at"))
        sym = krx.norm_symbol(a.symbol) if a.symbol else None
        return rules, [{"id": new_id(rules, "time-%s" % at.replace(":", "")), "type": "time", "at": at,
                        "label": a.label or "시각", "before_close": a.before_close, "date": day, "symbol": sym}]
    if not a.symbol:
        raise AlertError("--symbol 을 주세요 (또는 --from-plan, --opening-bell, --at)")
    sym = krx.norm_symbol(a.symbol)
    kinds = [(k, getattr(a, k)) for k in PRICE_TYPES if getattr(a, k) is not None]
    if a.pct is not None:
        if a.pct <= 0 or a.pct > 30:
            raise AlertError("--pct 는 0 보다 크고 30 이하입니다 (가격제한폭 ±30%)")
        if kinds:
            raise AlertError("--pct 와 가격 수준은 따로 더하세요")
        return rules, [{"id": new_id(rules, "%s-pct%g%s" % (sym, a.pct, a.direction[0] if a.direction else "")),
                        "type": "pct_move", "symbol": sym, "pct": a.pct, "direction": a.direction, "date": day,
                        "label": a.label}]
    if len(kinds) != 1:
        raise AlertError("가격 알림은 --above, --below, --stop, --target 가운데 하나를 주세요")
    k, v = kinds[0]
    if v <= 0:
        raise AlertError("가격은 0 보다 커야 합니다")
    return rules, [{"id": new_id(rules, "%s-%s-%d" % (sym, k, int(v))), "type": k, "symbol": sym, "price": float(v),
                    "date": day, "rearm": a.rearm, "label": a.label}]


def describe(r):
    t = r["type"]
    who = "%s%s" % (r.get("symbol") or "", " %s" % r["name"] if r.get("name") else "")
    if t in PRICE_TYPES:
        what = {"above": "%s원 이상", "below": "%s원 이하", "stop": "손절가 %s원 이하", "target": "목표가 %s원 이상"}[t] % (
            krx.price_str(float(r["price"])))
    elif t == "pct_move":
        what = "기준가 대비 %s%g%% 이상" % ({"up": "+", "down": "-"}.get(r.get("direction"), "±"), r["pct"])
    elif t == "time":
        what = "%s %s%s" % (r.get("label") or "시각", r["at"], " (개장이 늦은 날은 옮김)" if r.get("before_close") else "")
    else:
        what = "개장 (거래일마다)"
    when = "%s 하루" % r["date"] if r.get("date") else "날마다" if t in ("time", "opening_bell", "pct_move") else "울릴 때까지"
    return "%-24s %s %s, %s%s" % (r["id"], who, what, when, ", 되돌아가면 다시" if r.get("rearm") else "")


# ---------------------------------------------------------------- 명령

def cmd_clock(a, now):
    cal = data.calendar_for(now.date(), online=not a.no_network)
    s = cal.session(now.date())
    st, text = status(now, s)
    L = ["%s 한국 시간, %s." % (day_str(now), text)]
    if s is not None:
        L.append("오늘 장: 접속매매 %s ~ %s, 종가 단일가 ~ %s%s." % (krx.hm(s.open_t), krx.hm(s.auction_t),
                                                         krx.hm(s.close_t), "" if s.standard else " (기본 시간과 다름)"))
    nx = next_open(now, cal) if st != "open" and st != "auction" else None
    if nx is not None:
        L.append("다음 개장: %s %s%s." % (nx.day.isoformat(), krx.hm(nx.open_t),
                                       " (장 운영 정보 없이 평일 기본값, 휴장 여부 미확인)" if nx.source == "default" else ""))
    if s is not None and s.source == "default":
        L.append(s.note)
    print("\n".join(L))
    return 0


def cmd_add(a, now):
    rules = load_rules(a.rules)
    keep, new = build(a, rules, now.date())
    save_rules(a.rules, keep + new)
    print("알림을 더했습니다 (%s):" % a.rules)
    for r in new:
        print("  " + describe(r))
    if len(keep) < len(rules):
        print("같은 계획에서 만든 예전 알림 %d개를 바꿨습니다." % (len(rules) - len(keep)))
    print("check 를 몇 분마다 돌려야 울립니다 (/loop 5m 이나 예약 작업). 알림은 주문이 아닙니다.")
    return 0


def cmd_check(a, now):
    rules = load_rules(a.rules)
    state = load_state(a.state)
    online = not a.no_network
    session = data.session_for(now.date(), online=online)
    st, text = status(now, session)
    todays = [r for r in rules if applies(r, now.date())]
    syms = {r["symbol"] for r in todays if r["type"] in PRICE_TYPES + ("pct_move",)}
    live = a.any_time or st in ("open", "auction")
    note = None
    try:
        qts = quotes(syms) if (syms and live and online) else {}
    except AlertError as e:                   # 시세가 없어도 시각·개장 알림은 본다
        qts, note = {}, str(e)
    base = bases({r["symbol"] for r in todays if r["type"] == "pct_move"}, now.date(), state) \
        if (live and online) else {}
    rings = evaluate(rules, now, session, qts, base, state, a.any_time)
    save_state(a.state, state, now.date())
    res = {"now": now.strftime("%Y-%m-%d %H:%M"), "status": st, "status_text": text,
           "rang": [{"id": r["id"], "type": r["type"], "symbol": r.get("symbol"), "message": m} for r, m in rings],
           "watched": len(todays), "quotes": qts}
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0 if rings else 1
    L = ["%s 한국 시간, %s." % (day_str(now), text)]
    for r, m in rings:
        L.append("ALERT " + m)
    if not rings:
        quoted = ", ".join("%s %s원" % (s, krx.price_str(q["price"])) for s, q in sorted(qts.items()))
        L.append("울린 것 없음. 오늘 보는 알림 %d개%s." % (len(todays), ", " + quoted if quoted else ""))
    if syms and not live:
        L.append("정규장 밖이라 가격 알림은 보지 않았습니다 (--any-time 이면 봅니다).")
    missing = sorted(s for s in syms if live and online and s not in qts)
    if note:
        L.append(note)
    elif missing:
        L.append("시세를 받지 못한 종목: %s" % ", ".join(missing))
    L.append(SAFETY)
    print("\n".join(L))
    return 0 if rings else 1


def cmd_quote(a, now):
    syms = [krx.norm_symbol(s) for s in a.symbols]
    state = load_state(a.state)
    qts = quotes(set(syms))
    base = bases(set(syms), now.date(), state)
    save_state(a.state, state, now.date())
    out = {s: dict(qts.get(s) or {}, base=base.get(s)) for s in syms}
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    for s in syms:
        q = qts.get(s)
        if not q:
            print("%s: 시세 없음" % s)
            continue
        b = base.get(s)
        print("%s %s원 (%s 시세)%s" % (s, krx.price_str(q["price"]), hm_of(q) if q.get("ts") else "?",
                                    ", 기준가 %s원 대비 %s" % (krx.price_str(b), signed_pct(q["price"] / b - 1)) if b else ""))
    print("토스증권 현재가입니다. 정규장 밖에는 넥스트레이드 시세가 섞일 수 있습니다.")
    return 0


def cmd_list(a, now):
    rules = load_rules(a.rules)
    state = load_state(a.state)
    if not rules:
        print("알림이 없습니다. add 로 더하세요.")
        return 0
    for r in rules:
        rang = state["rang"].get(r["id"])
        print("  %s%s" % (describe(r), "  [울림 %s]" % rang["at"] if rang else ""))
    return 0


def cmd_reset(a, now, remove=False):
    rules = load_rules(a.rules)
    state = load_state(a.state)
    ids = {r["id"] for r in rules}
    if a.id not in ids:
        raise AlertError("%s 라는 알림이 없습니다 (list 로 보세요)" % a.id)
    state["rang"].pop(a.id, None)
    save_state(a.state, state, now.date())
    if remove:
        save_rules(a.rules, [r for r in rules if r["id"] != a.id])
        print("%s 알림을 지웠습니다." % a.id)
    else:
        print("%s 알림을 다시 울릴 수 있게 했습니다." % a.id)
    return 0


def main(argv=None):
    krx.utf8_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    cmds = ("clock", "add", "check", "quote", "list", "reset", "remove")
    cmd = argv.pop(0) if argv and argv[0] in cmds else "check"
    ap = argparse.ArgumentParser(prog="alerts.py %s" % cmd, description="가격 알림 (trader-kr-alerts).")
    ap.add_argument("--rules", default=RULES)
    ap.add_argument("--state", default=STATE)
    ap.add_argument("--now", help='이 한국 시간인 것처럼, 예: "2026-11-23 15:15"')
    ap.add_argument("--no-network", action="store_true", help="토스증권을 부르지 않는다 (장 시간은 캐시나 평일 기본값)")
    ap.add_argument("--json", action="store_true")
    if cmd == "add":
        ap.add_argument("--from-plan", action="store_true", help="청산 계획의 손절, 목표, 정리 시각")
        ap.add_argument("--plan", default=PLAN)
        ap.add_argument("--opening-bell", action="store_true")
        ap.add_argument("--symbol")
        for k in PRICE_TYPES:
            ap.add_argument("--" + k, type=float)
        ap.add_argument("--pct", type=float, help="기준가 대비 이만큼 (%%) 움직이면")
        ap.add_argument("--direction", choices=("up", "down"))
        ap.add_argument("--at", help="이 시각 (HH:MM, 한국 시간)")
        ap.add_argument("--before-close", action="store_true", help="개장이 늦은 날 마감에 맞춰 옮긴다")
        ap.add_argument("--label")
        ap.add_argument("--today", action="store_true", help="오늘만")
        ap.add_argument("--rearm", action="store_true", help="수준 밖으로 되돌아가면 다시 울린다")
    elif cmd == "check":
        ap.add_argument("--any-time", action="store_true", help="정규장 밖에도 가격 알림을 본다")
    elif cmd == "quote":
        ap.add_argument("symbols", nargs="+")
    elif cmd in ("reset", "remove"):
        ap.add_argument("id")
    a = ap.parse_args(argv)
    try:
        now = krx.parse_now(a.now)
        if cmd == "clock":
            return cmd_clock(a, now)
        if cmd == "add":
            return cmd_add(a, now)
        if cmd == "quote":
            return cmd_quote(a, now)
        if cmd == "list":
            return cmd_list(a, now)
        if cmd in ("reset", "remove"):
            return cmd_reset(a, now, remove=(cmd == "remove"))
        return cmd_check(a, now)
    except (AlertError, krx.KrxError, toss.TossError) as e:
        print("오류: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
