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
        if u.path == "/api/v1/prices":
            syms = q["symbols"].split(",")
            return 200, {}, json.dumps({"result": [{"symbol": s, "lastPrice": str(self.prices[s]), "currency": "KRW",
                                                    "timestamp": "2026-10-28T10:16:00+09:00"}
                                                   for s in syms if s in self.prices]})
        return 404, {}, json.dumps({"error": {"requestId": "x", "code": "not-found", "message": ""}})

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
