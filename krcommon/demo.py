#!/usr/bin/env python3
"""키 없이 합성 자료로 8단계를 한 번에 돌려 본다.

    python krcommon/demo.py                        # trader-kr-demo/ 에 파일을 만들고 8단계를 돌린다
    python krcommon/demo.py --replays 2000         # 타임머신을 짧게
    python krcommon/demo.py --dir 다른폴더

합성 종목 3개(SYN001~SYN003)의 1분봉과 합성 호가창·종목 상태를 만들고, 각 스킬의 스크립트를
오프라인 옵션으로 차례로 부른다. Claude 가 스킬을 쓸 때 부르는 것과 같은 스크립트다. 실제 시세가
아니고, 네트워크를 쓰지 않고, 실제 키도 읽지 않는다: 스크립트의 홈 폴더를 데모 폴더로 바꾸고 키
환경변수를 지운다. 거래일은 2026-10-28 (수), 신호는 10:15 에 나온다.
종료 코드 0 = 8단계가 모두 예상대로 끝남.
"""

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from krcommon import krx, liquidity, synth  # noqa: E402

MARK = ".trader-kr-demo"
DAY = synth.PLAN_DAY.isoformat()
SIGNAL = DAY + " 10:15:05"
NOW = DAY + " 10:15:30"          # 신호 25초 뒤에 계획하고 사는 것으로 본다
BUY = "10:15"
KEY_ENV = ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TOSS_CREDENTIALS", "KRX_API_KEY", "DART_API_KEY")

# 합성 호가창: 매수 1호가 30,450, 매도 1호가 30,500 (1칸 벌어짐)
BOOK = {"asks": [{"price": "30500", "quantity": "300"}, {"price": "30550", "quantity": "600"},
                 {"price": "30600", "quantity": "900"}],
        "bids": [{"price": "30450", "quantity": "300"}, {"price": "30400", "quantity": "600"},
                 {"price": "30350", "quantity": "900"}]}


class DemoError(Exception):
    pass


def charts():
    """SYN001 = 마지막 날 10:15 에 범위를 넘는 62 거래일, SYN002 = 마지막 날까지 무작위 걸음인
    62 거래일, SYN003 = 범위를 한 번도 넘지 않는 하루."""
    a = synth.make_history()
    b = synth.make_history(seed=23)
    before = [x for x in b["bars"] if krx.kst_of(x["t"]).date() < synth.PLAN_DAY]
    last, _ = synth.random_day(synth.PLAN_DAY, before[-1]["c"], random.Random(29))
    b = dict(b, symbol="SYN002", name="합성종목2", bars=before + last)
    c = dict(synth.flat_day(), symbol="SYN003", name="합성종목3")
    return [a, b, c]


def prev_close(chart):
    last = [x for x in chart["bars"] if krx.kst_of(x["t"]).date() < synth.PLAN_DAY]
    return int(last[-1]["c"])


def price_at(chart, hhmm):
    """그 시각에 끝난 1분봉의 종가."""
    end = krx.ts_of(krx.parse_now(DAY + " " + hhmm))
    rows = [x for x in chart["bars"] if x["t"] + 60 <= end]
    return int(rows[-1]["c"])


def what_happened(chart, plan, entry_hhmm):
    """합성 자료의 그날 나머지 1분봉으로 손절, 목표, 시간 청산 중 무엇이 먼저였는지 본다.
    한 봉 안에서는 손절을 먼저 본다. (가격, 시각 HH:MM, 이유)."""
    start = krx.ts_of(krx.parse_now(DAY + " " + entry_hhmm))
    flat = krx.ts_of(krx.parse_now(DAY + " " + plan["flat_by"]))
    stop, target = float(plan["stop"]), float(plan["target"])
    last = None
    for x in chart["bars"]:
        if x["t"] < start or x["t"] + 60 > flat:
            continue
        end = krx.kst_of(x["t"] + 60).strftime("%H:%M")
        if x["l"] <= stop:
            return min(x["o"], stop), end, "stop"
        if x["h"] >= target:
            return max(x["o"], target), end, "target"
        last = (x["c"], end, "flat")
    if last is None:
        raise DemoError("진입 뒤 봉이 없습니다")
    return last


def status_doc(chart, last):
    lo, up = krx.price_limits(prev_close(chart))
    stock = {"symbol": "SYN001", "name": chart["name"], "market": "KOSPI", "securityType": "STOCK",
             "isCommonShare": True, "status": "ACTIVE", "currency": "KRW",
             "koreanMarketDetail": {"liquidationTrading": False, "nxtSupported": True,
                                    "krxTradingSuspended": False, "nxtTradingSuspended": False}}
    return {"SYN001": {"stock": stock, "warnings": [], "fetched_at": NOW + ":00",
                       "price_limits": {"timestamp": DAY + "T09:00:00+09:00", "upperLimitPrice": str(up),
                                        "lowerLimitPrice": str(lo), "currency": "KRW"},
                       "orderbook": BOOK, "disclosures": [],
                       "price": {"lastPrice": str(last), "timestamp": DAY + "T%s:00+09:00" % BUY}}}


def prepare(folder):
    """데모 폴더를 만든다. 이미 있는 폴더는 데모가 만든 폴더일 때만 비우고 다시 쓴다."""
    if os.path.isdir(folder) and os.listdir(folder) and not os.path.exists(os.path.join(folder, MARK)):
        raise DemoError("%s 는 데모가 만든 폴더가 아닙니다. 비어 있는 폴더나 새 이름을 --dir 로 주세요" % folder)
    for sub in ("trader-kr", "bars"):
        shutil.rmtree(os.path.join(folder, sub), ignore_errors=True)
    os.makedirs(os.path.join(folder, "bars"), exist_ok=True)
    with open(os.path.join(folder, MARK), "w", encoding="utf-8") as fh:
        fh.write("python krcommon/demo.py 가 만든 폴더입니다. 다시 돌리면 trader-kr/ 와 bars/ 를 지우고 새로 씁니다.\n")
    out = {}
    for ch in charts():
        krx.write_json(os.path.join(folder, "bars", ch["symbol"] + ".json"), ch)
        out[ch["symbol"]] = ch
    with open(os.path.join(ROOT, "trader-kr-budget", "config.example.json"), encoding="utf-8") as fh:
        krx.write_json(os.path.join(folder, "trader-kr", "config.json"), json.load(fh))
    # 장 운영 정보: 2026-10-28 은 평일 기본 장 (데모 값)
    cal = krx.Calendar({DAY: krx.Session(synth.PLAN_DAY, source="demo")})
    krx.save_calendar(os.path.join(folder, "trader-kr", "cache", "calendar.json"), cal)
    krx.write_json(os.path.join(folder, "book.json"), BOOK)
    return out


def child_env(folder):
    """스크립트가 실제 키를 찾지 못하게: 키 환경변수를 지우고 홈 폴더를 데모 폴더로 바꾼다."""
    env = dict(os.environ)
    for k in KEY_ENV:
        env.pop(k, None)
    env["HOME"] = env["USERPROFILE"] = os.path.abspath(folder)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run(folder, script, args, show, expect=0):
    path = os.path.join(ROOT, script)
    t0 = time.time()
    p = subprocess.run([sys.executable, "-X", "utf8", path] + args, cwd=folder, env=child_env(folder),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    text = p.stdout.decode("utf-8", "replace").replace("\r\n", "\n").rstrip()
    shown = " ".join('"%s"' % x if " " in x else x for x in args)
    show("$ python %s %s" % (script.replace(os.sep, "/"), shown))
    if text:
        show(text)
    show("(%.1f초, 종료 코드 %d)" % (time.time() - t0, p.returncode))
    if p.returncode != expect:
        raise DemoError("%s 가 종료 코드 %d 로 끝났습니다 (예상 %d)" % (script, p.returncode, expect))
    return text


def read(folder, name):
    with open(os.path.join(folder, "trader-kr", name), encoding="utf-8") as fh:
        return json.load(fh)


def demo(folder, replays, show):
    show("합성 자료로 8단계를 돌립니다. 실제 시세가 아니고, 네트워크와 실제 키를 쓰지 않습니다.")
    show("폴더: %s" % os.path.abspath(folder))
    ch = prepare(folder)
    syn = ch["SYN001"]
    steps = []

    def step(n, title, skill):
        show("")
        show("== %d/8 %s (/%s) ==" % (n, title, skill))
        steps.append((n, title))

    step(1, "스캐너: 10:15 에 시가 범위를 넘은 종목", "trader-kr-scanner")
    run(folder, "trader-kr-scanner/scan.py", ["orb", "--offline", "bars", "--now", SIGNAL], show)
    hits = [h["symbol"] for h in read(folder, "watchlist.json")["hits"]]
    if "SYN001" not in hits:
        raise DemoError("스캐너가 SYN001 을 찾지 못했습니다 (%s)" % hits)

    step(2, "진입 타이머: 신호 확인", "trader-kr-entry")
    run(folder, "trader-kr-entry/entry.py", ["check", "SYN001", "--offline", "bars", "--now", SIGNAL, "--write"], show)

    step(3, "청산 계획: 손절가, 목표가, 청산 시각", "trader-kr-exit")
    run(folder, "trader-kr-exit/exit.py", ["plan", "--no-network", "--now", NOW,
                                           "--prev-close", str(prev_close(syn)), "--write"], show)
    plan = read(folder, "plan.json")

    step(4, "타임머신: 남은 장을 %s번 다시 돌리기" % format(replays, ","), "trader-kr-timemachine")
    run(folder, "trader-kr-timemachine/timemachine.py", ["run", "--offline", "bars/SYN001.json", "--now", NOW,
                                                         "--replays", str(replays), "--write"], show)

    last = price_at(syn, BUY)
    krx.write_json(os.path.join(folder, "status.json"), status_doc(syn, last))
    step(5, "수량 계산: 최대 손실과 호가창 비용", "trader-kr-budget")
    run(folder, "trader-kr-budget/size.py", ["--no-network", "--price", str(last), "--book", "book.json",
                                             "--now", NOW, "--write"], show)
    order = read(folder, "orders.json")["orders"][0]

    step(6, "위험 점검: 종목 상태, 시간, 한도", "trader-kr-risk")
    run(folder, "trader-kr-risk/check.py", ["--no-network", "--status", "status.json", "--now", NOW], show)

    step(7, "거래 일지: 산 것과 판 것을 계획과 비교", "trader-kr-journal")
    fill = liquidity.buy_cost(BOOK, int(order["qty"]))["avg"]
    run(folder, "trader-kr-journal/journal.py", ["open", "--entry-price", "%g" % fill, "--entry-time", BUY,
                                                 "--now", NOW], show)
    price, at, reason = what_happened(syn, plan, BUY)
    show("(합성 자료의 그날 나머지 봉에서 %s 에 %s: %s원)" % (
        at, {"stop": "손절가에 닿음", "target": "목표가에 닿음", "flat": "청산 시각"}[reason], format(int(price), ",")))
    run(folder, "trader-kr-journal/journal.py", ["close", "last", "--exit-price", "%g" % price, "--exit-time", at,
                                                 "--reason", reason, "--now", DAY + " " + at], show)
    run(folder, "trader-kr-journal/journal.py", ["report"], show)
    show("(한 건이 %s 것은 규칙이 좋은지 나쁜지 말해 주지 않습니다. 4단계 타임머신과 8단계 실험실이 여러 거래일로 따집니다.)"
         % ("이긴" if price > fill else "진"))

    step(8, "전략 검증 실험실: 48개 규칙을 앞에서 고르고 뒤에서 확인", "trader-kr-lab")
    run(folder, "trader-kr-lab/lab.py", ["run", "--symbols", "SYN001,SYN002", "--offline", "bars", "--sessions", "62",
                                         "--train", "30", "--test", "10", "--min-trades", "10",
                                         "--now", "2026-10-29 08:00", "--write"], show)

    show("")
    show("8단계를 모두 돌렸습니다. 만든 파일: %s" % os.path.join(os.path.abspath(folder), "trader-kr"))
    show("주문은 보내지 않았습니다. 이 팩에는 주문 도구가 없습니다.")
    show("실제 시세로 돌리려면: python krcommon/keys.py toss, python krcommon/doctor.py 뒤에 Claude 에게 스킬을 부탁합니다.")
    return steps


def main(argv=None):
    krx.utf8_stdio()
    ap = argparse.ArgumentParser(description="키 없이 합성 자료로 8단계를 돌려 본다 (네트워크 안 씀).")
    ap.add_argument("--dir", default="trader-kr-demo", help="데모 파일을 만들 폴더 (기본 trader-kr-demo)")
    ap.add_argument("--replays", type=int, default=10000, help="타임머신 재생 횟수 (기본 10,000)")
    a = ap.parse_args(argv)

    def show(text):
        print(text, flush=True)

    try:
        demo(a.dir, a.replays, show)
    except DemoError as e:
        print("demo.py: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
