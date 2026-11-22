#!/usr/bin/env python3
"""「주식 기초 노트」(공유용 기초편) 예시 숫자 만들기. 공개 자료만 담는다.

    python trader-kr-notes/notes.py basics

자료
  KRX 일별매매정보 캐시 (trader-kr/cache/invest): 코스피 보통주 올해 수익률 분포, 삼성전자 주가·상장주식 수·시가총액.
    수익률은 KRX '전일 대비' 로 이어 붙인 조정 가격(invest.Panel)이라 액면분할 등이 반영된다.
  네이버 증권 일별 시세: KODEX 200 (069500) 올해 종가.
  Yahoo 수정 종가: IVV (S&P500) 10년 월말 값과 큰 하락 구간.
  trader-kr/notes/share/etfs.json, basis.json: 국내 ETF 총보수, IVV 10년 연평균 (notes.py share 를 먼저 돌린다).
하나라도 못 읽거나 건수가 이상하면 trader-kr/notes/basics/report.json 에 ok:false, 종료 코드 2.
결과: trader-kr/notes/basics/{stats,kospi_dist,kodex_daily,ivv_monthly,ivv_drawdowns,etfs,markets}.json
"""
import datetime as dt
import importlib.util
import io
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))   # 저장소 폴더
NOTES = os.path.join("trader-kr", "notes")   # 프로젝트 폴더 기준
SHARE = os.path.join(NOTES, "share")
OUT, RAW = os.path.join(NOTES, "basics"), os.path.join(NOTES, "basics", "raw")
UA = {"User-Agent": "Mozilla/5.0"}
problems = []


def get(url, name, max_age_h=6):
    p = os.path.join(RAW, name)
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < max_age_h * 3600:
        return open(p, "rb").read()
    for i in range(3):
        try:
            raw = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60).read()
            open(p, "wb").write(raw)
            time.sleep(0.15)
            return raw
        except Exception as e:
            last = e
            time.sleep(2 + 2 * i)
    problems.append("받기 실패 %s (%s)" % (url, last))
    return None


def krx_part():
    spec = importlib.util.spec_from_file_location("inv_ro", os.path.join(ROOT, "trader-kr-invest", "invest.py"))
    inv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inv)
    today = dt.date.today()
    P = inv.load_panel(inv.STORE, today)
    end = P.dates[-1]
    ye = [(m, T) for m, T, _ in P.months if m == "%d-12" % (end.year - 1)]
    if not ye:
        problems.append("KRX 자료에 작년 12월 말이 없음")
        return None
    mye, T0 = ye[0]
    snap = P.snap.get(mye) or {}
    rows = []
    for s, sn in snap.items():
        if sn.mkt != "K" or sn.spac or not s.endswith("0") or s.endswith("*"):
            continue
        a, b = P.price(s, T0), P.price(s, end)
        if a and b and P.final[s][0] >= end:
            rows.append((s, sn.cap, b / a - 1.0))
    if len(rows) < 500:
        problems.append("코스피 보통주가 %d개뿐" % len(rows))
        return None
    rets = sorted(r for _, _, r in rows)
    n = len(rets)
    med = rets[n // 2] if n % 2 else (rets[n // 2 - 1] + rets[n // 2]) / 2
    capw = sum(c * r for _, c, r in rows) / sum(c for _, c, _ in rows)
    ew = sum(rets) / n
    top2 = sorted(rows, key=lambda x: -x[1])[:2]
    edges = [-1.0, -0.5, -0.3, -0.1, 0.0, 0.1, 0.3, 0.5, 1.0, float("inf")]
    labels = ["-50% 이하", "-50~-30%", "-30~-10%", "-10~0%", "0~10%", "10~30%", "30~50%", "50~100%", "100% 넘게"]
    dist = []
    for lo, hi, lab in zip(edges, edges[1:], labels):
        c = sum(1 for r in rets if lo < r <= hi) if lo > -1 else sum(1 for r in rets if r <= hi)
        dist.append({"bin": lab, "lo": None if lo == -1 else round(lo * 100), "hi": None if hi == float("inf") else round(hi * 100), "count": c})
    if sum(d["count"] for d in dist) != n:
        problems.append("분포 칸 합 %d != %d" % (sum(d["count"] for d in dist), n))
    # 삼성전자: 마지막 날 KRX 행의 종가·시가총액 -> 상장주식 수
    doc = inv.read_day(inv.STORE, end)
    sam = {r[0]: r for r in doc["rows"]}.get("005930")
    if not sam:
        problems.append("KRX 마지막 날에 삼성전자 없음")
        return None
    stats = {"krx_start": str(T0), "krx_end": str(end), "kospi_n": n, "kospi_median": round(med * 100, 1),
             "kospi_up_pct": round(sum(1 for r in rets if r > 0) / n * 100, 1),
             "kospi_capw": round(capw * 100, 1), "kospi_ew": round(ew * 100, 1),
             "kospi_beat_capw_pct": round(sum(1 for r in rets if r > capw) / n * 100, 1),
             "top2_names": "·".join(snap[s].mkt and P_name(doc, s) for s, _, _ in top2),
             "top2_weight": round(sum(c for _, c, _ in top2) / sum(c for _, c, _ in rows) * 100, 1),
             "sam_close": sam[3], "sam_cap": sam[5], "sam_shares": round(sam[5] / sam[3]), "sam_day": str(end)}
    return stats, dist


def P_name(doc, s):
    return {r[0]: r[2] for r in doc["rows"]}.get(s, s)


def kodex_part(since):
    rows = {}
    for page in range(1, 9):
        raw = get("https://m.stock.naver.com/api/stock/069500/price?pageSize=60&page=%d" % page, "nvp_069500_%d.json" % page)
        if raw is None:
            return None
        for r in json.loads(raw):
            rows[r["localTradedAt"]] = float(r["closePrice"].replace(",", ""))
        if min(rows) <= since:
            break
    ds = sorted(rows)
    base = [d for d in ds if d <= since]
    if not base:
        problems.append("KODEX 200 작년 말 종가를 찾지 못함")
        return None
    d0 = base[-1]
    yr = [d for d in ds if d > d0]
    daily = [{"date": d, "close": rows[d]} for d in [d0] + yr]
    moves = [(yr[i], rows[yr[i]] / rows[([d0] + yr)[i]] - 1) for i in range(len(yr))]
    up, dn = max(moves, key=lambda x: x[1]), min(moves, key=lambda x: x[1])
    peak_d = max(yr, key=lambda d: rows[d])
    last = ds[-1]
    st = {"kodex_ye_date": d0, "kodex_ye": rows[d0], "kodex_last_date": last, "kodex_last": rows[last],
          "kodex_ytd": round((rows[last] / rows[d0] - 1) * 100, 1), "kodex_up_date": up[0], "kodex_up": round(up[1] * 100, 1),
          "kodex_dn_date": dn[0], "kodex_dn": round(dn[1] * 100, 1), "kodex_peak_date": peak_d, "kodex_peak": rows[peak_d],
          "kodex_from_peak": round((rows[last] / rows[peak_d] - 1) * 100, 1),
          "kodex_days_up_5": sum(1 for _, m in moves if m >= 0.05), "kodex_days_dn_5": sum(1 for _, m in moves if m <= -0.05),
          "kodex_days": len(moves)}
    return st, daily


def drawdowns(pts, limit=-0.20):
    """[(날짜, 값)] 에서 직전 고점 대비 limit 보다 깊이 내려간 구간들. 고점을 다시 넘은 날을 회복일로 둔다."""
    eps, peak, in_dd = [], pts[0], None
    for d, q in pts:
        if q >= peak[1]:
            if in_dd:
                in_dd["recovered"] = str(d)
                eps.append(in_dd)
                in_dd = None
            peak = (d, q)
            continue
        dd = q / peak[1] - 1
        if in_dd is None and dd <= limit:
            in_dd = {"peak": str(peak[0]), "trough": str(d), "drop": dd}
        elif in_dd is not None and dd < in_dd["drop"]:
            in_dd.update(trough=str(d), drop=dd)
    if in_dd:
        in_dd["recovered"] = None
        eps.append(in_dd)
    for e in eps:
        e["drop"] = round(e["drop"] * 100, 1)
        e["months_to_recover"] = None if not e["recovered"] else round(
            (dt.date.fromisoformat(e["recovered"]) - dt.date.fromisoformat(e["peak"])).days / 30.44, 1)
    return eps


def ivv_part():
    raw = get("https://query1.finance.yahoo.com/v8/finance/chart/IVV?range=15y&interval=1d", "yh_IVV.json", 12)
    if raw is None:
        return None
    c = json.loads(raw)["chart"]["result"][0]
    pts = [(dt.datetime.fromtimestamp(a - 5 * 3600, dt.timezone.utc).date(), q)
           for a, q in zip(c["timestamp"], c["indicators"]["adjclose"][0]["adjclose"]) if q]
    end = pts[-1][0]
    pts = [p for p in pts if p[0] > end.replace(year=end.year - 10)]
    monthly = {}
    for d, q in pts:
        monthly[d.strftime("%Y-%m")] = (str(d), q)
    mrows = [{"month": m, "date": v[0], "value": round(v[1], 2)} for m, v in sorted(monthly.items())]
    eps = drawdowns(pts)
    return mrows, eps, str(end)


def main():
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(RAW, exist_ok=True)
    rep = {"run_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "problems": problems}
    k = krx_part()
    kd = kodex_part("%d-12-31" % (dt.date.today().year - 1))
    iv = ivv_part()
    try:
        etfs = json.load(io.open(os.path.join(SHARE, "etfs.json"), encoding="utf-8"))
        markets = json.load(io.open(os.path.join(SHARE, "markets.json"), encoding="utf-8"))
        srep = json.load(io.open(os.path.join(SHARE, "report.json"), encoding="utf-8"))
        age = dt.datetime.now(dt.timezone.utc) - dt.datetime.strptime(srep["run_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        if not srep.get("ok") or age > dt.timedelta(days=1):
            problems.append("share 결과가 ok 가 아니거나 %s 에 만든 것이라 오래됨 (notes.py share 를 먼저 돌릴 것)" % srep.get("run_at"))
        sb = {r["item"]: r["value"] for r in json.load(io.open(os.path.join(SHARE, "basis.json"), encoding="utf-8"))}
    except Exception as e:
        problems.append("share 결과 읽기 실패 (%s)" % e)
        etfs, sb = None, {}
    if problems or not (k and kd and iv and etfs):
        rep["ok"] = False
        json.dump(rep, io.open(os.path.join(OUT, "report.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("멈춤:", file=sys.stderr)
        for p in problems:
            print(" - " + p, file=sys.stderr)
        return 2
    stats, dist = k
    kst, daily = kd
    mrows, eps, ivv_end = iv
    stats.update(kst)
    fee_asof = next((e.get("asof") for e in etfs if e.get("code") == "360750"), None)
    stats.update({"ivv_end": ivv_end, "ivv_10y": sb.get("S&P500 (IVV) 10년 연평균 (%)"), "ivv_10y_asof": sb.get("10년 수익률 기준일"),
                  "ivv_ytd_asof": sb.get("올해 수익률 기준일"), "etf_fee_asof": fee_asof.replace(".", "-") if fee_asof else None,
                  "updated": dt.date.today().isoformat()})
    stat_rows = [{"key": k2, "value": v} for k2, v in stats.items()]
    data = {"stats": stat_rows, "kospi_dist": dist, "kodex_daily": daily, "ivv_monthly": mrows, "ivv_drawdowns": eps, "etfs": etfs, "markets": markets}
    for name, v in data.items():
        io.open(os.path.join(OUT, name + ".json"), "w", encoding="utf-8").write(json.dumps(v, ensure_ascii=False, indent=1))
    rep["ok"] = True
    rep["counts"] = {k2: len(v) for k2, v in data.items()}
    json.dump(rep, io.open(os.path.join(OUT, "report.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(rep["counts"], ensure_ascii=False))
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    print("drawdowns:", eps)
    return 0


if __name__ == "__main__":
    sys.exit(main())
