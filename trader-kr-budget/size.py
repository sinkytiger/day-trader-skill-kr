#!/usr/bin/env python3
"""수량 계산 (trader-kr-budget): 계획 하나를 예산과 거래당 최대 손실에 맞춰 수량을 정한다.
표준 라이브러리만 쓴다. 주문은 절대 내지 않는다.

trader-kr/plan.json (trader-kr-exit 가 씀) 을 읽어 주문 제안 하나를 trader-kr/orders.json 에
쓴다: 매수 1건과 그 뒤에 걸 손절·목표 (토스증권이라면 OCO 조건주문 하나로 걸 수 있는
형태). trader-kr-risk 가 그 제안을 검사하고, 주문 단계는 아직 없다 (1단계는 조회·계산만).

    python size.py                                  # trader-kr/plan.json + trader-kr/config.json
    python size.py --write                          # trader-kr/orders.json 에 쓰기
    python size.py --limit                          # 계획 진입가 지정가
    python size.py --limit 72300 --write            # 72,300원 지정가
    python size.py --price 72400                    # 지금 가격을 직접 주기
    python size.py --now "2026-10-28 10:15" --json  # 시계 고정 (한국 시간)

수량 (1주 단위):
    수량 = 다음 중 가장 작은 것의 내림
      최대 손실   max_loss_per_trade_krw / (주당 위험 + 손절 때 주당 비용)
      종목 한도   budget x max_position_pct / 진입가
      주문 한도   max_order_krw / 진입가
      현금        (budget - 현금 버퍼) / (진입가 + 매수 수수료)
                  (trader-kr/portfolio.json 의 현금이 더 적으면 그 현금)
    주당 비용 = 매수 수수료 + 손절가에 팔 때 수수료와 매도 거래세.

진입가: 지정가면 지정가. 시장가면 계획 진입가, 단 지금 가격(--price, 또는 오늘 계획이면
토스증권 현재가)이 더 높으면 그 가격. 계획 뒤 오른 가격은 같은 손절에서 손실이 커지기
때문이다. 0주(손절이 너무 멀어 최대 손실을 넘음)면 주문 없이 거절한다. 비용 뺀
손익비가 min_reward_risk 보다 낮아도 거절한다.
"""

import argparse
import datetime
import json
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
from krcommon import data, krx, toss  # noqa: E402

SETUP_WORDS = {"orb": "시가 범위 돌파", "level": "기준 가격 위 종가", "flag": "깃발", "bounce": "과매도 반등"}


class SizeError(Exception):
    pass


def p(x):
    return krx.price_str(x)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def shares_for(krw, per_share):
    if per_share <= 0 or krw <= 0:
        return 0
    return int(math.floor(float(krw) / float(per_share) + 1e-9))


def size_trade(plan, cfg, budget=None, limit=None, price=None, cash=None, today=None):
    """순수 함수: 계획 하나 + 규칙 -> 수량 계산 결과 (qty 는 0일 수 있음)."""
    c = dict(krx.CONFIG_DEFAULTS)
    c.update(cfg or {})
    if not isinstance(plan, dict):
        raise SizeError("계획이 JSON 객체가 아닙니다")
    try:
        sym = krx.norm_symbol(plan.get("symbol"))
    except krx.KrxError as e:
        raise SizeError("계획의 종목코드: %s" % e)
    if str(plan.get("side") or "buy").lower() != "buy":
        raise SizeError("매수 단타만 수량을 정합니다 (계획은 %r)" % plan.get("side"))
    if today and plan.get("date") and str(plan["date"]) != today:
        raise SizeError("계획은 %s 것이고 오늘은 %s 입니다. trader-kr-exit 로 새 계획을 만드세요" % (plan["date"], today))
    entry_plan, stop, target = _f(plan.get("entry")), _f(plan.get("stop")), _f(plan.get("target"))
    for name, v in (("진입가", entry_plan), ("손절가", stop), ("목표가", target)):
        if v is None or v <= 0:
            raise SizeError("계획에 0보다 큰 %s 가 필요합니다 (trader-kr-exit 를 돌리세요)" % name)
    if sym in {str(b).strip().upper() for b in (c.get("blocked") or [])}:
        raise SizeError("%s 는 차단 목록에 있습니다" % sym)
    budget = float(budget if budget is not None else (c.get("budget") or 0))
    if budget <= 0:
        raise SizeError("예산이 없습니다: --budget 을 주거나 trader-kr/config.json 에 \"budget\" 을 넣으세요")
    buf_pct = float(c.get("cash_buffer_pct") or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct 는 0 ~ 100 사이여야 합니다")
    cr, tr = float(c.get("commission_rate") or 0), float(c.get("sell_tax_rate") or 0)

    if price is not None and price <= stop:
        raise SizeError("지금 가격 %s 가 손절가 %s 이하입니다. 계획이 이미 깨졌습니다. trader-kr-exit 로 새로 계획하세요"
                        % (p(price), p(stop)))
    if limit is not None:
        if not krx.on_tick(limit):
            raise SizeError("지정가 %s 가 호가 단위(%s원)에 맞지 않습니다" % (p(limit), krx.tick_size(limit)))
        ref, ref_from = float(limit), "limit"
    elif price is not None and price > entry_plan:
        ref, ref_from = float(krx.ceil_tick(price)), "price"
    else:
        ref, ref_from = float(entry_plan), "plan"
    if stop >= ref:
        raise SizeError("손절가 %s 가 진입가 %s 아래가 아닙니다" % (p(stop), p(ref)))
    if target <= ref:
        raise SizeError("목표가 %s 가 진입가 %s 위가 아닙니다. 이미 움직인 뒤입니다. 건너뛰거나 trader-kr-exit 로 새로 계획하세요"
                        % (p(target), p(ref)))
    risk = ref - stop
    cost_stop = krx.cost_per_share(ref, stop, cr, tr)
    cost_target = krx.cost_per_share(ref, target, cr, tr)
    loss_ps = risk + cost_stop
    rr = round((target - ref) / risk, 2)
    rr_net = round((target - ref - cost_target) / loss_ps, 2)

    cash_buffer = round(budget * buf_pct / 100.0)
    available = budget - cash_buffer
    cash_note = "예산 %s - 현금 버퍼 %s" % (krx.won(budget), krx.won(cash_buffer))
    if cash is not None and cash - cash_buffer < available:
        available = cash - cash_buffer
        cash_note = "trader-kr/portfolio.json 현금 %s - 버퍼 %s" % (krx.won(cash), krx.won(cash_buffer))
    available = max(available, 0.0)

    limits = []
    max_loss = _f(c.get("max_loss_per_trade_krw"))
    if max_loss:
        limits.append({"name": "최대 손실", "krw": max_loss, "shares": shares_for(max_loss, loss_ps),
                       "how": "%s / (위험 %s + 비용 %.1f)원" % (krx.won(max_loss), p(risk), cost_stop)})
    cap_pct = _f(c.get("max_position_pct"))
    if cap_pct:
        cap = budget * cap_pct / 100.0
        limits.append({"name": "종목 한도", "krw": cap, "shares": shares_for(cap, ref),
                       "how": "%g%% x %s = %s / %s" % (cap_pct, krx.won(budget), krx.won(cap), p(ref))})
    max_order = _f(c.get("max_order_krw"))
    if max_order:
        limits.append({"name": "주문 한도", "krw": max_order, "shares": shares_for(max_order, ref),
                       "how": "%s / %s" % (krx.won(max_order), p(ref))})
    limits.append({"name": "현금", "krw": available, "shares": shares_for(available, ref * (1 + cr)),
                   "how": "%s = %s / (%s + 수수료)" % (cash_note, krx.won(available), p(ref))})
    qty = max(0, min(l["shares"] for l in limits))
    bound = [l["name"] for l in limits if l["shares"] == qty][0]
    for l in limits:
        l["binds"] = l["name"] == bound
    cost = qty * ref
    loss_at_stop = qty * risk + krx.trade_costs(ref, stop, qty, cr, tr)["total"] if qty else 0
    gain_at_target = qty * (target - ref) - krx.trade_costs(ref, target, qty, cr, tr)["total"] if qty else 0
    refusal = None
    if qty == 0:
        if bound == "최대 손실":
            refusal = ("1주만 사도 손절 때 %s 를 잃습니다 (위험 %s원 + 비용). 거래당 최대 손실 %s 보다 큽니다. "
                       "주문 없음. 건너뛰거나 trader-kr-exit 로 더 가까운 손절을 정하세요."
                       % (krx.won(loss_ps), p(risk), krx.won(max_loss)))
        else:
            refusal = "%s 1주가 %s 로 %s 한도를 넘습니다. 주문 없음." % (sym, krx.won(ref), bound)
    elif _f(c.get("min_reward_risk")) and rr_net < float(c["min_reward_risk"]) - 1e-9:
        refusal = ("비용 뺀 손익비 %.2f (목표 %s, 진입 %s, 손절 %s) 가 최소 %g 보다 낮아 trader-kr-risk 가 막습니다. 주문 없음."
                   % (rr_net, p(target), p(ref), p(stop), float(c["min_reward_risk"])))
    elif cost < float(c.get("min_order_krw") or 0):
        refusal = "%d주 %s 는 최소 주문 %s 보다 작습니다. 주문 없음." % (qty, krx.won(cost), krx.won(c["min_order_krw"]))
    return {"symbol": sym, "name": plan.get("name"), "side": "buy", "setup": plan.get("setup", ""),
            "date": plan.get("date"), "entry_time": plan.get("entry_time") or "", "entry_plan": entry_plan,
            "entry": ref, "entry_from": ref_from, "stop": stop, "target": target, "risk_per_share": risk,
            "cost_per_share_at_stop": round(cost_stop, 2), "reward_risk": rr, "reward_risk_net": rr_net,
            "stop_label": plan.get("stop_label", ""), "target_label": plan.get("target_label", ""),
            "budget": budget, "cash_buffer": cash_buffer, "cash_available": available,
            "max_loss_per_trade_krw": max_loss, "limits": limits, "qty": qty, "bound_by": bound, "cost": cost,
            "max_loss": loss_at_stop, "gain_at_target": gain_at_target,
            "flat_by": plan.get("flat_by") or c.get("flat_by"), "order_type": "limit" if limit is not None else "market",
            "commission_rate": cr, "sell_tax_rate": tr, "refusal": refusal}


def order_proposal(s):
    """주문 제안 하나 (증권사 중립 형식). 보내지 않는다."""
    if s["refusal"] or s["qty"] <= 0:
        return None
    o = {"symbol": s["symbol"], "name": s.get("name"), "side": "buy", "qty": int(s["qty"]), "type": s["order_type"],
         "time_in_force": "day"}
    if s["order_type"] == "limit":
        o["limit_price"] = int(s["entry"])
    o["exit"] = {"stop_price": int(s["stop"]), "take_profit_price": int(s["target"]), "flat_by": s["flat_by"],
                 "how": "체결 뒤 손절·목표를 한 쌍(OCO)으로 건다. 토스증권은 종목당 OCO 조건주문 1개."}
    o["why"] = "%s, %s: %d주, %s 에 묶임 (손절 때 %s 손실, 비용 포함)" % (
        SETUP_WORDS.get(s["setup"], s["setup"] or "단타"), s["symbol"], s["qty"], s["bound_by"], krx.won(s["max_loss"]))
    o["plan"] = {"entry": s["entry"], "stop": s["stop"], "target": s["target"], "risk_per_share": s["risk_per_share"],
                 "max_loss": s["max_loss"], "setup": s["setup"], "entry_time": s["entry_time"], "flat_by": s["flat_by"],
                 "date": s["date"], "reward_risk_net": s["reward_risk_net"]}
    return o


def plan_report(s, order, notes=()):
    L = []
    L.append("%s%s 매수, %s, 진입 %s%s" % (s["symbol"], (" " + s["name"]) if s.get("name") else "",
                                      SETUP_WORDS.get(s["setup"], s["setup"] or "셋업"), p(s["entry_plan"]),
                                      (" " + s["entry_time"]) if s["entry_time"] else ""))
    L.append("손절 %s%s. 목표 %s%s." % (p(s["stop"]), (": " + s["stop_label"]) if s["stop_label"] else "", p(s["target"]),
                                    (": " + s["target_label"]) if s["target_label"] else ""))
    for n in notes:
        L.append(n)
    src = {"plan": "계획 진입가", "price": "지금 가격 (계획 진입가보다 높음)", "limit": "지정가"}[s["entry_from"]]
    L.append("기준가 %s (%s). 주당 위험 %s원 + 손절 때 비용 %.1f원. 손익비 %.2f, 비용 뺀 손익비 %.2f." % (
        p(s["entry"]), src, p(s["risk_per_share"]), s["cost_per_share_at_stop"], s["reward_risk"], s["reward_risk_net"]))
    L.append("")
    L.append("한도 (1주 단위, 가장 작은 것이 정함):")
    for l in s["limits"]:
        L.append("  %-8s %-58s %5d주%s" % (l["name"], l["how"], l["shares"], "  <- 이것" if l["binds"] else ""))
    L.append("")
    if order is None:
        L.append("주문 없음: " + s["refusal"])
        L.append("교육과 도구일 뿐 투자 권유가 아닙니다 (not financial advice).")
        return "\n".join(L)
    L.append("수량: %s %d주 = %s (%s 기준). 손절 때 손실 %s, 목표 때 이익 %s (둘 다 비용 뺀 추정)." % (
        s["symbol"], s["qty"], krx.won(s["cost"]), p(s["entry"]), krx.won(s["max_loss"]), krx.won(s["gain_at_target"])))
    L.append("손절은 약속이 아닙니다. 급락, 하한가, VI 단일가 중에는 %s 보다 낮게 체결될 수 있습니다." % p(s["stop"]))
    L.append("")
    L.append("주문 제안 (아무것도 보내지 않았습니다):")
    how = "지정가 %s" % p(order["limit_price"]) if order["type"] == "limit" else "시장가"
    L.append("  매수 %s %d주, %s, 당일" % (order["symbol"], order["qty"], how))
    L.append("  체결 뒤: 손절 %s / 목표 %s 한 쌍, %s 까지 남으면 정리" % (p(order["exit"]["stop_price"]),
                                                          p(order["exit"]["take_profit_price"]), s["flat_by"]))
    L.append("")
    L.append("다음: trader-kr-risk 가 검사합니다. 1단계에는 주문 보내기가 없습니다.")
    L.append("교육과 도구일 뿐 투자 권유가 아닙니다 (not financial advice).")
    return "\n".join(L)


def write_orders(path, orders, cfg, source="", now_kst=None):
    doc = {"created_by": "trader-kr-budget", "created_at": (now_kst or krx.kst_now()).strftime("%Y-%m-%d %H:%M:%S"),
           "status": "proposed", "send_orders": bool(cfg.get("send_orders", False)),
           "note": "제안일 뿐입니다. trader-kr-risk 가 이 주문을 통과시키고 당신이 채팅에서 그 주문에 yes 라고 해야만 다음 단계로 갑니다. "
                   "1단계에는 주문을 보내는 도구가 없습니다.",
           "source": source, "orders": orders}
    krx.write_json(path, doc)


def run(a):
    now = krx.parse_now(a.now)
    today = now.date().isoformat()
    cfg = krx.read_config(a.config)
    plan = krx.read_json(a.plan, "계획 (trader-kr-exit 를 먼저 돌리세요)")
    limit = None
    if a.limit is not None:
        limit = _f(plan.get("entry")) if a.limit == "plan" else _f(a.limit)
        if limit is None or limit <= 0:
            raise SizeError("--limit 은 72300 같은 가격입니다")
    notes, price = [], None
    if a.price is not None:
        price = float(a.price)
    elif limit is None and not a.no_network and plan.get("date") == today:
        try:
            got = data.client().prices([krx.norm_symbol(plan.get("symbol"))])
            if got and got[0].get("lastPrice"):
                price = float(got[0]["lastPrice"])
                notes.append("토스증권 현재가 %s (%s)." % (p(price), got[0].get("timestamp") or "시각 없음"))
        except (toss.TossError, krx.KrxError) as e:
            notes.append("현재가를 받지 못해 계획 진입가로 정합니다: %s" % e)
    if price is not None and plan.get("entry") and price > float(plan["entry"]):
        notes.append("지금 가격 %s 가 계획 진입가 %s 보다 높아 위험이 큽니다. %s 기준으로 정합니다."
                     % (p(price), p(plan["entry"]), p(krx.ceil_tick(price))))
    cash = None
    if a.portfolio and os.path.exists(a.portfolio):
        pf = krx.read_json(a.portfolio, "계좌")
        cash = _f(pf.get("cash")) if isinstance(pf, dict) else None
    s = size_trade(plan, cfg, a.budget, limit, price, cash, today)
    order = order_proposal(s)
    if order and a.write:
        write_orders(a.write, [order], cfg, "trader-kr/plan.json (%s %s %s)" % (s["symbol"], s["setup"] or "계획", s["date"]), now)
    if a.json:
        print(json.dumps({"sizing": s, "orders": [order] if order else [], "notes": notes}, ensure_ascii=False, indent=2))
    else:
        print(plan_report(s, order, notes))
        if order and a.write:
            print("주문 제안 1건을 %s 에 썼습니다." % a.write)
    return 0 if order else 1


def main(argv=None):
    krx.utf8_stdio()
    ap = argparse.ArgumentParser(description="수량 계산 (trader-kr-budget): 예산과 최대 손실에 맞춘다.")
    ap.add_argument("--plan", default="trader-kr/plan.json")
    ap.add_argument("--config", default="trader-kr/config.json")
    ap.add_argument("--budget", type=float, help="설정의 예산 대신")
    ap.add_argument("--limit", nargs="?", const="plan", metavar="PRICE", help="지정가 (가격 없으면 계획 진입가)")
    ap.add_argument("--price", type=float, help="지금 가격 (주면 토스증권을 부르지 않음)")
    ap.add_argument("--portfolio", default="trader-kr/portfolio.json", help="계좌 현금 (있으면 수량 상한)")
    ap.add_argument("--no-network", action="store_true")
    ap.add_argument("--now", help='이 한국 시간인 것처럼, 예: "2026-10-28 10:15"')
    ap.add_argument("--write", nargs="?", const="trader-kr/orders.json", metavar="PATH")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        return run(a)
    except (SizeError, krx.KrxError, data.DataError) as e:
        print("size.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
