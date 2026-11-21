#!/usr/bin/env python3
"""임원·주요주주 보고 (trader-kr-insider). 표준 라이브러리만 쓴다.

원본 (미국판 trader-pelosi) 은 미국 하원의원의 주식 거래 보고 (PTR) 를 읽었다. 국내판은 상장회사 임원과
주요주주(10% 이상 주주 등)가 DART 에 내는 두 보고를 읽는다.

  소유상황보고서 (D002)  산 뒤에 낸다. 변동이 있는 날부터 5일까지 (자본시장법 제173조 ①).
  거래계획보고서 (D005)  사기 전에 낸다. 거래기간 개시일 30일 전까지 (법 제173조의3 ①, 시행령 제200조의3 ④).

    python insider.py                                # 관심 종목의 새 보고, 원문에서 거래 줄까지
    python insider.py --symbols 005930 --days 90     # 한 종목의 지난 90일
    python insider.py market                         # 시장 전체 (유가·코스닥) 새 보고 목록
    python insider.py market --plans --read          # 거래계획만, 원문까지
    python insider.py show 20261120000527            # 보고 하나를 줄로

본 것: trader-kr/state/insider.json 에 접수번호를 30일 동안 둔다. 종료 코드: 새 보고 0, 없음 1, 오류 2.
키: DART_API_KEY (또는 python krcommon/keys.py dart 로 등록한 파일). 키 값은 화면에 찍지 않는다.
"""

import argparse
import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
from krcommon import dart, dartdoc, financials as fin, krx, watch  # noqa: E402

STATE = os.path.join("trader-kr", "state", "insider.json")
KEEP_DAYS = 30
MARKET_CLS = ("Y", "K")
PUBLIC = ("공개된 보고를 빨리 읽을 뿐 미공개 정보가 아닙니다. 임원이나 주요주주가 사고팔았다는 것이 주가가 어디로 갈지 "
          "말해 주지 않습니다.")
HOLD_NOTE = ("변동일은 장내 매매면 결제일입니다 (시행령 제200조 ④). 보고 기한은 변동일부터 5일이고 토요일·공휴일·근로자의 "
             "날은 세지 않습니다 (법 제173조 ①, 시행령 제200조 ①·제153조 ①). 아래 평일 수는 공휴일을 빼지 않은 어림입니다.")
PLAN_NOTE = ("거래계획은 예상 수량과 단가입니다. 거래금액의 30% 범위에서 계획과 달리 거래할 수 있고 (시행령 제200조의3 ⑥), "
             "사망·파산·큰 가격 변동 등으로 철회될 수 있습니다 (법 제173조의3 ④).")
SAFETY = "교육과 도구일 뿐 투자 권유가 아닙니다 (not financial advice)."
DATE = re.compile(r"(\d{4})\s*[년.\-/]\s*(\d{1,2})\s*[월.\-/]\s*(\d{1,2})")


class InsiderError(Exception):
    pass


def norm(text):
    return re.sub(r"[\sㆍ·・]+", "", str(text or ""))


def kind_of(report_nm):
    n = norm(report_nm)
    if "거래계획보고" in n:
        return "거래계획 철회" if "철회" in n else "거래계획"
    if "소유상황보고" in n:
        return "소유상황"
    return None


# ---------------------------------------------------------------- 원문 읽기

def cells(line):
    return [c.strip() for c in line.split("|")]


def to_date(cell):
    m = DATE.search(cell or "")
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def to_int(cell):
    """'14,428,256' -> 14428256, '-2,000' -> -2000, '(1,234)' -> 1234 (괄호는 다른 증권의 단가), '-' -> None."""
    m = re.search(r"-?\d[\d,]*", str(cell or ""))
    return int(m.group(0).replace(",", "")) if m else None


def value_after(lines, label):
    """label 로 시작하는 칸의 다음 칸 (첫 번째)."""
    want = norm(label)
    for ln in lines:
        cs = cells(ln)
        for i, c in enumerate(cs[:-1]):
            if norm(c).startswith(want):
                return cs[i + 1]
    return None


def relation(lines):
    reg, pos, major = value_after(lines, "임원(등기여부)"), value_after(lines, "직위명"), None
    for ln in lines:
        cs = cells(ln)
        if len(cs) >= 2 and norm(cs[0]) == "주요주주":
            major = cs[1]
            break
    parts = []
    if reg and reg != "-":
        parts.append("임원 %s%s" % (reg, " %s" % pos if pos and pos != "-" else ""))
    if major and major != "-":
        parts.append("주요주주 %s" % major)
    return ", ".join(parts) or None


def parse_holding(text):
    """소유상황보고서 원문 -> 보고구분, 관계, 세부변동 줄."""
    lines = text.split("\n")
    rows = []
    start = next((i for i, ln in enumerate(lines) if "세부변동내역" in ln), None)
    if start is not None:
        for ln in lines[start + 1:]:
            cs = cells(ln)
            if norm(cs[0]).startswith("합계") or ln.startswith("*"):
                break
            if len(cs) < 7 or not to_date(cs[1]):
                continue
            change = to_int(cs[4])
            if change is not None and "(-)" in cs[0] and change > 0:
                change = -change
            rows.append({"reason": cs[0], "date": to_date(cs[1]), "kind": cs[2], "before": to_int(cs[3]),
                         "change": change, "after": to_int(cs[5]), "price": to_int(cs[6]),
                         "plan_date": to_date(cs[8]) if len(cs) > 8 else None})
    return {"type": value_after(lines, "보고구분"), "reporter_type": value_after(lines, "보고자 구분"),
            "relation": relation(lines), "rows": rows}


def parse_plan(text):
    """거래계획보고서 원문 -> 관계, 거래목적, 계획 줄."""
    lines = text.split("\n")
    purpose = None
    for i, ln in enumerate(lines[:-1]):
        if norm(ln) == "거래목적" and i > 0 and "거래목적" in lines[i - 1]:
            purpose = lines[i + 1][:80]
            break
    rows = []
    start = next((i for i, ln in enumerate(lines) if "거래수량 및 거래금액" in ln), None)
    if start is not None:
        for ln in lines[start + 1:]:
            cs = cells(ln)
            if ln.startswith(("①", "※", "(3)")):
                break
            if len(cs) < 8 or not (to_date(cs[0]) and to_date(cs[1])):
                continue
            rows.append({"start": to_date(cs[0]), "end": to_date(cs[1]), "days": to_int(cs[2]), "method": cs[3],
                         "kind": cs[4], "qty": to_int(cs[5]), "price": to_int(cs[6]), "amount": to_int(cs[7])})
    return {"reporter_type": value_after(lines, "보고자 구분"), "relation": relation(lines), "purpose": purpose,
            "rows": rows}


def weekdays_between(d0, d1):
    """d0 다음 날부터 d1 까지 평일 수 (공휴일은 빼지 않음)."""
    a, b = datetime.date.fromisoformat(d0), datetime.date.fromisoformat(d1)
    n, d = 0, a
    while d < b:
        d += datetime.timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def summarize(rec):
    """소유상황 줄을 종류별로 더한다. 금액은 장내 매매 줄만 (증여·상여·전환 같은 줄의 단가는 매매가가 아니다)."""
    rows = rec.get("rows") or []
    if rec["kind"] != "소유상황" or not rows:
        return rec
    net, after = {}, {}
    for r in rows:
        if r["change"]:
            net[r["kind"]] = net.get(r["kind"], 0) + r["change"]
        if r["after"] is not None:
            after[r["kind"]] = r["after"]
    trades = [r for r in rows if "장내" in r["reason"] and r["change"] and r["price"]]
    rec.update({"net": net, "after": after, "market_rows": len(trades),
                "market_amount": sum(r["change"] * r["price"] for r in trades) if trades else None,
                "last_change": max(r["date"] for r in rows)})
    rec["weekdays_to_filing"] = weekdays_between(rec["last_change"], rec["date"]) if rec.get("date") else None
    return rec


# ---------------------------------------------------------------- 모으기

def record(r):
    d = str(r.get("rcept_dt") or "")
    nm = " ".join(str(r.get("report_nm") or "").split())
    rc = str(r.get("rcept_no") or "")
    return {"rcept_no": rc, "date": "%s-%s-%s" % (d[:4], d[4:6], d[6:8]) if len(d) == 8 else d,
            "symbol": str(r.get("stock_code") or "").strip(), "name": r.get("corp_name"), "report": nm,
            "kind": kind_of(nm), "fix": nm.startswith("["), "reporter": r.get("flr_nm"),
            "market": {"Y": "유가", "K": "코스닥"}.get(r.get("corp_cls")), "url": dartdoc.viewer_url(rc)}


def from_watch(symbols, days, key, codes, transport=None):
    """관심 종목마다 회사 고유번호로 기간 안의 공시 목록을 받아 두 보고만 고른다."""
    out, missing = [], []
    for s, _, _ in symbols:
        c = codes.get(s)
        if not c:
            missing.append(s)
            continue
        for r in dart.corp_list(c["corp_code"], days[0], days[-1], key, transport):
            if kind_of(r.get("report_nm")) and str(r.get("stock_code") or "").strip() == s:
                out.append(r)
    return out, missing


def from_market(days, key, cache_dir=dart.CACHE_DIR, now_kst=None, transport=None):
    out = []
    for d in days:
        for r in dart.day_list(d, key, cache_dir=cache_dir, now_kst=now_kst, transport=transport):
            if kind_of(r.get("report_nm")) and r.get("corp_cls") in MARKET_CLS and str(r.get("stock_code") or "").strip():
                out.append(r)
    return out


def read_one(rec, key, doc_cache=dartdoc.CACHE_DIR, doc_transport=None):
    try:
        text = dartdoc.read(dartdoc.fetch(rec["rcept_no"], key, doc_cache, doc_transport))[0][1]
    except dart.DartError as e:
        rec["read_error"] = str(e)
        return rec
    rec.update(parse_plan(text) if rec["kind"].startswith("거래계획") else parse_holding(text))
    if not rec.get("rows") and rec["kind"] != "거래계획 철회":
        rec["read_error"] = "원문에서 거래 줄을 찾지 못했습니다. 원문을 직접 읽으세요"
    return summarize(rec)


# ---------------------------------------------------------------- 출력

def n(x):
    return "-" if x is None else format(x, ",")


def signed(x):
    return "-" if x is None else ("+" if x > 0 else "-" if x < 0 else "") + format(abs(x), ",")


def eok(won):
    return "%s억원" % format(round(won / 1e8, 2), ",") if won is not None else "-"


def lines_of(rec, held):
    who = "%s %s" % (rec["symbol"], rec["name"] or "")
    head = "[%s] %s %s 접수, 보고자 %s" % (rec["kind"], who, rec["date"], rec.get("reporter") or "-")
    extra = [x for x in (rec.get("market"), rec.get("relation"), rec.get("type"), "정정" if rec["fix"] else None,
                         "보유 중" if rec["symbol"] in held else None, "본 것" if rec.get("seen") else None) if x]
    L = ["%s%s" % (head, " (%s)" % ", ".join(extra) if extra else "")]
    for r in rec.get("rows") or []:
        if rec["kind"] == "소유상황":
            L.append("    %s %s %s주%s, 결제·변동일 %s%s" % (
                r["reason"], r["kind"], signed(r["change"]), " @%s원" % n(r["price"]) if r["price"] else "",
                r["date"], ", 거래계획 %s 보고분" % r["plan_date"] if r.get("plan_date") else ""))
        else:
            L.append("    계획: %s %s %s주%s%s, %s ~ %s%s" % (           # 방향은 거래방법의 (+)/(-) 가 말한다
                r["method"], r["kind"], n(abs(r["qty"])) if r["qty"] is not None else "-",
                " @%s원" % n(r["price"]) if r["price"] else "", " (약 %s)" % eok(abs(r["amount"])) if r["amount"] else "",
                r["start"], r["end"], " (%s일)" % r["days"] if r["days"] else ""))
    if rec.get("purpose"):
        L.append("    목적: %s" % rec["purpose"])
    if rec.get("net") is not None:
        late = rec.get("weekdays_to_filing")
        L.append("    합계 %s. 장내 매매 금액 %s (%d줄). 변동 후 %s." % (
            ", ".join("%s %s주" % (k, signed(v)) for k, v in rec["net"].items()) or "변동 없음",
            eok(rec.get("market_amount")), rec["market_rows"],
            ", ".join("%s %s주" % (k, n(v)) for k, v in rec["after"].items()) or "-"))
        if late is not None:
            L.append("    마지막 변동일 %s 부터 접수까지 평일 %d일%s" % (
                rec["last_change"], late, " (5일을 넘었을 수 있음. 공휴일과 예외 사유는 원문으로 확인)" if late > 5 else ""))
    if rec.get("read_error"):
        L.append("    원문: %s" % rec["read_error"])
    L.append("    %s  접수번호 %s" % (rec["url"], rec["rcept_no"]))
    return L


def text_report(res, held):
    kinds = {}
    for r in res["items"]:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    scope = "시장 전체 (유가·코스닥)" if res["market"] else "관심 종목 %d개" % len(res["symbols"])
    L = ["임원·주요주주 보고: %s 기준, %s ~ %s 접수분, %s." % (res["now"], res["days"][0], res["days"][-1], scope)]
    if res.get("missing"):
        L.append("회사 고유번호를 찾지 못한 종목: %s (상장 종목코드인지 보세요)" % ", ".join(res["missing"]))
    if not res["items"]:
        L.append("새 보고가 없습니다.")
    else:
        L.append("%s 보고 %d건 (%s)%s." % ("보여 줄" if res["all"] else "새", len(res["items"]),
                                        ", ".join("%s %d" % kv for kv in sorted(kinds.items())),
                                        ", 원문은 %d건만 읽음 (--max-read)" % res["read"] if res.get("unread") else ""))
        for rec in res["items"]:
            L.extend(lines_of(rec, held))
        if any(r["kind"] == "소유상황" for r in res["items"]):
            L.append(HOLD_NOTE)
        if any(r["kind"].startswith("거래계획") for r in res["items"]):
            L.append(PLAN_NOTE)
        L.append(PUBLIC)
    L.append(SAFETY)
    return L


# ---------------------------------------------------------------- 명령

def need_key(env=None):
    key = dart.api_key(env)
    if not key:
        raise InsiderError("DART_API_KEY 가 없습니다. 터미널에서 python krcommon/keys.py dart 로 등록하세요 "
                           "(키를 채팅에 붙여 넣지 않습니다)")
    return key


def load_state(path):
    doc = krx.read_json(path, "본 보고 기록", required=False)
    doc = doc if isinstance(doc, dict) else {}
    return {"seen": dict(doc.get("seen") or {}), "seen_market": dict(doc.get("seen_market") or {})}


def prune(seen, today):
    cut = (today - datetime.timedelta(days=KEEP_DAYS)).isoformat()
    return {k: v for k, v in seen.items() if str(v) >= cut}


def run_new(a, now, key, market, transport=None, doc_transport=None, codes=None):
    days = watch.window(now, a.days, a.since)
    if market and len(days) > 31:
        raise InsiderError("시장 전체는 31일까지입니다 (하루 전체 공시 목록을 받으므로)")
    symbols, missing = [], []
    if market:
        rows = from_market(days, key, a.cache_dir, now, transport)
    else:
        if a.symbols:
            symbols = [(watch._sym(x), None, "--symbols") for x in a.symbols]
            if not all(s for s, _, _ in symbols):
                raise InsiderError("종목코드는 숫자 6자리입니다: %s" % " ".join(a.symbols))
        else:
            symbols = watch.watched(a.root)
        if not symbols:
            raise InsiderError("관심 종목이 없습니다. --symbols 005930 처럼 주거나 market 으로 시장 전체를 보세요")
        codes = codes if codes is not None else fin.corp_codes(key, now_kst=now)
        rows, missing = from_watch(symbols, days, key, codes, transport)
    if a.plans:
        rows = [r for r in rows if kind_of(r.get("report_nm")).startswith("거래계획")]
    state = load_state(a.state)
    seen = state["seen_market" if market else "seen"]
    recs = {}
    for r in rows:
        rec = record(r)
        rec["seen"] = rec["rcept_no"] in seen
        if rec["rcept_no"] and (a.all or not rec["seen"]):
            recs[rec["rcept_no"]] = rec
    items = sorted(recs.values(), key=lambda x: x["rcept_no"], reverse=True)
    want_read = a.read if market else not a.no_read
    read = 0
    if want_read:
        for rec in items[:a.max_read]:
            read_one(rec, key, a.doc_cache, doc_transport)
            read += 1
    res = {"now": now.strftime("%Y-%m-%d %H:%M"), "days": [d.isoformat() for d in days], "market": market,
           "all": a.all, "symbols": [{"symbol": s, "name": nm, "from": w} for s, nm, w in symbols],
           "missing": missing, "items": items, "read": read, "unread": want_read and len(items) > read}
    fresh = [x for x in items if not x.get("seen")]
    if not a.dry_run:
        for x in fresh:
            seen[x["rcept_no"]] = now.date().isoformat()
        state["seen_market" if market else "seen"] = prune(seen, now.date())
        krx.write_json(a.state, state)
    held = {s for s, _ in watch.held(a.root if not market else "trader-kr")}
    print(json.dumps(res, ensure_ascii=False, indent=1) if a.json else "\n".join(text_report(res, held)))
    return 0 if items else 1


def run_show(a, key, doc_transport=None):
    text = dartdoc.read(dartdoc.fetch(a.rcept_no, key, a.doc_cache, doc_transport))[0][1]
    head = "\n".join(text.split("\n")[:8])
    kind = "거래계획" if "거래계획보고서" in norm(head) else ("소유상황" if "소유상황보고서" in norm(head) else None)
    if not kind:
        raise InsiderError("%s 는 임원·주요주주 소유상황보고서나 거래계획보고서가 아닙니다" % a.rcept_no)
    lines = text.split("\n")
    rc = a.rcept_no
    reporter = value_after(lines, "보고자 :") or ""
    rec = {"rcept_no": rc, "kind": kind, "symbol": value_after(lines, "회사코드") or "",
           "name": value_after(lines, "회 사 명"), "date": "%s-%s-%s" % (rc[:4], rc[4:6], rc[6:8]), "fix": False,
           "reporter": reporter.replace(" ", "") if re.search(r"[가-힣]", reporter) else reporter or None,
           "url": dartdoc.viewer_url(rc)}
    rec.update(parse_plan(text) if kind == "거래계획" else parse_holding(text))
    summarize(rec)
    if a.json:
        print(json.dumps(rec, ensure_ascii=False, indent=1))
    else:
        print("\n".join(lines_of(rec, set()) + [PLAN_NOTE if kind == "거래계획" else HOLD_NOTE, PUBLIC, SAFETY]))
    return 0


def main(argv=None, transport=None, doc_transport=None, codes=None):
    krx.utf8_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv.pop(0) if argv and argv[0] in ("new", "market", "show") else "new"
    ap = argparse.ArgumentParser(prog="insider.py %s" % cmd, description="임원·주요주주 보고 (trader-kr-insider).")
    if cmd == "show":
        ap.add_argument("rcept_no", help="접수번호 14자리")
    else:
        if cmd == "new":
            ap.add_argument("--symbols", nargs="+", help="종목코드 (없으면 trader-kr/ 의 파일들에서)")
            ap.add_argument("--no-read", action="store_true", help="원문을 읽지 않고 목록만")
        else:
            ap.add_argument("--read", action="store_true", help="원문까지 읽어 거래 줄을 보인다")
        ap.add_argument("--root", default="trader-kr")
        ap.add_argument("--plans", action="store_true", help="거래계획보고만")
        ap.add_argument("--days", type=int, help="오늘까지 며칠 (기본: 바로 앞 평일부터)")
        ap.add_argument("--since", help="이 날짜부터, 예: 2026-11-18")
        ap.add_argument("--max-read", type=int, default=20, help="원문을 읽을 보고 수 (기본 20)")
        ap.add_argument("--all", action="store_true", help="이미 본 것도 보여 준다")
        ap.add_argument("--dry-run", action="store_true", help="본 것으로 기록하지 않는다")
        ap.add_argument("--state", default=STATE)
        ap.add_argument("--cache-dir", default=dart.CACHE_DIR)
        ap.add_argument("--now", help='이 한국 시간인 것처럼, 예: "2026-11-20 18:30"')
    ap.add_argument("--doc-cache", default=dartdoc.CACHE_DIR)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        key = need_key()
        if cmd == "show":
            return run_show(a, key, doc_transport)
        return run_new(a, krx.parse_now(a.now), key, cmd == "market", transport, doc_transport, codes)
    except (InsiderError, dart.DartError, krx.KrxError, fin.FinError) as e:
        print("오류: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
