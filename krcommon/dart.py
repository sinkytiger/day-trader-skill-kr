"""DART 공시 목록: 그날 그 종목에 나온 공시를 찾는다. 표준 라이브러리만 쓴다.

타임머신의 재료(지난 거래일 봉)에는 실적 발표나 유상증자 같은 사건이 없을 수 있다. 그래서
위험 점검과 타임머신이 그날의 공시를 보여 주고 경고한다. 막지는 않는다 (거래정지 같은 상태는
토스증권 종목 정보가 막는다).

키: 환경변수 DART_API_KEY (OpenDART 에서 무료로 발급). 없으면 확인하지 않았다고 말한다.
키 값은 화면에 찍지 않는다.

호출: https://opendart.fss.or.kr/api/list.json 에 날짜만 주면 그날 전체 회사 공시가 쪽(100건)으로
나온다. 응답의 stock_code 로 종목을 고른다. 하루 목록은 trader-kr/cache/dart/YYYYMMDD.json 에
두고, 오늘 것은 10분이 지나면 다시 받는다.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from . import krx

URL = "https://opendart.fss.or.kr/api/list.json"
CACHE_DIR = os.path.join("trader-kr", "cache", "dart")
REFRESH = 600
MAX_PAGES = 60
# 단타 전에 특히 볼 공시 (제목에 들어 있으면 강조)
FLAGS = ("잠정", "실적", "유상증자", "무상증자", "전환사채", "신주인수권", "교환사채", "합병", "분할", "조회공시",
         "불성실공시", "최대주주", "매매거래정지", "관리종목", "상장폐지", "감자", "자기주식", "공개매수")


class DartError(Exception):
    pass


def api_key(env=None, home=None):
    """환경변수 DART_API_KEY, 없으면 ~/.dart/api_key 파일 (krcommon/keys.py dart 가 씀)."""
    env = os.environ if env is None else env
    k = (env.get("DART_API_KEY") or "").strip()
    if k:
        return k
    path = os.path.join(home or os.path.expanduser("~"), ".dart", "api_key")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            k = fh.read().strip()
    return k or None


def _http(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "day-trader-skill-kr/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        raise DartError("DART 에 연결하지 못했습니다: %s" % getattr(e, "reason", e))


def fetch_day(day, key, transport=None, sleep=time.sleep):
    """그날 전체 공시 목록 (list.json 의 list 항목들)."""
    transport = transport or _http
    ymd = day.strftime("%Y%m%d")
    out, page, total = [], 1, 1
    while page <= min(total, MAX_PAGES):
        q = urllib.parse.urlencode({"crtfc_key": key, "bgn_de": ymd, "end_de": ymd, "page_no": page,
                                    "page_count": 100})
        try:
            doc = json.loads(transport(URL + "?" + q))
        except ValueError:
            raise DartError("DART 응답이 JSON 이 아닙니다")
        status = str(doc.get("status"))
        if status == "013":            # 조회된 자료 없음
            return out
        if status != "000":
            raise DartError("DART 응답 %s: %s" % (status, doc.get("message") or ""))
        out.extend(doc.get("list") or [])
        total = int(doc.get("total_page") or 1)
        page += 1
        if page <= total:
            sleep(0.1)
    return out


def day_list(day, key, cache_dir=CACHE_DIR, now_kst=None, transport=None, sleep=time.sleep):
    """캐시를 거친 그날 공시 목록. 지난 날은 한 번만, 오늘은 10분마다 받는다."""
    now_kst = now_kst or krx.kst_now()
    path = os.path.join(cache_dir, "%s.json" % day.strftime("%Y%m%d")) if cache_dir else None
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
            fetched = krx.parse_iso(doc["fetched_at"])
            final = fetched.date() > day
            if final or (krx.ts_of(now_kst) - krx.ts_of(fetched)) < REFRESH:
                return doc["list"]
        except (ValueError, KeyError, krx.KrxError):
            pass
    rows = fetch_day(day, key, transport, sleep)
    if path:
        os.makedirs(cache_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"fetched_at": krx.iso_kst(now_kst), "list": rows}, fh, ensure_ascii=False)
    return rows


def for_symbol(rows, symbol):
    """그 종목 공시만, 접수번호 순."""
    got = [r for r in rows if str(r.get("stock_code") or "").strip() == symbol]
    return sorted(got, key=lambda r: str(r.get("rcept_no")))


def flagged(r):
    name = str(r.get("report_nm") or "")
    return any(f in name for f in FLAGS)


def describe(rows, symbol, day):
    """경고 한 줄 (공시가 없으면 None)."""
    if not rows:
        return None
    hot = [r for r in rows if flagged(r)]
    shown = (hot or rows)[:4]
    names = ", ".join(" ".join(str(r.get("report_nm") or "").split()) for r in shown)
    more = " 외 %d건" % (len(rows) - len(shown)) if len(rows) > len(shown) else ""
    return ("%s 에 %s 공시 %d건 (%s%s). 실적·증자 같은 사건은 지난 거래일 재료에 없을 수 있습니다"
            % (day.isoformat(), symbol, len(rows), names, more))


def check(symbol, day, cache_dir=CACHE_DIR, now_kst=None, env=None, transport=None, sleep=time.sleep):
    """(그 종목 그날 공시 목록 또는 None, 설명). 키가 없거나 실패하면 목록은 None."""
    key = api_key(env)
    if not key:
        return None, "DART_API_KEY 가 없어 오늘 공시를 확인하지 않았습니다"
    try:
        rows = for_symbol(day_list(day, key, cache_dir, now_kst, transport, sleep), symbol)
    except DartError as e:
        return None, "DART 공시를 확인하지 못했습니다: %s" % e
    return rows, describe(rows, symbol, day)
