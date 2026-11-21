"""관심 종목: 다른 스킬이 trader-kr/ 에 남긴 파일에서 종목을 모은다. 표준 라이브러리만 쓴다.

공시 알림과 임원·주요주주 보고가 같이 쓴다. 스캐너 결과 (watchlist.json), 청산 계획 (plan.json),
오늘 계획들 (today/<종목>/plan.json), 일지에서 아직 청산하지 않은 거래 (journal.jsonl).
"""

import datetime
import glob
import json
import os

from . import krx


def window(now, days=None, since=None):
    """[시작일, ..., 오늘]. 기본은 바로 앞 평일부터 (장 마감 뒤 공시가 다음 날 시가를 흔들 수 있어서)."""
    today = now.date()
    if since:
        start = krx.parse_day(since)
        if start is None:
            raise krx.KrxError("--since 는 2026-11-20 같은 날짜입니다 (받은 값 %r)" % since)
    elif days is not None:
        if days < 1 or days > 366:
            raise krx.KrxError("--days 는 1 ~ 366 입니다")
        start = today - datetime.timedelta(days=days - 1)
    else:
        start = today - datetime.timedelta(days=1)
        while start.weekday() >= 5:
            start -= datetime.timedelta(days=1)
    if start > today:
        raise krx.KrxError("시작일이 오늘보다 늦습니다")
    return [start + datetime.timedelta(days=i) for i in range((today - start).days + 1)]


def _sym(x):
    try:
        return krx.norm_symbol(x)
    except krx.KrxError:
        return None


def held(root="trader-kr"):
    """일지에서 아직 청산하지 않은 거래의 종목 [(종목, 이름)]."""
    out = []
    path = os.path.join(root, "journal.jsonl")
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("symbol") and r.get("exit_price") is None:
                s = _sym(r.get("symbol"))
                if s:
                    out.append((s, r.get("name")))
    return out


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
    for s, name in held(root):
        add(s, name, "일지 (열린 거래)")
    return out
