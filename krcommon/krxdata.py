"""KRX Open API 일별매매정보: 한국거래소 공식 일봉 (KRX 종가). 표준 라이브러리만 쓴다.

왜 필요한가: 토스증권 일봉(1d)의 종가는 KRX+NXT 통합 시세라서 20:00 NXT 애프터마켓 마지막
가격이다. 2026-10-28 삼성전자는 토스 일봉 종가 245,000원, KRX 종가(15:30 종가 단일가)
242,000원이었다. 장 전 스캔의 신고가, 이동평균, RSI 는 KRX 종가로 재야 한다.

호출: GET https://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd?basDd=YYYYMMDD (코스피),
      sto/ksq_bydd_trd (코스닥). 키는 AUTH_KEY 헤더. 응답 OutBlock_1 에 그날 전 종목이 온다.
      숫자는 문자열(콤마 가능, 없으면 "-"). 자료는 매일 08:00 에 전 거래일 것으로 갱신되고,
      키당 하루 10,000건까지 부를 수 있다.

키: 환경변수 KRX_API_KEY, 또는 ~/.krx/api_key 파일 (한 줄). 키 값은 화면에 찍지 않는다.
openapi.krx.co.kr 에서 키를 받고, 유가증권·코스닥 일별매매정보를 각각 이용신청해야 한다.

캐시: trader-kr/cache/krx/YYYYMMDD.json (그날 두 시장 전 종목). 발표가 끝난 날은 다시 받지 않는다.
"""

import datetime
import json
import os
import time
import urllib.error
import urllib.request

from . import krx

BASE = "https://data-dbg.krx.co.kr/svc/apis/sto/"
MARKETS = (("KOSPI", "stk_bydd_trd"), ("KOSDAQ", "ksq_bydd_trd"))
CACHE_DIR = os.path.join("trader-kr", "cache", "krx")
PUBLISH = datetime.time(8, 0)          # 전 거래일 자료가 올라오는 시각
PUBLISH_MARGIN = datetime.timedelta(minutes=30)
PAUSE = 0.2                            # 하나씩 받을 때 호출 사이 쉬는 시간 (초)
WORKERS = 6                            # 캐시에 없는 날을 동시에 받는 수


class KrxDataError(Exception):
    pass


def api_key(env=None, home=None):
    env = os.environ if env is None else env
    k = (env.get("KRX_API_KEY") or "").strip()
    if k:
        return k
    path = os.path.join(home or os.path.expanduser("~"), ".krx", "api_key")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            k = fh.read().strip()
        return k or None
    return None


def _http(url, headers, timeout=30):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        code = e.code
        e.close()
        return code, ""
    except (urllib.error.URLError, OSError) as e:
        raise KrxDataError("KRX 에 연결하지 못했습니다: %s" % getattr(e, "reason", e))


def num(s):
    """'1,234' -> 1234.0, '-' 또는 '' -> None."""
    t = str(s if s is not None else "").replace(",", "").strip()
    if t in ("", "-"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def parse_row(r, market):
    sym = str(r.get("ISU_CD") or "").strip()
    c = num(r.get("TDD_CLSPRC"))
    if not sym or c is None:
        return None
    return {"symbol": sym, "name": r.get("ISU_NM"), "market": market, "date": None, "o": num(r.get("TDD_OPNPRC")),
            "h": num(r.get("TDD_HGPRC")), "l": num(r.get("TDD_LWPRC")), "c": c, "v": num(r.get("ACC_TRDVOL")) or 0.0,
            "value": num(r.get("ACC_TRDVAL")), "mktcap": num(r.get("MKTCAP"))}


SERVICE = {"KOSPI": "유가증권 일별매매정보", "KOSDAQ": "코스닥 일별매매정보"}


def fetch_market(day, market, key, transport=None):
    """그날 한 시장 전 종목 {종목: 행}. 휴장일이면 빈 사전. 키나 이용신청 문제면 KrxDataError."""
    transport = transport or _http
    ep = dict(MARKETS)[market]
    status, text = transport(BASE + ep + "?basDd=" + day.strftime("%Y%m%d"),
                             {"AUTH_KEY": key, "Accept": "application/json", "User-Agent": "day-trader-skill-kr/0.1"})
    if status in (401, 403):
        raise KrxDataError("KRX 가 키를 받지 않았습니다 (HTTP %s). openapi.krx.co.kr 에서 키 승인과 '%s' 이용신청이 "
                           "됐는지 확인하세요" % (status, SERVICE[market]))
    if status != 200:
        raise KrxDataError("KRX %s 응답 HTTP %s" % (ep, status))
    try:
        doc = json.loads(text)
    except ValueError:
        raise KrxDataError("KRX 응답이 JSON 이 아닙니다 (%s)" % ep)
    if "OutBlock_1" not in doc and (doc.get("respCode") or doc.get("respMsg")):
        # 승인 안 된 서비스나 잘못된 키는 200 에 오류 문구로 올 수도 있다
        raise KrxDataError("KRX 가 '%s' 호출을 거절했습니다 (%s %s). 키 승인과 이용신청을 확인하세요"
                           % (SERVICE[market], doc.get("respCode") or "", doc.get("respMsg") or ""))
    out = {}
    for r in doc.get("OutBlock_1") or []:
        row = parse_row(r, market)
        if row:
            row["date"] = day.isoformat()
            out[row["symbol"]] = row
    return out


def fetch_day(day, key, transport=None):
    """그날 두 시장 전 종목 {종목: 행}. 휴장일이면 빈 사전."""
    out = {}
    for market, _ in MARKETS:
        out.update(fetch_market(day, market, key, transport))
    return out


def check_key(key, now_kst=None, transport=None, lookback=10):
    """키로 두 서비스를 하나씩 불러 본다. [{market, service, ok, date, rows, message}]."""
    now_kst = now_kst or krx.kst_now()
    out = []
    for market, _ in MARKETS:
        rec = {"market": market, "service": SERVICE[market], "ok": False, "date": None, "rows": 0, "message": ""}
        d = now_kst.date()
        tried = 0
        try:
            while tried < lookback:
                if d.weekday() < 5 and published(d, now_kst):
                    tried += 1
                    rows = fetch_market(d, market, key, transport)
                    if rows:
                        rec.update(ok=True, date=d.isoformat(), rows=len(rows),
                                   message="%s %s 자료 %s종목을 받았습니다" % (SERVICE[market], d.isoformat(),
                                                                         "{:,}".format(len(rows))))
                        break
                d -= datetime.timedelta(days=1)
            if not rec["ok"]:
                rec["message"] = ("%s: 최근 %d 거래일 자료가 비어 있습니다. 이용신청 승인 전이면 이렇게 나올 수 있습니다"
                                  % (SERVICE[market], tried))
        except KrxDataError as e:
            rec["message"] = str(e)
        out.append(rec)
    return out


def published(day, now_kst):
    """day 의 자료가 올라왔다고 볼 수 있는가 (다음 날 08:30 이후)."""
    return now_kst >= datetime.datetime.combine(day + datetime.timedelta(days=1), PUBLISH) + PUBLISH_MARGIN


def day_table(day, key, cache_dir=CACHE_DIR, now_kst=None, transport=None):
    """캐시를 거친 그날 표. 발표가 끝난 날만 캐시에 확정한다 (빈 표 = 휴장일)."""
    now_kst = now_kst or krx.kst_now()
    path = os.path.join(cache_dir, day.strftime("%Y%m%d") + ".json") if cache_dir else None
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)["rows"]
        except (ValueError, KeyError):
            pass
    rows = fetch_day(day, key, transport)
    if path and (rows or published(day, now_kst)):
        os.makedirs(cache_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"date": day.isoformat(), "source": "KRX Open API", "rows": rows}, fh, ensure_ascii=False)
    return rows


def _cached(cache_dir, d):
    return bool(cache_dir) and os.path.exists(os.path.join(cache_dir, d.strftime("%Y%m%d") + ".json"))


def daily_series(symbols, end_day, sessions, key, cache_dir=CACHE_DIR, now_kst=None, transport=None,
                 sleep=time.sleep, progress=None, workers=None):
    """end_day 까지 거래일 sessions 개의 KRX 일봉 {종목: [{date, o, h, l, c, v, value}]} 와 설명.
    아직 발표 전인 날(오늘, 08:30 전의 어제)은 건너뛴다. 캐시에 없는 날은 WORKERS 개씩 동시에 받는다
    (KRX 응답이 한 번에 몇 초씩 걸려서, 60 거래일을 하나씩 받으면 8분 넘게 걸린다)."""
    from concurrent.futures import ThreadPoolExecutor
    now_kst = now_kst or krx.kst_now()
    workers = workers or WORKERS
    want = set(symbols)
    series = {s: [] for s in symbols}
    names, tables = {}, {}
    limit = end_day - datetime.timedelta(days=int(sessions * 1.6) + 20)
    days = []
    d = end_day
    while d >= limit:
        if d.weekday() < 5 and published(d, now_kst):
            days.append(d)
        d -= datetime.timedelta(days=1)
    calls, found, i = 0, 0, 0
    while found < sessions and i < len(days):
        need = max(1, sessions - found)
        batch = days[i:i + max(need, workers)] if workers > 1 else days[i:i + 1]
        i += len(batch)
        missing = [x for x in batch if not _cached(cache_dir, x)]
        if missing and workers > 1:
            with ThreadPoolExecutor(max_workers=min(workers, len(missing))) as pool:
                for x, rows in zip(missing, pool.map(lambda x: day_table(x, key, cache_dir, now_kst, transport), missing)):
                    tables[x] = rows
        for x in batch:
            if x not in tables:
                if not _cached(cache_dir, x) and PAUSE:
                    sleep(PAUSE)
                tables[x] = day_table(x, key, cache_dir, now_kst, transport)
        calls += 2 * len(missing)
        for x in batch:
            if tables[x] and found < sessions:
                found += 1
                for s in want:
                    r = tables[x].get(s)
                    if r:
                        series[s].append({"date": r["date"], "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"],
                                          "v": r["v"], "value": r["value"]})
                        names[s] = r.get("name")
        if progress and missing:
            progress("  KRX 일별매매정보 %d건 호출, %s 까지 (거래일 %d / %d)" % (calls, batch[-1].isoformat(), found,
                                                                          sessions))
    used = sorted({r["date"] for rows in series.values() for r in rows})
    for s in series:
        series[s].sort(key=lambda r: r["date"])
    info = {"calls": calls, "sessions": found, "last": used[-1] if used else None, "names": names}
    return series, info


def all_symbols(day, key, cache_dir=CACHE_DIR, now_kst=None, transport=None):
    """그날 KRX 표의 전 종목 (전 종목 스캔용)."""
    return sorted(day_table(day, key, cache_dir, now_kst, transport))
