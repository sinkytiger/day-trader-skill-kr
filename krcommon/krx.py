"""국내 주식시장 규칙: 한국 시간, 장 운영 시간, 호가 단위, 가격제한폭, 매매 비용.

표준 라이브러리만 쓴다. 한국 시간은 UTC+9 고정이다(서머타임 없음).
모든 시각은 "한국 시간 naive datetime" 또는 유닉스 초로 다룬다.

장 운영 시간 기본값 (한국거래소 정규장):
  09:00 ~ 15:20  접속매매. 단타 판단과 시뮬레이션은 이 구간만 쓴다.
  15:20 ~ 15:30  종가 단일가 매매. 15:30에 한 번 체결된다.
신년 첫 거래일이나 수능일처럼 시간이 바뀌는 날은 토스증권 장 운영 정보
(market-calendar/KR)를 쓴다. 그 정보가 없으면 평일 기본값을 쓰고 그렇다고 알린다.

호가 단위 (2023-01-25 이후 유가증권·코스닥 공통 주문 가격 단위, ETF·ETN 제외):
  2,000원 미만 1원 / 5,000원 미만 5원 / 20,000원 미만 10원 / 50,000원 미만 50원
  200,000원 미만 100원 / 500,000원 미만 500원 / 그 이상 1,000원
  손절가, 목표가, 지정가는 이 단위에 맞춘다. 2026-10-29 토스증권 호가창으로 확인했다.
  체결가는 다르다: 토스증권 1분봉에는 호가 사이 가격(삼성전자 245,250원 등)이 섞여 나온다.
  최우선 매수·매도 호가의 한가운데서 체결되는 중간가호가 체결이다. 2025-03 넥스트레이드
  출범 때 한국거래소와 넥스트레이드가 함께 도입했다 (금융위원회 보도자료). 5원 단위 종목에서는
  4,012.5원 같은 값이 원 단위로 버려져 4,012원으로 나온다. 2026-10-29
  기준으로 소형주는 체결가의 1~2%, 삼성전자는 10% 정도였다. 그래서 체결가로 들어온 진입가는
  매수 호가 쪽으로 올려(ceil_tick) 계산한다. krcommon/doctor.py 가 호가창 간격과 이 표가
  맞는지 점검한다.

가격제한폭: 기준가(보통 전일 종가)의 30% 를 기준가 호가 단위로 버린 폭을 위아래로 똑같이
적용한다 (2026-10-29 삼성전자 상한가 314,500원, 하한가 169,500원과 맞음).

매도 거래세 기본값 0.20%: 2026-01-01 매도분부터 코스피 증권거래세 0.05%와
농어촌특별세 0.15%, 코스닥 0.20%. 수수료율은 계좌마다 다르니 설정에서 바꾼다.
"""

import datetime
import json
import os
import re
import unicodedata
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

KST = datetime.timedelta(hours=9)
EPOCH = datetime.datetime(1970, 1, 1)
OPEN = datetime.time(9, 0)
AUCTION = datetime.time(15, 20)
CLOSE = datetime.time(15, 30)
STEP_5M = 300

TICK_TABLE = ((2000, 1), (5000, 5), (20000, 10), (50000, 50), (200000, 100), (500000, 500))
TOP_TICK = 1000
LIMIT_PCT = Decimal("0.30")

DEFAULT_COMMISSION = 0.00015   # 토스증권 문서의 예시 값. 본인 계좌 수수료율로 바꾼다.
DEFAULT_SELL_TAX = 0.002       # 2026년 매도분, 코스피·코스닥 공통 0.20%

# 하루 단타 규칙의 기본값. trader-kr/config.json 에서 바꾼다.
CONFIG_DEFAULTS = {
    "budget": None,
    "cash_buffer_pct": 10,
    "max_position_pct": 75,
    "max_order_krw": 4000000,
    "min_order_krw": 10000,
    "max_loss_per_trade_krw": 50000,
    "max_daily_loss_krw": 150000,
    "max_trades_per_day": 3,
    "max_orders_per_day": 10,
    "min_reward_risk": 1.0,
    "require_stop": True,
    "no_entries_before": "09:15",
    "no_entries_after": "14:50",
    "flat_by": "15:15",
    "commission_rate": DEFAULT_COMMISSION,
    "sell_tax_rate": DEFAULT_SELL_TAX,
    "send_orders": False,
    "blocked": [],
    "allow_preferred": False,
    "allow_etf": False,
    "allow_margin": False,
    "allow_short": False,
    "require_market_status": True,
    "max_cost_to_risk": 0.3,
    "use_book_slippage": True,
}

SYMBOL = re.compile(r"^[0-9A-Z]{6}$")


class KrxError(Exception):
    pass


def utf8_stdio():
    """Windows 에서 출력을 파이프로 받으면 cp949 로 나가 Claude 가 깨진 글자를 읽는다.
    PYTHONIOENCODING 을 따로 정하지 않았으면 UTF-8 로 맞춘다."""
    import sys
    if os.environ.get("PYTHONIOENCODING"):
        return
    for s in (sys.stdout, sys.stderr):
        try:
            if (getattr(s, "encoding", "") or "").lower().replace("-", "") != "utf8":
                s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


# ---------------------------------------------------------------- 시각

def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def kst_now():
    return utc_now() + KST


def kst_of(ts):
    """유닉스 초 -> 한국 시간 naive datetime (컴퓨터 시간대와 무관)."""
    return EPOCH + datetime.timedelta(seconds=int(ts)) + KST


def ts_of(kst_dt):
    return int((kst_dt.replace(tzinfo=None) - KST - EPOCH).total_seconds())


def iso_kst(kst_dt):
    return kst_dt.strftime("%Y-%m-%dT%H:%M:%S+09:00")


def parse_iso(text):
    """'2026-03-25T09:30:00.123+09:00' 같은 ISO 시각 -> 한국 시간 naive datetime."""
    if not text:
        return None
    t = str(text).strip().replace("Z", "+00:00")
    m = re.match(r"^(.*T\d{2}:\d{2}:\d{2})(\.\d+)?([+-]\d{2}:\d{2})?$", t)
    if m:
        t = m.group(1) + (m.group(3) or "")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise KrxError("시각 형식이 이상합니다: %r" % text)
    if dt.tzinfo is None:
        return dt
    return (dt - dt.utcoffset()).replace(tzinfo=None) + KST


def parse_now(text):
    """--now 값 -> 한국 시간. '2026-10-28 10:15', '10:15'(오늘), ISO+오프셋 모두 받는다."""
    if not text:
        return kst_now()
    t = str(text).strip()
    if t.upper().endswith(" KST"):
        t = t[:-4].strip()
    if re.match(r"^\d{1,2}:\d{2}$", t):
        t = "%s %s" % (kst_now().date().isoformat(), t.zfill(5))
    try:
        return parse_iso(t.replace(" ", "T") if "T" not in t else t)
    except KrxError:
        raise KrxError('--now 는 "2026-10-28 10:15" 같은 한국 시간이어야 합니다 (받은 값 %r)' % text)


def parse_hhmm(text, what):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise KrxError("%s 는 15:15 같은 시각이어야 합니다 (받은 값 %r)" % (what, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def hm(t):
    return "%02d:%02d" % (t.hour, t.minute)


def minutes_of(t):
    return t.hour * 60 + t.minute


# ---------------------------------------------------------------- 장 운영 시간

class Session(object):
    """하루 정규장. open ~ auction 이 접속매매, auction ~ close 가 종가 단일가."""

    def __init__(self, day, open_t=OPEN, auction_t=AUCTION, close_t=CLOSE, source="default", note=""):
        self.day = day
        self.open_t, self.auction_t, self.close_t = open_t, auction_t, close_t
        self.source = source
        self.note = note

    @property
    def open_dt(self):
        return datetime.datetime.combine(self.day, self.open_t)

    @property
    def auction_dt(self):
        return datetime.datetime.combine(self.day, self.auction_t)

    @property
    def close_dt(self):
        return datetime.datetime.combine(self.day, self.close_t)

    @property
    def standard(self):
        return (self.open_t, self.auction_t, self.close_t) == (OPEN, AUCTION, CLOSE)

    def slots(self, step=STEP_5M):
        """접속매매 구간의 봉 개수 (5분봉이면 기본 76개)."""
        return int((self.auction_dt - self.open_dt).total_seconds()) // step

    def shift(self, t):
        """기본 장 시간 기준 시각 t 를 이 날의 장 시간에 맞춰 옮긴다.
        개장이 늦은 날은 개장 기준 시각(진입 시작)이 늦어지고,
        마감 기준 시각(진입 마감, 청산)은 접속매매 끝에 맞춰 옮긴다."""
        if self.standard:
            return t
        base = datetime.datetime.combine(self.day, t)
        if minutes_of(t) < 12 * 60:
            moved = base + (self.open_dt - datetime.datetime.combine(self.day, OPEN))
        else:
            moved = base + (self.auction_dt - datetime.datetime.combine(self.day, AUCTION))
        return moved.time()

    def to_doc(self):
        return {"date": self.day.isoformat(), "open": hm(self.open_t), "auction": hm(self.auction_t),
                "close": hm(self.close_t), "source": self.source, "note": self.note}


def default_session(day):
    """평일이면 기본 장 시간, 주말이면 None. 공휴일은 모른다 (source='default')."""
    if day.weekday() >= 5:
        return None
    return Session(day, note="토스증권 장 운영 정보 없이 평일 기본 시간을 썼습니다. 휴장일인지는 확인하지 않았습니다")


def parse_day(text):
    """'2026-10-28', '20261028', '2026-10-28T00:00:00+09:00' -> date. 못 읽으면 None."""
    s = str(text or "").strip()
    try:
        if len(s) == 8 and s.isdigit():
            return datetime.date(int(s[:4]), int(s[4:6]), int(s[6:]))
        return datetime.date.fromisoformat(s[:10])
    except ValueError:
        return None


def session_from_calendar_day(doc):
    """토스증권 market-calendar/KR 의 KrMarketDay 하나 -> Session 또는 None(휴장)."""
    if not isinstance(doc, dict) or not doc.get("date"):
        raise KrxError("장 운영 정보 형식이 이상합니다")
    day = parse_day(doc["date"])
    if day is None:
        raise KrxError("장 운영 정보의 날짜를 읽지 못했습니다: %r" % doc["date"])
    integ = doc.get("integrated") or {}
    reg = integ.get("regularMarket") if isinstance(integ, dict) else None
    if not reg:
        return None
    start = parse_iso(reg.get("startTime"))
    end = parse_iso(reg.get("endTime"))
    auction = parse_iso(reg.get("singlePriceAuctionStartTime")) if reg.get("singlePriceAuctionStartTime") else None
    if start is None or end is None:
        raise KrxError("장 운영 정보에 정규장 시작·종료가 없습니다")
    start_t = start.replace(second=0).time()
    end_t = end.replace(second=0).time()
    auction_t = auction.replace(second=0).time() if auction else (
        datetime.datetime.combine(day, end_t) - datetime.timedelta(minutes=10)).time()
    return Session(day, start_t, auction_t, end_t, source="toss-calendar")


class Calendar(object):
    """날짜 -> Session. 토스증권에서 받은 날은 그대로, 없는 날은 평일 기본값."""

    def __init__(self, days=None):
        self.days = dict(days or {})     # "YYYY-MM-DD" -> Session 또는 None(휴장)

    def add_response(self, result, asked=None):
        """토스 응답(전일·당일·익일 영업일)을 더한다. asked 날짜가 응답에 없고 전 영업일과
        다음 영업일 사이에 있으면 그날은 휴장이다 (예: 평일 공휴일)."""
        seen = {}
        for key in ("previousBusinessDay", "today", "nextBusinessDay"):
            d = (result or {}).get(key)
            if d:
                s = session_from_calendar_day(d)
                day = parse_day(d["date"])
                self.days[day.isoformat()] = s
                seen[key] = day
        if asked is not None and asked.isoformat() not in self.days:
            prev, nxt = seen.get("previousBusinessDay"), seen.get("nextBusinessDay")
            if prev and nxt and prev < asked < nxt:
                self.days[asked.isoformat()] = None

    def known(self, day):
        return day.isoformat() in self.days

    def session(self, day):
        key = day.isoformat()
        if key in self.days:
            return self.days[key]
        return default_session(day)

    def to_doc(self):
        return {k: (v.to_doc() if v else None) for k, v in sorted(self.days.items())}

    @classmethod
    def from_doc(cls, doc):
        days = {}
        for k, v in (doc or {}).items():
            if v is None:
                days[k] = None
            else:
                day = datetime.date.fromisoformat(k)
                days[k] = Session(day, parse_hhmm(v["open"], "open"), parse_hhmm(v["auction"], "auction"),
                                  parse_hhmm(v["close"], "close"), v.get("source", "cache"), v.get("note", ""))
        return cls(days)


def load_calendar(path):
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return Calendar.from_doc(json.load(fh))
        except (ValueError, KeyError, KrxError):
            return Calendar()
    return Calendar()


def save_calendar(path, cal):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cal.to_doc(), fh, ensure_ascii=False, indent=1)
        fh.write("\n")


# ---------------------------------------------------------------- 가격

def D(x, what="숫자"):
    try:
        if isinstance(x, Decimal):
            return x
        if isinstance(x, float):
            return Decimal(repr(x))
        return Decimal(str(x).strip().replace(",", "").replace("원", ""))
    except (InvalidOperation, ValueError):
        raise KrxError("%s 가 숫자가 아닙니다: %r" % (what, x))


def tick_size(price):
    p = D(price)
    for limit, t in TICK_TABLE:
        if p < limit:
            return t
    return TOP_TICK


def floor_tick(price):
    """호가 단위로 내림. 매수 손절가처럼 진입가에서 멀어지는 쪽."""
    p = D(price)
    t = tick_size(p)
    return int((p / t).to_integral_value(rounding=ROUND_FLOOR) * t)


def ceil_tick(price):
    p = D(price)
    t = tick_size(p)
    return int((p / t).to_integral_value(rounding=ROUND_CEILING) * t)


def round_tick(price):
    """호가 단위로 반올림 (0.5 는 올림)."""
    p = D(price)
    t = tick_size(p)
    return int((p / t).to_integral_value(rounding=ROUND_HALF_UP) * t)


def on_tick(price):
    p = D(price)
    return p == p.to_integral_value() and int(p) == floor_tick(p)


def price_limits(base):
    """기준가(보통 전일 종가) ±30% 의 상·하한가 추정값. 실제 값은 토스 price-limits.
    폭 = 기준가 x 30% 를 기준가 호가 단위로 버린 값, 하한가 = 기준가 - 폭, 상한가 = 기준가 + 폭."""
    b = D(base)
    if b <= 0:
        raise KrxError("기준가가 0 이하입니다")
    t = tick_size(b)
    width = (b * LIMIT_PCT / t).to_integral_value(rounding=ROUND_FLOOR) * t
    return int(b - width), int(b + width)


def won(x):
    """금액 표시: 1234567 -> '1,234,567원'."""
    v = D(x).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return "{:,}원".format(int(v))


def text_width(text):
    """고정폭 화면에서 차지하는 칸 수. 한글처럼 넓은 글자는 2칸."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in str(text))


def pad(text, width):
    """%-Ns 처럼 왼쪽 맞춤하되 한글을 2칸으로 센다."""
    text = str(text)
    return text + " " * max(0, width - text_width(text))


def price_str(x):
    v = D(x)
    if v == v.to_integral_value():
        return "{:,}".format(int(v))
    return "{:,}".format(float(v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)))


def pct_str(x, digits=2):
    return ("%%.%df%%%%" % digits) % (float(x) * 100)


# ---------------------------------------------------------------- 비용

def _floor_won(x):
    return int(D(x).to_integral_value(rounding=ROUND_FLOOR))


def trade_costs(entry, exit_price, qty, commission_rate=DEFAULT_COMMISSION, sell_tax_rate=DEFAULT_SELL_TAX):
    """한 번 사고 한 번 팔 때 드는 수수료와 거래세 추정 (원 미만 버림).
    실제 금액은 증권사 정산 방식에 따라 몇 원 다를 수 있다."""
    q = int(qty)
    buy_value = D(entry) * q
    sell_value = D(exit_price) * q
    cr, tr = D(commission_rate or 0), D(sell_tax_rate or 0)
    buy_fee = _floor_won(buy_value * cr)
    sell_fee = _floor_won(sell_value * cr)
    tax = _floor_won(sell_value * tr)
    return {"buy_fee": buy_fee, "sell_fee": sell_fee, "sell_tax": tax, "total": buy_fee + sell_fee + tax}


def cost_per_share(entry, exit_price, commission_rate=DEFAULT_COMMISSION, sell_tax_rate=DEFAULT_SELL_TAX):
    """주당 비용 (버림 없이): 매수 수수료 + 매도 수수료 + 매도 거래세."""
    cr, tr = D(commission_rate or 0), D(sell_tax_rate or 0)
    return float(D(entry) * cr + D(exit_price) * (cr + tr))


# ---------------------------------------------------------------- 종목·설정

def norm_symbol(text):
    """'005930', 'A005930', ' 005930 ' -> '005930'. 국내 6자리 코드만 받는다."""
    s = str(text or "").strip().upper()
    if len(s) == 7 and s.startswith("A") and SYMBOL.match(s[1:]):
        s = s[1:]
    if not SYMBOL.match(s):
        raise KrxError("국내 종목코드는 005930 같은 6자리입니다 (받은 값 %r)" % text)
    return s


def read_config(path, defaults=None):
    cfg = dict(defaults or CONFIG_DEFAULTS)
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except ValueError as e:
            raise KrxError("%s 가 올바른 JSON 이 아닙니다: %s" % (path, e))
        if not isinstance(doc, dict):
            raise KrxError("%s 는 JSON 객체여야 합니다" % path)
        cfg.update(doc)
    return cfg


def write_json(path, doc, indent=2):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=indent)
        fh.write("\n")
    os.replace(tmp, path)


def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise KrxError("%s 가 없습니다: %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise KrxError("%s 가 올바른 JSON 이 아닙니다: %s" % (path, e))
