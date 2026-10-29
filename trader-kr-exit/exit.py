#!/usr/bin/env python3
"""청산 계획 (trader-kr-exit): 들어가기 전에 손절가와 목표가를 정한다.
표준 라이브러리만 쓴다. 매수만 본다. 주문은 절대 내지 않는다.

    python exit.py plan                                     # trader-kr/entry.json 에서: 범위 중간 손절, 2R 목표
    python exit.py plan --stop or-mid --target 1R --write   # trader-kr/plan.json 에 저장
    python exit.py plan --symbol 005930 --entry 72300 --or-high 72100 --or-low 71400
    python exit.py plan --stop atr:1.5 --offline 005930.json  # ATR 은 그날 봉이 필요하다
    python exit.py plan --symbol 000660 --entry 201000 --setup level --stop pct:1
    python exit.py show                                     # 지금 trader-kr/plan.json

손절 (매수 손절가는 호가 단위로 내림, 진입가에서 멀어지는 쪽):
  or-mid     시가 범위 중간값 (orb 진입의 기본값)
  or-low     시가 범위 저가
  price:X    직접 정한 가격
  pct:X      진입가 아래 X%
  atr:K      진입 봉까지 14개 봉 ATR 의 K배 아래 (봉이 14개보다 적으면 있는 만큼 평균)
  level 셋업은 시가 범위가 없으니 손절을 직접 주어야 한다.

목표 (호가 단위로 반올림):
  2R (기본), 1R, 1.5R ...   진입가 + R x 위험, 위험 = 진입가 - 손절가
  price:X                  직접 정한 가격

비용: 수수료(설정 commission_rate, 기본 0.015%)와 매도 거래세(sell_tax_rate,
기본 0.20%)를 넣은 순손익비도 같이 보여 준다. min_reward_risk 는 이 순손익비로
본다 (trader-kr-risk 가 막는다. 여기서는 알리기만 한다).

진입가가 호가 사이 체결가(예: 245,250원, 중간가 체결)면 바로 위 호가(245,500원)로 계산한다.
거절 (이유와 함께): 손절가가 진입가 이상, 목표가가 진입가 이하, 위험이 호가 2칸
또는 가격의 0.05% 보다 좁음, 목표가가 오늘 상한가보다 높음, 신호가 안 난 entry.json
(--entry 를 주면 미리 계획 가능), 공매도.

flat_by (기본 15:15, 설정 "flat_by"): 이기든 지든 이 시각까지 판다. 15:20 부터는
종가 단일가라 접속매매로 팔 수 없다. 개장이 늦은 날은 마감 기준으로 옮긴다.

손절은 계획이지 보장이 아니다. 갭, 급락, 하한가, VI 단일가 중에는 손절가보다
훨씬 나쁜 가격에 체결되거나 체결이 안 될 수 있다.
종료 코드: plan 0 = 계획, 1 = 거절 또는 오류. show 0 = 계획 있음.
"""

import argparse
import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
from krcommon import bars as kbars, data, krx, toss  # noqa: E402

INTERVALS = {"1m": 60, "5m": 300}
ATR_BARS = 14
MIN_RISK_PCT = 0.05
MIN_RISK_TICKS = 2
NOTE = "손절은 계획이지 보장이 아닙니다. 갭, 급락, 하한가, VI 단일가 중에는 손절가보다 나쁘게 체결되거나 체결되지 않을 수 있습니다."
WORDS = {1: "한", 2: "두", 3: "세", 4: "네", 5: "다섯"}


class PlanError(Exception):
    pass


def p(x):
    return krx.price_str(x)


def parse_clock(text, what):
    """'10:15', '14:05' -> time. 24시간제."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise PlanError("%s 는 10:15 같은 시각이어야 합니다 (받은 값 %r)" % (what, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def parse_stop(text):
    t = str(text).strip().lower()
    if t in ("or-mid", "or_mid", "mid"):
        return "or_mid", None
    if t in ("or-low", "or_low", "low"):
        return "or_low", None
    m = re.match(r"^(price|pct|atr):\s*([0-9][0-9,]*\.?[0-9]*)$", t)
    if not m:
        raise PlanError("--stop 은 or-mid, or-low, price:X, pct:X, atr:K 중 하나입니다 (받은 값 %r)" % text)
    kind, val = m.group(1), float(m.group(2).replace(",", ""))
    if val <= 0:
        raise PlanError("--stop %s 는 0보다 큰 숫자가 필요합니다" % kind)
    if kind == "pct" and val >= 30:
        raise PlanError("--stop pct:X 는 30 미만이어야 합니다 (가격제한폭이 30%%)")
    return kind, val


def parse_target(text):
    t = str(text).strip()
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*[Rr]$", t)
    if m:
        r = float(m.group(1))
        if r <= 0:
            raise PlanError("--target 은 1R, 2R 처럼 0보다 큰 배수여야 합니다")
        return "r", r
    m = re.match(r"^price:\s*([0-9][0-9,]*\.?[0-9]*)$", t.lower())
    if m:
        return "price", float(m.group(1).replace(",", ""))
    raise PlanError("--target 은 2R, 1R, 1.5R ... 또는 price:X 입니다 (받은 값 %r)" % text)


def r_label(r):
    n = int(r) if float(r) == int(r) else None
    return "%gR (위험의 %s 배)" % (r, WORDS.get(n, "%g" % r))


def compute(entry, stop_rule, stop_param, target_rule, target_param, or_high=None, or_low=None,
            atr=None, atr_bars=None, commission_rate=krx.DEFAULT_COMMISSION, sell_tax_rate=krx.DEFAULT_SELL_TAX,
            upper_limit=None, lower_limit=None):
    """손절가, 목표가, 위험, 손익비 (총·순). 정수 원, 호가 단위. 안 되면 PlanError."""
    if krx.D(entry) <= 0:
        raise PlanError("진입가는 0보다 커야 합니다")
    e = krx.ceil_tick(entry)
    entry_note = None
    if krx.D(entry) != e:
        # 호가 사이 체결가 (중간가 체결). 시장가로 사면 매수 호가에 체결되니 위 호가로 계산한다.
        entry_note = ("진입가 %s 는 호가 사이 체결가(중간가 체결로 보임)라서 바로 위 호가 %s 로 계산했습니다"
                      % (p(entry), p(e)))
    detail = ""
    if stop_rule in ("or_mid", "or_low"):
        if or_high is None or or_low is None:
            raise PlanError("%s 손절은 시가 범위(--or-high, --or-low)가 필요합니다" % stop_rule.replace("_", "-"))
        oh, ol = float(or_high), float(or_low)
        if oh <= ol:
            raise PlanError("시가 범위 고가 %s 가 저가 %s 보다 높아야 합니다" % (p(oh), p(ol)))
        if stop_rule == "or_mid":
            raw = (oh + ol) / 2.0
            label = "시가 범위 중간값"
            detail = "(%s + %s) / 2 = %s" % (p(oh), p(ol), p(raw))
        else:
            raw, label = ol, "시가 범위 저가"
    elif stop_rule == "price":
        raw, label = stop_param, "직접 정한 가격"
    elif stop_rule == "pct":
        raw = e * (1 - stop_param / 100.0)
        label = "진입가 아래 %g%%" % stop_param
        detail = "%s x (1 - %g%%) = %s" % (p(e), stop_param, p(round(raw, 2)))
    elif stop_rule == "atr":
        if atr is None:
            raise PlanError("atr 손절은 그날 봉이 필요합니다")
        raw = e - stop_param * atr
        label = "ATR(%d) 의 %g배 아래" % (atr_bars, stop_param)
        detail = "진입까지 %d개 봉 ATR %s%s" % (atr_bars, p(round(atr, 2)), " (14개 미만)" if atr_bars < ATR_BARS else "")
    else:
        raise PlanError("모르는 손절 규칙 %r" % stop_rule)
    stop = krx.floor_tick(raw)
    if krx.D(stop) != krx.D(raw).quantize(krx.D("0.0001")):
        detail = (detail + ", 호가 단위로 내림" if detail else "%s 를 호가 단위로 내림" % p(round(raw, 2)))
    if stop >= e:
        raise PlanError("손절가 %s (%s) 가 진입가 %s 이상입니다. 매수 손절가는 진입가 아래여야 합니다" % (p(stop), label, p(e)))
    if stop <= 0:
        raise PlanError("손절가가 %s 로 나옵니다. 0보다 큰 손절을 고르세요" % p(stop))
    risk = e - stop
    floor_risk = max(MIN_RISK_TICKS * krx.tick_size(stop), e * MIN_RISK_PCT / 100.0)
    if risk < floor_risk:
        raise PlanError("손절이 진입가보다 %s원 아래뿐입니다. 호가 %d칸(%s원) 또는 가격의 %.2f%% 보다 좁으면 보통 흔들림에도 "
                        "손절됩니다. 넓히세요." % (p(risk), MIN_RISK_TICKS, p(MIN_RISK_TICKS * krx.tick_size(stop)),
                                           MIN_RISK_PCT))
    if target_rule == "r":
        target = krx.round_tick(e + target_param * risk)
        t_label = r_label(target_param)
    else:
        target = krx.round_tick(target_param)
        t_label = None
    if target <= e:
        raise PlanError("목표가 %s 가 진입가 %s 이하입니다. 매수 목표가는 진입가 위여야 합니다" % (p(target), p(e)))
    if upper_limit is not None and target > upper_limit:
        raise PlanError("목표가 %s 가 오늘 상한가 %s 보다 높아 닿을 수 없습니다. 목표를 낮추세요" % (p(target), p(upper_limit)))
    rr = round(float(target - e) / risk, 2)
    cs = krx.cost_per_share(e, stop, commission_rate, sell_tax_rate)
    ct = krx.cost_per_share(e, target, commission_rate, sell_tax_rate)
    rr_net = round((target - e - ct) / (risk + cs), 2)
    if t_label is None:
        t_label = "직접 정한 가격 (%gR)" % rr
    out = {"entry": e, "stop": stop, "stop_label": label, "stop_detail": detail, "target": target,
           "target_r": target_param if target_rule == "r" else rr, "target_label": t_label, "risk": risk,
           "reward_risk": rr, "reward_risk_net": rr_net, "cost_per_share_at_stop": round(cs, 2),
           "cost_per_share_at_target": round(ct, 2), "warnings": [entry_note] if entry_note else []}
    if lower_limit is not None and stop < lower_limit:
        out["warnings"].append("손절가 %s 가 오늘 하한가 %s 보다 낮습니다. 하한가에 묶이면 손절 주문이 체결되지 않을 수 "
                               "있습니다" % (p(stop), p(lower_limit)))
    if cs > 0.5 * risk:
        out["warnings"].append("손절 때 비용이 주당 %s원으로 위험 %s원의 %.0f%% 입니다. 이 손절 폭에서는 비용이 큽니다"
                               % (p(round(cs, 1)), p(risk), 100.0 * cs / risk))
    return out


# ---------------------------------------------------------------- ATR

def atr_of(day_bars_5m, n=ATR_BARS):
    """마지막 n개 봉의 평균 진폭 (적으면 전부). 첫 봉은 고가 - 저가만 본다."""
    trs, prev = [], None
    for b in day_bars_5m:
        tr = b["h"] - b["l"]
        if prev is not None:
            tr = max(tr, abs(b["h"] - prev), abs(b["l"] - prev))
        trs.append(tr)
        prev = b["c"]
    use = trs[-n:]
    return sum(use) / len(use), len(use)


# ---------------------------------------------------------------- 계획

def gather(a):
    if a.symbol and a.from_path:
        raise PlanError("--from 과 --symbol 은 함께 쓰지 않습니다")
    if a.side in ("sell", "short"):
        raise PlanError("매수만 계획합니다. 매수로 계획하세요.")
    if a.symbol:
        if a.entry is None:
            raise PlanError("--symbol 에는 --entry 가격이 필요합니다")
        if (a.or_high is None) != (a.or_low is None):
            raise PlanError("--or-high 와 --or-low 는 둘 다 주거나 둘 다 빼세요")
        info = {"symbol": krx.norm_symbol(a.symbol), "name": None, "entry": a.entry,
                "setup": a.setup or ("orb" if a.or_high is not None else "level"), "date": a.date,
                "entry_time": a.time, "entry_bar": a.entry_bar, "interval": a.interval or "5m",
                "or_high": a.or_high, "or_low": a.or_low, "or_minutes": a.or_minutes, "session": None,
                "source": "flags"}
    else:
        path = a.from_path or "trader-kr/entry.json"
        doc = krx.read_json(path, "진입 결과 (/trader-kr-entry 를 먼저 돌리거나 --symbol, --entry 를 주세요)")
        if str(doc.get("side") or "buy").lower() not in ("buy", "long"):
            raise PlanError("%s 는 %s 입니다. 매수만 계획합니다" % (path, doc.get("side")))
        if doc.get("status") != "triggered" and a.entry is None:
            raise PlanError("%s 에 따르면 %s 는 신호가 아니라 %s 입니다. 신호가 나면 계획하거나, --entry 가격으로 미리 계획하세요"
                            % (path, doc.get("symbol"), doc.get("status") or "알 수 없음"))
        orr = doc.get("opening_range") or {}
        info = {"symbol": krx.norm_symbol(doc.get("symbol")), "name": doc.get("name"),
                "entry": a.entry if a.entry is not None else doc.get("price"),
                "setup": a.setup or doc.get("setup") or "orb", "date": a.date or doc.get("date"),
                "entry_time": a.time or (doc.get("time") if a.entry is None else None),
                "entry_bar": a.entry_bar if a.entry_bar is not None else (doc.get("bar_index") if a.entry is None else None),
                "interval": a.interval or doc.get("interval") or "5m",
                "or_high": a.or_high if a.or_high is not None else orr.get("high"),
                "or_low": a.or_low if a.or_low is not None else orr.get("low"),
                "or_minutes": orr.get("minutes") or a.or_minutes, "session": doc.get("session"), "source": path}
        if info["entry"] is None:
            raise PlanError("%s 에 가격이 없습니다" % path)
    if info["interval"] not in INTERVALS:
        raise PlanError("봉 단위는 5m 또는 1m 입니다")
    return info


def session_of(info, day, online):
    s = info.get("session")
    if s and s.get("date") == day.isoformat():
        return krx.Session(day, krx.parse_hhmm(s["open"], "open"), krx.parse_hhmm(s["auction"], "auction"),
                           krx.parse_hhmm(s["close"], "close"), s.get("source", "entry"), s.get("note", ""))
    return data.session_for(day, online=online)


def resolve_flat_by(session, explicit, cfg, entry_time):
    raw = explicit or cfg.get("flat_by") or krx.CONFIG_DEFAULTS["flat_by"]
    t = krx.parse_hhmm(raw, "--flat-by" if explicit else "flat_by")
    note = ""
    if session is not None:
        moved = session.shift(t) if not explicit else t
        if moved != t:
            note = "오늘 장 시간에 맞춰 %s 로 옮겼습니다" % krx.hm(moved)
            t = moved
        if datetime.datetime.combine(session.day, t) > session.auction_dt:
            raise PlanError("flat_by %s 는 접속매매가 끝나는 %s 보다 늦습니다" % (krx.hm(t), krx.hm(session.auction_t)))
    if entry_time and entry_time >= t:
        raise PlanError("진입 %s 가 청산 시각 %s 와 같거나 늦습니다. 들고 있을 시간이 없습니다" % (krx.hm(entry_time), krx.hm(t)))
    return krx.hm(t), note


def limits_for(info, day, today, online, a):
    """(하한가, 상한가, 출처). 오늘 계획이면 토스에서, 아니면 --prev-close 로 추정."""
    if a.prev_close:
        lo, hi = krx.price_limits(a.prev_close)
        return lo, hi, "전일 종가 %s 기준 추정" % p(a.prev_close)
    if online and day == today:
        try:
            r = data.client().price_limits(info["symbol"])
            if r.get("upperLimitPrice") and r.get("lowerLimitPrice"):
                return float(r["lowerLimitPrice"]), float(r["upperLimitPrice"]), "토스증권 상·하한가"
        except toss.TossError:
            return None, None, "상·하한가를 받지 못했습니다"
    return None, None, None


def build_plan(a, now_kst):
    info = gather(a)
    cfg = krx.read_config(a.config)
    stop_text = a.stop
    if not stop_text:
        if info["setup"] != "orb":
            raise PlanError("%s 셋업은 기본 손절이 없습니다. --stop price:X, pct:X, atr:K 중 하나를 주세요" % info["setup"])
        stop_text = "or-mid"
    stop_rule, stop_param = parse_stop(stop_text)
    target_rule, target_param = parse_target(a.target or "2R")
    try:
        day = datetime.date.fromisoformat(str(info["date"])) if info["date"] else now_kst.date()
    except ValueError:
        raise PlanError("날짜는 YYYY-MM-DD 입니다 (받은 값 %r)" % info["date"])
    online = not a.offline and not a.no_network
    session = session_of(info, day, online)
    entry_time = parse_clock(info["entry_time"], "진입 시각") if info["entry_time"] else None
    atr = atr_n = None
    if stop_rule == "atr":
        start = datetime.datetime.combine(day, datetime.time(8, 59))
        end = datetime.datetime.combine(day, entry_time) if entry_time else now_kst
        try:
            chart = data.load_chart(info["symbol"], start, end, offline=a.offline, now_kst=now_kst)
        except data.DataError as e:
            raise PlanError(str(e))
        if session is None:
            raise PlanError("%s 는 휴장일이라 ATR 을 계산할 봉이 없습니다" % day)
        step = INTERVALS[info["interval"]]
        b5 = [b for b in kbars.session_slots(kbars.split_days(chart["bars"]).get(day, []), session, step,
                                             datetime.datetime.combine(day, entry_time) if entry_time else now_kst)
              if b["complete"]]
        if info["entry_bar"] is not None:
            b5 = [b for b in b5 if b["slot"] <= int(info["entry_bar"])]
        if len(b5) < 3:
            raise PlanError("atr 손절은 진입까지 완성된 %s 봉이 3개 이상 필요합니다 (%d개)" % (info["interval"], len(b5)))
        atr, atr_n = atr_of(b5)
    lo, hi, lim_src = limits_for(info, day, now_kst.date(), online, a)
    c = compute(info["entry"], stop_rule, stop_param, target_rule, target_param, info["or_high"], info["or_low"],
                atr, atr_n, cfg.get("commission_rate"), cfg.get("sell_tax_rate"), hi, lo)
    flat_by, flat_note = resolve_flat_by(session, a.flat_by, cfg, entry_time)
    orr = None
    if info["or_high"] is not None and info["or_low"] is not None:
        orr = {"high": float(info["or_high"]), "low": float(info["or_low"]), "minutes": int(info["or_minutes"] or 15)}
    plan = {"created_by": "trader-kr-exit", "symbol": info["symbol"], "name": info.get("name"), "side": "buy",
            "date": day.isoformat(), "setup": info["setup"], "entry": c["entry"],
            "entry_time": krx.hm(entry_time) if entry_time else None, "entry_bar": info["entry_bar"],
            "interval": info["interval"], "opening_range": orr, "stop": c["stop"], "stop_rule": stop_rule,
            "stop_label": c["stop_label"], "target": c["target"], "target_r": c["target_r"],
            "target_label": c["target_label"], "risk_per_share": c["risk"], "reward_risk": c["reward_risk"],
            "reward_risk_net": c["reward_risk_net"], "cost_per_share_at_stop": c["cost_per_share_at_stop"],
            "cost_per_share_at_target": c["cost_per_share_at_target"],
            "commission_rate": cfg.get("commission_rate"), "sell_tax_rate": cfg.get("sell_tax_rate"),
            "flat_by": flat_by, "stop_detail": c["stop_detail"], "price_limits": {"lower": lo, "upper": hi,
                                                                                 "source": lim_src} if lo else None,
            "session": session.to_doc() if session else None,
            "created_at": now_kst.strftime("%Y-%m-%d %H:%M:%S"), "note": NOTE}
    if atr is not None:
        plan["atr"] = round(atr, 2)
        plan["atr_bars"] = atr_n
    warnings = list(c["warnings"])
    if session is None:
        warnings.append("%s 는 휴장일입니다" % day.isoformat())
    if flat_note:
        warnings.append("청산 시각 %s: %s." % (flat_by, flat_note))
    if lim_src and not lo:
        warnings.append(lim_src)
    mrr = cfg.get("min_reward_risk")
    if mrr is not None and c["reward_risk_net"] < float(mrr):
        warnings.append("비용을 뺀 손익비 %.2f 가 설정 min_reward_risk %g 보다 낮습니다. trader-kr-risk 가 막습니다"
                        % (c["reward_risk_net"], float(mrr)))
    if day != now_kst.date():
        warnings.append("이 계획은 오늘(%s)이 아니라 %s 것입니다" % (now_kst.date().isoformat(), day.isoformat()))
    return plan, warnings


def describe(plan):
    setup = {"orb": "시가 범위 돌파", "level": "기준 가격 위 종가"}.get(plan["setup"], plan["setup"])
    pct = 100.0 * plan["risk_per_share"] / plan["entry"]
    when = ""
    if plan.get("entry_time"):
        when = " %s" % plan["entry_time"]
        if plan.get("entry_bar") is not None:
            when += " (%d번째 봉)" % plan["entry_bar"]
    out = ["청산 계획: %s%s 매수, %s, %s, %s봉" % (plan["symbol"], (" " + plan["name"]) if plan.get("name") else "",
                                           plan["date"], setup, "5분" if plan["interval"] == "5m" else "1분")]
    out.append("  진입        %s%s" % (p(plan["entry"]), when))
    out.append("  손절        %s  %s%s" % (p(plan["stop"]), plan["stop_label"],
                                         (": " + plan["stop_detail"]) if plan.get("stop_detail") else ""))
    out.append("  목표        %s  %s" % (p(plan["target"]), plan["target_label"]))
    out.append("  주당 위험   %s원 (진입가의 %.2f%%)" % (p(plan["risk_per_share"]), pct))
    out.append("  손익비      %.2f (비용 뺀 순손익비 %.2f, 수수료 %s, 매도세 %s)" % (
        plan["reward_risk"], plan["reward_risk_net"], krx.pct_str(plan.get("commission_rate") or 0, 3),
        krx.pct_str(plan.get("sell_tax_rate") or 0, 2)))
    if plan.get("price_limits"):
        pl = plan["price_limits"]
        out.append("  가격제한    하한가 %s, 상한가 %s (%s)" % (p(pl["lower"]), p(pl["upper"]), pl["source"]))
    out.append("  청산 시각   %s, 이기든 지든: 다음 날로 넘기지 않음" % plan["flat_by"])
    out.append("들어가기 전에 정합니다. 손절가에 닿으면 나옵니다. 손절가를 더 멀리 옮기지 마세요.")
    out.append(NOTE)
    return out


def cmd_plan(a):
    now = krx.parse_now(a.now)
    plan, warnings = build_plan(a, now)
    if a.write:
        krx.write_json(a.write, plan)
    if a.json:
        print(json.dumps(dict(plan, warnings=warnings), ensure_ascii=False, indent=2))
        return 0
    for line in describe(plan):
        print(line)
    for w in warnings:
        print("참고: " + w)
    if a.write:
        print("%s 에 썼습니다." % a.write)
    print("다음: /trader-kr-timemachine 으로 다시 돌려 보고, /trader-kr-budget 으로 최대 손실에 맞춰 수량을 정합니다. 주문은 내지 않았습니다.")
    return 0


def cmd_show(a):
    if not os.path.exists(a.plan):
        print("%s 에 계획이 없습니다. 만들기: exit.py plan --write" % a.plan)
        return 1
    plan = krx.read_json(a.plan, "계획")
    for k in ("symbol", "date", "entry", "stop", "target", "risk_per_share", "reward_risk", "flat_by"):
        if plan.get(k) is None:
            raise PlanError("%s 에 %r 가 없습니다. exit.py plan --write 로 새로 만드세요" % (a.plan, k))
    if a.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    base = {"setup": "orb", "interval": "5m", "stop_label": "", "target_label": "", "reward_risk_net": plan["reward_risk"]}
    base.update(plan)
    for line in describe(base):
        print(line)
    today = krx.parse_now(a.now).date().isoformat()
    if plan["date"] != today:
        print("참고: 이 계획은 오늘(%s)이 아니라 %s 것입니다." % (today, plan["date"]))
    return 0


def main(argv=None):
    krx.utf8_stdio()
    ap = argparse.ArgumentParser(description="청산 계획 (trader-kr-exit): 진입 전에 손절가와 목표가를 정한다.")
    sub = ap.add_subparsers(dest="cmd")
    q = sub.add_parser("plan", help="손절가와 목표가 정하기")
    q.add_argument("--from", dest="from_path", metavar="PATH", help="entry.json (기본 trader-kr/entry.json)")
    q.add_argument("--symbol")
    q.add_argument("--entry", type=float, help="진입가 (또는 --from 과 함께 미리 계획할 가격)")
    q.add_argument("--or-high", type=float)
    q.add_argument("--or-low", type=float)
    q.add_argument("--or-minutes", type=int, default=15)
    q.add_argument("--setup", choices=["orb", "level"])
    q.add_argument("--date", help="거래일 YYYY-MM-DD (기본: 진입 결과의 날짜 또는 오늘)")
    q.add_argument("--time", help="진입 시각, 예: 10:15")
    q.add_argument("--entry-bar", type=int)
    q.add_argument("--interval", choices=["5m", "1m"])
    q.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"], help="매수만")
    q.add_argument("--stop", help="or-mid (orb 기본), or-low, price:X, pct:X, atr:K")
    q.add_argument("--target", help="2R (기본), 1R, 1.5R, price:X")
    q.add_argument("--flat-by", help="HH:MM (기본: 설정 flat_by, 15:15)")
    q.add_argument("--prev-close", type=float, help="전일 종가 (상·하한가 추정용)")
    q.add_argument("--config", default="trader-kr/config.json")
    q.add_argument("--offline", metavar="PATH", help="atr 손절용 1분봉 파일")
    q.add_argument("--no-network", action="store_true", help="토스증권을 부르지 않는다")
    q.add_argument("--now", help='이 한국 시간인 것처럼, 예: "2026-10-28 10:15"')
    q.add_argument("--write", nargs="?", const="trader-kr/plan.json", metavar="PATH", help="저장 (기본 trader-kr/plan.json)")
    q.add_argument("--json", action="store_true")
    q = sub.add_parser("show", help="지금 계획 보기")
    q.add_argument("--plan", default="trader-kr/plan.json")
    q.add_argument("--now")
    q.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"plan": cmd_plan, "show": cmd_show}[a.cmd](a)
    except (PlanError, krx.KrxError, data.DataError) as e:
        print("exit.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
