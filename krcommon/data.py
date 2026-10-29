"""스크립트들이 같이 쓰는 자료 읽기: 차트(토스 또는 오프라인), 장 운영 정보."""

import datetime
import os

from . import bars, krx, toss

CACHE_DIR = os.path.join("trader-kr", "cache", "toss", "1m")
CALENDAR_PATH = os.path.join("trader-kr", "cache", "calendar.json")


class DataError(Exception):
    pass


_client = None


def client():
    """프로세스 안에서 하나만 쓰는 토스 클라이언트 (토큰을 아끼려고)."""
    global _client
    if _client is None:
        _client = toss.Client()
    return _client


def set_client(c):
    """시험에서 가짜 클라이언트를 끼운다."""
    global _client
    _client = c


def load_chart(symbol, start_kst, end_kst, offline=None, now_kst=None, cache_dir=CACHE_DIR, progress=None):
    """1분봉 차트 문서. offline 이면 파일에서, 아니면 토스증권에서 (캐시 사용)."""
    try:
        if offline:
            ch = bars.load_offline(offline, symbol)
            lo, hi = krx.ts_of(start_kst), krx.ts_of(end_kst)
            ch["bars"] = [b for b in ch["bars"] if lo <= b["t"] + 60 <= hi]
            return ch
        got, info = bars.fetch_1m(client(), symbol, start_kst, end_kst, cache_dir, now_kst, progress)
        ch = bars.chart_doc(symbol, got, "toss")
        ch["fetch"] = info
        return ch
    except (bars.BarError, toss.TossError, krx.KrxError) as e:
        raise DataError(str(e))


def calendar_for(day, online=True, path=CALENDAR_PATH):
    """day 의 장 운영 정보가 든 Calendar. online 이면 토스에서 받아 캐시에 더한다.
    못 받으면 평일 기본값 (그 경우 session.source == 'default')."""
    cal = krx.load_calendar(path)
    if online and not cal.known(day):
        try:
            res = client().calendar(day.isoformat())
            cal.add_response(res, asked=day)
            krx.save_calendar(path, cal)
        except (toss.TossError, krx.KrxError, ValueError, KeyError, TypeError, AttributeError):
            pass
    return cal


def session_for(day, online=True, path=CALENDAR_PATH):
    return calendar_for(day, online, path).session(day)


def day_start(day):
    return datetime.datetime.combine(day, datetime.time(0, 0))


def day_end(day):
    return datetime.datetime.combine(day, datetime.time(23, 59))
