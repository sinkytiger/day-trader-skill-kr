"""토스증권 Open API 시세 클라이언트. 표준 라이브러리만 쓴다.

이 팩은 시세·종목 정보만 읽는다 (캔들, 현재가, 호가창, 상·하한가, 종목 정보, 유의사항, 장 운영 정보, 랭킹). 주문·계좌 API 는 부르지 않는다.

인증 정보 (아래 순서로 찾는다. 값은 화면에 찍지 않는다):
  1. 환경변수 TOSS_CLIENT_ID, TOSS_CLIENT_SECRET
  2. 환경변수 TOSS_CREDENTIALS 가 가리키는 JSON 파일 {"client_id": ..., "client_secret": ...}
  3. ~/.tossinvest/credentials.json
토큰은 ~/.tossinvest/token.json 에 만료 시각과 함께 저장한다. 클라이언트당 토큰이
하나만 유효해서, 스크립트마다 새로 받으면 앞의 토큰이 무효가 되기 때문이다.

호출 한도: 그룹별 초당 요청 수를 넘지 않게 쉬어 가며 부른다. 429 를 받으면
Retry-After 만큼 기다렸다가 다시 부른다. 403 은 대부분 허용 IP 미등록이다.

토스증권 자료는 "투자자 본인의 매매 목적"으로만 쓸 수 있다. 받은 자료는
trader-kr/cache/ 에만 저장하고 저장소에는 올리지 않는다.
"""

import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://openapi.tossinvest.com"
UA = "day-trader-skill-kr/0.1 (+https://github.com/sinkytiger/day-trader-skill-kr)"
# 문서의 그룹별 초당 한도에 여유를 둔 값
TPS = {"AUTH": 4, "STOCK": 4, "STOCK_ALL": 1, "MARKET_INFO": 2, "MARKET_DATA": 12,
       "MARKET_DATA_CHART": 16, "RANKING": 4}
MAX_RETRY = 4
TOKEN_MARGIN = 300   # 만료 5분 전이면 새로 받는다


class TossError(Exception):
    def __init__(self, msg, status=None, code=None):
        Exception.__init__(self, msg)
        self.status = status
        self.code = code


def home_dir():
    return os.path.join(os.path.expanduser("~"), ".tossinvest")


def find_credentials(env=None):
    env = os.environ if env is None else env
    cid, sec = env.get("TOSS_CLIENT_ID"), env.get("TOSS_CLIENT_SECRET")
    if cid and sec:
        return cid.strip(), sec.strip()
    for path in (env.get("TOSS_CREDENTIALS"), os.path.join(home_dir(), "credentials.json")):
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    doc = json.load(fh)
            except ValueError:
                raise TossError("%s 가 올바른 JSON 이 아닙니다" % path)
            if doc.get("client_id") and doc.get("client_secret"):
                return str(doc["client_id"]).strip(), str(doc["client_secret"]).strip()
            raise TossError("%s 에 client_id 와 client_secret 이 없습니다" % path)
    raise TossError("토스증권 Open API 인증 정보가 없습니다. 토스증권 PC 웹 설정 > Open API 에서 발급받아 "
                    "~/.tossinvest/credentials.json 에 {\"client_id\": \"...\", \"client_secret\": \"...\"} "
                    "로 저장하거나 TOSS_CLIENT_ID, TOSS_CLIENT_SECRET 환경변수에 넣으세요. 채팅에는 붙여 넣지 마세요.")


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
        raise TossError("토스증권에 연결하지 못했습니다: %s" % (e.reason,))
    except OSError as e:
        raise TossError("네트워크 오류: %s" % (e,))


class Client(object):
    """토스증권 Open API 시세 클라이언트.

    transport(method, url, headers, body) -> (status, headers, text) 를 바꿔 끼우면
    네트워크 없이 시험할 수 있다. sleep, clock 도 같다."""

    def __init__(self, credentials=None, transport=None, sleep=time.sleep, clock=time.time,
                 token_path=None, base=BASE):
        self._cred = credentials
        self.transport = transport or _urllib_transport
        self.sleep = sleep
        self.clock = clock
        self.base = base
        self.token_path = token_path if token_path is not None else os.path.join(home_dir(), "token.json")
        self._token = None
        self._last = {}
        self.calls = 0

    # ------------------------------------------------------------ 토큰

    def _load_token(self):
        if self._token:
            return self._token
        if self.token_path and os.path.exists(self.token_path):
            try:
                with open(self.token_path, encoding="utf-8") as fh:
                    tok = json.load(fh)
                if tok.get("access_token") and float(tok.get("expires_at", 0)) - self.clock() > TOKEN_MARGIN:
                    self._token = tok
                    return tok
            except (ValueError, OSError):
                pass
        return None

    def _issue_token(self):
        cid, sec = self._cred or find_credentials()
        self._throttle("AUTH")
        body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": cid,
                                       "client_secret": sec}).encode("ascii")
        status, hdrs, text = self.transport("POST", self.base + "/oauth2/token",
                                            {"Content-Type": "application/x-www-form-urlencoded",
                                             "Accept": "application/json", "User-Agent": UA}, body)
        self.calls += 1
        if status == 403:
            raise TossError("토큰 발급이 막혔습니다 (403). 토스증권 설정 > Open API > 허용 IP 관리에 "
                            "이 컴퓨터의 공인 IP 를 등록했는지 확인하세요.", 403, "access_denied")
        if status != 200:
            raise TossError("토큰 발급 실패 (HTTP %s): %s" % (status, _err_text(text)), status, _err_code(text))
        try:
            doc = json.loads(text)
            tok = {"access_token": doc["access_token"],
                   "expires_at": self.clock() + float(doc.get("expires_in", 3600))}
        except (ValueError, KeyError):
            raise TossError("토큰 응답 형식이 이상합니다")
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

    def _throttle(self, group):
        tps = TPS.get(group, 4)
        gap = 1.0 / tps
        last = self._last.get(group)
        now = self.clock()
        if last is not None and now - last < gap:
            self.sleep(gap - (now - last))
        self._last[group] = self.clock()

    def get(self, path, params=None, group="MARKET_DATA"):
        q = urllib.parse.urlencode([(k, _q(v)) for k, v in (params or {}).items() if v is not None])
        url = self.base + path + ("?" + q if q else "")
        refreshed = False
        for attempt in range(MAX_RETRY + 1):
            self._throttle(group)
            status, hdrs, text = self.transport("GET", url, {"Authorization": "Bearer " + self.token(),
                                                            "Accept": "application/json", "User-Agent": UA}, None)
            self.calls += 1
            if status == 200:
                try:
                    doc = json.loads(text)
                except ValueError:
                    raise TossError("토스증권 응답이 JSON 이 아닙니다 (%s)" % path)
                return doc.get("result") if isinstance(doc, dict) else doc
            if status == 401 and not refreshed:
                self._token = None
                if self.token_path and os.path.exists(self.token_path):
                    try:
                        os.remove(self.token_path)
                    except OSError:
                        pass
                self._issue_token()
                refreshed = True
                continue
            if status == 429 and attempt < MAX_RETRY:
                wait = _retry_after(hdrs)
                self.sleep(wait if wait is not None else (2 ** attempt) + random.random() * 0.5)
                continue
            if status >= 500 and attempt < 2 and _err_code(text) != "maintenance":
                self.sleep(1.0 + attempt)
                continue
            if status == 403:
                raise TossError("토스증권이 호출을 막았습니다 (403 %s). 허용 IP 에 이 컴퓨터의 공인 IP 가 "
                                "등록됐는지 확인하세요." % (_err_code(text) or ""), 403, _err_code(text))
            raise TossError("토스증권 %s 응답 HTTP %s: %s" % (path, status, _err_text(text)), status, _err_code(text))
        raise TossError("토스증권 호출 한도 초과가 계속됩니다 (%s). 잠시 뒤 다시 하세요." % path, 429)

    # ------------------------------------------------------------ 엔드포인트

    def candles(self, symbol, interval="1m", count=200, before=None, adjusted=True):
        """한 쪽(최대 200개). (봉 목록 최신순, nextBefore)."""
        res = self.get("/api/v1/candles", {"symbol": symbol, "interval": interval, "count": count,
                                           "before": before, "adjusted": "true" if adjusted else "false"},
                       "MARKET_DATA_CHART") or {}
        return res.get("candles") or [], res.get("nextBefore")

    def stocks(self, symbols):
        out = []
        syms = list(symbols)
        for i in range(0, len(syms), 200):
            out.extend(self.get("/api/v1/stocks", {"symbols": ",".join(syms[i:i + 200])}, "STOCK") or [])
        return out

    def list_stocks(self, market, security_type="STOCK", common_share=None):
        return self.get("/api/v1/stocks/all", {"market": market, "securityType": security_type,
                                               "commonShare": None if common_share is None else
                                               ("true" if common_share else "false")}, "STOCK_ALL") or []

    def warnings(self, symbol):
        return self.get("/api/v1/stocks/%s/warnings" % urllib.parse.quote(symbol), None, "STOCK") or []

    def orderbook(self, symbol):
        return self.get("/api/v1/orderbook", {"symbol": symbol}, "MARKET_DATA") or {}

    def price_limits(self, symbol):
        return self.get("/api/v1/price-limits", {"symbol": symbol}, "MARKET_DATA") or {}

    def prices(self, symbols):
        out = []
        syms = list(symbols)
        for i in range(0, len(syms), 200):
            out.extend(self.get("/api/v1/prices", {"symbols": ",".join(syms[i:i + 200])}, "MARKET_DATA") or [])
        return out

    def calendar(self, date=None):
        return self.get("/api/v1/market-calendar/KR", {"date": date}, "MARKET_INFO") or {}

    def rankings(self, kind="MARKET_TRADING_AMOUNT", duration="realtime", count=100, exclude_caution=True):
        res = self.get("/api/v1/rankings", {"type": kind, "marketCountry": "KR", "duration": duration,
                                            "excludeInvestmentCaution": "true" if exclude_caution else "false",
                                            "count": count}, "RANKING") or {}
        return res


def _q(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _retry_after(hdrs):
    for k, v in (hdrs or {}).items():
        if str(k).lower() == "retry-after":
            try:
                return max(0.0, float(v))
            except (TypeError, ValueError):
                return None
    return None


def _err_doc(text):
    try:
        doc = json.loads(text)
        return (doc or {}).get("error") or {}
    except (ValueError, AttributeError):
        return {}


def _err_code(text):
    return _err_doc(text).get("code")


def _err_text(text):
    e = _err_doc(text)
    if e:
        return "%s %s" % (e.get("code") or "", e.get("message") or "")
    return (text or "")[:200]
