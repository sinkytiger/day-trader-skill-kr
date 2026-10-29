"""호가창으로 체결 비용 재기. 표준 라이브러리만 쓴다.

토스증권 호가창(GET /api/v1/orderbook)의 매도 호가(asks)와 매수 호가(bids)로 다음을 잰다.

  스프레드      매도 1호가 - 매수 1호가 (호가 칸 수)
  시장가 매수    수량만큼 매도 호가를 위에서부터 먹었을 때 평균 체결가
  중간가 절약    시장가 평균 체결가와 중간가(1호가들의 한가운데)의 차이 x 수량. 중간가호가나
               지정가로 들어가면 아낄 수 있는 몫의 상한이다 (안 채워질 수 있다)
  손절 때 밀림   손절가에 닿았을 때 매수 잔량이 지금과 같은 모양이라고 치고, 수량만큼 시장가로
               팔면 손절가보다 얼마나 아래에서 평균 체결되는지

손절 때 밀림은 추정이다. 급락할 때는 매수 잔량이 지금보다 얇아지는 일이 많아서 실제로는 더
밀릴 수 있다. 보이는 잔량보다 많이 팔아야 하면 못 채운 수량을 따로 알린다.
"""

from . import krx


def levels(orderbook):
    """토스 호가창 -> (asks 오름차순, bids 내림차순) [(가격, 잔량)]."""
    def side(key, reverse):
        out = []
        for x in (orderbook or {}).get(key) or []:
            try:
                p, q = float(x["price"]), float(x.get("quantity") or x.get("volume") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            if p > 0 and q > 0:
                out.append((p, q))
        return sorted(out, reverse=reverse)
    return side("asks", False), side("bids", True)


def walk(book_side, qty):
    """한쪽 호가를 차례로 먹는다. {filled, avg, worst, levels, unfilled}."""
    left, cost, used, worst = float(qty), 0.0, 0, None
    for p, q in book_side:
        if left <= 0:
            break
        take = min(left, q)
        cost += take * p
        left -= take
        used += 1
        worst = p
    filled = float(qty) - left
    return {"filled": filled, "avg": (cost / filled) if filled else None, "worst": worst, "levels": used,
            "unfilled": left}


def spread(asks, bids):
    if not asks or not bids:
        return None
    a, b = asks[0][0], bids[0][0]
    t = krx.tick_size(b)
    return {"ask": a, "bid": b, "mid": (a + b) / 2.0, "ticks": int(round((a - b) / t)), "tick": t}


def buy_cost(orderbook, qty):
    """시장가 매수 qty 주의 체결 추정과 중간가로 들어갔을 때 아낄 수 있는 몫."""
    asks, bids = levels(orderbook)
    sp = spread(asks, bids)
    if sp is None:
        return None
    w = walk(asks, qty)
    if not w["filled"]:
        return None
    return {"spread": sp, "avg": w["avg"], "levels": w["levels"], "unfilled": w["unfilled"],
            "over_mid_per_share": w["avg"] - sp["mid"], "mid_saving": (w["avg"] - sp["mid"]) * w["filled"]}


def stop_slippage(orderbook, qty):
    """손절가에 닿았을 때 qty 주를 시장가로 팔면 1호가보다 얼마나 아래에서 평균 체결되는지 (원/주).
    지금 매수 잔량의 모양을 그대로 손절가 아래로 옮겨 잰다. 못 채운 수량은 마지막 칸 아래 한 칸에서
    팔린다고 친다 (보수적). 자료가 없으면 None."""
    asks, bids = levels(orderbook)
    if not bids:
        return None
    w = walk(bids, qty)
    best = bids[0][0]
    if w["filled"]:
        gap = best - w["avg"]
    else:
        gap = 0.0
    if w["unfilled"] > 0:
        last = bids[-1][0]
        extra = best - (last - krx.tick_size(last))
        gap = (gap * w["filled"] + extra * w["unfilled"]) / float(qty)
    return {"per_share": max(0.0, gap), "levels": w["levels"], "unfilled": w["unfilled"],
            "visible_bid_qty": sum(q for _, q in bids)}
