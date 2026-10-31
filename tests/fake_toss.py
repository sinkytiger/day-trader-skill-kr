"""네트워크 없이 토스증권 Open API 를 흉내 낸다 (시험용)."""

import json
import urllib.parse

from krcommon import krx


class FakeToss(object):
    """transport(method, url, headers, body) 자리에 끼운다."""

    def __init__(self, bars_by_symbol=None, script=None):
        self.bars = bars_by_symbol or {}        # 종목 -> 시작 시각 기준 1분봉 목록
        self.script = list(script or [])         # 앞에서부터 꺼내 쓰는 (status, headers, body) 강제 응답
        self.log = []
        self.tokens = 0
        self.stocks = {}
        self.warnings = {}
        self.limits = {}
        self.ranking = []
        self.calendar = {}
        self.prices = {}
        self.orderbook = {}
        self.account_list = [{"accountNo": "12345678901", "accountSeq": 7, "accountType": "BROKERAGE"}]
        self.holding_items = []
        self.order_book = []                     # 주문 내역 (status 가 OPEN 묶음인지 CLOSED 묶음인지로 나뉜다)
        self.page_size = None                    # 정하면 CLOSED 목록을 이 크기로 잘라 쪽을 넘기게 한다
        self.headers_log = []

    def __call__(self, method, url, headers, body):
        u = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        self.log.append((method, u.path, q))
        if u.path == "/oauth2/token":
            self.tokens += 1
            return 200, {}, json.dumps({"access_token": "tok%d" % self.tokens, "token_type": "Bearer",
                                        "expires_in": 86400})
        if self.script:
            return self.script.pop(0)
        if u.path == "/api/v1/candles":
            return 200, {}, json.dumps({"result": self._candles(q)})
        if u.path == "/api/v1/stocks":
            syms = q["symbols"].split(",")
            return 200, {}, json.dumps({"result": [self.stocks[s] for s in syms if s in self.stocks]})
        if u.path.startswith("/api/v1/stocks/") and u.path.endswith("/warnings"):
            return 200, {}, json.dumps({"result": self.warnings.get(u.path.split("/")[4], [])})
        if u.path == "/api/v1/price-limits":
            return 200, {}, json.dumps({"result": self.limits.get(q["symbol"], {})})
        if u.path == "/api/v1/orderbook":
            return 200, {}, json.dumps({"result": self.orderbook.get(q["symbol"], {"asks": [], "bids": []})})
        if u.path == "/api/v1/rankings":
            return 200, {}, json.dumps({"result": {"rankedAt": "2026-10-28T10:14:00+09:00", "rankings": [
                {"rank": i + 1, "symbol": s, "currency": "KRW", "price": {"lastPrice": "1", "basePrice": "1"},
                 "tradingVolume": "1", "tradingAmount": "1"} for i, s in enumerate(self.ranking)]}})
        if u.path == "/api/v1/market-calendar/KR":
            return 200, {}, json.dumps({"result": self.calendar[q.get("date")]})
        self.headers_log.append((method, u.path, dict(headers or {})))
        if u.path in ("/api/v1/holdings", "/api/v1/orders") and "X-Tossinvest-Account" not in (headers or {}):
            return 400, {}, json.dumps({"error": {"code": "missing-header", "message": ""}})
        if u.path == "/api/v1/accounts":
            return 200, {}, json.dumps({"result": self.account_list})
        if u.path == "/api/v1/holdings":
            return 200, {}, json.dumps({"result": {"items": self.holding_items}})
        if u.path == "/api/v1/orders":
            return 200, {}, json.dumps({"result": self._orders(q)})
        if u.path == "/api/v1/prices":
            syms = q["symbols"].split(",")
            return 200, {}, json.dumps({"result": [{"symbol": s, "lastPrice": str(self.prices[s]), "currency": "KRW",
                                                    "timestamp": "2026-10-28T10:16:00+09:00"}
                                                   for s in syms if s in self.prices]})
        return 404, {}, json.dumps({"error": {"requestId": "x", "code": "not-found", "message": ""}})

    def _orders(self, q):
        group = {"OPEN": ("PENDING", "PARTIAL_FILLED", "PENDING_CANCEL", "PENDING_REPLACE")}
        rows = [o for o in self.order_book
                if (o["status"] in group["OPEN"]) == (q["status"] == "OPEN")
                and (not q.get("from") or o["orderedAt"][:10] >= q["from"])
                and (not q.get("to") or o["orderedAt"][:10] <= q["to"])]
        if q["status"] == "OPEN" or not self.page_size:
            return {"orders": rows, "nextCursor": None, "hasNext": False}
        start = int(q.get("cursor") or 0)
        end = start + min(self.page_size, int(q.get("limit") or 20))
        more = end < len(rows)
        return {"orders": rows[start:end], "nextCursor": str(end) if more else None, "hasNext": more}

    def _candles(self, q):
        rows = sorted(self.bars.get(q["symbol"], []), key=lambda b: -b["t"])
        before = krx.ts_of(krx.parse_iso(q["before"])) if q.get("before") else None
        out = []
        for b in rows:
            end = b["t"] + 60
            if before is not None and end > before:
                continue
            out.append(b)
            if len(out) >= int(q.get("count", 100)):
                break
        older = out and any(x["t"] < out[-1]["t"] for x in rows)
        nxt = krx.iso_kst(krx.kst_of(out[-1]["t"] + 60 - 60)) if older else None
        return {"candles": [{"timestamp": krx.iso_kst(krx.kst_of(b["t"] + 60)), "openPrice": str(b["o"]),
                             "highPrice": str(b["h"]), "lowPrice": str(b["l"]), "closePrice": str(b["c"]),
                             "volume": str(int(b["v"])), "currency": "KRW"} for b in out],
                "nextBefore": nxt}


def stock_info(symbol, name="합성종목", security_type="STOCK", common=True, suspended=False, liquidation=False):
    return {"symbol": symbol, "name": name, "englishName": "SYN", "isinCode": "KR0000000000", "market": "KOSPI",
            "securityType": security_type, "isCommonShare": common, "status": "ACTIVE", "currency": "KRW",
            "sharesOutstanding": "1000000",
            "koreanMarketDetail": {"liquidationTrading": liquidation, "nxtSupported": True,
                                   "krxTradingSuspended": suspended, "nxtTradingSuspended": False}}


def order(oid, symbol, side, qty, avg, at, status="FILLED", price=None, commission=None, tax=None, filled=None,
          currency="KRW"):
    """토스증권 주문 한 건 (명세 Order 모양). at 은 '2026-10-31T10:20:00+09:00' 같은 체결 시각."""
    filled = qty if filled is None else filled
    return {"orderId": oid, "symbol": symbol, "side": side, "orderType": "LIMIT" if price else "MARKET",
            "timeInForce": "DAY", "status": status, "price": None if price is None else str(price),
            "quantity": str(qty), "orderAmount": None, "currency": currency, "orderedAt": at, "canceledAt": None,
            "execution": {"filledQuantity": str(filled), "averageFilledPrice": str(avg) if filled else None,
                          "filledAmount": str(avg * filled) if filled else None,
                          "commission": None if commission is None else str(commission),
                          "tax": None if tax is None else str(tax), "filledAt": at if filled else None,
                          "settlementDate": None}}


def holding(symbol, qty, avg, last, name="합성종목", country="KR", currency="KRW"):
    pnl = (last - avg) * qty
    return {"symbol": symbol, "name": name, "marketCountry": country, "currency": currency, "quantity": str(qty),
            "lastPrice": str(last), "averagePurchasePrice": str(avg),
            "marketValue": {"purchaseAmount": str(avg * qty), "amount": str(last * qty),
                            "amountAfterCost": str(last * qty)},
            "profitLoss": {"amount": str(pnl), "amountAfterCost": str(pnl), "rate": str(pnl / (avg * qty)),
                           "rateAfterCost": str(pnl / (avg * qty))},
            "dailyProfitLoss": {"amount": "0", "rate": "0"}, "cost": {"commission": "0", "tax": None}}
