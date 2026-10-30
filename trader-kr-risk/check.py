#!/usr/bin/env python3
"""주문 전 위험 점검 (trader-kr-risk). 표준 라이브러리만 쓴다.

주문 제안 (trader-kr/orders.json) 하나하나를 규칙 (trader-kr/config.json), 계좌
(trader-kr/portfolio.json), 오늘 낸 주문 (trader-kr/state/broker.json), 종목 상태 (토스증권
종목 정보·매수 유의사항·상·하한가)에 비춰 PASS 또는 BLOCK 으로 판정한다.
앞의 PASS 가 체결됐다고 치고 차례로 보므로, 하나씩은 괜찮아도 합치면 한도를 넘는
주문을 잡는다.

    python check.py                                 # trader-kr/ 파일들, trader-kr/state/risk.json 에 기록
    python check.py --now "2026-10-28 10:15"        # 시계 고정 (한국 시간)
    python check.py --status status.json            # 종목 상태를 파일에서 (네트워크 없이)
    python check.py --no-write --json

진입과 청산. 진입은 포지션을 열거나 늘린다 (매수). 청산은 가진 것을 줄이기만 한다.
빠져나가는 길은 늘 열려 있어야 하므로 청산은 형식과 공매도만 막고 나머지는 경고로 바꾼다.

모든 주문을 막는 것:
  형식      종목코드 6자리, 매수/매도, 1주 단위 수량, 시장가/지정가, 지정가는 호가 단위
  공매도    가진 것보다 많이 파는 매도 ("allow_short" 은 꺼 둔다)

진입을 막는 것 (추가로):
  종목      주식이 아님 (ETF 는 "allow_etf"), 우선주 ("allow_preferred"), 상장 상태가
            ACTIVE 가 아님, KRX 거래정지, 정리매매
  유의      투자경고, 투자위험, 단기과열, 정리매매, 신주인수권, VI 발동 중
  (경고만)  오늘 그 종목 DART 공시 (잠정실적, 증자 등). DART_API_KEY 가 있을 때
  (경고만)  신호 뒤 15분이 넘게 지난 계획
  상태 모름 종목 상태를 받지 못함 ("require_market_status": true 일 때)
  가격제한  진입가나 목표가가 상한가보다 높음 (손절가가 하한가보다 낮으면 경고)
  차단 목록 "blocked" 의 종목
  금액      min_order_krw 미만, max_order_krw 초과
  종목 한도 계좌의 max_position_pct 초과
  현금      현금 버퍼를 깨거나 미수 (빌린 돈)가 필요함
  중복      같은 종목·방향이 이번 묶음에 두 번, 또는 오늘 이미 냄
  하루 주문 max_orders_per_day
  손절      "require_stop" 인데 손절이 없음, 손절이 진입가 위
  손실      수량 x (진입가 - 손절가) + 비용 이 max_loss_per_trade_krw 초과
  손익비    비용 뺀 손익비가 min_reward_risk 미만
  하루 손실 오늘 진입들의 손절 때 손실 합 + 이번 것이 max_daily_loss_krw 초과
  거래 수   오늘 진입이 이미 max_trades_per_day
  시간      no_entries_before 전, no_entries_after 뒤, 휴장, 접속매매 시간(09:00~15:20) 밖

종료 코드 0 = 모두 통과, 1 = 하나라도 막힘. 판정은 주문 지문과 함께
trader-kr/state/risk.json 에 남긴다. 다음 단계는 오늘 이 주문 그대로 통과한 것만 받는다.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
from krcommon import dart, data, krx, liquidity, toss  # noqa: E402

ORDER_FIELDS = ("symbol", "side", "qty", "type", "limit_price", "time_in_force", "exit")
TYPES = {"market", "limit"}
TIFS = {"day"}
FRESH_MINUTES = 15
STALE_MINUTES = 15   # 신호 뒤 이만큼 지나면 경고
BLOCK_WARNINGS = {"INVESTMENT_WARNING": "투자경고종목", "INVESTMENT_RISK": "투자위험종목", "OVERHEATED": "단기과열종목",
                  "LIQUIDATION_TRADING": "정리매매", "STOCK_WARRANTS": "신주인수권",
                  "VI_STATIC": "정적 VI 발동 중", "VI_DYNAMIC": "동적 VI 발동 중", "VI_STATIC_AND_DYNAMIC": "정적·동적 VI 발동 중"}


class RiskError(Exception):
    pass


def p(x):
    return krx.price_str(x)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fingerprint(order):
    core = {k: order[k] for k in ORDER_FIELDS if k in order and order[k] is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- 시간

def window_check(now, session, c):
    """지금 진입할 수 없는 이유 목록과 경고."""
    warns = []
    if session is None:
        return ["시간: 오늘은 휴장입니다 (%s)" % now.strftime("%Y-%m-%d %a")], warns
    if session.source == "default":
        msg = "장 운영 정보를 받지 못해 오늘이 휴장일인지 모릅니다 (평일 기본 장 시간으로 봄)"
        if c.get("require_market_status", True):
            return ["시간: " + msg], warns
        warns.append(msg)
    elif not session.standard:
        warns.append("오늘 장 시간이 다릅니다: %s 개장, 접속매매 %s 까지" % (krx.hm(session.open_t), krx.hm(session.auction_t)))
    if now < session.open_dt or now >= session.auction_dt:
        return ["시간: 접속매매 시간이 아닙니다 (지금 %s, 접속매매 %s ~ %s)" % (
            now.strftime("%H:%M"), krx.hm(session.open_t), krx.hm(session.auction_t))], warns
    out = []
    b, a = c.get("no_entries_before"), c.get("no_entries_after")
    if b:
        t = session.shift(krx.parse_hhmm(b, "no_entries_before"))
        if now.time() < t:
            out.append("시간: %s 전에는 새로 진입하지 않습니다 (지금 %s)" % (krx.hm(t), now.strftime("%H:%M")))
    if a:
        t = session.shift(krx.parse_hhmm(a, "no_entries_after"))
        if now.time().replace(second=0, microsecond=0) > t:
            out.append("시간: %s 뒤에는 새로 진입하지 않습니다 (지금 %s)" % (krx.hm(t), now.strftime("%H:%M")))
    return out, warns


# ---------------------------------------------------------------- 종목 상태

def fetch_status(symbols, today):
    """토스증권에서 종목 정보, 매수 유의사항, 상·하한가를 받는다. 못 받은 종목은 error."""
    out = {}
    c = data.client()
    try:
        info = {s["symbol"]: s for s in c.stocks(symbols)}
    except toss.TossError as e:
        return {s: {"error": str(e)} for s in symbols}
    for sym in symbols:
        rec = {"stock": info.get(sym), "fetched_at": krx.kst_now().strftime("%Y-%m-%d %H:%M:%S")}
        try:
            rec["warnings"] = c.warnings(sym)
            rec["price_limits"] = c.price_limits(sym)
            try:
                rec["orderbook"] = c.orderbook(sym)
            except toss.TossError:
                rec["orderbook"] = None      # 호가창이 없으면 체결 비용만 빼고 본다
        except toss.TossError as e:
            rec["error"] = str(e)
        if rec.get("stock") is None and "error" not in rec:
            rec["error"] = "토스증권에 %s 종목 정보가 없습니다" % sym
        day = krx.parse_day(today)
        rows, note = dart.check(sym, day) if day else (None, None)
        rec["disclosures"] = rows
        if rows is None and note:
            rec["disclosure_note"] = note
        out[sym] = rec
    try:
        for q in c.prices(symbols):
            if q.get("symbol") in out:
                out[q["symbol"]]["price"] = {"lastPrice": q.get("lastPrice"), "timestamp": q.get("timestamp")}
    except toss.TossError:
        pass               # 현재가가 없으면 계획 진입가로 재고 그렇다고 경고한다
    return out


def active(w, today):
    """오늘 걸린 유의사항인지. 날짜를 못 읽으면 걸린 것으로 본다 (막는 쪽)."""
    t = krx.parse_day(today)
    s_raw, e_raw = w.get("startDate"), w.get("endDate")
    s = krx.parse_day(s_raw) if s_raw else None
    e = krx.parse_day(e_raw) if e_raw else None
    if (s_raw and s is None) or (e_raw and e is None) or t is None:
        return True
    return (s is None or s <= t) and (e is None or t <= e)


def fresh_price(st, now, minutes=FRESH_MINUTES):
    """토스 현재가가 minutes 분 안의 것이면 그 가격, 아니면 None."""
    q = (st or {}).get("price") or {}
    px = _f(q.get("lastPrice"))
    if not px or px <= 0 or not q.get("timestamp"):
        return None
    try:
        t = krx.parse_iso(q["timestamp"])
    except krx.KrxError:
        return None
    age = (now - t).total_seconds()
    return px if -60 <= age <= minutes * 60 else None


def status_reasons(sym, st, today, c):
    """(막는 이유, 경고, 하한가, 상한가)."""
    reasons, warns = [], []
    if not st or st.get("error"):
        msg = "상태 모름: %s 종목 상태를 받지 못했습니다 (%s)" % (sym, (st or {}).get("error") or "자료 없음")
        if c.get("require_market_status", True):
            reasons.append(msg)
        else:
            warns.append(msg)
        return reasons, warns, None, None
    s = st.get("stock") or {}
    typ = s.get("securityType")
    if typ == "ETF" and c.get("allow_etf"):
        pass
    elif typ != "STOCK":
        reasons.append("종목: %s 는 %s 입니다. 이 팩은 국내 주식만 다룹니다 (ETF 는 \"allow_etf\")" % (sym, typ or "유형 모름"))
    if s.get("isCommonShare") is False and not c.get("allow_preferred"):
        reasons.append("종목: %s 는 우선주입니다 (\"allow_preferred\": false). 우선주는 거래량이 적어 단타에 불리합니다" % sym)
    if s.get("status") and s.get("status") != "ACTIVE":
        reasons.append("종목: %s 상장 상태가 %s 입니다" % (sym, s["status"]))
    kd = s.get("koreanMarketDetail") or {}
    if kd.get("krxTradingSuspended"):
        reasons.append("종목: %s 는 KRX 거래정지 중입니다" % sym)
    if kd.get("liquidationTrading"):
        reasons.append("종목: %s 는 정리매매 중입니다 (상장폐지 절차)" % sym)
    seen = set()
    for w in st.get("warnings") or []:
        k = w.get("warningType")
        if not active(w, today) or k in seen:
            continue
        seen.add(k)
        if k in BLOCK_WARNINGS:
            reasons.append("유의: %s %s (%s ~ %s, %s)" % (sym, BLOCK_WARNINGS[k], w.get("startDate") or "?",
                                                       w.get("endDate") or "진행 중", w.get("exchange") or "거래소 무관"))
        else:
            warns.append("유의사항 %s 가 있습니다 (%s)" % (k, sym))
    if st.get("disclosures"):
        warns.append("공시: " + dart.describe(st["disclosures"], sym, krx.parse_day(today)))
    elif st.get("disclosure_note"):
        warns.append(st["disclosure_note"])
    pl = st.get("price_limits") or {}
    lo, hi = _f(pl.get("lowerLimitPrice")), _f(pl.get("upperLimitPrice"))
    return reasons, warns, lo, hi


# ---------------------------------------------------------------- 계좌·기록

def account(cfg, portfolio):
    if portfolio is None:
        budget = _f(cfg.get("budget"))
        if not budget:
            raise RiskError("trader-kr/portfolio.json 도 설정의 budget 도 없습니다: 주문을 비교할 계좌가 없습니다")
        return {"cash": budget, "shares": {}, "values": {}}
    cash = _f(portfolio.get("cash"))
    if cash is None:
        raise RiskError("portfolio.json 에 \"cash\" 숫자가 필요합니다")
    shares, values = {}, {}
    for pos in portfolio.get("positions", []):
        try:
            t = krx.norm_symbol(pos.get("symbol") or pos.get("ticker"))
        except krx.KrxError:
            continue
        n = _f(pos.get("qty", pos.get("shares"))) or 0
        shares[t] = shares.get(t, 0) + n
        values[t] = values.get(t, 0) + n * (_f(pos.get("price")) or _f(pos.get("avg_price")) or 0)
    return {"cash": cash, "shares": shares, "values": values}


def placed_today(state, today):
    return [r for r in (state or {}).get("orders", [])
            if r.get("status") == "placed" and str(r.get("date") or str(r.get("at", ""))[:10]) == today]


def is_entry(rec):
    k = rec.get("kind")
    return (k == "entry") if k else str(rec.get("side", "")).lower() == "buy"


# ---------------------------------------------------------------- 점검

def check(orders, cfg, portfolio=None, state=None, status=None, now=None, session=None):
    """순수 함수. status = {종목: 토스 상태}, session = 오늘 krx.Session 또는 None."""
    c = dict(krx.CONFIG_DEFAULTS)
    c.update(cfg or {})
    now = now or krx.kst_now()
    today = now.date().isoformat()
    acct = account(c, portfolio)
    top_warn = []
    if c.get("send_orders"):
        top_warn.append("\"send_orders\": true 입니다. PASS 한 주문은 trader-kr-broker 가 모의투자로 보낼 수 있습니다 "
                        "(그 주문 하나에 대한 당신의 yes 와 승인 카드가 더 있어야)")
    blocked = {str(b).strip().upper() for b in c.get("blocked") or []}
    done = placed_today(state, today)
    entries_today = [r for r in done if is_entry(r)]
    loss_today = sum(_f(r.get("max_loss")) or 0 for r in entries_today)
    placed_keys = {(str(r.get("symbol", "")), r.get("side")) for r in done}
    win_reasons, win_warns = window_check(now, session, c)
    top_warn.extend(win_warns)
    cr, tr = float(c.get("commission_rate") or 0), float(c.get("sell_tax_rate") or 0)
    seen, results = set(), []
    batch_entries, batch_loss = 0, 0.0
    for i, o in enumerate(orders, 1):
        reasons, warns, info, later = [], [], [], []
        try:
            sym = krx.norm_symbol(o.get("symbol"))
        except krx.KrxError as e:
            sym = str(o.get("symbol") or "")
            reasons.append("형식: %s" % e)
        side = str(o.get("side", "")).strip().lower()
        typ = str(o.get("type", "market")).strip().lower()
        tif = str(o.get("time_in_force", "day")).strip().lower()
        qty = o.get("qty")
        plan = o.get("plan") if isinstance(o.get("plan"), dict) else {}
        ex = o.get("exit") if isinstance(o.get("exit"), dict) else {}
        if side not in ("buy", "sell"):
            reasons.append("형식: side 는 buy 또는 sell 입니다 (받은 값 %r)" % o.get("side"))
        q = _f(qty)
        if q is None or q <= 0 or abs(q - round(q)) > 1e-9:
            reasons.append("형식: 수량은 1주 단위 양의 정수입니다 (받은 값 %r)" % qty)
            q = None
        if typ not in TYPES:
            reasons.append("형식: 주문 유형은 market 또는 limit 입니다 (받은 값 %r)" % typ)
        if tif not in TIFS:
            reasons.append("형식: time_in_force 는 day 만 받습니다 (받은 값 %r)" % tif)
        lp = _f(o.get("limit_price"))
        if typ == "limit":
            if lp is None or lp <= 0:
                reasons.append("형식: 지정가 주문에 limit_price 가 필요합니다")
            elif not krx.on_tick(lp):
                reasons.append("형식: 지정가 %s 가 호가 단위(%s원)에 맞지 않습니다" % (p(lp), krx.tick_size(lp)))
        for k in ("stop_price", "take_profit_price"):
            v = _f(ex.get(k))
            if v is not None and not krx.on_tick(v):
                reasons.append("형식: %s %s 가 호가 단위(%s원)에 맞지 않습니다" % (k, p(v), krx.tick_size(v)))

        ref = lp if typ == "limit" and lp else _f(plan.get("entry"))
        ref_from = "지정가" if typ == "limit" and lp else "계획 진입가"
        live = fresh_price((status or {}).get(sym), now) if sym else None
        if typ == "market" and sym:
            if live is not None and (ref is None or live > ref):
                ref, ref_from = float(krx.ceil_tick(live)), "지금 가격 (계획 진입가보다 높음)"
            elif live is None and side == "buy":
                warns.append("15분 안의 현재가가 없어 계획 진입가로 손실을 쟀습니다. 그 뒤 가격이 올랐으면 손실이 더 큽니다")
        value = q * ref if (q and ref) else None
        pday = plan.get("date")
        if side == "buy" and pday and str(pday) != today:
            reasons.append("계획 날짜: 이 주문의 계획은 %s 것입니다 (오늘 %s). trader-kr-exit 로 새로 계획하세요" % (pday, today))
        elif side == "buy" and plan.get("entry_time"):
            try:
                sig = datetime.datetime.combine(now.date(), krx.parse_hhmm(plan["entry_time"], "entry_time"))
                late = int((now - sig).total_seconds() // 60)
                if late > STALE_MINUTES:
                    warns.append("신호 뒤 %d분이 지났습니다. 늦은 진입은 다른 거래입니다: 가격, 손절 거리, 확률이 바뀌었습니다"
                                 % late)
            except krx.KrxError:
                pass

        held = acct["shares"].get(sym, 0.0)
        kind = "exit" if (side == "sell" and held > 0 and q is not None and q <= held + 1e-9) else "entry"
        if side == "sell" and kind == "entry" and not c.get("allow_short"):
            reasons.append("공매도: 가진 %s 주보다 많이 팝니다. 공매도는 꺼져 있습니다" % ("%g" % held))
        if kind == "entry" and value is None and q:
            later.append("가격: 수량을 잴 진입가가 없습니다 (계획의 entry 또는 지정가)")

        # 종목 상태
        lo_lim = hi_lim = None
        if kind == "entry" and sym:
            r_s, w_s, lo_lim, hi_lim = status_reasons(sym, (status or {}).get(sym), today, c)
            later.extend(r_s)
            warns.extend(w_s)
        if sym in blocked:
            later.append("차단 목록: %s" % sym)
        if value is not None:
            if value < float(c.get("min_order_krw") or 0):
                later.append("금액: %s 는 최소 주문 %s 보다 작습니다" % (krx.won(value), krx.won(c["min_order_krw"])))
            if c.get("max_order_krw") and value > float(c["max_order_krw"]):
                later.append("금액: %s 는 주문 한도 %s 를 넘습니다" % (krx.won(value), krx.won(c["max_order_krw"])))

        total = acct["cash"] + sum(acct["values"].values())
        if value is not None and side == "buy" and kind == "entry":
            need = value * (1 + cr)
            after = acct["cash"] - need
            buf = total * float(c.get("cash_buffer_pct") or 0) / 100.0
            if after < 0 and not c.get("allow_margin"):
                later.append("현금: %s 가 필요한데 현금은 %s 뿐입니다. 미수(빌린 돈)가 됩니다" % (krx.won(need), krx.won(acct["cash"])))
            elif after < buf:
                later.append("현금: 남는 현금 %s 가 버퍼 %g%% (%s) 보다 적습니다" % (krx.won(max(after, 0)),
                                                                       float(c.get("cash_buffer_pct") or 0), krx.won(buf)))
            pos_after = acct["values"].get(sym, 0.0) + value
            cap = total * float(c.get("max_position_pct") or 100) / 100.0
            if total > 0 and pos_after > cap + 0.5:
                later.append("종목 한도: %s 가 %s 로 계좌 %s 의 %.1f%% 가 되어 한도 %g%% 를 넘습니다"
                             % (sym, krx.won(pos_after), krx.won(total), 100.0 * pos_after / total,
                                float(c.get("max_position_pct"))))

        max_loss = risk_ps = rr_net = stop = target = None
        if kind == "entry" and side == "buy":
            stop = _f(ex.get("stop_price")) or _f(plan.get("stop"))
            target = _f(ex.get("take_profit_price")) or _f(plan.get("target"))
            if live is not None and stop and live <= stop:
                later.append("가격: 지금 가격 %s 가 손절가 %s 이하입니다. 계획이 이미 깨졌습니다. 새로 계획하세요"
                             % (p(live), p(stop)))
            if not stop:
                if c.get("require_stop", True):
                    later.append("손절: 손절가가 없습니다. 모든 진입에 손절이 필요합니다 (\"require_stop\": true)")
                else:
                    warns.append("손절 없음: 최대 손실이 정해지지 않아 하루 손실에 넣지 않았습니다")
            elif ref:
                if stop >= ref:
                    later.append("손절: 손절가 %s 는 진입가 %s 보다 아래여야 합니다" % (p(stop), p(ref)))
                else:
                    risk_ps = ref - stop
                    if q:
                        book = ((status or {}).get(sym) or {}).get("orderbook")
                        slip = liquidity.stop_slippage(book, q) if book and c.get("use_book_slippage", True) else None
                        slip_ps = slip["per_share"] if slip else 0.0
                        max_loss = q * (risk_ps + slip_ps) + krx.trade_costs(ref, stop, q, cr, tr)["total"]
                        if slip and slip["unfilled"]:
                            warns.append("보이는 매수 잔량으로는 손절 때 %d주를 다 못 팝니다. 실제 손실이 더 클 수 있습니다"
                                         % slip["unfilled"])
                        ctr = (krx.cost_per_share(ref, stop, cr, tr) + slip_ps) / risk_ps
                        max_ctr = _f(c.get("max_cost_to_risk"))
                        if max_ctr and ctr > max_ctr:
                            later.append("비용: 손절 때 비용%s이 주당 위험의 %.0f%% 로 한도 %.0f%% 를 넘습니다"
                                         % ("과 예상 밀림" if slip_ps else "", 100 * ctr, 100 * max_ctr))
                        cap_l = _f(c.get("max_loss_per_trade_krw"))
                        if cap_l and max_loss > cap_l:
                            later.append("손실: %d주 x 위험 %s원 + 비용 = 손절 때 %s, 거래당 최대 손실 %s 초과"
                                         % (int(q), p(risk_ps), krx.won(max_loss), krx.won(cap_l)))
                    min_rr = _f(c.get("min_reward_risk"))
                    if target:
                        if target <= ref:
                            later.append("목표: 목표가 %s 는 진입가 %s 보다 위여야 합니다" % (p(target), p(ref)))
                        else:
                            cs = krx.cost_per_share(ref, stop, cr, tr)
                            ct = krx.cost_per_share(ref, target, cr, tr)
                            rr_net = round((target - ref - ct) / (risk_ps + cs), 2)
                            if min_rr and rr_net < min_rr - 1e-9:
                                later.append("손익비: 비용 뺀 손익비 %.2f (목표 %s, 진입 %s, 손절 %s) 가 최소 %g 미만"
                                             % (rr_net, p(target), p(ref), p(stop), min_rr))
                    elif min_rr:
                        later.append("손익비: 잴 목표가가 없습니다 (min_reward_risk %g)" % min_rr)
            if hi_lim is not None:
                if ref and ref > hi_lim:
                    later.append("가격제한: 진입가 %s 가 상한가 %s 보다 높습니다" % (p(ref), p(hi_lim)))
                if target and target > hi_lim:
                    later.append("가격제한: 목표가 %s 가 상한가 %s 보다 높아 닿을 수 없습니다" % (p(target), p(hi_lim)))
            if lo_lim is not None and stop and stop < lo_lim:
                warns.append("손절가 %s 가 하한가 %s 보다 낮습니다. 하한가에 묶이면 손절이 체결되지 않을 수 있습니다"
                             % (p(stop), p(lo_lim)))
            daily = _f(c.get("max_daily_loss_krw"))
            if daily and max_loss is not None and loss_today + batch_loss + max_loss > daily:
                later.append("하루 손실: 오늘 이미 %s 걸려 있고 이번 %s 를 더하면 %s, 하루 한도 %s 초과"
                             % (krx.won(loss_today + batch_loss), krx.won(max_loss),
                                krx.won(loss_today + batch_loss + max_loss), krx.won(daily)))
            cap_t = int(c.get("max_trades_per_day") or 0)
            if cap_t and len(entries_today) + batch_entries >= cap_t:
                later.append("거래 수: 오늘 진입 %d건 + 이번 묶음에서 앞서 통과 %d건, 하루 한도 %d건"
                             % (len(entries_today), batch_entries, cap_t))
            later.extend(win_reasons)
            if stop and ref and risk_ps:
                info.append("진입 %s (%s), 손절 %s, 목표 %s: 주당 위험 %s원" % (p(ref), ref_from, p(stop),
                                                                     p(target) if target else "없음", p(risk_ps)))
                info.append("손절 때 손실 %s%s, 비용 뺀 손익비 %s%s" % (
                    krx.won(max_loss) if max_loss is not None else "-",
                    (" (한도 %s)" % krx.won(c["max_loss_per_trade_krw"])) if _f(c.get("max_loss_per_trade_krw")) else "",
                    ("%.2f" % rr_net) if rr_net is not None else "-",
                    (" (최소 %g)" % float(c["min_reward_risk"])) if _f(c.get("min_reward_risk")) else ""))
        key = (sym, side)
        if key in seen:
            later.append("중복: 이번 묶음에 %s %s 가 두 번 있습니다" % (side, sym))
        if key in placed_keys:
            later.append("중복: 오늘 이미 %s %s 를 냈습니다" % (side, sym))
        cap_n = int(c.get("max_orders_per_day") or 0)
        passed = sum(1 for r in results if r["verdict"] == "PASS")
        if cap_n and len(done) + passed >= cap_n:
            later.append("하루 주문: 오늘 %d건 + 이번 묶음 통과 %d건, 하루 한도 %d건" % (len(done), passed, cap_n))
        seen.add(key)
        if kind == "entry":
            reasons.extend(later)
        else:
            warns.extend("청산이라 막지 않음: " + x for x in later)
        verdict = "BLOCK" if reasons else "PASS"
        if verdict == "PASS":
            if kind == "entry":
                batch_entries += 1
                batch_loss += max_loss or 0.0
            if value is not None:
                if side == "buy":
                    acct["cash"] -= value * (1 + cr)
                    acct["values"][sym] = acct["values"].get(sym, 0.0) + value
                    acct["shares"][sym] = held + (q or 0)
                else:
                    acct["cash"] += value
                    acct["values"][sym] = max(acct["values"].get(sym, 0.0) - value, 0.0)
                    acct["shares"][sym] = held - (q or 0)
        results.append({"index": i, "symbol": sym, "name": o.get("name"), "side": side, "kind": kind,
                        "qty": int(q) if q else qty, "value_krw": round(value) if value is not None else None,
                        "entry_ref": ref, "stop": stop, "target": target, "risk_per_share": risk_ps,
                        "max_loss": max_loss, "reward_risk_net": rr_net, "price_limits": [lo_lim, hi_lim],
                        "fingerprint": fingerprint(o), "verdict": verdict, "reasons": reasons, "warnings": warns,
                        "info": info})
    return {"date": today, "now_kst": now.strftime("%Y-%m-%d %H:%M"), "send_orders": bool(c.get("send_orders")),
            "results": results, "pass": sum(1 for r in results if r["verdict"] == "PASS"),
            "block": sum(1 for r in results if r["verdict"] == "BLOCK"), "warnings": top_warn,
            "cash_after": round(acct["cash"]),
            "today": {"orders_placed": len(done), "entries_placed": len(entries_today), "loss_at_risk": round(loss_today)},
            "session": session.to_doc() if session else None}


def report(res):
    L = []
    for w in res["warnings"]:
        L.append("경고: " + w)
    if res["warnings"]:
        L.append("")
    for r in res["results"]:
        L.append("%-5s #%d %s %s%s %s주, %s" % (r["verdict"], r["index"], "매수" if r["side"] == "buy" else "매도",
                                              r["symbol"], (" " + r["name"]) if r.get("name") else "", r["qty"],
                                              "진입" if r["kind"] == "entry" else "청산"))
        for x in r.get("info", []):
            L.append("          " + x)
        for x in r["reasons"]:
            L.append("        - " + x)
        for x in r["warnings"]:
            L.append("        ~ " + x)
    L.append("")
    L.append("%d건 통과, %d건 막힘 (%s 한국 시간). 통과 주문 뒤 현금: %s." % (res["pass"], res["block"], res["now_kst"],
                                                                 krx.won(res["cash_after"])))
    if res["block"]:
        L.append("막힌 주문은 다음 단계로 가지 않습니다. 주문이나 규칙을 고치고 다시 점검하세요.")
    else:
        L.append("모두 통과했습니다. 그래도 주문은 주문마다 당신이 yes 라고 해야 하고, 보내는 것은 trader-kr-broker (모의투자) 뿐입니다.")
    L.append("교육과 도구일 뿐 투자 권유가 아닙니다 (not financial advice).")
    return "\n".join(L)


def main(argv=None):
    krx.utf8_stdio()
    ap = argparse.ArgumentParser(description="주문 제안을 한도에 비춰 점검한다 (trader-kr-risk).")
    ap.add_argument("--orders", default="trader-kr/orders.json")
    ap.add_argument("--config", default="trader-kr/config.json")
    ap.add_argument("--portfolio", default="trader-kr/portfolio.json")
    ap.add_argument("--state", default="trader-kr/state/broker.json", help="오늘 낸 주문 기록")
    ap.add_argument("--status", help="종목 상태 JSON 파일 ({종목: {stock, warnings, price_limits}}), 네트워크 없이")
    ap.add_argument("--status-out", default="trader-kr/state/market_status.json")
    ap.add_argument("--no-network", action="store_true", help="토스증권을 부르지 않는다 (상태는 --status 로)")
    ap.add_argument("--now", help='이 한국 시간인 것처럼, 예: "2026-10-28 10:15"')
    ap.add_argument("--out", default="trader-kr/state/risk.json")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        now = krx.parse_now(a.now)
        doc = krx.read_json(a.orders, "주문 제안 (trader-kr-budget 을 먼저 돌리세요)")
        orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
        if not isinstance(orders, list) or not orders:
            raise RiskError("%s 에 주문이 없습니다" % a.orders)
        cfg = krx.read_config(a.config)
        portfolio = krx.read_json(a.portfolio, "계좌", required=False)
        state = krx.read_json(a.state, "주문 기록", required=False)
        online = not a.no_network
        syms = []
        for o in orders:
            try:
                s = krx.norm_symbol(o.get("symbol"))
                if s not in syms:
                    syms.append(s)
            except krx.KrxError:
                pass
        if a.status:
            status = krx.read_json(a.status, "종목 상태")
        elif online and syms:
            status = fetch_status(syms, now.date().isoformat())
            if not a.no_write:
                krx.write_json(a.status_out, status)
        else:
            status = {}
        session = data.session_for(now.date(), online=online)
        res = check(orders, cfg, portfolio, state, status, now, session)
        if not a.no_write:
            rec = dict(res, checked_at=krx.kst_now().strftime("%Y-%m-%d %H:%M:%S"), orders_file=a.orders)
            krx.write_json(a.out, rec)
    except (RiskError, krx.KrxError, data.DataError) as e:
        print("check.py: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(res, ensure_ascii=False, indent=2) if a.json else report(res))
    return 1 if res["block"] else 0


if __name__ == "__main__":
    sys.exit(main())
