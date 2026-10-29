#!/usr/bin/env python3
"""토스증권 Open API 연결 점검. 키를 발급받고 허용 IP 를 등록한 뒤 한 번 돌린다.

    python krcommon/doctor.py              # 인증 정보, 토큰, 장 운영 정보, 1분봉 하나
    python krcommon/doctor.py --symbol 000660

키 값은 화면에 찍지 않는다. 어디서 찾았는지만 말한다. 시세 API 만 부른다.
종료 코드 0 = 모두 통과, 1 = 하나라도 실패.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
from krcommon import krx, toss  # noqa: E402


def where_credentials(env=None):
    env = os.environ if env is None else env
    if env.get("TOSS_CLIENT_ID") and env.get("TOSS_CLIENT_SECRET"):
        return "환경변수 TOSS_CLIENT_ID, TOSS_CLIENT_SECRET"
    if env.get("TOSS_CREDENTIALS") and os.path.exists(env["TOSS_CREDENTIALS"]):
        return "TOSS_CREDENTIALS 가 가리키는 파일 %s" % env["TOSS_CREDENTIALS"]
    return "파일 %s" % os.path.join(toss.home_dir(), "credentials.json")


def tick_check(orderbook, candles=()):
    """호가창 가격 칸 간격을 krx 호가 단위표와 비교한다. 체결가는 중간가 체결 때문에 호가
    사이에 찍힐 수 있어서, 주문 단위는 호가창으로 본다. (맞음 여부, 설명) 또는 자료가 적으면 None."""
    import math
    from functools import reduce
    levels = sorted({float(x["price"]) for side in ("asks", "bids") for x in (orderbook or {}).get(side) or []
                     if x.get("price")})
    if len(levels) < 4:
        return None
    mid = levels[len(levels) // 2]
    table = krx.tick_size(mid)
    band = [int(v) for v in levels if krx.tick_size(v) == table and v == int(v)]
    diffs = [b - a for a, b in zip(band, band[1:]) if b > a]
    if not diffs:
        return None
    g = reduce(math.gcd, diffs)
    between = 0
    total = 0
    for c in candles:
        if float(c.get("volume") or 0) > 0:
            for k in ("openPrice", "highPrice", "lowPrice", "closePrice"):
                total += 1
                between += 0 if krx.on_tick(c[k]) else 1
    tail = (" 최근 1분봉 체결가 중 %d%% 는 호가 사이 가격(중간가 체결)이었습니다." % round(100.0 * between / total)
            if total else "")
    if g == table:
        return True, "호가창 간격 %s원, 표 %s원. 맞습니다.%s" % (krx.price_str(g), krx.price_str(table), tail)
    return False, ("호가창 간격 %s원이 표 %s원과 다릅니다. krcommon/krx.py 의 TICK_TABLE 을 확인하세요.%s"
                   % (krx.price_str(g), krx.price_str(table), tail))


def run(client, symbol, out, env=None, today=None):
    ok = True
    today = today or krx.kst_now().date()
    try:
        cid, _ = toss.find_credentials(env)
        out("1. 인증 정보: 찾음 (%s)." % where_credentials(env))
        if not cid.startswith("tsck_"):
            out("   참고: client_id 가 tsck_ 로 시작하지 않습니다. 토스증권에서 받은 값인지 확인하세요.")
    except toss.TossError as e:
        out("1. 인증 정보: 없음. %s" % e)
        return False
    try:
        client.token()
        out("2. 토큰: 받음 (%s 에 만료 시각과 함께 저장)." % client.token_path)
    except toss.TossError as e:
        out("2. 토큰: 실패. %s" % e)
        return False
    try:
        cal = krx.Calendar()
        cal.add_response(client.calendar(today.isoformat()))
        s = cal.session(today)
        if s is None:
            out("3. 장 운영 정보: 오늘(%s)은 휴장입니다." % today)
        else:
            out("3. 장 운영 정보: 오늘(%s) %s 개장, 접속매매 %s 까지, %s 마감 (%s)." % (
                today, krx.hm(s.open_t), krx.hm(s.auction_t), krx.hm(s.close_t), s.source))
    except (toss.TossError, krx.KrxError) as e:
        out("3. 장 운영 정보: 실패. %s" % e)
        ok = False
    try:
        candles, _ = client.candles(symbol, "1m", 200)
        if candles:
            last = max(candles, key=lambda c: c["timestamp"])
            out("4. 1분봉: %s 마지막 봉 %s 종료, 종가 %s." % (symbol, krx.parse_iso(last["timestamp"]).strftime(
                "%Y-%m-%d %H:%M"), krx.price_str(last["closePrice"])))
            try:
                note = tick_check(client.orderbook(symbol), candles)
            except toss.TossError as e:
                note = (False, "호가창을 받지 못했습니다. %s" % e)
            if note:
                out("5. 호가 단위: " + note[1])
                ok = ok and note[0]
        else:
            out("4. 1분봉: %s 봉이 비어 있습니다." % symbol)
            ok = False
    except (toss.TossError, krx.KrxError) as e:
        out("4. 1분봉: 실패. %s" % e)
        ok = False
    try:
        from krcommon import keys
        extra = []
        for kind, label in (("krx", "KRX Open API (장 전 스캔)"), ("dart", "OpenDART (공시 경고)")):
            desc, found = keys.where(kind, env)
            extra.append("%s %s" % (label, "있음" if found else "없음"))
        out("6. 선택 키: %s. KRX 승인 확인은 python krcommon/keys.py krx --check" % ", ".join(extra))
    except Exception:
        pass
    out("호출 %d번. %s" % (client.calls, "모두 통과했습니다." if ok else "실패한 항목을 고친 뒤 다시 돌리세요."))
    return ok


def main(argv=None):
    krx.utf8_stdio()
    ap = argparse.ArgumentParser(description="토스증권 Open API 연결 점검 (키 값은 찍지 않음).")
    ap.add_argument("--symbol", default="005930", help="1분봉을 받아 볼 종목 (기본 005930)")
    a = ap.parse_args(argv)
    try:
        sym = krx.norm_symbol(a.symbol)
    except krx.KrxError as e:
        print("doctor.py: %s" % e, file=sys.stderr)
        return 1
    return 0 if run(toss.Client(), sym, print) else 1


if __name__ == "__main__":
    sys.exit(main())
