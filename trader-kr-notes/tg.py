#!/usr/bin/env python3
"""텔레그램으로 나에게 알림 보내기. 봇 토큰과 채팅 번호는 ~/.telegram/bot.json 에 둔다 (저장소 밖).
토큰 값은 화면에 찍지 않는다. 채팅에 붙여 넣지 않는다.

    python trader-kr-notes/notes.py tg setup                 # 본인 터미널에서만: 봇 토큰 숨김 입력 -> 봇에게 말 걸면 채팅 번호를 찾아 저장 -> 시험 메시지 1통
    python trader-kr-notes/notes.py tg status                # 설정 여부만 (토큰 값은 찍지 않음)
    python trader-kr-notes/notes.py tg send < msg.txt        # 표준입력의 글을 보낸다 (길면 나눠 보냄)
    python trader-kr-notes/notes.py tg send --dry-run < msg.txt   # 보내지 않고 나눌 조각만 보여 줌

보내는 곳은 setup 때 봇에게 직접 말을 건 1:1 채팅 하나뿐이다 (단체방은 받지 않는다).
글은 꾸밈 없는 문자로 보낸다 (Markdown 해석 안 함).
종료 코드: 0 보냄(또는 dry-run), 1 오류, 3 설정 없음(보내지 않음).
"""

import argparse
import getpass
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

LIMIT = 3800  # 텔레그램 한 통 한도는 4096자. 여유를 둔다.


class TgError(Exception):
    pass


def conf_path():
    return os.path.join(os.path.expanduser("~"), ".telegram", "bot.json")


def load_conf():
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if tok and chat:
        return {"token": tok, "chat_id": chat, "where": "환경변수 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID"}
    p = conf_path()
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            c = json.load(f)
        if c.get("token") and c.get("chat_id"):
            c["where"] = "파일 %s" % p
            return c
    return None


def write_private(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def api(token, method, params=None, timeout=40):
    url = "https://api.telegram.org/bot%s/%s" % (token, method)
    data = urllib.parse.urlencode(params or {}).encode("utf-8")
    try:
        raw = urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=timeout).read()
        res = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            res = json.loads(e.read().decode("utf-8"))
        except Exception:
            raise TgError("텔레그램 응답 오류 HTTP %s" % e.code)
    except Exception as e:  # 네트워크 오류 메시지에 토큰이 섞이지 않게 지운다
        raise TgError("텔레그램 연결 실패 (%s)" % str(e).replace(token, "<토큰>"))
    if not res.get("ok"):
        raise TgError("텔레그램이 거절함: %s" % str(res.get("description", "")).replace(token, "<토큰>"))
    return res["result"]


def split(text, limit=LIMIT):
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # 한 줄이 너무 길면 자른다
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = line
        else:
            cur = line if not cur else cur + "\n" + line
    if cur.strip():
        parts.append(cur)
    return parts


def read_stdin():
    data = sys.stdin.buffer.read()
    return data.decode("utf-8-sig").strip()


def cmd_status(a):
    c = load_conf()
    if not c:
        print("텔레그램 설정 없음. 본인 터미널에서: python trader-kr-notes/notes.py tg setup")
        return 3
    print("텔레그램 설정 있음 (%s). 토큰 값은 찍지 않았습니다." % c["where"])
    return 0


def cmd_send(a):
    text = read_stdin()
    if not text:
        print("보낼 글이 없습니다 (표준입력이 비었음).", file=sys.stderr)
        return 1
    parts = split(text)
    if a.dry_run:
        print("dry-run: 보내지 않음. %d자, %d통으로 나눔." % (len(text), len(parts)))
        for i, p in enumerate(parts, 1):
            print("----- %d/%d (%d자) -----" % (i, len(parts), len(p)))
            print(p)
        return 0
    c = load_conf()
    if not c:
        print("텔레그램 설정이 없어 보내지 않았습니다. 본인 터미널에서: python trader-kr-notes/notes.py tg setup", file=sys.stderr)
        return 3
    try:
        for i, p in enumerate(parts):
            if i:
                time.sleep(1.1)
            api(c["token"], "sendMessage", {"chat_id": c["chat_id"], "text": p, "disable_web_page_preview": "true"})
    except TgError as e:
        print("보내기 실패 (%d/%d통에서): %s" % (i + 1, len(parts), e), file=sys.stderr)
        return 1
    print("보냄: %d자, %d통." % (len(text), len(parts)))
    return 0


def cmd_setup(a):
    print("BotFather 에서 받은 봇 토큰을 붙여 넣으세요 (화면에 보이지 않습니다).")
    if sys.stdin.isatty():
        tok = getpass.getpass("봇 토큰: ").strip()
    else:
        tok = (sys.stdin.readline() or "").strip()
    if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{30,}", tok):
        print("토큰 형식이 이상합니다 (숫자:영문 형태가 아님). 저장하지 않았습니다.")
        return 1
    try:
        me = api(tok, "getMe")
        print("봇 확인: @%s" % me.get("username"))
        print("이제 텔레그램에서 @%s 를 열고 /start 를 누르거나 아무 말이나 보내세요. 90초 기다립니다." % me.get("username"))
        chat, offset, until = None, None, time.time() + 90
        while time.time() < until and chat is None:
            ups = api(tok, "getUpdates", {"timeout": 25, **({"offset": offset} if offset else {})}, timeout=40)
            for u in ups:
                offset = u["update_id"] + 1
                m = u.get("message") or {}
                ch = m.get("chat") or {}
                if ch.get("type") == "private":
                    chat = ch
        if chat is None:
            print("90초 안에 1:1 메시지를 받지 못했습니다. 저장하지 않았습니다. 다시 돌려 주세요.")
            return 1
        write_private(conf_path(), json.dumps({"token": tok, "chat_id": str(chat["id"]),
                                               "chat_name": chat.get("first_name", "")}, ensure_ascii=False) + "\n")
        print("저장했습니다: %s (채팅 상대: %s)" % (conf_path(), chat.get("first_name", "")))
        api(tok, "sendMessage", {"chat_id": chat["id"], "text": "연결됐습니다. 앞으로 이 채팅으로 주간 점검 결과를 보냅니다."})
        print("시험 메시지를 보냈습니다. 텔레그램에서 확인하세요.")
        return 0
    except TgError as e:
        print("설정 실패: %s" % e)
        return 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="텔레그램 알림 (토큰 값은 찍지 않음)")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("setup", help="봇 토큰 등록과 채팅 번호 찾기 (본인 터미널에서)")
    sub.add_parser("status", help="설정 여부")
    s = sub.add_parser("send", help="표준입력의 글을 보낸다")
    s.add_argument("--dry-run", action="store_true", help="보내지 않고 조각만 보여 줌")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"setup": cmd_setup, "status": cmd_status, "send": cmd_send}[a.cmd](a)
    except (EOFError, KeyboardInterrupt):
        print("입력이 없어 멈췄습니다.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
