"""네트워크 없이 한국투자증권 모의투자 API 를 흉내 낸다 (시험용).

시장가는 지금 가격에 바로 체결되고, 지정가 매도는 가격이 그 위로 오면, 지정가 매수는 그 아래로 오면 체결된다.
"""

import json
import urllib.parse


class FakeKis(object):
    """transport(method, url, headers, body) 자리에 끼운다."""

    def __init__(self, prices=None, cash=10000000):
        self.prices = dict(prices or {})
        self.orders = []
        self.log = []
        self.tokens = 0
        self.script = []
        self.cash = cash
        self.time = "101600"
        self.expired = "2099-12-31 23:59:59"

    # ------------------------------------------------------------ 시장
    def set_price(self, sym, price):
        self.prices[sym] = price
        for o in self.orders:
            if o["symbol"] == sym and not o["cancelled"] and o["filled"] < o["qty"] and o["price"] is not None:
                if (o["side"] == "sell" and price >= o["price"]) or (o["side"] == "buy" and price <= o["price"]):
                    self._fill(o, o["price"])

    def _fill(self, o, price):
        o["avg"] = float(price)
        o["filled"] = o["qty"]
        o["time"] = self.time

    def holdings(self):
        h = {}
        for o in self.orders:
            sign = 1 if o["side"] == "buy" else -1
            h[o["symbol"]] = h.get(o["symbol"], 0) + sign * o["filled"]
        return h

    # ------------------------------------------------------------ HTTP
    def __call__(self, method, url, headers, body):
        u = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
        b = json.loads(body.decode("utf-8")) if body else {}
        self.log.append({"method": method, "path": u.path, "tr_id": headers.get("tr_id"), "query": q, "body": b,
                         "headers": dict(headers)})
        if u.path == "/oauth2/tokenP":
            self.tokens += 1
            return 200, {}, json.dumps({"access_token": "kt%d" % self.tokens, "token_type": "Bearer",
                                        "expires_in": 86400, "access_token_token_expired": self.expired})
        if self.script:
            return self.script.pop(0)
        if u.path.endswith("/trading/order-cash"):
            return self._order(b, headers.get("tr_id"))
        if u.path.endswith("/trading/order-rvsecncl"):
            o = next((x for x in self.orders if x["odno"] == b["ORGN_ODNO"]), None)
            if o is None:
                return 200, {}, json.dumps({"rt_cd": "1", "msg_cd": "40330000", "msg1": "원주문이 없습니다"})
            o["cancelled"] = True
            return 200, {}, json.dumps({"rt_cd": "0", "msg_cd": "40310000", "msg1": "취소 주문 완료",
                                        "output": {"krx_fwdg_ord_orgno": "00950", "odno": "9%s" % o["odno"],
                                                   "ord_tmd": self.time}})
        if u.path.endswith("/trading/inquire-daily-ccld"):
            rows = [o for o in self.orders if not q.get("ODNO") or o["odno"] == q["ODNO"]]
            return 200, {"tr_cont": "D"}, json.dumps({"rt_cd": "0", "msg_cd": "", "msg1": "조회되었습니다",
                                                      "ctx_area_fk100": "", "ctx_area_nk100": "",
                                                      "output1": [self._row(o) for o in rows], "output2": {}})
        if u.path.endswith("/trading/inquire-balance"):
            out1 = [{"pdno": s, "prdt_name": "합성종목", "hldg_qty": str(n), "ord_psbl_qty": str(n),
                     "pchs_avg_pric": "30500.0000", "prpr": str(self.prices.get(s, 0))}
                    for s, n in self.holdings().items() if n > 0]
            return 200, {"tr_cont": "D"}, json.dumps({"rt_cd": "0", "msg1": "조회되었습니다", "ctx_area_fk100": "",
                                                      "ctx_area_nk100": "", "output1": out1,
                                                      "output2": [{"dnca_tot_amt": str(self.cash),
                                                                   "prvs_rcdl_excc_amt": str(self.cash),
                                                                   "tot_evlu_amt": str(self.cash)}]})
        if u.path.endswith("/quotations/inquire-price"):
            sym = q.get("FID_INPUT_ISCD")
            return 200, {}, json.dumps({"rt_cd": "0", "msg1": "정상처리", "output": {
                "stck_prpr": str(self.prices.get(sym, 0)), "stck_mxpr": "39550", "stck_llam": "21350"}})
        return 404, {}, json.dumps({"rt_cd": "1", "msg_cd": "404", "msg1": "없는 경로"})

    def _order(self, b, tr_id):
        side = {"VTTC0012U": "buy", "VTTC0011U": "sell"}.get(tr_id)
        if side is None:
            return 200, {}, json.dumps({"rt_cd": "1", "msg_cd": "IGW00002", "msg1": "모의투자 TR 이 아닙니다"})
        price = None if b["ORD_DVSN"] == "01" else float(b["ORD_UNPR"])
        o = {"odno": "%010d" % (1000 + len(self.orders)), "orgno": "00950", "symbol": b["PDNO"], "side": side,
             "qty": int(b["ORD_QTY"]), "price": price, "filled": 0, "avg": 0.0, "cancelled": False, "time": self.time}
        self.orders.append(o)
        now = self.prices.get(b["PDNO"])
        if price is None:
            self._fill(o, now)
        elif (side == "sell" and now >= price) or (side == "buy" and now <= price):
            self._fill(o, price)
        return 200, {}, json.dumps({"rt_cd": "0", "msg_cd": "APBK0013", "msg1": "주문 전송 완료 되었습니다.",
                                    "output": {"KRX_FWDG_ORD_ORGNO": o["orgno"], "ODNO": o["odno"],
                                               "ORD_TMD": self.time}})

    @staticmethod
    def _row(o):
        return {"odno": o["odno"], "ord_gno_brno": o["orgno"], "pdno": o["symbol"], "prdt_name": "합성종목",
                "sll_buy_dvsn_cd": "02" if o["side"] == "buy" else "01", "ord_qty": str(o["qty"]),
                "ord_unpr": str(int(o["price"] or 0)), "tot_ccld_qty": str(o["filled"]), "avg_prvs": str(o["avg"]),
                "rmn_qty": str(0 if o["cancelled"] else o["qty"] - o["filled"]),
                "cncl_yn": "Y" if o["cancelled"] else "N", "ord_tmd": o["time"],
                "ord_dvsn_name": "시장가" if o["price"] is None else "지정가"}
