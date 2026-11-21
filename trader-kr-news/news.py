#!/usr/bin/env python3
"""공시 알림 (trader-kr-news). 표준 라이브러리만 쓴다.

관심 종목에 새로 나온 DART 공시만 보여 준다. 원본 (미국판 trader-news) 은 Yahoo 뉴스 제목과 SEC 8-K 를
읽었다. 국내판은 회사가 금융감독원에 낸 공시 (DART) 를 읽는다. 뉴스 제목은 읽지 않는다.

    python news.py                                  # 관심 종목: 스캐너 결과, 계획, 오늘 계획들, 일지의 열린 거래
    python news.py --symbols 005930 000660 --days 5
    python news.py --all --json                     # 이미 본 것까지, 기계용
    python news.py market                           # 시장 전체에서 중요 공시만 (증자, 거래정지, 잠정실적 ...)
    python news.py read 20261120000527              # 공시 원문 (생년월일·주소·전화번호는 가림)

기간: 기본은 바로 앞 평일부터 오늘까지 (월요일이면 금요일부터). 장 마감 뒤에 나온 공시가 다음 날 시가를
흔들 수 있어서다. 공휴일이 끼면 --days 로 늘린다.

본 것: trader-kr/state/news.json 에 접수번호를 30일 동안 둔다. 한 번 보여 준 공시는 다시 보여 주지 않는다
(--all 이면 보여 준다). --dry-run 은 기록하지 않는다.

종료 코드: 새 공시가 있으면 0, 없으면 1, 오류는 2.

키: DART_API_KEY (또는 python krcommon/keys.py dart 로 등록한 파일). 키 값은 화면에 찍지 않는다.
"""

import argparse
import datetime
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
from krcommon import dart, dartdoc, krx  # noqa: E402

STATE = os.path.join("trader-kr", "state", "news.json")
KEEP_DAYS = 30
MARKET_CLS = ("Y", "K")
NOTE = "공시 제목은 단서입니다. 무슨 일이 있었는지 말하기 전에 원문을 읽으세요 (news.py read 접수번호)."
SAFETY = "교육과 도구일 뿐 투자 권유가 아닙니다 (not financial advice)."

# (꼬리표, 제목에 들어 있으면 붙는 말). 제목은 빈칸과 가운뎃점을 뺀 뒤 견준다
TAGS = (
    ("실적", ("잠정", "손익구조", "결산실적", "분기보고서", "반기보고서", "사업보고서")),
    ("자금조달", ("유상증자", "무상증자", "전환사채", "신주인수권", "교환사채", "증권신고서", "투자설명서", "감자",
              "증권발행결과")),
    ("지분", ("최대주주", "대량보유", "소유상황보고", "거래계획보고")),
    ("거래소조치", ("매매거래정지", "거래정지", "관리종목", "상장폐지", "상장적격성", "불성실공시", "조회공시",
               "투자주의", "투자경고", "투자위험", "단기과열", "풍문", "주가급변")),
    ("계약·투자", ("단일판매", "공급계약", "신규시설투자", "타법인주식", "유형자산", "영업양수", "영업양도", "자산양수",
              "자산양도")),
    ("합병·분할", ("합병", "분할", "공개매수", "주식교환", "주식이전")),
    ("주주환원", ("자기주식", "배당", "소각")),
    ("법률·위험", ("소송", "횡령", "배임", "회생", "파산", "부도", "감사의견", "의견거절", "부적정", "영업정지", "해산",
              "채무불이행")),
    ("주총", ("주주총회",)),
)
# 단타 전에 꼭 볼 공시 (! 표시). 시장 전체 보기 (market) 는 이것만 보여 준다
HOT = ("잠정", "손익구조", "유상증자", "전환사채", "신주인수권", "교환사채", "감자", "최대주주변경", "매매거래정지",
       "거래정지", "관리종목", "상장폐지", "상장적격성", "불성실공시", "조회공시", "횡령", "배임", "회생", "파산", "부도",
       "의견거절", "공개매수", "합병", "분할", "단일판매", "공급계약", "거래계획보고")
FIX = ("[기재정정]", "[첨부정정]", "[첨부추가]", "[발행조건확정]", "[정정명령부과]", "[정정제출요구]", "[변경등록]")
RM = {"정": "뒤에 정정 있음", "철": "철회됨"}


class NewsError(Exception):
    pass


def norm_name(name):
    return re.sub(r"[\sㆍ·・]+", "", str(name or ""))


def tags_of(report_nm):
    n = norm_name(report_nm)
    out = [t for t, words in TAGS if any(w in n for w in words)]
    if str(report_nm or "").lstrip().startswith(FIX):
        out.insert(0, "정정")
    return out or ["기타"]


def is_hot(report_nm):
    n = norm_name(report_nm)
    return any(w in n for w in HOT)


def item(r):
    nm = " ".join(str(r.get("report_nm") or "").split())
    rcept = str(r.get("rcept_no") or "")
    d = str(r.get("rcept_dt") or "")
    rm = str(r.get("rm") or "")
    return {"rcept_no": rcept, "date": "%s-%s-%s" % (d[:4], d[4:6], d[6:8]) if len(d) == 8 else d,
            "symbol": str(r.get("stock_code") or "").strip(), "name": r.get("corp_name"), "report": nm,
            "filer": r.get("flr_nm"), "tags": tags_of(nm), "hot": is_hot(nm),
            "notes": [RM[c] for c in rm if c in RM], "market": {"Y": "유가", "K": "코스닥"}.get(r.get("corp_cls")),
            "url": dartdoc.viewer_url(rcept)}


# ---------------------------------------------------------------- 관심 종목

def _sym(x):
    try:
        return krx.norm_symbol(x)
    except krx.KrxError:
        return None


def watched(root="trader-kr"):
    """[(종목, 이름, 어디서)]. 스캐너 결과, 계획, 오늘 계획들, 일지의 열린 거래. 같은 종목은 한 번."""
    out, seen = [], set()

    def add(sym, name, where):
        s = _sym(sym)
        if s and s not in seen:
            seen.add(s)
            out.append((s, name, where))

    wl = krx.read_json(os.path.join(root, "watchlist.json"), "스캐너 결과", required=False)
    rows = wl.get("hits", wl.get("symbols", [])) if isinstance(wl, dict) else (wl or [])
    for h in rows:
        if isinstance(h, dict):
            add(h.get("symbol"), h.get("name"), "스캐너")
        else:
            add(h, None, "스캐너")
    plan = krx.read_json(os.path.join(root, "plan.json"), "계획", required=False)
    if isinstance(plan, dict) and plan.get("symbol"):
        add(plan.get("symbol"), plan.get("name"), "계획")
    for p in sorted(glob.glob(os.path.join(root, "today", "*", "plan.json"))):
        doc = krx.read_json(p, "오늘 계획", required=False)
        if isinstance(doc, dict) and doc.get("symbol"):
            add(doc.get("symbol"), doc.get("name"), "오늘 계획")
    jpath = os.path.join(root, "journal.jsonl")
    if os.path.exists(jpath):
        with open(jpath, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("symbol") and r.get("exit_price") is None:
                    add(r.get("symbol"), r.get("name"), "일지 (열린 거래)")
    return out


# ---------------------------------------------------------------- 기간과 본 것

def window(now, days=None, since=None):
    """[시작일, ..., 오늘]. 기본은 바로 앞 평일부터."""
    today = now.date()
    if since:
        start = krx.parse_day(since)
        if start is None:
            raise NewsError("--since 는 2026-11-20 같은 날짜입니다 (받은 값 %r)" % since)
    elif days is not None:
        if days < 1 or days > 31:
            raise NewsError("--days 는 1 ~ 31 입니다")
        start = today - datetime.timedelta(days=days - 1)
    else:
        start = today - datetime.timedelta(days=1)
        while start.weekday() >= 5:
            start -= datetime.timedelta(days=1)
    if start > today:
        raise NewsError("시작일이 오늘보다 늦습니다")
    return [start + datetime.timedelta(days=i) for i in range((today - start).days + 1)]


def load_state(path):
    doc = krx.read_json(path, "본 공시 기록", required=False)
    doc = doc if isinstance(doc, dict) else {}
    return {"seen": dict(doc.get("seen") or {}), "seen_market": dict(doc.get("seen_market") or {})}


def prune(seen, today):
    cut = (today - datetime.timedelta(days=KEEP_DAYS)).isoformat()
    return {k: v for k, v in seen.items() if str(v) >= cut}


# ---------------------------------------------------------------- 모으기

def gather(days, key, cache_dir=dart.CACHE_DIR, now_kst=None, transport=None, sleep=None):
    rows = []
    for d in days:
        kw = {"cache_dir": cache_dir, "now_kst": now_kst, "transport": transport}
        if sleep is not None:
            kw["sleep"] = sleep
        rows.extend(dart.day_list(d, key, **kw))
    return rows


def watch_report(rows, symbols, seen, show_all=False):
    """(종목마다 공시 (새것 또는 --all 이면 전부, 최신 먼저), 종목 이름). 이름은 본 공시에서도 가져온다."""
    wanted = {s for s, _, _ in symbols}
    by = {s: [] for s in wanted}
    names = {}
    for r in rows:
        it = item(r)
        if it["symbol"] in wanted and it["name"]:
            names[it["symbol"]] = it["name"]
        if it["symbol"] in wanted and it["rcept_no"]:
            it["seen"] = it["rcept_no"] in seen
            if show_all or not it["seen"]:
                by[it["symbol"]].append(it)
    for s in by:
        uniq = {x["rcept_no"]: x for x in by[s]}
        by[s] = sorted(uniq.values(), key=lambda x: x["rcept_no"], reverse=True)
    return by, names


def market_report(rows, seen, show_all=False, limit=60):
    out = {}
    for r in rows:
        if r.get("corp_cls") not in MARKET_CLS or not str(r.get("stock_code") or "").strip():
            continue
        it = item(r)
        if not it["hot"] or not it["rcept_no"]:
            continue
        it["seen"] = it["rcept_no"] in seen
        if show_all or not it["seen"]:
            out[it["rcept_no"]] = it
    got = sorted(out.values(), key=lambda x: x["rcept_no"], reverse=True)
    return got[:limit], max(0, len(got) - limit)


# ---------------------------------------------------------------- 출력

def line(it, with_name=False):
    who = "%s %s " % (it["symbol"], it["name"] or "") if with_name else ""
    head = "%s %s%s [%s] %s" % ("!" if it["hot"] else " ", who, it["date"], ", ".join(it["tags"]), it["report"])
    extra = []
    if it.get("market"):
        extra.append(it["market"])
    if it.get("filer") and it["filer"] != it.get("name"):
        extra.append("제출 %s" % it["filer"])
    extra.extend(it["notes"])
    if it.get("seen"):
        extra.append("본 것")
    return ["  %s%s" % (head, "  (%s)" % ", ".join(extra) if extra else ""),
            "      %s  접수번호 %s" % (it["url"], it["rcept_no"])]


def text_watch(res):
    n_new = sum(len(v) for v in res["by_symbol"].values())
    n_hot = sum(1 for v in res["by_symbol"].values() for x in v if x["hot"])
    srcs = {}
    for _, _, w in res["symbols"]:
        srcs[w] = srcs.get(w, 0) + 1
    L = ["공시 알림: %s 기준, %s ~ %s 접수분. 관심 종목 %d개 (%s)." % (
        res["now"], res["days"][0], res["days"][-1], len(res["symbols"]),
        ", ".join("%s %d" % kv for kv in srcs.items()))]
    L.append(("%s 공시 %d건 (! 중요 %d건)." % ("보여 줄" if res["all"] else "새", n_new, n_hot)) if n_new
             else "새 공시가 없습니다.")
    for s, name, _ in res["symbols"]:
        got = res["by_symbol"].get(s) or []
        who = " ".join(x for x in (s, name or res["names"].get(s)) if x)
        if not got:
            L.append("%s: 새 공시 없음" % who)
            continue
        L.append(who)
        for it in got:
            L.extend(line(it))
    if n_new:
        L.append(NOTE)
    L.append(SAFETY)
    return L


def text_market(res):
    L = ["시장 전체 중요 공시: %s 기준, %s ~ %s 접수분 (유가·코스닥, ! 표시 공시만)." % (
        res["now"], res["days"][0], res["days"][-1])]
    if not res["items"]:
        L.append("새 중요 공시가 없습니다.")
    else:
        L.append("%d건%s." % (len(res["items"]), " (더 있음 %d건, --limit 로 늘림)" % res["more"] if res["more"] else ""))
        for it in res["items"]:
            L.extend(line(it, with_name=True))
        L.append(NOTE)
    L.append(SAFETY)
    return L


# ---------------------------------------------------------------- 명령

def need_key(env=None):
    key = dart.api_key(env)
    if not key:
        raise NewsError("DART_API_KEY 가 없습니다. 터미널에서 python krcommon/keys.py dart 로 등록하세요 "
                        "(키를 채팅에 붙여 넣지 않습니다)")
    return key


def run_watch(a, now, key, transport=None):
    if a.symbols:
        symbols = [(s, None, "--symbols") for s in (_sym(x) for x in a.symbols) if s]
        if len(symbols) != len(a.symbols):
            raise NewsError("종목코드는 숫자 6자리입니다: %s" % " ".join(a.symbols))
    else:
        symbols = watched(a.root)
    if not symbols:
        raise NewsError("관심 종목이 없습니다. --symbols 005930 처럼 주거나 /trader-kr-scanner 를 먼저 돌리세요")
    days = window(now, a.days, a.since)
    state = load_state(a.state)
    rows = gather(days, key, a.cache_dir, now, transport)
    by, names = watch_report(rows, symbols, state["seen"], a.all)
    res = {"now": now.strftime("%Y-%m-%d %H:%M"), "days": [d.isoformat() for d in days], "all": a.all,
           "symbols": symbols, "by_symbol": by, "names": names}
    fresh = [x for v in by.values() for x in v if not x.get("seen")]
    if not a.dry_run:
        for x in fresh:
            state["seen"][x["rcept_no"]] = now.date().isoformat()
        state["seen"] = prune(state["seen"], now.date())
        krx.write_json(a.state, state)
    if a.json:
        print(json.dumps(dict(res, symbols=[{"symbol": s, "name": n, "from": w} for s, n, w in symbols]),
                         ensure_ascii=False, indent=1))
    else:
        print("\n".join(text_watch(res)))
    return 0 if fresh or (a.all and any(by.values())) else 1


def run_market(a, now, key, transport=None):
    days = window(now, a.days, a.since)
    state = load_state(a.state)
    rows = gather(days, key, a.cache_dir, now, transport)
    items, more = market_report(rows, state["seen_market"], a.all, a.limit)
    res = {"now": now.strftime("%Y-%m-%d %H:%M"), "days": [d.isoformat() for d in days], "items": items, "more": more}
    fresh = [x for x in items if not x.get("seen")]
    if not a.dry_run:
        for x in fresh:
            state["seen_market"][x["rcept_no"]] = now.date().isoformat()
        state["seen_market"] = prune(state["seen_market"], now.date())
        krx.write_json(a.state, state)
    print(json.dumps(res, ensure_ascii=False, indent=1) if a.json else "\n".join(text_market(res)))
    return 0 if items else 1


def run_read(a, key, transport=None):
    parts = dartdoc.read(dartdoc.fetch(a.rcept_no, key, a.doc_cache, transport))
    text = "\n\n".join(t for _, t in parts)
    total = len(text)
    if a.find:
        lines = text.split("\n")
        hits = [i for i, ln in enumerate(lines) if a.find in ln]
        keep = sorted({j for i in hits for j in range(max(0, i - 2), min(len(lines), i + 3))})
        text = "\n".join(lines[j] for j in keep) if keep else "%r 이 원문에 없습니다." % a.find
    shown = text[:a.max_chars]
    if a.json:
        print(json.dumps({"rcept_no": a.rcept_no, "url": dartdoc.viewer_url(a.rcept_no), "chars": total,
                          "files": [n for n, _ in parts], "text": shown}, ensure_ascii=False, indent=1))
        return 0
    print("공시 원문 %s (%s), %s자. 생년월일·주소·전화번호·이메일은 가렸습니다." % (
        a.rcept_no, dartdoc.viewer_url(a.rcept_no), format(total, ",")))
    print(shown)
    if len(text) > len(shown):
        print("... (%s자 중 %s자만 보였습니다. --max-chars 로 늘리거나 --find 로 찾으세요)" % (
            format(len(text), ","), format(len(shown), ",")))
    return 0


def main(argv=None, transport=None, doc_transport=None):
    krx.utf8_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv.pop(0) if argv and argv[0] in ("check", "market", "read") else "check"
    ap = argparse.ArgumentParser(prog="news.py %s" % cmd, description="관심 종목의 새 DART 공시 (trader-kr-news).")
    if cmd == "read":
        ap.add_argument("rcept_no", help="접수번호 14자리")
        ap.add_argument("--max-chars", type=int, default=12000)
        ap.add_argument("--find", help="이 말이 든 줄과 앞뒤 두 줄만")
        ap.add_argument("--doc-cache", default=dartdoc.CACHE_DIR)
    else:
        if cmd == "check":
            ap.add_argument("--symbols", nargs="+", help="종목코드 (없으면 trader-kr/ 의 파일들에서)")
            ap.add_argument("--root", default="trader-kr")
        else:
            ap.add_argument("--limit", type=int, default=60)
        ap.add_argument("--days", type=int, help="오늘까지 며칠 (기본: 바로 앞 평일부터)")
        ap.add_argument("--since", help="이 날짜부터, 예: 2026-11-18")
        ap.add_argument("--all", action="store_true", help="이미 본 것도 보여 준다")
        ap.add_argument("--dry-run", action="store_true", help="본 것으로 기록하지 않는다")
        ap.add_argument("--state", default=STATE)
        ap.add_argument("--cache-dir", default=dart.CACHE_DIR)
        ap.add_argument("--now", help='이 한국 시간인 것처럼, 예: "2026-11-20 08:30"')
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        key = need_key()
        if cmd == "read":
            return run_read(a, key, doc_transport)
        now = krx.parse_now(a.now)
        return (run_market if cmd == "market" else run_watch)(a, now, key, transport)
    except (NewsError, dart.DartError, krx.KrxError) as e:
        print("오류: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
