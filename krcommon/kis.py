"""한국투자증권 Open API 모의투자 클라이언트. 표준 라이브러리만 쓴다.

모의투자 서버만 부른다 (https://openapivts.koreainvestment.com:29443). 실전 서버 주소는 이 저장소에 없고,
인증 정보가 실전 계좌라고 하면 거절한다. 실전 주문은 이 단계에서 만들지 않는다.

쓰는 API (한국투자증권 공식 예제 koreainvestment/open-trading-api 에서 확인, 2026-10-30):
  POST /oauth2/tokenP                                  접근토큰. 하루 유효, 1분에 한 번 발급, 발급 때 알림톡
  POST /uapi/domestic-stock/v1/trading/order-cash      현금 주문. 모의 매수 VTTC0012U, 매도 VTTC0011U
  POST /uapi/domestic-stock/v1/trading/order-rvsecncl  정정·취소. 모의 VTTC0013U
  GET  /uapi/domestic-stock/v1/trading/inquire-daily-ccld  일별 주문체결. 모의 VTTC0081R (한 번에 15건)
  GET  /uapi/domestic-stock/v1/trading/inquire-balance     잔고. 모의 VTTC8434R
  GET  /uapi/domestic-stock/v1/quotations/inquire-price    현재가. FHKST01010100 (J = KRX)

인증 정보 (값은 화면에 찍지 않는다):
  1. 환경변수 KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT ("12345678-01")
  2. ~/.kis/credentials.json {"app_key", "app_secret", "account", "mock": true}
토큰은 ~/.kis/token.json 에 만료 시각과 함께 저장해 다시 쓴다. 발급할 때마다 알림톡이 가고 1분에 한 번만
발급되기 때문이다. 모의투자는 호출 한도가 낮아(공식 예제는 0.5초 간격) 호출 사이를 0.5초 띄운다.
"""

import datetime
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

MOCK = "https://openapivts.koreainvestment.com:29443"
PACE = 0.5            # 모의투자 호출 간격(초)
RATE_LIMIT = "EGW00201"
MAX_RETRY = 3
TOKEN_MARGIN = 600    # 만료 10분 전이면 새로 받는다
KST = datetime.timedelta(hours=9)

ORDER = "/uapi/domestic-stock/v1/trading/order-cash"
CANCEL = "/uapi/domestic-stock/v1/trading/order-rvsecncl"
CCLD = "/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
BALANCE = "/uapi/domestic-stock/v1/trading/inquire-balance"
PRICE = "/uapi/domestic-stock/v1/quotations/inquire-price"
TR = {"buy": "VTTC0012U", "sell": "VTTC0011U", "cancel": "VTTC0013U", "ccld": "VTTC0081R",
      "balance": "VTTC8434R", "price": "FHKST01010100"}


class KisError(Exception):
    def __init__(self, msg, code=None, status=None):
        Exception.__init__(self, msg)
        self.code = code
        self.status = status


def home_dir():
    return os.path.join(os.path.expanduser("~"), ".kis")


def split_account(text):
    """'12345678-01' 또는 '1234567801' -> ('12345678', '01')."""
    digits = "".join(ch for ch in str(text or "") if ch.isdigit())
    if len(digits) != 10:
        raise KisError("계좌번호는 앞 8자리와 뒤 2자리, 모두 10자리여야 합니다 (예: 50123456-01)")
    return digits[:8], digits[8:]


def find_credentials(env=None):
    """(app_key, app_secret, (cano, prdt)). 실전 계좌로 적혀 있으면 거절한다."""
    env = os.environ if env is None else env
    key, sec, acct = env.get("KIS_APP_KEY"), env.get("KIS_APP_SECRET"), env.get("KIS_ACCOUNT")
    mock = True
    if not (key and sec and acct):
        path = os.path.join(home_dir(), "credentials.json")
        if not os.path.exists(path):
            raise KisError("한국투자증권 모의투자 인증 정보가 없습니다. 본인 터미널에서 python krcommon/keys.py kis 로 "
                           "등록하세요. 채팅에는 붙여 넣지 마세요.")
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as e:
            raise KisError("%s 를 읽지 못했습니다: %s" % (path, e))
        key, sec, acct = doc.get("app_key"), doc.get("app_secret"), doc.get("account")
        mock = doc.get("mock", True)
    if mock is not True:
        raise KisError("실전 계좌는 이 단계에서 지원하지 않습니다. 모의투자 앱키와 모의투자 계좌만 씁니다.")
    if not (key and sec):
        raise KisError("앱키나 앱시크릿이 비었습니다. python krcommon/keys.py kis 로 다시 등록하세요.")
    return str(key).strip(), str(sec).strip(), split_account(acct)


def _urllib_transport(method, url, headers, body, timeout=20):
    """(status, headers dict, body text). HTTP 오류도 예외 없이 돌려준다."""
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            text = e.read().decode("utf-8", "replace")
        except Exception:
            text = ""
        hdrs = dict(e.headers or {})
        e.close()
        return e.code, hdrs, text
    except urllib.error.URLError as e:
        raise KisError("한국투자증권에 연결하지 못했습니다: %s" % (e.reason,))
    except OSError as e:
        raise KisError("네트워크 오류: %s" % (e,))


def _num(x, default=0.0):
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return default


class Client(object):
    """모의투자 클라이언트. transport, sleep, clock 을 바꿔 끼우면 네트워크 없이 시험할 수 있다."""

    def __init__(self, credentials=None, transport=None, sleep=time.sleep, clock=time.time, token_path=None):
        self._cred = credentials
        self.transport = transport or _urllib_transport
        self.sleep = sleep
        self.clock = clock
        self.base = MOCK
        self.token_path = token_path if token_path is not None else os.path.join(home_dir(), "token.json")
        self._token = None
        self._last = None
        self.calls = 0

    def cred(self):
        if self._cred is None:
            self._cred = find_credentials()
        return self._cred

    def account(self):
        return self.cred()[2]

    # ------------------------------------------------------------ 토큰

    def _load_token(self):
        if self._token and self._token["expires_at"] - self.clock() > TOKEN_MARGIN:
            return self._token
        if self.token_path and os.path.exists(self.token_path):
            try:
                with open(self.token_path, encoding="utf-8") as fh:
                    tok = json.load(fh)
                same_key = tok.get("key_tail") == self.cred()[0][-4:]
                if same_key and tok.get("access_token") and float(tok.get("expires_at", 0)) - self.clock() > TOKEN_MARGIN:
                    self._token = tok
                    return tok
            except (ValueError, OSError):
                pass
        return None

    def _issue_token(self):
        key, sec, _ = self.cred()
        body = json.dumps({"grant_type": "client_credentials", "appkey": key, "appsecret": sec}).encode("utf-8")
        self._pace()
        status, hdrs, text = self.transport("POST", self.base + "/oauth2/tokenP",
                                            {"Content-Type": "application/json"}, body)
        self.calls += 1
        try:
            doc = json.loads(text)
        except ValueError:
            doc = {}
        if status != 200 or not doc.get("access_token"):
            raise KisError("토큰 발급 실패 (HTTP %s): %s. 토큰은 1분에 한 번만 발급됩니다."
                           % (status, doc.get("error_description") or doc.get("msg1") or text[:200]), status=status)
        expires = None
        if doc.get("access_token_token_expired"):
            try:
                kst = datetime.datetime.strptime(doc["access_token_token_expired"], "%Y-%m-%d %H:%M:%S")
                expires = (kst - KST - datetime.datetime(1970, 1, 1)).total_seconds()
            except ValueError:
                expires = None
        if expires is None:
            expires = self.clock() + float(doc.get("expires_in") or 86400)
        tok = {"access_token": doc["access_token"], "expires_at": expires, "key_tail": key[-4:]}
        self._token = tok
        if self.token_path:
            try:
                d = os.path.dirname(self.token_path)
                if d:
                    os.makedirs(d, exist_ok=True)
                with open(self.token_path, "w", encoding="utf-8") as fh:
                    json.dump(tok, fh)
                try:
                    os.chmod(self.token_path, 0o600)
                except OSError:
                    pass
            except OSError:
                pass
        return tok

    def token(self):
        return (self._load_token() or self._issue_token())["access_token"]

    # ------------------------------------------------------------ 호출

    def _pace(self):
        now = self.clock()
        if self._last is not None and now - self._last < PACE:
            self.sleep(PACE - (now - self._last))
        self._last = self.clock()

    def call(self, method, path, tr_id, params=None, body=None, tr_cont=""):
        """(본문 dict, 응답 헤더). rt_cd 가 "0" 이 아니면 KisError."""
        key, sec, _ = self.cred()
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode([(k, "" if v is None else str(v)) for k, v in params.items()])
        data = json.dumps(body).encode("utf-8") if body is not None else None
        refreshed = False
        for attempt in range(MAX_RETRY + 1):
            self._pace()
            headers = {"Content-Type": "application/json; charset=utf-8", "authorization": "Bearer " + self.token(),
                       "appkey": key, "appsecret": sec, "tr_id": tr_id, "custtype": "P", "tr_cont": tr_cont}
            status, hdrs, text = self.transport(method, url, headers, data)
            self.calls += 1
            try:
                doc = json.loads(text) if text else {}
            except ValueError:
                raise KisError("응답이 JSON 이 아닙니다 (%s, HTTP %s)" % (path, status), status=status)
            # 업무 오류는 msg_cd/msg1, 게이트웨이 오류(EGW...)는 error_code/error_description 으로 온다
            code = doc.get("msg_cd") or doc.get("error_code")
            msg = (doc.get("msg1") or doc.get("error_description") or "").strip()
            if code == RATE_LIMIT and attempt < MAX_RETRY:
                self.sleep(1.0 + attempt)
                continue
            token_trouble = status in (401, 403) or "token" in msg.lower() or "토큰" in msg
            if token_trouble and not refreshed:
                self._token = None
                if self.token_path and os.path.exists(self.token_path):
                    try:
                        os.remove(self.token_path)
                    except OSError:
                        pass
                refreshed = True
                continue
            if status == 200 and str(doc.get("rt_cd")) == "0":
                return doc, {k.lower(): v for k, v in hdrs.items()}
            raise KisError("%s (%s%s)" % (msg or "요청 실패", code or "코드 없음",
                                          "" if status == 200 else ", HTTP %s" % status), code, status)
        raise KisError("호출 한도를 계속 넘었습니다 (%s). 잠시 뒤 다시 하세요." % RATE_LIMIT, RATE_LIMIT)

    # ------------------------------------------------------------ 주문

    def order(self, side, symbol, qty, price=None):
        """현금 주문. price 가 없으면 시장가("01"), 있으면 지정가("00"). {odno, orgno, time}."""
        if side not in ("buy", "sell"):
            raise KisError("side 는 buy 나 sell 입니다")
        if int(qty) <= 0:
            raise KisError("수량은 1주 이상이어야 합니다")
        cano, prdt = self.account()
        body = {"CANO": cano, "ACNT_PRDT_CD": prdt, "PDNO": str(symbol),
                "ORD_DVSN": "01" if price is None else "00", "ORD_QTY": str(int(qty)),
                "ORD_UNPR": "0" if price is None else str(int(round(float(price)))),
                "EXCG_ID_DVSN_CD": "KRX", "SLL_TYPE": "01" if side == "sell" else "", "CNDT_PRIC": ""}
        doc, _ = self.call("POST", ORDER, TR[side], body=body)
        out = doc.get("output") or {}
        return {"odno": out.get("ODNO") or out.get("odno"), "orgno": out.get("KRX_FWDG_ORD_ORGNO") or out.get("krx_fwdg_ord_orgno"),
                "time": out.get("ORD_TMD") or out.get("ord_tmd"), "msg": doc.get("msg1")}

    def cancel(self, orgno, odno):
        """남은 수량 전부 취소."""
        cano, prdt = self.account()
        body = {"CANO": cano, "ACNT_PRDT_CD": prdt, "KRX_FWDG_ORD_ORGNO": str(orgno or ""), "ORGN_ODNO": str(odno),
                "ORD_DVSN": "00", "RVSE_CNCL_DVSN_CD": "02", "ORD_QTY": "0", "ORD_UNPR": "0", "QTY_ALL_ORD_YN": "Y",
                "EXCG_ID_DVSN_CD": "KRX", "CNDT_PRIC": ""}
        doc, _ = self.call("POST", CANCEL, TR["cancel"], body=body)
        out = doc.get("output") or {}
        return {"odno": out.get("odno") or out.get("ODNO"), "msg": doc.get("msg1")}

    # ------------------------------------------------------------ 조회

    def orders(self, day, odno="", symbol=""):
        """그날 주문·체결 목록 (연속조회 포함). day 는 date."""
        cano, prdt = self.account()
        ymd = day.strftime("%Y%m%d")
        fk = nk = ""
        tr_cont = ""
        rows = []
        for _ in range(20):
            params = {"CANO": cano, "ACNT_PRDT_CD": prdt, "INQR_STRT_DT": ymd, "INQR_END_DT": ymd,
                      "SLL_BUY_DVSN_CD": "00", "PDNO": symbol or "", "CCLD_DVSN": "00", "INQR_DVSN": "00",
                      "INQR_DVSN_3": "00", "ORD_GNO_BRNO": "", "ODNO": odno or "", "INQR_DVSN_1": "",
                      "CTX_AREA_FK100": fk, "CTX_AREA_NK100": nk, "EXCG_ID_DVSN_CD": "KRX"}
            doc, hdrs = self.call("GET", CCLD, TR["ccld"], params=params, tr_cont=tr_cont)
            for r in doc.get("output1") or []:
                rows.append({"odno": r.get("odno"), "orgno": r.get("ord_gno_brno"), "symbol": r.get("pdno"),
                             "name": r.get("prdt_name"), "side": "buy" if r.get("sll_buy_dvsn_cd") == "02" else "sell",
                             "qty": int(_num(r.get("ord_qty"))), "price": _num(r.get("ord_unpr")),
                             "filled": int(_num(r.get("tot_ccld_qty"))), "avg": _num(r.get("avg_prvs")),
                             "remaining": int(_num(r.get("rmn_qty"))), "cancelled": r.get("cncl_yn") == "Y",
                             "time": r.get("ord_tmd"), "kind": r.get("ord_dvsn_name")})
            if (hdrs.get("tr_cont") or "") not in ("M", "F"):
                break
            fk, nk, tr_cont = doc.get("ctx_area_fk100") or "", doc.get("ctx_area_nk100") or "", "N"
        return rows

    def balance(self):
        """{cash, positions: [{symbol, name, qty, sellable, avg, price}]}."""
        cano, prdt = self.account()
        fk = nk = ""
        tr_cont = ""
        positions, summary = [], {}
        for _ in range(10):
            params = {"CANO": cano, "ACNT_PRDT_CD": prdt, "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02",
                      "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00",
                      "CTX_AREA_FK100": fk, "CTX_AREA_NK100": nk}
            doc, hdrs = self.call("GET", BALANCE, TR["balance"], params=params, tr_cont=tr_cont)
            for r in doc.get("output1") or []:
                if int(_num(r.get("hldg_qty"))) > 0:
                    positions.append({"symbol": r.get("pdno"), "name": r.get("prdt_name"),
                                      "qty": int(_num(r.get("hldg_qty"))), "sellable": int(_num(r.get("ord_psbl_qty"))),
                                      "avg": _num(r.get("pchs_avg_pric")), "price": _num(r.get("prpr"))})
            out2 = doc.get("output2") or []
            if isinstance(out2, list) and out2:
                summary = out2[0]
            elif isinstance(out2, dict):
                summary = out2
            if (hdrs.get("tr_cont") or "") not in ("M", "F"):
                break
            fk, nk, tr_cont = doc.get("ctx_area_fk100") or "", doc.get("ctx_area_nk100") or "", "N"
        return {"cash": _num(summary.get("dnca_tot_amt")), "cash_d2": _num(summary.get("prvs_rcdl_excc_amt")),
                "total": _num(summary.get("tot_evlu_amt")), "positions": positions}

    def price(self, symbol):
        """KRX 현재가 {price, upper, lower}."""
        doc, _ = self.call("GET", PRICE, TR["price"], params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol})
        out = doc.get("output") or {}
        p = _num(out.get("stck_prpr"), None)
        if not p:
            raise KisError("%s 현재가가 비었습니다" % symbol)
        return {"price": p, "upper": _num(out.get("stck_mxpr"), None), "lower": _num(out.get("stck_llam"), None)}
