"""토스증권 본인 계좌 읽기: 계좌 목록, 보유 주식, 주문 내역. 읽기만 한다. 표준 라이브러리만 쓴다.

이 팩에서 토스증권 계좌 API 를 부르는 곳은 이 파일 하나이고, 모두 GET 이다.
  GET /api/v1/accounts            계좌 목록. 다른 호출의 계좌 헤더에 넣을 accountSeq 를 얻는다
  GET /api/v1/holdings            보유 주식
  GET /api/v1/orders?status=...   주문 목록. OPEN 은 진행 중, CLOSED 는 끝난 주문 (체결 수량·평균가·수수료·세금 포함)
주문 넣기·정정·취소, 조건주문, 매수 가능 금액, 매도 가능 수량은 부르지 않는다. 이 파일에 POST 나 그런
경로가 들어오면 tests/test_repo.py 가 실패한다.

토스증권 명세: 주문 목록에는 Open API 가 받는 호가(지정가·시장가·장마감지정가)로 낸 주문만 나온다.
장전·장후 시간외 종가 같은 주문은 보이지 않는다. 계좌번호는 끝 네 자리만 화면에 남긴다.
"""

from . import toss

HEADER = "X-Tossinvest-Account"
PAGE = 100          # CLOSED 목록 한 쪽 최대
MAX_PAGES = 30


class AccountError(Exception):
    pass


def num(x):
    """명세의 decimal 문자열 -> float. 없거나 이상하면 None."""
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def mask(account_no):
    s = "".join(ch for ch in str(account_no or "") if ch.isalnum())
    return ("*" * max(0, len(s) - 4) + s[-4:]) if s else "?"


class Account(object):
    """본인 계좌 하나. client 는 toss.Client (시험에서는 가짜 transport 를 끼운 것)."""

    def __init__(self, client=None, account_seq=None):
        self.c = client or toss.Client()
        self._seq = int(account_seq) if account_seq is not None else None
        self.masked = None

    def _get(self, path, params, group, with_account=True):
        headers = {HEADER: str(self.seq())} if with_account else None
        try:
            return self.c.get(path, params, group, headers=headers)
        except toss.TossError as e:
            if e.status == 403 and e.code == "forbidden":
                raise AccountError("계좌를 읽을 권한이 없습니다 (403 forbidden). 토스증권 PC 웹 설정 > Open API 에서 "
                                   "이 키로 계좌·주문 내역을 볼 수 있는지 확인하세요.")
            if e.status == 404 and e.code == "account-not-found":
                raise AccountError("계좌를 찾지 못했습니다 (account-not-found). --account-seq 를 빼고 다시 하세요.")
            raise AccountError(str(e))

    def accounts(self):
        res = self._get("/api/v1/accounts", None, "ACCOUNT", with_account=False) or []
        return [a for a in res if isinstance(a, dict)]

    def seq(self):
        if self._seq is not None:
            return self._seq
        accs = [a for a in self.accounts() if (a.get("accountType") or "BROKERAGE") == "BROKERAGE"]
        if not accs:
            raise AccountError("토스증권 종합매매 계좌가 없습니다")
        if len(accs) > 1:
            raise AccountError("계좌가 %d개입니다 (%s). --account-seq 로 하나를 고르세요" % (
                len(accs), ", ".join("%s: %s" % (a.get("accountSeq"), mask(a.get("accountNo"))) for a in accs)))
        self._seq = int(accs[0]["accountSeq"])
        self.masked = mask(accs[0].get("accountNo"))
        return self._seq

    def holdings(self):
        """(국내 보유 목록, 뺀 해외 종목 수). 국내 하나는 {symbol, name, qty, avg, last, value, pnl, rate}.
        pnl, rate 는 수수료·세금 뒤 값 (토스증권 계산)."""
        res = self._get("/api/v1/holdings", None, "ASSET") or {}
        kr, other = [], 0
        for it in res.get("items") or []:
            if not isinstance(it, dict):
                continue
            if it.get("marketCountry") != "KR" or it.get("currency") != "KRW":
                other += 1
                continue
            mv, pl = it.get("marketValue") or {}, it.get("profitLoss") or {}
            kr.append({"symbol": str(it.get("symbol") or ""), "name": it.get("name"),
                       "qty": num(it.get("quantity")) or 0.0, "avg": num(it.get("averagePurchasePrice")),
                       "last": num(it.get("lastPrice")), "value": num(mv.get("amountAfterCost")),
                       "pnl": num(pl.get("amountAfterCost")), "rate": num(pl.get("rateAfterCost"))})
        return kr, other

    def orders(self, status, day_from=None, day_to=None):
        """주문 목록 전부 (쪽을 넘겨 가며). status 는 OPEN 또는 CLOSED, 날짜는 주문 시각 기준 'YYYY-MM-DD' (KST)."""
        if status not in ("OPEN", "CLOSED"):
            raise AccountError("status 는 OPEN 또는 CLOSED 입니다")
        out, cursor = [], None
        for _ in range(MAX_PAGES):
            params = {"status": status, "from": day_from, "to": day_to}
            if status == "CLOSED":
                params.update(limit=PAGE, cursor=cursor)
            res = self._get("/api/v1/orders", params, "ORDER_HISTORY") or {}
            out.extend(o for o in res.get("orders") or [] if isinstance(o, dict))
            cursor = res.get("nextCursor")
            if status == "OPEN" or not res.get("hasNext") or not cursor:
                return out
        raise AccountError("주문 목록이 %d쪽을 넘습니다. 날짜를 좁히세요" % MAX_PAGES)


def fill_of(order):
    """체결이 있는 국내 주문이면 {id, symbol, side, qty, avg, at, fees, status}, 아니면 None.
    fees 는 수수료 + 세금 (토스증권이 준 값, 모르면 None)."""
    if order.get("currency") != "KRW":
        return None
    ex = order.get("execution") or {}
    qty = num(ex.get("filledQuantity")) or 0.0
    avg = num(ex.get("averageFilledPrice"))
    if qty <= 0 or not avg or order.get("side") not in ("BUY", "SELL"):
        return None
    com, tax = num(ex.get("commission")), num(ex.get("tax"))
    return {"id": str(order.get("orderId")), "symbol": str(order.get("symbol") or ""), "side": order["side"],
            "qty": qty, "avg": avg, "at": ex.get("filledAt") or order.get("orderedAt"),
            "fees": None if com is None else com + (tax or 0.0), "status": order.get("status")}
