"""분봉 자료: 토스증권 1분봉 받기와 캐시, 오프라인 파일, 5분봉 만들기.

토스증권 1분봉의 timestamp 는 봉이 끝나는 시각이다. 09:01 봉은 09:00:00 ~
09:00:59 체결을 모은다. 이 팩 안에서는 봉 시작 시각(유닉스 초)을 "t" 로 쓴다.
체결이 없는 분은 직전 종가로 채운 거래량 0 봉으로 내려온다.

캐시: trader-kr/cache/toss/1m/<종목>.json 에 받은 1분봉을 쌓는다. "from"~"to"
(봉 종료 시각) 사이의 정규장 시간대 봉은 빠짐없이 들어 있다는 뜻이다.
같은 구간을 다시 부르지 않는다. 이 파일은 본인만 쓰는 자료이고 저장소에
올리지 않는다.

오프라인 파일: {"symbol", "interval": "1m", "bars": [{"t", "o", "h", "l", "c", "v"}]}
(.json 또는 .json.gz). 시험용 합성 자료도 이 형식이다.
"""

import datetime
import gzip
import json
import os

from . import krx

KEEP_FROM = datetime.time(9, 1)    # 저장하는 봉의 종료 시각 범위 (수능일 16:30 종가 체결 봉까지)
KEEP_TO = datetime.time(16, 31)
NEAR_NOW = 1800                    # 벽시계로 30분 안의 구간은 실제로 받은 봉까지만 확정한다
SETTLE = 60                        # 끝난 지 60초가 안 된 봉은 캐시에 확정하지 않는다
GAP_FILL_DAYS = 45


class BarError(Exception):
    pass


# ---------------------------------------------------------------- 형식 변환

def candle_row(c):
    """토스 Candle -> (종료 시각 유닉스 초, [o, h, l, c, v])."""
    try:
        end = krx.parse_iso(c["timestamp"])
        row = [float(c["openPrice"]), float(c["highPrice"]), float(c["lowPrice"]), float(c["closePrice"]),
               float(c.get("volume") or 0)]
    except (KeyError, TypeError, ValueError, krx.KrxError):
        raise BarError("토스증권 캔들 형식이 이상합니다: %r" % (c,))
    return krx.ts_of(end), row


def _keep(end_ts):
    t = krx.kst_of(end_ts).time()
    return KEEP_FROM <= t <= KEEP_TO


def rows_to_bars(rows):
    """{종료 시각: [o,h,l,c,v]} -> 시작 시각 기준 봉 목록 (오래된 것부터)."""
    out = []
    for end in sorted(rows):
        o, h, l, c, v = rows[end]
        out.append({"t": int(end) - 60, "o": o, "h": h, "l": l, "c": c, "v": v})
    return out


# ---------------------------------------------------------------- 캐시

def _cache_path(cache_dir, symbol):
    return os.path.join(cache_dir, "%s.json" % symbol)


def load_cache(cache_dir, symbol):
    if not cache_dir:
        return None
    p = _cache_path(cache_dir, symbol)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            doc = json.load(fh)
        rows = {int(r[0]): [float(x) for x in r[1:6]] for r in doc.get("bars", [])}
        return {"from": int(doc["from"]), "to": int(doc["to"]), "rows": rows}
    except (ValueError, KeyError, TypeError, IndexError):
        return None      # 깨진 캐시는 버리고 새로 받는다


def save_cache(cache_dir, symbol, cache):
    if not cache_dir:
        return
    os.makedirs(cache_dir, exist_ok=True)
    doc = {"symbol": symbol, "source": "toss", "note": "본인 매매 목적 전용. 공유하지 않는다.",
           "from": cache["from"], "to": cache["to"],
           "bars": [[e] + cache["rows"][e] for e in sorted(cache["rows"])]}
    p = _cache_path(cache_dir, symbol)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, separators=(",", ":"))
    os.replace(tmp, p)


MAX_PAGES = 5000
MAX_EMPTY_PAGES = 3


def _page_down(client, symbol, before_ts, stop_ts, rows, progress=None):
    """before_ts(포함)부터 과거로 쪽을 넘기며 stop_ts 이하 봉이 나올 때까지 받는다.
    (가장 오래된 봉 종료 시각, 더 과거 자료가 없는지).
    빈 쪽이라도 nextBefore 가 있으면 계속 가고, nextBefore 가 과거로 가지 않으면 멈춘다."""
    before = krx.iso_kst(krx.kst_of(before_ts))
    oldest, exhausted, pages, empty = None, False, 0, 0
    while pages < MAX_PAGES:
        candles, nxt = client.candles(symbol, "1m", 200, before)
        pages += 1
        empty = 0 if candles else empty + 1
        for c in candles:
            end, row = candle_row(c)
            if end > before_ts:
                continue
            if _keep(end):
                rows[end] = row
            oldest = end if oldest is None else min(oldest, end)
        if progress and pages % 20 == 0:
            progress("  %s 1분봉 %d쪽째, %s 까지" % (symbol, pages, krx.kst_of(oldest).strftime("%Y-%m-%d %H:%M")
                                                   if oldest else "-"))
        if not nxt or empty >= MAX_EMPTY_PAGES:
            exhausted = True
            break
        if oldest is not None and oldest <= stop_ts:
            break
        try:
            if krx.ts_of(krx.parse_iso(nxt)) >= krx.ts_of(krx.parse_iso(before)):
                break                     # 커서가 과거로 가지 않는다: 무한 반복 방지
        except krx.KrxError:
            break
        before = nxt
    return oldest, exhausted


def _settled_top(want_to, got, wall_ts, old_to=None):
    """캐시가 '빠짐없이 받았다'고 기록할 끝 시각.
    벽시계로 NEAR_NOW 보다 오래된 구간만 요청 끝까지 확정한다. 최근 구간은 실제로 받은
    마지막 봉까지만 (그것도 SETTLE 초가 지난 봉까지만) 확정한다. 자료가 늦게 올라와도
    받지 않은 봉을 받았다고 적지 않는다."""
    if want_to <= wall_ts - NEAR_NOW:
        return want_to
    settled = [e for e in got if e <= wall_ts - SETTLE]
    top = max(settled) if settled else None
    if old_to is not None:
        return max(old_to, top) if top is not None else old_to
    return top


def fetch_1m(client, symbol, start_kst, end_kst, cache_dir=None, now_kst=None, progress=None, wall_kst=None):
    """start_kst ~ end_kst 에 끝나는 1분봉 (시작 시각 기준 목록)과 출처 설명.
    캐시에 있는 구간은 다시 부르지 않는다. now_kst 는 호출한 쪽의 시계(--now 로 바뀔 수 있음),
    wall_kst 는 실제 벽시계다. 캐시 확정은 벽시계로만 판단한다."""
    wall_kst = wall_kst or krx.kst_now()
    now_kst = now_kst or wall_kst
    wall_ts = krx.ts_of(wall_kst)
    now_ts = krx.ts_of(now_kst.replace(second=0))
    want_from = krx.ts_of(start_kst)
    want_to = min(krx.ts_of(end_kst), now_ts, krx.ts_of(wall_kst.replace(second=0)))
    if want_to < want_from:
        return [], {"calls": 0}
    cache = load_cache(cache_dir, symbol)
    calls0 = getattr(client, "calls", 0)
    if cache and (cache["to"] < want_from - GAP_FILL_DAYS * 86400 or cache["from"] > want_to):
        cache = None        # 너무 떨어진 구간: 캐시를 새로 시작한다
    if cache is None:
        rows = {}
        oldest, exhausted = _page_down(client, symbol, want_to, want_from, rows, progress)
        top = _settled_top(want_to, rows, wall_ts)
        start = want_from if exhausted or oldest is None else min(oldest, want_from)
        cache = {"from": start, "to": top if top is not None else start - 60, "rows": rows}
    else:
        if want_to > cache["to"]:
            got = {}
            _page_down(client, symbol, want_to, cache["to"], got, progress)
            cache["rows"].update(got)
            cache["to"] = _settled_top(want_to, got, wall_ts, cache["to"])
        if want_from < cache["from"]:
            got = {}
            oldest, exhausted = _page_down(client, symbol, cache["from"] - 60, want_from, got, progress)
            cache["rows"].update(got)
            cache["from"] = want_from if exhausted or oldest is None else min(oldest, want_from)
    save_cache(cache_dir, symbol, cache)
    rows = {e: r for e, r in cache["rows"].items() if want_from <= e <= want_to}
    return rows_to_bars(rows), {"calls": getattr(client, "calls", 0) - calls0,
                                "covered_to": krx.iso_kst(krx.kst_of(cache["to"]))}


# ---------------------------------------------------------------- 차트 문서

def chart_doc(symbol, bars, source, interval="1m", name=None):
    data_time = (bars[-1]["t"] + 60) if bars else None
    return {"symbol": symbol, "name": name, "interval": interval, "source": source, "bars": bars,
            "data_time": data_time}


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def offline_path(path, symbol):
    if os.path.isdir(path):
        for name in (symbol + ".json", symbol + ".json.gz", symbol + "-1m.json", symbol + "-1m.json.gz"):
            p = os.path.join(path, name)
            if os.path.exists(p):
                return p
        raise BarError("%s 에 %s.json 이 없습니다" % (path, symbol))
    if not os.path.exists(path):
        raise BarError("오프라인 파일이 없습니다: %s" % path)
    return path


def load_offline(path, symbol):
    p = offline_path(path, symbol)
    try:
        doc = json.loads(read_text(p))
    except (OSError, ValueError) as e:
        raise BarError("%s 를 읽지 못했습니다: %s" % (p, e))
    if not isinstance(doc, dict) or "bars" not in doc:
        raise BarError("%s 는 분봉 파일 형식이 아닙니다 ({\"symbol\", \"bars\": [...]})" % p)
    if str(doc.get("symbol") or symbol) != symbol:
        raise BarError("%s 는 %s 자료입니다. %s 가 아닙니다" % (p, doc.get("symbol"), symbol))
    if str(doc.get("interval") or "1m") != "1m":
        raise BarError("오프라인 파일은 1분봉이어야 합니다 (%s)" % doc.get("interval"))
    bars = []
    for b in doc["bars"]:
        try:
            bars.append({"t": int(b["t"]), "o": float(b["o"]), "h": float(b["h"]), "l": float(b["l"]),
                         "c": float(b["c"]), "v": float(b.get("v") or 0)})
        except (KeyError, TypeError, ValueError):
            raise BarError("%s 의 봉 형식이 이상합니다: %r" % (p, b))
    bars.sort(key=lambda x: x["t"])
    out = chart_doc(symbol, bars, "offline", name=doc.get("name"))
    if doc.get("data_time"):
        out["data_time"] = int(doc["data_time"])
    return out


# ---------------------------------------------------------------- 하루 단위

def split_days(bars):
    """{날짜: [1분봉]} (한국 시간 기준)."""
    days = {}
    for b in bars:
        days.setdefault(krx.kst_of(b["t"]).date(), []).append(b)
    return days


def session_slots(day_bars, session, step=krx.STEP_5M, now_kst=None):
    """접속매매 구간(개장 ~ 종가 단일가 시작)을 step 초 봉으로 묶는다.
    봉 하나는 안의 1분봉이 다 있고 끝 시각이 now 이전일 때 complete.
    반환: 슬롯 번호 순 목록 {slot, t, kst, end, o, h, l, c, v, n, complete}."""
    open_ts = krx.ts_of(session.open_dt)
    auction_ts = krx.ts_of(session.auction_dt)
    now_ts = krx.ts_of(now_kst) if now_kst else None
    per = step // 60
    groups = {}
    for b in day_bars:
        if not open_ts <= b["t"] < auction_ts:
            continue
        if now_ts is not None and b["t"] + 60 > now_ts:
            continue           # 아직 안 끝난 1분봉
        k = (b["t"] - open_ts) // step
        groups.setdefault(k, []).append(b)
    out = []
    for k in sorted(groups):
        g = sorted(groups[k], key=lambda x: x["t"])
        start = open_ts + k * step
        end = start + step
        complete = len(g) == per and (now_ts is None or end <= now_ts)
        out.append({"slot": k, "t": start, "kst": krx.kst_of(start), "end": krx.kst_of(end),
                    "o": g[0]["o"], "h": max(x["h"] for x in g), "l": min(x["l"] for x in g), "c": g[-1]["c"],
                    "v": sum(x["v"] for x in g), "n": len(g), "complete": complete})
    return out


def auction_close(day_bars, session):
    """종가 단일가 구간(15:20~15:30)의 마지막 봉 종가. 체결이 있었던 봉을 우선한다.
    토스 1분봉은 정각 체결을 다음 봉에 넣으므로 15:30:00 체결이 15:30~15:31 봉에 있을 수 있다.
    그 봉까지 본다 (NXT 애프터마켓은 15:40 단일가로 시작해서 그 봉에 섞이지 않는다)."""
    a, c = krx.ts_of(session.auction_dt), krx.ts_of(session.close_dt)
    seg = [b for b in day_bars if a <= b["t"] <= c]
    if not seg:
        return None
    traded = [b for b in seg if b["v"] > 0]
    return (traded or seg)[-1]["c"]


def day_close(day_bars, session):
    """그날 정규장 종가: 종가 단일가 체결가, 없으면 접속매매 마지막 종가."""
    px = auction_close(day_bars, session)
    if px is not None:
        return px
    reg = [b for b in day_bars if krx.ts_of(session.open_dt) <= b["t"] < krx.ts_of(session.auction_dt)]
    return reg[-1]["c"] if reg else None
