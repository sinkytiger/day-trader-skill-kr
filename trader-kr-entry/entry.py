#!/usr/bin/env python3
"""진입 타이머 (trader-kr-entry): 셋업을 봉 단위로 지켜보다 들어갈 순간을 알려 준다.
표준 라이브러리만 쓴다. 매수만 본다. 주문은 절대 내지 않는다.

    python entry.py check 005930                          # 시가 범위 돌파, 5분봉
    python entry.py check 005930 --now "2026-10-28 10:15" --offline 005930.json
    python entry.py check 005930 --setup level --level 72000   # 이 가격 위 첫 종가
    python entry.py check 005930 --interval 1m --min-volume-x 1.5 --write
    python entry.py watch 005930 --every 30 --until 14:50  # 봉이 끝날 때마다 한 줄
    python entry.py watchlist                             # trader-kr/watchlist.json 의 종목 전부

셋업:
  orb    시가 범위 돌파. 범위는 개장 뒤 --or-minutes 분(기본 15분: 09:00, 09:05,
         09:10 5분봉)의 고가와 저가. 범위가 끝난 뒤 처음으로 범위 고가 "위에서
         끝난" 완성 봉이 신호다. 꼬리만 넘은 봉은 신호가 아니다.
  level  --level 가격 위에서 끝난 첫 완성 봉 (예: 스캐너의 20일 신고가).
  --min-volume-x X  신호 봉 거래량이 시가 범위 봉 평균의 X 배 이상이어야 한다.
         09:00 봉에는 동시호가 체결이 들어 있어서 1.0 도 이미 높은 기준이다.

신호 시각은 신호 봉이 끝나는 시각(10:10 봉이면 10:15), 가격은 그 봉 종가다.
봉은 안의 1분봉이 다 끝나야 센다. 접속매매(09:00~15:20) 봉만 본다.

상태: triggered(신호), waiting(개장 전, 범위 형성 중, 아직 돌파 없음),
expired(no_entries_after 까지 신호 없음, 기본 14:50), no-data(휴장 또는 자료 없음).
개장이 늦은 날(신년 첫 거래일, 수능일)은 토스증권 장 운영 정보로 시간을 옮긴다.

trader-kr/config.json (선택): "no_entries_before"(09:15), "no_entries_after"(14:50).

자료: 토스증권 Open API 1분봉 (KRX+NXT 통합 시세). --offline PATH 는 저장된
분봉 파일(.json/.json.gz) 또는 <종목>.json 이 든 폴더를 읽는다. --now 없이
오프라인 파일을 읽으면 파일의 마지막 봉 시각을 지금으로 본다.

종료 코드: check 0 = 신호, 1 = 그 밖. watch 0 = 신호, 1 = 그 밖.
"""

import argparse
import datetime
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
from krcommon import bars as kbars, data, krx  # noqa: E402

INTERVALS = {"1m": 60, "5m": 300}
DEFAULTS = {"no_entries_before": krx.CONFIG_DEFAULTS["no_entries_before"],
            "no_entries_after": krx.CONFIG_DEFAULTS["no_entries_after"]}
GRACE = 10       # 봉이 끝나고 몇 초 뒤에 묻는다 (자료가 올라올 시간)
PAUSE = 0.2


class EntryError(Exception):
    pass


def read_config(path):
    try:
        doc = krx.read_config(path, DEFAULTS)
    except krx.KrxError as e:
        raise EntryError(str(e))
    cfg = {k: doc.get(k) or DEFAULTS[k] for k in DEFAULTS}
    for k in DEFAULTS:
        try:
            krx.parse_hhmm(cfg[k], k)
        except krx.KrxError as e:
            raise EntryError(str(e))
    return cfg


def p(x):
    return krx.price_str(x)


def evaluate(chart, now_kst, session, setup="orb", or_minutes=15, interval="5m", level=None,
             min_volume_x=None, cfg=None):
    """순수 함수: now_kst 시점의 셋업 상태. session 은 그날 krx.Session 또는 None(휴장)."""
    cfg = cfg or dict(DEFAULTS)
    if interval not in INTERVALS:
        raise EntryError("--interval 은 5m 또는 1m 입니다")
    step = INTERVALS[interval]
    if or_minutes <= 0 or (or_minutes * 60) % step:
        raise EntryError("--or-minutes 는 %s 봉의 정수배여야 합니다" % interval)
    if setup not in ("orb", "level"):
        raise EntryError("--setup 은 orb 또는 level 입니다")
    if setup == "level" and level is None:
        raise EntryError("--setup level 은 --level 가격이 필요합니다 (예: 20일 신고가)")
    n_or = or_minutes * 60 // step
    day = now_kst.date()
    res = {"symbol": chart["symbol"], "name": chart.get("name"), "source": chart.get("source") or "toss",
           "setup": setup, "side": "buy",
           "date": day.isoformat(), "interval": interval, "or_minutes": or_minutes,
           "now": now_kst.strftime("%Y-%m-%d %H:%M:%S"), "status": "waiting", "phase": "", "reason": "",
           "opening_range": None, "level": None, "trigger": None, "last": None, "forming": None,
           "min_volume_x": min_volume_x, "_bars": []}
    if chart.get("data_time"):
        res["data_time"] = krx.kst_of(chart["data_time"]).strftime("%Y-%m-%d %H:%M:%S")
    if session is None:
        res.update(status="no-data", phase="closed", reason="오늘은 휴장입니다 (주말 또는 휴장일)")
        return res
    res["session"] = session.to_doc()
    open_dt, auction_dt = session.open_dt, session.auction_dt
    before = datetime.datetime.combine(day, session.shift(krx.parse_hhmm(cfg["no_entries_before"], "no_entries_before")))
    cutoff = datetime.datetime.combine(day, session.shift(krx.parse_hhmm(cfg["no_entries_after"], "no_entries_after")))
    cutoff = min(cutoff, auction_dt)
    or_end = open_dt + datetime.timedelta(minutes=or_minutes)
    res.update(opens=krx.hm(session.open_t), continuous_until=krx.hm(session.auction_t),
               no_entries_before=krx.hm(before.time()), no_entries_after=krx.hm(cutoff.time()))

    day_bars = kbars.split_days(chart["bars"]).get(day, [])
    slots = kbars.session_slots(day_bars, session, step, now_kst)
    done = [b for b in slots if b["complete"]]
    part = [b for b in slots if not b["complete"]]
    if part and part[-1]["kst"] <= now_kst:
        f = part[-1]
        res["forming"] = {"bar_start": krx.hm(f["kst"].time()), "closes": krx.hm(f["end"].time()), "price": f["c"],
                          "minutes": f["n"]}
    res["_bars"] = done
    res["bars_closed"] = len(done)

    if now_kst < open_dt:
        res.update(phase="pre-open", reason="정규장은 %s 에 열립니다%s" % (
            krx.hm(session.open_t), " (시가 범위는 %s 에 정해집니다)" % krx.hm(or_end.time()) if setup == "orb" else ""))
        return res
    or_bars = [b for b in done if b["slot"] < n_or]
    avg_vol = (sum(b["v"] for b in or_bars) / float(len(or_bars))) if or_bars else None
    if or_bars:
        res["opening_range"] = {"high": max(b["h"] for b in or_bars), "low": min(b["l"] for b in or_bars),
                                "minutes": or_minutes, "bars": len(or_bars), "of": n_or,
                                "complete": now_kst >= or_end and len(or_bars) == n_or,
                                "avg_volume": int(round(avg_vol))}
    if setup == "orb":
        if now_kst < or_end:
            res.update(phase="forming", reason="시가 범위 봉 %d / %d 개 완성. 범위는 %s 에 정해집니다" % (
                len(or_bars), n_or, krx.hm(or_end.time())))
            return res
        if len(or_bars) < n_or:
            res.update(status="no-data", phase="open", reason="시가 범위 봉이 비어 있습니다 (%d / %d)" % (len(or_bars), n_or))
            return res
        lvl = max(b["h"] for b in or_bars)
    else:
        lvl = float(level)
    res["level"] = lvl

    first_slot = n_or if setup == "orb" else 0
    trig, thin = None, []
    for b in done:
        if b["slot"] < first_slot or b["end"] < before or b["end"] > cutoff:
            continue
        if b["c"] > lvl:
            vx = (b["v"] / avg_vol) if avg_vol else None
            if min_volume_x and (vx is None or vx < min_volume_x):
                thin.append(b)
                continue
            trig = b
            break
    last = done[-1] if done else None
    if last:
        res["last"] = {"bar_start": krx.hm(last["kst"].time()), "close": last["c"],
                       "vs_level_pct": round(100.0 * (last["c"] / lvl - 1), 2)}
    if trig:
        vx = (trig["v"] / avg_vol) if avg_vol else None
        res.update(status="triggered", phase="open" if now_kst < auction_dt else "after-close",
                   reason="%s 봉이 %s 위에서 끝났습니다" % (krx.hm(trig["kst"].time()),
                                                     "시가 범위 고가" if setup == "orb" else "기준 가격"))
        res["trigger"] = {"time": krx.hm(trig["end"].time()), "bar_start": krx.hm(trig["kst"].time()),
                          "bar_index": trig["slot"], "price": trig["c"], "volume": trig["v"],
                          "volume_x": round(vx, 2) if vx is not None else None,
                          "pct_over": round(100.0 * (trig["c"] / lvl - 1), 2)}
        res["minutes_since"] = int((now_kst - trig["end"]).total_seconds() // 60)
        return res
    if not done:
        res.update(status="no-data", phase="open", reason="%s 의 완성된 봉이 아직 없습니다" % day.isoformat())
        return res
    what = ("시가 범위 고가 %s 위 종가" % p(lvl)) if setup == "orb" else ("%s 위 종가" % p(lvl))
    if now_kst >= cutoff and not (part and part[-1]["end"] <= cutoff):
        res.update(status="expired", phase="after-close" if now_kst >= auction_dt else "after-cutoff",
                   reason="%s 까지 %s 가 없었습니다 (새 진입 마감)" % (krx.hm(cutoff.time()), what))
        return res
    res["phase"] = "open"
    if last["c"] > lvl and last in thin:
        res["reason"] = "%s 봉이 %s 에 위에서 끝났지만 거래량이 범위 평균의 %.2f배로 기준 %.2f배보다 적습니다" % (
            krx.hm(last["kst"].time()), p(last["c"]), last["v"] / avg_vol if avg_vol else 0, min_volume_x)
    elif last["c"] > lvl and last["end"] < before:
        res["reason"] = "%s 봉이 %s 에 위에서 끝났지만 %s 전에는 진입하지 않습니다 (설정)" % (
            krx.hm(last["kst"].time()), p(last["c"]), krx.hm(before.time()))
    else:
        gap = lvl - last["c"]
        res["reason"] = "마지막 종가 %s (%s 봉), %s 까지 %s %s (%+.2f%%)" % (
            p(last["c"]), krx.hm(last["kst"].time()), "범위 고가 %s" % p(lvl) if setup == "orb" else "기준 %s" % p(lvl),
            p(abs(gap)), "남음" if gap >= 0 else "넘음", 100.0 * (last["c"] / lvl - 1))
    return res


def public(res):
    return {k: v for k, v in res.items() if not k.startswith("_")}


def entry_doc(res):
    """trader-kr/entry.json (다음 스킬이 읽는 형식)."""
    t = res.get("trigger") or {}
    orr = res.get("opening_range")
    return {"created_by": "trader-kr-entry", "symbol": res["symbol"], "name": res.get("name"), "setup": res["setup"],
            "side": "buy", "date": res["date"], "status": res["status"], "time": t.get("time"),
            "bar_start": t.get("bar_start"), "bar_index": t.get("bar_index"), "price": t.get("price"),
            "level": res.get("level"), "interval": res["interval"],
            "opening_range": {"high": orr["high"], "low": orr["low"], "minutes": orr["minutes"]} if orr else None,
            "session": res.get("session"), "source": res.get("source", "toss"), "checked_at": res["now"], "reason": res.get("reason")}


STATUS_WORD = {"triggered": "신호", "waiting": "대기", "expired": "만료", "no-data": "자료 없음"}
PHASE_WORD = {"pre-open": "개장 전", "forming": "범위 형성 중", "closed": "휴장"}


def describe(res):
    when = datetime.datetime.strptime(res["now"], "%Y-%m-%d %H:%M:%S")
    setup = ("시가 범위 돌파 (%d분 범위)" % res["or_minutes"]) if res["setup"] == "orb" else (
        "%s 위 종가" % p(res["level"]) if res.get("level") is not None else "기준 가격 위 종가")
    head = "%s%s  %s %s  %s, %s봉 (%s)" % (res["symbol"], (" " + res["name"]) if res.get("name") else "",
                                          when.strftime("%Y-%m-%d"), when.strftime("%H:%M"), setup,
                                          "5분" if res["interval"] == "5m" else "1분",
                                          "저장된 파일" if res.get("source") == "offline" else "토스증권")
    out = [head]
    s = res.get("session") or {}
    if s.get("source") == "default" and res["status"] != "no-data":
        out.append("참고: " + s.get("note", ""))
    elif s and (s.get("open") != "09:00" or s.get("auction") != "15:20"):
        out.append("오늘 장 시간이 다릅니다: %s 개장, 접속매매 %s 까지." % (s["open"], s["auction"]))
    orr = res.get("opening_range")
    if orr:
        out.append("시가 범위 %s~%s: 고가 %s, 저가 %s (%d / %d 봉%s)." % (
            res["opens"], (datetime.datetime.strptime(res["opens"], "%H:%M") +
                           datetime.timedelta(minutes=orr["minutes"])).strftime("%H:%M"),
            p(orr["high"]), p(orr["low"]), orr["bars"], orr["of"], "" if orr["complete"] else ", 형성 중"))
    t = res.get("trigger")
    if t:
        out.append("신호 %s, %s: %s 봉(%d번째)이 %s %s 위에서 끝났습니다 (%+.2f%%%s)." % (
            t["time"], p(t["price"]), t["bar_start"], t["bar_index"],
            "범위 고가" if res["setup"] == "orb" else "기준 가격", p(res["level"]), t["pct_over"],
            (", 거래량 범위 평균의 %.2f배" % t["volume_x"]) if res.get("min_volume_x") and t.get("volume_x") else ""))
        step = INTERVALS[res["interval"]] // 60
        if res.get("minutes_since", 0) > step and res.get("last"):
            out.append("%d분 전 신호입니다. 마지막 종가는 %s (%s 봉). 늦은 진입은 다른 거래입니다: 가격, "
                       "손절 거리, 확률이 다 바뀌었습니다." % (res["minutes_since"], p(res["last"]["close"]),
                                                     res["last"]["bar_start"]))
            out.append("주문은 내지 않았습니다.")
        else:
            out.append("들어간다면 지금입니다. 주문은 내지 않았습니다.")
        out.append("다음: 사기 전에 손절가와 목표가부터 정하세요: /trader-kr-exit (exit.py plan --write).")
    else:
        label = PHASE_WORD.get(res["phase"]) or STATUS_WORD.get(res["status"], res["status"])
        out.append("%s  %s." % (label, res["reason"]))
        f = res.get("forming")
        if f and res["status"] == "waiting" and res["phase"] in ("open", "forming"):
            out.append("%s 봉은 아직 만들어지는 중입니다 (지금까지 %s, %s 에 끝남). 끝나야 셉니다." % (
                f["bar_start"], p(f["price"]), f["closes"]))
        if res["status"] == "expired":
            out.append("오늘 이 셋업으로는 진입하지 않습니다. 주문은 내지 않았습니다.")
    return out


def bar_line(b, res):
    orr = res.get("opening_range") or {}
    lvl = res.get("level")
    t = res.get("trigger")
    n_or = orr.get("of") or 0
    start = krx.hm(b["kst"].time())
    if res["setup"] == "orb" and b["slot"] < n_or:
        seen = [x for x in res["_bars"] if x["slot"] <= b["slot"]]
        note = "범위 지금까지 %s ~ %s" % (p(min(x["l"] for x in seen)), p(max(x["h"] for x in seen)))
        if b["slot"] == n_or - 1 and orr:
            note = "시가 범위 확정: 고가 %s, 저가 %s" % (p(orr["high"]), p(orr["low"]))
    elif t and t["bar_index"] == b["slot"]:
        note = "신호: %s 위에서 끝남 (%+.2f%%). 들어간다면 지금: %s, %s" % (p(lvl), t["pct_over"], t["time"], p(t["price"]))
    elif lvl is None:
        note = ""
    elif b["c"] > lvl:
        note = "%s 위" % p(lvl)
    else:
        note = "%s 아래 %s" % (p(lvl), p(lvl - b["c"]))
    return "%s 봉  종가 %s  %s" % (start, p(b["c"]), note)


class SimClock(object):
    """잠잘 때만 움직이는 시계. 하루를 기다리지 않고 다시 돌린다."""

    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += datetime.timedelta(seconds=max(1, int(round(seconds))))


def watch_loop(provider, session_of, clock, sleep, emit, opts, every=30, until=None, max_errors=5, on_trigger=None):
    """봉이 끝날 때마다 확인한다. 신호면 0, 만료·마감·--until 이면 1.
    provider(now_kst) -> chart, session_of(day) -> Session 또는 None."""
    step = INTERVALS[opts.get("interval", "5m")]
    printed, errors, said_pre, first = -1, 0, False, True
    next_fetch = None
    while True:
        now = clock()
        if next_fetch is not None and now < next_fetch:
            sleep(max(1, min(every, (next_fetch - now).total_seconds())))
            continue
        session = session_of(now.date())
        if session is None:
            emit("%s: 오늘은 휴장입니다. 지켜볼 것이 없습니다." % now.date().isoformat())
            return 1, None
        first_close = session.open_dt + datetime.timedelta(seconds=step)
        if now < first_close:
            if not said_pre:
                emit("%s  %s: 첫 봉이 끝나는 %s 부터 봅니다." % (now.strftime("%H:%M"),
                                                       "개장 전" if now < session.open_dt else "개장, 아직 완성 봉 없음",
                                                       krx.hm(first_close.time())))
                said_pre = True
            next_fetch = first_close + datetime.timedelta(seconds=GRACE)
            continue
        try:
            chart = provider(now)
            errors = 0
        except (EntryError, data.DataError) as e:
            errors += 1
            emit("%s  봉을 읽지 못했습니다: %s" % (now.strftime("%H:%M:%S"), e))
            if errors >= max_errors:
                emit("%d번 연속 실패해서 멈춥니다." % errors)
                return 1, None
            next_fetch = now + datetime.timedelta(seconds=every)
            continue
        res = evaluate(chart, now, session, **opts)
        done = res["_bars"]
        lines = describe(res)
        fresh = first
        if first:
            emit(lines[0])
            if done and not res.get("trigger"):
                emit(bar_line(done[-1], res))
            printed = done[-1]["slot"] if done else -1
            first = False
        else:
            for b in done:
                if b["slot"] > printed:
                    emit(bar_line(b, res))
                    printed = b["slot"]
        if res["status"] == "triggered":
            for line in lines[1:]:
                if line.startswith("시가 범위") or (line.startswith("신호") and not fresh):
                    continue
                emit(line)
            if on_trigger:
                on_trigger(res)
            return 0, res
        if res["status"] == "expired" or now >= session.auction_dt:
            emit("만료  %s." % (res["reason"] or "신호 없이 접속매매가 끝났습니다"))
            return 1, res
        if until and now >= until:
            emit("--until %s 에 신호 없이 멈춥니다. %s." % (krx.hm(until.time()), res["reason"]))
            return 1, res
        cur = int((now - session.open_dt).total_seconds()) // step
        nxt = session.open_dt + datetime.timedelta(seconds=(cur + 1) * step + GRACE)
        behind = (not done) or done[-1]["slot"] < cur - 1
        wait = every if behind else (nxt - now).total_seconds()
        cutoff = datetime.datetime.combine(now.date(), krx.parse_hhmm(res.get("no_entries_after") or "14:50", "cutoff"))
        for stop_at in (until, cutoff, session.auction_dt):
            if stop_at and stop_at > now:
                wait = min(wait, (stop_at - now).total_seconds())
        next_fetch = now + datetime.timedelta(seconds=max(1, wait))


# ---------------------------------------------------------------- 명령

def _opts(a, cfg):
    if a.side in ("sell", "short"):
        raise EntryError("매수만 봅니다. 공매도 진입은 없습니다.")
    if a.setup == "orb" and a.level is not None:
        raise EntryError("--level 은 --setup level 에서만 씁니다 (orb 의 기준은 범위 고가)")
    if a.min_volume_x is not None and a.min_volume_x <= 0:
        raise EntryError("--min-volume-x 는 0보다 커야 합니다")
    if a.level is not None and a.level <= 0:
        raise EntryError("--level 은 0보다 큰 가격이어야 합니다")
    return {"setup": a.setup, "or_minutes": a.or_minutes, "interval": a.interval, "level": a.level,
            "min_volume_x": a.min_volume_x, "cfg": cfg}


def _session(a, day):
    return data.session_for(day, online=not a.offline)


def _load(a, sym, now):
    """(chart, now). 오프라인 파일에 --now 가 없으면 파일의 마지막 봉 시각이 지금이다."""
    if a.offline and now is None:
        ch = data.load_chart(sym, datetime.datetime(2000, 1, 1), datetime.datetime(2100, 1, 1), offline=a.offline)
        if not ch.get("data_time"):
            raise EntryError("오프라인 파일에 봉이 없습니다")
        return ch, krx.kst_of(ch["data_time"])
    now = now or krx.kst_now()
    start = datetime.datetime.combine(now.date(), datetime.time(9, 0)) - datetime.timedelta(minutes=1)
    try:
        ch = data.load_chart(sym, start, now, offline=a.offline, now_kst=now)
    except data.DataError as e:
        raise EntryError(str(e))
    return ch, now


def _sym(text):
    try:
        return krx.norm_symbol(text)
    except krx.KrxError as e:
        raise EntryError(str(e))


def cmd_check(a):
    cfg = read_config(a.config)
    opts = _opts(a, cfg)
    sym = _sym(a.symbol)
    chart, now = _load(a, sym, krx.parse_now(a.now) if a.now else None)
    res = evaluate(chart, now, _session(a, now.date()), **opts)
    if a.write:
        krx.write_json(a.write, entry_doc(res))
    if a.json:
        print(json.dumps(public(res), ensure_ascii=False, indent=2, default=str))
    else:
        for line in describe(res):
            print(line)
        if a.write:
            print("%s 에 썼습니다 (상태 %s)." % (a.write, res["status"]))
    return 0 if res["status"] == "triggered" else 1


def cmd_watch(a):
    cfg = read_config(a.config)
    opts = _opts(a, cfg)
    sym = _sym(a.symbol)
    if a.every <= 0:
        raise EntryError("--every 는 0초보다 커야 합니다")
    start = krx.parse_now(a.now) if a.now else None
    if a.offline or start is not None:
        if start is None:
            start = _load(a, sym, None)[1]
        clock = SimClock(start)
        now_fn, sleep_fn = clock.now, clock.sleep
    else:
        now_fn, sleep_fn = krx.kst_now, time.sleep

    def provider(now):
        return _load(a, sym, now)[0]

    until = None
    if a.until:
        until = datetime.datetime.combine(now_fn().date(), krx.parse_hhmm(a.until, "--until"))

    def on_trigger(res):
        krx.write_json(a.out, entry_doc(res))
        print("%s 에 썼습니다." % a.out, flush=True)

    def emit(line):
        print(line, flush=True)

    code, _ = watch_loop(provider, lambda d: _session(a, d), now_fn, sleep_fn, emit, opts, every=a.every,
                         until=until, on_trigger=on_trigger)
    return code


def cmd_watchlist(a):
    cfg = read_config(a.config)
    opts = _opts(a, cfg)
    try:
        doc = krx.read_json(a.watchlist, "관심 목록 (/trader-kr-scanner 가 씁니다)")
    except krx.KrxError as e:
        raise EntryError(str(e))
    hits = [h for h in (doc.get("hits") or []) if isinstance(h, dict) and h.get("symbol")]
    if not hits:
        raise EntryError("%s 에 종목이 없습니다" % a.watchlist)
    if a.offline and not os.path.isdir(a.offline):
        raise EntryError("watchlist --offline 은 <종목>.json 파일이 든 폴더여야 합니다")
    now = krx.parse_now(a.now) if a.now else None
    rows = []
    for i, h in enumerate(hits):
        o = dict(opts)
        try:
            sym = _sym(h["symbol"])
            if a.setup == "level" and o["level"] is None:
                o["level"] = h.get("level")
                if o["level"] is None:
                    raise EntryError("기준 가격이 필요합니다: --level 또는 관심 목록의 \"level\"")
            if i and not a.offline:
                time.sleep(PAUSE)
            chart, when = _load(a, sym, now)
            if h.get("name"):
                chart["name"] = h["name"]
            rows.append((sym, evaluate(chart, when, _session(a, when.date()), **o), None))
        except (EntryError, data.DataError) as e:
            rows.append((str(h.get("symbol")), None, str(e)))
    if a.json:
        print(json.dumps([{"symbol": s, "result": public(r) if r else None, "error": e} for s, r, e in rows],
                         ensure_ascii=False, indent=2, default=str))
    else:
        print("%s %s %s %-10s %-10s %-10s %s" % (krx.pad("종목", 7), krx.pad("상태", 12), krx.pad("시각", 6), "가격",
                                                 "기준", "마지막", "기준 대비"))
        for s, r, e in rows:
            if e:
                print("%-7s 오류: %s" % (s, e))
                continue
            t = r.get("trigger") or {}
            last = r.get("last") or {}
            st = "신호" if r["status"] == "triggered" else (PHASE_WORD.get(r["phase"]) or STATUS_WORD.get(r["status"]))
            print("%-7s %s %-6s %-10s %-10s %-10s %s" % (
                s, krx.pad(st, 12), t.get("time") or "-", p(t["price"]) if t else "-",
                p(r["level"]) if r.get("level") is not None else "-", p(last["close"]) if last else "-",
                ("%+.2f%%" % last["vs_level_pct"]) if last.get("vs_level_pct") is not None else "-"))
        n = sum(1 for _, r, _ in rows if r and r["status"] == "triggered")
        print("%d / %d 종목 신호. 하나를 골라 entry.py check <종목> --write, 그다음 /trader-kr-exit." % (n, len(rows)))
        print("주문은 내지 않았습니다.")
    return 0 if any(r and r["status"] == "triggered" for _, r, _ in rows) else 1


def main(argv=None):
    krx.utf8_stdio()
    ap = argparse.ArgumentParser(description="진입 타이머 (trader-kr-entry): 셋업을 봉 단위로 지켜본다.")
    sub = ap.add_subparsers(dest="cmd")

    def common(q):
        q.add_argument("--setup", choices=["orb", "level"], default="orb")
        q.add_argument("--or-minutes", type=int, default=15, help="시가 범위 길이 (기본 15분)")
        q.add_argument("--interval", choices=["5m", "1m"], default="5m")
        q.add_argument("--level", type=float, help="이 가격 위 종가 (--setup level)")
        q.add_argument("--min-volume-x", type=float, help="신호 봉 거래량 / 범위 봉 평균")
        q.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"], help="매수만")
        q.add_argument("--now", help='이 한국 시간인 것처럼, 예: "2026-10-28 10:15"')
        q.add_argument("--offline", metavar="PATH", help="저장된 1분봉 파일 또는 폴더")
        q.add_argument("--config", default="trader-kr/config.json")

    q = sub.add_parser("check", help="지금 셋업 상태")
    q.add_argument("symbol")
    common(q)
    q.add_argument("--write", nargs="?", const="trader-kr/entry.json", metavar="PATH", help="결과 저장 (기본 trader-kr/entry.json)")
    q.add_argument("--json", action="store_true")
    q = sub.add_parser("watch", help="신호나 만료까지 봉마다 확인")
    q.add_argument("symbol")
    common(q)
    q.add_argument("--every", type=float, default=30, help="자료가 늦을 때 다시 묻는 간격(초)")
    q.add_argument("--until", help="이 시각(HH:MM)에 멈춤")
    q.add_argument("--out", default="trader-kr/entry.json")
    q = sub.add_parser("watchlist", help="trader-kr/watchlist.json 의 종목 전부")
    common(q)
    q.add_argument("--watchlist", default="trader-kr/watchlist.json")
    q.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"check": cmd_check, "watch": cmd_watch, "watchlist": cmd_watchlist}[a.cmd](a)
    except (EntryError, krx.KrxError, data.DataError) as e:
        print("entry.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("entry.py: 멈췄습니다.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
