#!/usr/bin/env python3
"""오늘 장 한 번에 (trader-kr-today): 지금 시각에 맞춰 스캔부터 위험 점검까지 이어서 하고 한 장으로 보여 준다.

    python today.py                          # 지금 시각에 맞춰
    python today.py --max 5 --sim 0          # 후보 5개까지, 타임머신 재생은 안 함
    python today.py --symbols 005930,000660  # 거래대금 상위 대신 이 종목만
    python today.py --offline 폴더 --status 폴더/status.json --now "2026-10-28 10:15:30"   # 저장된 파일로

시각에 따라 하는 일이 다르다.
  09:00 전          장 전 일봉 스캔 (KRX 키가 있으면 한국거래소 종가)
  시가 범위 형성 중  범위가 끝나는 시각을 알려 준다
  진입 시간         시가 범위 돌파 스캔 -> 신호가 최근인 후보부터 진입 확인 -> 청산 계획 -> 수량 ->
                    위험 점검. 통과한 후보 중 앞의 --sim 개는 타임머신으로 남은 장을 재생한다
  진입 마감 뒤      열린 거래와 정리 시각
  장 마감 뒤        오늘 거래 일지

각 단계는 다른 스킬의 스크립트를 그대로 부른다 (같은 프로세스, 토스증권 연결 하나). 후보마다 파일은
trader-kr/today/<종목>/ 에, 단계별 전체 출력은 trader-kr/today/report.md 에, 요약은 trader-kr/today.json 에 남긴다.
주문은 내지 않는다.
"""

import argparse
import contextlib
import datetime
import importlib.util
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from krcommon import bars as kbars, data, krx  # noqa: E402

BASE = "trader-kr/today"
SUMMARY = "trader-kr/today.json"
SCRIPTS = {"scan": ("trader-kr-scanner", "scan"), "entry": ("trader-kr-entry", "entry"),
           "exit": ("trader-kr-exit", "exit"), "tm": ("trader-kr-timemachine", "timemachine"),
           "size": ("trader-kr-budget", "size"), "risk": ("trader-kr-risk", "check"),
           "journal": ("trader-kr-journal", "journal")}
STALE_MINUTES = 15
WEEKDAYS = "월화수목금토일"


class TodayError(Exception):
    pass


def script(key):
    """옆 스킬 폴더의 스크립트를 모듈로 불러온다 (폴더 이름에 하이픈이 있어 import 로는 못 부름)."""
    folder, name = SCRIPTS[key]
    mod_name = "trader_kr_today_" + name
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    path = os.path.join(ROOT, folder, name + ".py")
    if not os.path.exists(path):
        raise TodayError("%s 가 없습니다. trader-kr-* 폴더를 같이 설치해야 합니다" % path)
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.modules[mod_name] = mod
    return mod


class Log(object):
    """단계마다 부른 명령과 전체 출력. report.md 로 남긴다."""

    def __init__(self):
        self.steps = []

    def call(self, key, argv, title):
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = script(key).main(argv)
        except SystemExit as e:                    # argparse 오류
            code = e.code if isinstance(e.code, int) else 1
        except TodayError as e:
            err.write(str(e))
            code = 1
        folder, name = SCRIPTS[key]
        self.steps.append({"title": title, "command": "python %s/%s.py %s" % (folder, name, " ".join(argv)),
                           "code": code, "stdout": out.getvalue().rstrip(), "stderr": err.getvalue().rstrip()})
        return code, out.getvalue(), err.getvalue()

    def markdown(self, head):
        L = ["# 오늘 장 단계별 출력", "", head, ""]
        for s in self.steps:
            L += ["## %s" % s["title"], "", "```", "$ " + s["command"], s["stdout"]]
            if s["stderr"]:
                L.append(s["stderr"])
            L += ["(종료 코드 %s)" % s["code"], "```", ""]
        return "\n".join(L)


def read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def last_line(text):
    lines = [x.strip() for x in (text or "").strip().splitlines() if x.strip()]
    return lines[-1] if lines else ""


def clock(day, t):
    return datetime.datetime.combine(day, t)


def phase(now, session, cfg, or_minutes):
    """(단계, 진입 시작, 진입 마감, 정리 시각). 단계: closed, pre, forming, entries, late, after."""
    if session is None:
        return "closed", None, None, None
    day = session.day
    start = clock(day, session.shift(krx.parse_hhmm(cfg["no_entries_before"], "no_entries_before")))
    start = max(start, session.open_dt + datetime.timedelta(minutes=or_minutes))
    end = clock(day, session.shift(krx.parse_hhmm(cfg["no_entries_after"], "no_entries_after")))
    flat = clock(day, session.shift(krx.parse_hhmm(cfg["flat_by"], "flat_by")))
    if now < session.open_dt:
        return "pre", start, end, flat
    if now < start:
        return "forming", start, end, flat
    if now.replace(second=0, microsecond=0) <= end:
        return "entries", start, end, flat
    if now < session.close_dt:
        return "late", start, end, flat
    return "after", start, end, flat


def offline_prev_close(path, sym, day):
    try:
        ch = kbars.load_offline(path, sym)
    except kbars.BarError:
        return None
    rows = [b for b in ch["bars"] if krx.kst_of(b["t"]).date() < day]
    return rows[-1]["c"] if rows else None


def minutes_since(now, day, hhmm):
    try:
        t = clock(day, krx.parse_hhmm(hhmm, "신호 시각"))
    except krx.KrxError:
        return None
    return int((now - t).total_seconds() // 60)


# ---------------------------------------------------------------- 후보 하나

def plan_one(log, a, hit, now_text, now, status_doc, budget):
    """진입 확인 -> 청산 계획 -> 수량 -> 위험 점검. 후보 요약 dict. budget 이 없으면 계획까지만."""
    sym = hit["symbol"]
    d = BASE + "/" + sym
    os.makedirs(d, exist_ok=True)
    paths = {k: d + "/" + k + ".json" for k in ("entry", "plan", "orders", "risk", "status", "timemachine")}
    c = {"symbol": sym, "name": hit.get("name"), "dir": d, "verdict": "", "reasons": [], "warnings": []}
    common = ["--now", now_text, "--config", a.config]

    argv = ["check", sym, "--write", paths["entry"], "--or-minutes", str(a.or_minutes)] + common
    if a.offline:
        argv += ["--offline", a.offline]
    code, out, err = log.call("entry", argv, "%s 진입 확인" % sym)
    e = read(paths["entry"]) if code == 0 else None
    if not e or e.get("status") != "triggered":
        c.update(verdict="신호 아님", reasons=[last_line(err) or last_line(out) or "진입 신호가 확인되지 않았습니다"])
        return c
    c.update(name=e.get("name") or c["name"], signal_time=e.get("time"), signal_price=e.get("price"),
             age=minutes_since(now, now.date(), e.get("time") or ""))

    argv = ["plan", "--from", paths["entry"], "--write", paths["plan"]] + common
    if a.offline:
        argv.append("--no-network")
        pc = offline_prev_close(a.offline, sym, now.date())
        if pc:
            argv += ["--prev-close", "%g" % pc]
    code, out, err = log.call("exit", argv, "%s 청산 계획" % sym)
    plan = read(paths["plan"]) if code == 0 else None
    if not plan:
        c.update(verdict="계획 없음", reasons=[last_line(err) or last_line(out)])
        return c
    c["plan"] = {k: plan.get(k) for k in ("entry", "stop", "target", "target_r", "target_label", "stop_label",
                                          "flat_by", "reward_risk_net", "risk_per_share")}
    if not budget:
        c["verdict"] = "계획만"
        return c

    st = (status_doc or {}).get(sym) or {}
    argv = ["--plan", paths["plan"], "--write", paths["orders"], "--json"] + common
    if a.offline:
        argv.append("--no-network")
        last = (st.get("price") or {}).get("lastPrice")
        if last:
            argv += ["--price", str(last)]
        if st.get("orderbook"):
            book = d + "/book.json"
            krx.write_json(book, st["orderbook"])
            argv += ["--book", book]
    if os.path.exists(paths["orders"]):
        os.remove(paths["orders"])                 # 지난 실행의 주문 제안이 남지 않게
    code, out, err = log.call("size", argv, "%s 수량 계산" % sym)
    try:
        sized = json.loads(out)
    except ValueError:
        sized = None
    if not sized:
        c.update(verdict="주문 없음", reasons=[last_line(err) or last_line(out)])
        return c
    s = sized["sizing"]
    c["size"] = {k: s.get(k) for k in ("qty", "cost", "max_loss", "gain_at_target", "entry", "entry_from",
                                       "bound_by", "book_buy", "reward_risk_net")}
    c["notes"] = sized.get("notes") or []
    if not sized.get("orders"):
        c.update(verdict="주문 없음", reasons=[s.get("refusal") or "수량이 0입니다"])
        return c

    argv = ["--orders", paths["orders"], "--out", paths["risk"], "--status-out", paths["status"]] + common
    if a.offline:
        argv.append("--no-network")
        if a.status:
            argv += ["--status", a.status]
    code, out, err = log.call("risk", argv, "%s 위험 점검" % sym)
    r = read(paths["risk"])
    res = (r or {}).get("results") or []
    if not res:
        c.update(verdict="BLOCK", reasons=[last_line(err) or last_line(out) or "위험 점검 결과가 없습니다"])
        return c
    c.update(verdict=res[0]["verdict"], reasons=res[0].get("reasons") or [], warnings=res[0].get("warnings") or [])
    return c


def simulate(log, a, c, now_text):
    paths = c["dir"] + "/plan.json", c["dir"] + "/timemachine.json"
    argv = ["run", "--plan", paths[0], "--write", paths[1], "--now", now_text, "--replays", str(a.replays),
            "--config", a.config]
    if a.offline:
        f = os.path.join(a.offline, c["symbol"] + ".json") if os.path.isdir(a.offline) else a.offline
        argv += ["--offline", f]
    code, out, err = log.call("tm", argv, "%s 타임머신" % c["symbol"])
    t = read(paths[1]) if code == 0 else None
    if not t:
        c["sim"] = {"error": last_line(err) or last_line(out)}
        return
    m = t.get("mc") or {}
    r = float(m.get("r_at_target") or 0)
    touched = (m.get("p_target") or 0) + (m.get("p_stop") or 0)
    c["sim"] = {"replays": m.get("replays"), "p_target": m.get("p_target"), "p_stop": m.get("p_stop"),
                "p_neither": m.get("p_neither"), "avg_R_net": m.get("avg_R_net"),
                "target_share": m["p_target"] / touched if touched else None,
                "break_even": 1 / (1 + r) if r > 0 else None,
                "verdict": (t.get("verdict") or "").split(":")[0].strip()}


# ---------------------------------------------------------------- 출력

def p(x):
    return krx.price_str(x) if x is not None else "-"


def signed_won(x):
    return ("+" if x > 0 else "-" if x < 0 else "") + krx.won(abs(x))


def card(i, c):
    head = "%d. %s %s %s" % (i, krx.pad(c["verdict"], 9), c["symbol"], c.get("name") or "")
    if c.get("signal_time"):
        age = c.get("age")
        ago = "" if age is None or age < 0 else " (방금)" if age == 0 else " (%d분 전)" % age
        head += "  신호 %s%s, %s" % (c["signal_time"], ago, p(c.get("signal_price")))
    L = [head]
    pl, s = c.get("plan"), c.get("size")
    if pl:
        tr = pl.get("target_r")
        L.append("   손절 %s / 목표 %s%s / %s 까지 정리. 비용 뺀 손익비 %.2f" % (
            p(pl["stop"]), p(pl["target"]), " (%gR)" % tr if tr else "", pl.get("flat_by") or "-",
            float((s or {}).get("reward_risk_net") or pl.get("reward_risk_net") or 0)))
    if s and s.get("qty"):
        L.append("   %d주 = %s. 손절 때 %s, 목표 때 %s (비용 뺀 추정)" % (
            s["qty"], krx.won(s["cost"]), signed_won(-float(s["max_loss"])), signed_won(float(s["gain_at_target"]))))
        if pl and s.get("entry") and float(s["entry"]) > float(pl["entry"]):
            L.append("   기준가 %s: 지금 가격이 계획 진입가 %s 보다 높아 그 가격으로 위험을 다시 쟀습니다" % (
                p(s["entry"]), p(pl["entry"])))
        b = s.get("book_buy")
        if b and b.get("avg"):
            over = b.get("over_mid_per_share") or 0
            L.append("   호가: 시장가면 평균 %s%s" % (p(round(b["avg"], 1)),
                                               ", 중간가보다 주당 %.0f원 비쌈" % over if over >= 0.5 else ""))
    sim = c.get("sim")
    if sim and sim.get("verdict"):
        share = ""
        if sim.get("target_share") is not None and sim.get("break_even"):
            share = " 닿은 것 중 목표 %.0f%% (본전 %.0f%%)." % (100 * sim["target_share"], 100 * sim["break_even"])
        L.append("   재생 %s번: 목표 먼저 %.0f%%, 손절 먼저 %.0f%%.%s %s" % (
            format(sim.get("replays") or 0, ","), 100 * sim["p_target"], 100 * sim["p_stop"], share, sim["verdict"]))
    elif sim and sim.get("error"):
        L.append("   재생 못 함: %s" % sim["error"])
    for r in c.get("reasons") or []:
        L.append("   이유: %s" % r)
    for w in (c.get("warnings") or [])[:3]:
        L.append("   주의: %s" % w)
    return L


def open_trades(path=os.path.join("trader-kr", "journal.jsonl")):
    rows = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("exit_price") is None:
                    rows.append(r)
    return rows


def open_lines():
    rows = open_trades()
    if not rows:
        return []
    out = ["열린 거래 %d건 (일지):" % len(rows)]
    for r in rows:
        out.append("  %s %s주, 진입 %s (%s), 손절 %s, %s 까지 정리" % (
            r.get("symbol"), r.get("qty"), p(r.get("entry_price")), r.get("entry_time") or "-",
            p(r.get("stop")), r.get("flat_by") or "-"))
    return out


FOOT = ["주문은 내지 않았습니다. 모의투자로 넣으려면 /trader-kr-broker (후보마다 yes), 직접 샀다면 가격·시각·수량을 알려 주면 일지에 적습니다.",
        "교육과 도구일 뿐 투자 권유가 아닙니다 (not financial advice)."]


# ---------------------------------------------------------------- 실행

def run(a, show):
    cfg = krx.read_config(a.config, krx.CONFIG_DEFAULTS)
    if a.budget is not None:
        # 수량 계산과 위험 점검이 같은 예산을 보도록, 예산을 넣은 설정 파일 하나를 모든 단계에 넘긴다
        cfg["budget"] = a.budget
        a.config = BASE + "/config.json"
        krx.write_json(a.config, cfg)
    now = krx.parse_now(a.now)
    now_text = now.strftime("%Y-%m-%d %H:%M:%S")
    online = not a.offline
    session = data.session_for(now.date(), online=online)
    ph, start, end, flat = phase(now, session, cfg, a.or_minutes)
    when = "%s (%s) %s" % (now.date().isoformat(), WEEKDAYS[now.weekday()], now.strftime("%H:%M"))
    status_doc = krx.read_json(a.status, "종목 상태 파일") if a.status else None
    log = Log()
    summary = {"created_by": "trader-kr-today", "now": now_text, "phase": ph, "candidates": []}
    lines = []

    if ph == "closed":
        lines.append("오늘 장: %s, 휴장입니다. 새 진입은 없습니다." % when)
        lines += open_lines()
    elif ph == "pre":
        lines.append("오늘 장: %s, 장 시작 전입니다. 장 전 일봉 스캔을 돌립니다." % when)
        argv = ["daily", "--now", now_text, "--out", BASE + "/watchlist-daily.json", "--show", "10"]
        if a.offline:
            argv += ["--offline", a.offline]
        if a.symbols:
            argv += ["--symbols", a.symbols]
        code, out, err = log.call("scan", argv, "장 전 일봉 스캔")
        lines += [x for x in (out.rstrip() or err.rstrip()).splitlines()]
        lines.append("")
        lines.append("시가 범위 돌파는 %s 뒤에 봅니다. 그때 다시 \"오늘 장 봐 줘\" 라고 하면 됩니다." % start.strftime("%H:%M"))
    elif ph == "forming":
        lines.append("오늘 장: %s, 시가 범위 (%d분) 형성 중입니다. 새 진입은 %s 부터 봅니다." % (
            when, a.or_minutes, start.strftime("%H:%M")))
        lines.append("첫 신호는 %s 에 끝나는 5분봉에서 나올 수 있습니다." % (
            start + datetime.timedelta(minutes=5)).strftime("%H:%M"))
        lines += open_lines()
    elif ph in ("late", "after"):
        if ph == "late":
            lines.append("오늘 장: %s, 새 진입 마감(%s)이 지났습니다. 열린 거래는 %s 까지 정리합니다." % (
                when, end.strftime("%H:%M"), flat.strftime("%H:%M")))
            lines += open_lines() or ["열린 거래가 없습니다 (일지 기준)."]
        else:
            lines.append("오늘 장: %s, 장이 끝났습니다. 오늘 거래 일지입니다." % when)
            code, out, err = log.call("journal", ["--config", a.config, "report", "--since", now.date().isoformat()],
                                      "오늘 거래 일지")
            lines += (out.rstrip() or err.rstrip()).splitlines()
            lines += open_lines()
    else:
        lines += entries(log, a, cfg, now, now_text, when, end, flat, status_doc, summary, online)

    lines += [""] + FOOT
    os.makedirs(BASE, exist_ok=True)
    report = BASE + "/report.md"
    with open(report, "w", encoding="utf-8") as fh:
        fh.write(log.markdown("%s, 단계 %s" % (when, ph)) + "\n")
    summary["report"] = report
    krx.write_json(SUMMARY, summary)
    if log.steps:
        lines.insert(-2, "단계별 전체 출력: %s" % report)
    for x in lines:
        show(x)
    return 0


def entries(log, a, cfg, now, now_text, when, end, flat, status_doc, summary, online):
    L = ["오늘 장: %s, 접속매매 중. 새 진입은 %s 까지, 정리는 %s." % (when, end.strftime("%H:%M"), flat.strftime("%H:%M"))]
    L += open_lines()
    watch = BASE + "/watchlist.json"
    if os.path.exists(watch):
        os.remove(watch)
    argv = ["orb", "--now", now_text, "--out", watch, "--or-minutes", str(a.or_minutes), "--config", a.config]
    if a.offline:
        argv += ["--offline", a.offline]
    if a.symbols:
        argv += ["--symbols", a.symbols]
    code, out, err = log.call("scan", argv, "시가 범위 돌파 스캔")
    wl = read(watch)
    if not wl:
        L.append("스캔을 못 했습니다: %s" % (last_line(err) or last_line(out)))
        if online:
            L.append("토스증권 연결은 python krcommon/doctor.py 로 점검하고, 키 없이 흐름만 보려면 python krcommon/demo.py.")
        return L
    hits = sorted(wl.get("hits") or [], key=lambda h: h.get("at") or "", reverse=True)
    L.append("스캔: %s. 시가 범위 돌파 %d종목." % (wl.get("candidates") or "후보", len(hits)))
    if not hits:
        L.append("지금까지 시가 범위 고가 위에서 끝난 5분봉이 없습니다. 몇 분 뒤 다시 보면 됩니다.")
        return L
    pick = hits[:a.max]
    rest = hits[a.max:]
    L.append("신호가 가까운 %d종목을 계획했습니다%s." % (
        len(pick), "" if not rest else " (나머지 %d종목: %s%s)" % (
            len(rest), ", ".join("%s %s" % (h["symbol"], h.get("name") or "") for h in rest[:6]),
            " ..." if len(rest) > 6 else "")))
    budget = cfg.get("budget")
    if not budget:
        L.append("예산이 없어 손절·목표까지만 정했습니다. 수량과 위험 점검은 예산이 있어야 합니다: --budget 5000000 처럼 주거나, "
                 "trader-kr-budget/config.example.json 을 %s 로 복사해 budget 을 고치세요." % a.config)
    cands = [plan_one(log, a, h, now_text, now, status_doc, budget) for h in pick]
    order = {"PASS": 0, "BLOCK": 1, "주문 없음": 2, "계획만": 3, "계획 없음": 4, "신호 아님": 5}
    cands.sort(key=lambda c: (order.get(c["verdict"], 5), c.get("age") if c.get("age") is not None else 999))
    for c in [x for x in cands if x["verdict"] == "PASS"][:a.sim]:
        simulate(log, a, c, now_text)
    L.append("")
    for i, c in enumerate(cands, 1):
        L += card(i, c)
    n_pass = sum(1 for c in cands if c["verdict"] == "PASS")
    L.append("")
    if budget:
        L.append("통과 %d, 막힘·주문 없음 %d. 후보마다 따로 점검했습니다. 여러 개를 사면 하루 한도 (거래 %s번, 하루 손실 %s) 를 "
                 "함께 봐야 합니다." % (n_pass, len(cands) - n_pass, cfg.get("max_trades_per_day"),
                                   krx.won(cfg.get("max_daily_loss_krw") or 0)))
    stale = [c["symbol"] for c in cands if c.get("age") is not None and c["age"] > STALE_MINUTES]
    if stale:
        L.append("신호 뒤 %d분이 지난 후보 (%s) 는 늦은 진입입니다. 일지로 재 보면 늦은 진입의 성적이 보통 더 나쁩니다."
                 % (STALE_MINUTES, ", ".join(stale)))
    summary["candidates"] = cands
    summary["scan"] = {"candidates": wl.get("candidates"), "scanned": wl.get("scanned"), "hits": len(hits)}
    return L


def main(argv=None):
    krx.utf8_stdio()
    ap = argparse.ArgumentParser(description="오늘 장 한 번에 (trader-kr-today): 스캔부터 위험 점검까지. 주문은 내지 않는다.")
    ap.add_argument("--max", type=int, default=3, help="계획할 후보 수 (기본 3, 신호가 최근인 것부터)")
    ap.add_argument("--sim", type=int, default=2, help="타임머신으로 재생할 통과 후보 수 (기본 2, 0 이면 안 함)")
    ap.add_argument("--replays", type=int, default=10000)
    ap.add_argument("--symbols", help="거래대금 상위 대신 이 종목만, 005930,000660 처럼")
    ap.add_argument("--budget", type=float, help="설정의 예산 대신 (원)")
    ap.add_argument("--or-minutes", type=int, default=15)
    ap.add_argument("--config", default="trader-kr/config.json")
    ap.add_argument("--offline", metavar="DIR", help="저장된 1분봉 폴더 (네트워크 없이)")
    ap.add_argument("--status", metavar="FILE", help="오프라인 종목 상태 ({종목: {stock, warnings, price_limits, "
                                                     "orderbook, price}})")
    ap.add_argument("--now", help='이 한국 시간인 것처럼, 예: "2026-10-28 10:15"')
    a = ap.parse_args(argv)
    if a.max < 1 or a.sim < 0 or a.replays < 100:
        print("today.py: --max 는 1 이상, --sim 은 0 이상, --replays 는 100 이상이어야 합니다", file=sys.stderr)
        return 2
    try:
        return run(a, print)
    except (TodayError, krx.KrxError, data.DataError) as e:
        print("today.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
