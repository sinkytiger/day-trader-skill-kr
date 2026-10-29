#!/usr/bin/env python3
"""차트 스캐너 (trader-kr-scanner): 여러 종목 차트에서 셋업을 찾는다. 표준 라이브러리만 쓴다.

    python scan.py universe                          # 코스피·코스닥 보통주 -> trader-kr/universe.json
    python scan.py daily                             # 장 전: 일봉 셋업 -> trader-kr/watchlist.json
    python scan.py daily --symbols 005930,000660     # 또는 --universe (전 종목), --limit 200
    python scan.py orb                               # 09:20 뒤: 지금까지 시가 범위 돌파
    python scan.py orb --at 10:15 --or-minutes 15    # 10:15 기준으로 자르기
    python scan.py orb --offline 폴더 --now "2026-10-28 10:15" --symbols 005930,000660

후보 종목 (daily, orb 공통):
  기본       토스증권 시장 거래대금 상위 100 (투자유의 종목 제외). orb 는 실시간,
             daily 는 1일 기준. 단타는 거래가 많은 종목에서 한다.
  --universe trader-kr/universe.json 의 전 종목 (먼저 scan.py universe). 종목마다 한 번씩
             부르므로 몇 분 걸린다.
  --symbols / --watchlist FILE 직접 고르기.
  어느 쪽이든 토스증권 종목 정보로 주식·보통주·상장·거래정지 아님만 남긴다
  (--include-preferred 로 우선주 포함).

universe  토스증권 마켓별 전체 종목 (KOSPI, KOSDAQ), 주식만, 보통주만 (--include-preferred).

daily     장 전 일봉 종가 스캔 (일봉 60개). KRX_API_KEY 가 있으면 KRX Open API 일별매매정보의 한국거래소
          공식 종가를 쓴다 (하루 두 번 호출로 전 종목, 매일 08:00 에 전 거래일 갱신). 없으면 토스증권
          일봉을 쓰고 경고한다: 토스 일봉 종가는 KRX+NXT 통합이라 20:00 NXT 마지막 가격이다. 유동성: 마지막 종가 >= --min-price
          (1,000원), 거래대금(종가 x 거래량) >= --min-value (10억원). 셋업 (종가만으로):
            breakout_20d     마지막 종가가 그 전 20일 종가보다 모두 높음
            bull_flag        마지막 5일 전 10일 동안 10% 넘게 오르고, 마지막 5일 종가가 5% 범위 안
            reclaim_ma20     20일 평균 아래서 끝났다가 위에서 끝남
            oversold         RSI 14 (Wilder) 30 미만
            oversold_bounce  어제 RSI 14 가 30 미만이고 오늘 올라서 끝남
          오늘 일봉은 애프터마켓까지 끝나는 20:00 전에는 뺀다 (통합 일봉이 그때까지 바뀜).

orb       시가 범위 돌파, 5분봉 (토스증권 1분봉을 묶음). 범위는 개장 뒤 --or-minutes
          (15)분. 범위 고가 = 범위 봉의 가장 높은 고가, 신호 = 범위 뒤 처음으로 그 위에서
          끝난 완성 봉, 그 봉이 끝난 시각. 개장부터 지금까지 5분봉이 다 있는 차트만 쓴다.
          하한: --min-price 1,000원.

스캔은 규칙에 맞는 차트 목록이지 살 종목 목록이 아니다.
종료 코드 0 = 무언가 찾음, 1 = 못 찾음 또는 실행 못 함.
"""

import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
from krcommon import bars as kbars, data, krx, krxdata, toss  # noqa: E402

STEP = 300
MIN_DAILY_BARS = 30
DAILY_FINAL = datetime.time(20, 0)   # KRX+NXT 통합 일봉은 애프터마켓이 끝나는 20:00 에 확정
NOTE = "스캔은 규칙에 맞는 차트 목록이지 살 종목 목록이 아닙니다."
SETUPS = [("breakout_20d", "20일 종가 신고가"), ("bull_flag", "상승 깃발"), ("reclaim_ma20", "20일 평균 회복"),
          ("oversold", "과매도 (RSI 30 미만)"), ("oversold_bounce", "과매도 반등")]
LABEL = dict(SETUPS)


class ScanError(Exception):
    pass


def p(x):
    return krx.price_str(x)


def progress(a, msg):
    if not getattr(a, "json", False):
        print(msg, file=sys.stderr)


# ---------------------------------------------------------------- 셋업 (원본과 같은 계산)

def rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    g = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    lo = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
    return 100 - 100 / (1 + ag / al) if al else 100.0


def find_setups(c):
    last = c[-1]
    hi20 = max(c[-21:-1])
    ma20 = sum(c[-20:]) / 20.0
    ma20_prev = sum(c[-21:-1]) / 20.0
    run = c[-6] / c[-16] - 1
    rng5 = (max(c[-5:]) - min(c[-5:])) / last
    r, r_prev = rsi(c), rsi(c[:-1])
    tags = []
    if last > hi20:
        tags.append("breakout_20d")
    if run > 0.10 and rng5 < 0.05:
        tags.append("bull_flag")
    if c[-2] < ma20_prev and last > ma20:
        tags.append("reclaim_ma20")
    if r is not None and r < 30:
        tags.append("oversold")
    if r_prev is not None and r_prev < 30 and last > c[-2]:
        tags.append("oversold_bounce")
    return tags, {"rsi": r, "high20": hi20, "ma20": ma20}


def daily_closes(rows, now_kst):
    """[{date, c, v}] -> 완성된 일봉만 (오늘 것은 15:30 이후에만), 날짜순."""
    today = now_kst.date()
    done_today = now_kst.time() >= DAILY_FINAL
    out = {}
    for r in rows:
        d = datetime.date.fromisoformat(str(r["date"]))
        if d > today or (d == today and not done_today):
            continue
        out[d] = r
    return [out[d] for d in sorted(out)]


def scan_daily(series, symbols, now_kst, min_price, min_value):
    """순수 함수. series = {종목: [{date, c, v}]}. (hits, counts, last_date)."""
    hits, dates = [], {}
    counts = {"requested": len(symbols), "fetched": 0, "illiquid": 0, "short": 0, "scanned": 0}
    for sym in symbols:
        rows = series.get(sym)
        if not rows:
            continue
        counts["fetched"] += 1
        bars = daily_closes(rows, now_kst)
        if len(bars) < MIN_DAILY_BARS:
            counts["short"] += 1
            continue
        last = bars[-1]
        value = float(last["value"]) if last.get("value") is not None else float(last["c"]) * float(last.get("v") or 0)
        if float(last["c"]) < min_price or value < min_value:
            counts["illiquid"] += 1
            continue
        counts["scanned"] += 1
        dates[last["date"]] = dates.get(last["date"], 0) + 1
        closes = [float(b["c"]) for b in bars]
        tags, nums = find_setups(closes)
        if tags:
            hits.append({"symbol": sym, "setups": tags, "price": closes[-1], "close_date": str(last["date"]),
                         "rsi": round(nums["rsi"], 1) if nums["rsi"] is not None else None,
                         "value_krw": round(value), "level": max(closes[-21:-1])})
    hits.sort(key=lambda h: (-h["value_krw"], h["symbol"]))
    counts["hits"] = len(hits)
    last_date = max(dates, key=lambda d: (dates[d], d)) if dates else None
    return hits, counts, last_date


def orb_window(now_kst, at, or_minutes, until, session, explicit_day=None):
    day = explicit_day or now_kst.date()
    if or_minutes < 5 or or_minutes % 5 or or_minutes > 120:
        raise ScanError("--or-minutes 는 5 ~ 120 사이 5의 배수입니다 (보통 15)")
    if session is None:
        raise ScanError("%s 는 휴장입니다. 거래일에 범위가 끝난 뒤 돌리거나 --offline 으로 지난 날을 다시 보세요" % day)
    range_end = session.open_dt + datetime.timedelta(minutes=or_minutes)
    first_close = range_end + datetime.timedelta(minutes=5)
    if at is not None:
        cutoff = datetime.datetime.combine(day, at)
        if explicit_day is None and cutoff > now_kst + datetime.timedelta(seconds=59):
            raise ScanError("--at %s 는 지금(%s)보다 늦습니다" % (krx.hm(at), now_kst.strftime("%H:%M")))
    else:
        cutoff = now_kst.replace(second=0, microsecond=0)
    cutoff = min(cutoff, session.auction_dt)
    if cutoff < first_close:
        raise ScanError("지금 %s: %d분 시가 범위는 %s ~ %s 이고 범위 뒤 첫 봉은 %s 에 끝납니다. %s 뒤에 다시 돌리세요"
                        % (cutoff.strftime("%H:%M"), or_minutes, krx.hm(session.open_t), krx.hm(range_end.time()),
                           krx.hm(first_close.time()), krx.hm(first_close.time())))
    if until is not None and cutoff.time() > session.shift(until):
        raise ScanError("지금 %s 는 새 진입 마감 %s 뒤입니다. 지금 찾은 돌파는 이미 늦었습니다. 지난 장은 --at 10:15 로 다시 보세요"
                        % (cutoff.strftime("%H:%M"), krx.hm(session.shift(until))))
    return day, cutoff


def orb_hit(slots, n_range):
    rng = slots[:n_range]
    oh, ol = max(b["h"] for b in rng), min(b["l"] for b in rng)
    for k in range(n_range, len(slots)):
        b = slots[k]
        if b["c"] > oh:
            return {"price": b["c"], "at": krx.hm(b["end"].time()), "bar_start": krx.hm(b["kst"].time()),
                    "bar_index": k, "or_high": oh, "or_low": ol, "pct_over": round((b["c"] / oh - 1) * 100, 2),
                    "level": oh, "_t": b["end"]}
    return None


def scan_orb(charts, symbols, day, cutoff, session, or_minutes, min_price=0.0):
    """순수 함수. charts = {종목: 1분봉 차트}. (hits, counts)."""
    n_range = or_minutes // 5
    want = int((cutoff - session.open_dt).total_seconds()) // STEP
    counts = {"requested": len(symbols), "fetched": 0, "unusable": 0, "illiquid": 0, "scanned": 0}
    hits = []
    for sym in symbols:
        ch = charts.get(sym)
        if not ch:
            continue
        counts["fetched"] += 1
        day_bars = kbars.split_days(ch["bars"]).get(day, [])
        slots = [b for b in kbars.session_slots(day_bars, session, STEP, cutoff) if b["complete"]]
        if want < n_range or len(slots) != want or any(b["slot"] != k for k, b in enumerate(slots)):
            counts["unusable"] += 1
            continue
        if slots[-1]["c"] < min_price:
            counts["illiquid"] += 1
            continue
        counts["scanned"] += 1
        h = orb_hit(slots, n_range)
        if h:
            hits.append(dict({"symbol": sym, "name": ch.get("name"), "setups": ["orb"]}, **h))
    hits.sort(key=lambda x: (x["_t"], x["symbol"]))
    for h in hits:
        del h["_t"]
    counts["hits"] = len(hits)
    return hits, counts


# ---------------------------------------------------------------- 후보

def stock_ok(info, include_preferred=False):
    if not info or info.get("securityType") != "STOCK" or info.get("status") != "ACTIVE":
        return False
    if info.get("isCommonShare") is False and not include_preferred:
        return False
    kd = info.get("koreanMarketDetail") or {}
    return not kd.get("krxTradingSuspended") and not kd.get("liquidationTrading")


def candidates(a, duration):
    """(종목 목록, 이름 사전, 설명)."""
    names = {}
    if a.symbols:
        syms = [krx.norm_symbol(s) for s in a.symbols.split(",") if s.strip()]
        how = "직접 고른 %d종목" % len(syms)
    elif a.watchlist:
        doc = krx.read_json(a.watchlist, "관심 목록")
        rows = doc.get("hits", doc.get("symbols", [])) if isinstance(doc, dict) else doc
        syms = [krx.norm_symbol(r["symbol"] if isinstance(r, dict) else r) for r in rows]
        how = "%s 의 %d종목" % (a.watchlist, len(syms))
    elif a.universe and getattr(a, "krx_key", None):
        day = krx.kst_now().date() - datetime.timedelta(days=1)
        syms = []
        for _ in range(10):                       # 가장 최근 발표된 거래일의 KRX 표
            if day.weekday() < 5 and krxdata.published(day, krx.kst_now()):
                syms = krxdata.all_symbols(day, a.krx_key)
                if syms:
                    break
            day -= datetime.timedelta(days=1)
        how = "KRX %s 일별매매정보 전 종목 %d개" % (day.isoformat(), len(syms))
    elif a.universe:
        doc = krx.read_json(a.universe_file, "종목 목록 (먼저 scan.py universe)")
        syms = [r["symbol"] for r in doc.get("stocks", [])]
        names = {r["symbol"]: r.get("name") for r in doc.get("stocks", [])}
        how = "%s 전 종목 %d개" % (a.universe_file, len(syms))
    else:
        res = data.client().rankings("MARKET_TRADING_AMOUNT", duration, 100, True)
        syms = [r["symbol"] for r in res.get("rankings", [])]
        how = "토스증권 시장 거래대금 상위 %d (%s, 투자유의 제외, %s 기준)" % (len(syms), duration,
                                                                res.get("rankedAt") or "시각 없음")
    if a.limit:
        syms = syms[:a.limit]
    if not a.offline and syms:
        info = {s["symbol"]: s for s in data.client().stocks(syms)}
        keep = [s for s in syms if stock_ok(info.get(s), a.include_preferred)]
        names.update({s: (info.get(s) or {}).get("name") for s in keep})
        if len(keep) < len(syms):
            how += ", 주식·보통주·거래 가능만 %d개" % len(keep)
        syms = keep
    return syms, names, how


# ---------------------------------------------------------------- 명령

def cmd_universe(a):
    rows = []
    c = data.client()
    for market in ("KOSPI", "KOSDAQ"):
        got = c.list_stocks(market, "STOCK", None if a.include_preferred else True)
        for r in got:
            rows.append({"symbol": r["symbol"], "name": r.get("name"), "market": market,
                         "common": r.get("isCommonShare", True)})
    rows.sort(key=lambda r: r["symbol"])
    doc = {"created_by": "trader-kr-scanner", "created_at": krx.kst_now().strftime("%Y-%m-%d %H:%M:%S"),
           "source": "toss", "note": "토스증권에서 거래 가능한 종목. 본인 매매 목적 전용.", "stocks": rows}
    krx.write_json(a.out, doc)
    print("코스피·코스닥 %s 종목 %s개를 %s 에 썼습니다." % ("주식(우선주 포함)" if a.include_preferred else "보통주",
                                                  "{:,}".format(len(rows)), a.out))
    return 0 if rows else 1


def load_daily_offline(path, sym):
    p_ = os.path.join(path, "%s-1d.json" % sym) if os.path.isdir(path) else path
    if not os.path.exists(p_):
        return None
    doc = krx.read_json(p_, "일봉 파일")
    return [{"date": b["date"], "c": float(b["c"]), "v": float(b.get("v") or 0)} for b in doc.get("bars", [])]


def cmd_daily(a):
    now = krx.parse_now(a.now)
    if a.offline:
        syms = [krx.norm_symbol(s) for s in (a.symbols or "").split(",") if s.strip()]
        if not syms:
            syms = sorted(f[:-8] for f in os.listdir(a.offline) if f.endswith("-1d.json"))
        names, how = {}, "오프라인 %d종목" % len(syms)
        series = {s: load_daily_offline(a.offline, s) for s in syms}
    else:
        source = a.source
        a.krx_key = krxdata.api_key() if source in ("auto", "krx") else None
        if source == "krx" and not a.krx_key:
            raise ScanError("KRX_API_KEY 가 없습니다. openapi.krx.co.kr 에서 키를 받아 환경변수 KRX_API_KEY 나 "
                            "~/.krx/api_key 파일에 넣으세요 (README 의 KRX Open API 준비)")
        syms, names, how = candidates(a, "1d")
        series = {}
        if a.krx_key:
            src_word = "KRX 공식 일봉 (한국거래소 종가)"
            progress(a, "KRX 일별매매정보를 받습니다: %s (캐시에 있는 날은 건너뜀)..." % how)
            try:
                series, info = krxdata.daily_series(syms, now.date(), 60, a.krx_key, now_kst=now,
                                                    progress=lambda m: progress(a, m))
            except krxdata.KrxDataError as e:
                raise ScanError(str(e))
            for s, n in info["names"].items():
                names.setdefault(s, n)
        else:
            src_word = "토스증권 일봉"
            progress(a, "일봉을 받습니다: %s ..." % how)
            progress(a, "참고: KRX_API_KEY 가 없어 토스증권 일봉을 씁니다. 토스 일봉 종가는 KRX+NXT 통합이라 20:00 NXT "
                        "애프터마켓 마지막 가격이고, KRX 종가와 다를 수 있습니다.")
            for i, s in enumerate(syms):
                try:
                    rows, _ = data.client().candles(s, "1d", 60)
                    series[s] = [{"date": krx.parse_iso(r["timestamp"]).date().isoformat(),
                                  "c": float(r["closePrice"]), "v": float(r.get("volume") or 0)} for r in rows]
                except toss.TossError as e:
                    progress(a, "  %s: %s" % (s, e))
                if (i + 1) % 100 == 0:
                    progress(a, "  %d / %d" % (i + 1, len(syms)))
    hits, counts, last_date = scan_daily(series, syms, now, a.min_price, a.min_value)
    for h in hits:
        h["name"] = names.get(h["symbol"])
    doc = {"created_by": "trader-kr-scanner", "created_at": now.strftime("%Y-%m-%d %H:%M:%S"), "mode": "daily",
           "as_of": ("%s 종가" % last_date) if last_date else None, "candidates": how, "scanned": counts["scanned"],
           "source": "offline" if a.offline else ("krx" if getattr(a, "krx_key", None) else "toss"),
           "filters": {"min_price": a.min_price, "min_value": a.min_value}, "note": NOTE, "hits": hits}
    if not a.offline and not getattr(a, "krx_key", None):
        doc["warning"] = "토스증권 일봉 종가는 20:00 NXT 애프터마켓 마지막 가격이라 KRX 종가와 다를 수 있습니다"
    if not a.no_write:
        krx.write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts), ensure_ascii=False, indent=2))
        return 0 if hits else 1
    print("일봉 스캔, %s, %s 종가까지 (%s). 후보: %s." % (now.strftime("%Y-%m-%d %H:%M"), last_date or "없음",
                                                     "오프라인" if a.offline else src_word, how))
    if doc.get("warning"):
        print("참고: " + doc["warning"] + ". KRX_API_KEY 를 넣으면 KRX 공식 종가로 잽니다.")
    print("%d종목 받음, 유동성 통과 %d (종가 %s원 이상, 거래대금 %s 이상)%s." % (
        counts["fetched"], counts["fetched"] - counts["illiquid"] - counts["short"], p(a.min_price), krx.won(a.min_value),
        (", %d종목은 일봉 %d개 미만" % (counts["short"], MIN_DAILY_BARS)) if counts["short"] else ""))
    if hits:
        per = [(k, sum(1 for h in hits if k in h["setups"])) for k, _ in SETUPS]
        print("%d종목이 셋업에 맞음: %s. 거래대금 많은 순:" % (len(hits), ", ".join("%s %d" % (LABEL[k], n) for k, n in per if n)))
    else:
        print("셋업 없음.")
    for h in hits[:a.show]:
        print("  %s %-10s %10s  RSI %5s  %s" % (h["symbol"], (h.get("name") or "")[:10], p(h["price"]),
                                             ("%.1f" % h["rsi"]) if h["rsi"] is not None else "-",
                                             ", ".join(LABEL[t] for t in h["setups"])))
    if len(hits) > a.show:
        print("  ... 그리고 %d종목 더 (관심 목록에 있음, --show N)." % (len(hits) - a.show))
    if not a.no_write:
        print("%s 에 썼습니다 (daily, %d종목)." % (a.out, len(hits)))
    print(NOTE + " 다음: 차트 하나를 골라 /trader-kr-entry 종목.")
    return 0 if hits else 1


def cmd_orb(a):
    now = krx.parse_now(a.now)
    at = krx.parse_hhmm(a.at, "--at") if a.at else None
    cfg = krx.read_config(a.config)
    until = krx.parse_hhmm(a.until or cfg.get("no_entries_after") or "14:50", "--until")
    explicit_day = now.date() if a.offline else None
    if a.offline and at is None and not a.now:
        raise ScanError("오프라인 다시보기는 --at HH:MM 또는 --now \"YYYY-MM-DD HH:MM\" 이 필요합니다")
    session = data.session_for(now.date(), online=not a.offline)
    day, cutoff = orb_window(now, at, a.or_minutes, until, session, explicit_day)
    start = datetime.datetime.combine(day, datetime.time(8, 59))
    charts = {}
    if a.offline:
        syms = [krx.norm_symbol(s) for s in (a.symbols or "").split(",") if s.strip()] or sorted(
            f[:-5] for f in os.listdir(a.offline) if f.endswith(".json") and not f.endswith("-1d.json"))
        how = "오프라인 %d종목" % len(syms)
        for s in syms:
            try:
                charts[s] = data.load_chart(s, start, cutoff, offline=a.offline)
            except data.DataError:
                pass
    else:
        syms, names, how = candidates(a, "realtime")
        progress(a, "1분봉을 받습니다: %s, %s ~ %s ..." % (how, krx.hm(session.open_t), cutoff.strftime("%H:%M")))
        for i, s in enumerate(syms):
            try:
                charts[s] = data.load_chart(s, start, cutoff, now_kst=now)
                charts[s]["name"] = names.get(s)
            except data.DataError as e:
                progress(a, "  %s: %s" % (s, e))
            if (i + 1) % 25 == 0:
                progress(a, "  %d / %d" % (i + 1, len(syms)))
    hits, counts = scan_orb(charts, syms, day, cutoff, session, a.or_minutes, a.min_price)
    doc = {"created_by": "trader-kr-scanner", "created_at": now.strftime("%Y-%m-%d %H:%M:%S"), "mode": "orb",
           "as_of": "%s %s" % (day.isoformat(), cutoff.strftime("%H:%M")), "candidates": how,
           "scanned": counts["scanned"], "source": "offline" if a.offline else "toss", "or_minutes": a.or_minutes,
           "interval": "5m", "filters": {"min_price": a.min_price}, "session": session.to_doc(), "note": NOTE,
           "hits": hits}
    if not a.no_write:
        krx.write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts), ensure_ascii=False, indent=2))
        return 0 if hits else 1
    print("시가 범위 돌파 스캔, %s %s: %d분 범위 (%s ~ %s), 5분봉, 토스증권. 후보: %s." % (
        day.isoformat(), cutoff.strftime("%H:%M"), a.or_minutes, krx.hm(session.open_t),
        krx.hm((session.open_dt + datetime.timedelta(minutes=a.or_minutes)).time()), how))
    print("%d종목 받음, %d종목 사용 가능%s." % (counts["fetched"], counts["scanned"],
                                     (" (%d종목은 빈 봉이 있음)" % counts["unusable"]) if counts["unusable"] else ""))
    if hits:
        print("%d / %d 종목이 %s 까지 시가 범위 고가 위에서 끝났습니다:" % (len(hits), counts["scanned"], cutoff.strftime("%H:%M")))
        for h in hits[:a.show]:
            print("  %s  %s %-10s %10s  범위 %s ~ %s  %+.2f%%" % (h["at"], h["symbol"], (h.get("name") or "")[:10],
                                                              p(h["price"]), p(h["or_low"]), p(h["or_high"]),
                                                              h["pct_over"]))
        if len(hits) > a.show:
            print("  ... 그리고 %d종목 더 (--show N)." % (len(hits) - a.show))
    else:
        print("%s 까지 시가 범위 고가 위에서 끝난 차트가 없습니다." % cutoff.strftime("%H:%M"))
    if not a.no_write:
        print("%s 에 썼습니다 (orb, %d종목)." % (a.out, len(hits)))
    print(NOTE + " 다음: 하나를 골라 /trader-kr-entry 종목.")
    return 0 if hits else 1


def main(argv=None):
    krx.utf8_stdio()
    ap = argparse.ArgumentParser(description="차트 스캐너 (trader-kr-scanner).")
    sub = ap.add_subparsers(dest="cmd")
    q = sub.add_parser("universe", help="코스피·코스닥 종목 목록")
    q.add_argument("--include-preferred", action="store_true")
    q.add_argument("--out", default="trader-kr/universe.json")

    def common(q):
        q.add_argument("--symbols", help="005930,000660 처럼 쉼표로")
        q.add_argument("--watchlist", help="종목이 든 JSON")
        q.add_argument("--universe", action="store_true", help="trader-kr/universe.json 전 종목")
        q.add_argument("--universe-file", default="trader-kr/universe.json")
        q.add_argument("--limit", type=int)
        q.add_argument("--include-preferred", action="store_true")
        q.add_argument("--offline", metavar="DIR", help="저장된 파일 폴더 (네트워크 없이)")
        q.add_argument("--now", help='이 한국 시간인 것처럼, 예: "2026-10-28 10:15"')
        q.add_argument("--out", default="trader-kr/watchlist.json")
        q.add_argument("--no-write", action="store_true")
        q.add_argument("--show", type=int, default=20)
        q.add_argument("--json", action="store_true")

    q = sub.add_parser("daily", help="장 전 일봉 셋업")
    common(q)
    q.add_argument("--source", choices=["auto", "krx", "toss"], default="auto",
                   help="일봉 출처 (auto: KRX_API_KEY 가 있으면 KRX 공식 종가, 없으면 토스)")
    q.add_argument("--min-price", type=float, default=1000)
    q.add_argument("--min-value", type=float, default=1e9, help="최소 거래대금 (기본 10억원)")
    q = sub.add_parser("orb", help="지금까지 시가 범위 돌파")
    common(q)
    q.add_argument("--at", help="이 시각(HH:MM)에 자르기")
    q.add_argument("--or-minutes", type=int, default=15)
    q.add_argument("--until", help="새 진입 마감 (기본 설정 no_entries_after)")
    q.add_argument("--min-price", type=float, default=1000)
    q.add_argument("--config", default="trader-kr/config.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"universe": cmd_universe, "daily": cmd_daily, "orb": cmd_orb}[a.cmd](a)
    except (ScanError, krx.KrxError, data.DataError, toss.TossError) as e:
        print("scan.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
