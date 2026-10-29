#!/usr/bin/env python3
"""API 키 등록. 키는 화면에 보이지 않게 입력받아 홈 폴더의 파일에 저장한다. 키 값은 찍지 않는다.

본인 터미널(PowerShell, 터미널 앱)에서 돌린다. 키를 채팅에 붙여 넣지 않는다.

    python krcommon/keys.py status            # 어떤 키가 어디 있는지 (값은 찍지 않음)
    python krcommon/keys.py krx               # KRX Open API 키 입력 -> ~/.krx/api_key, 서비스 승인 확인
    python krcommon/keys.py krx --check       # 저장하지 않고 지금 키로 승인만 확인
    python krcommon/keys.py krx --from-env    # 지금 KRX_API_KEY 환경변수 값을 파일로 옮겨 저장
    python krcommon/keys.py dart              # OpenDART 키 -> ~/.dart/api_key
    python krcommon/keys.py toss              # 토스증권 client_id, client_secret -> ~/.tossinvest/credentials.json

입력은 숨김 입력(getpass)으로 받는다. 붙여 넣어도 화면에 나오지 않는다. 표준입력으로 넘겨도 된다
(예: Get-Clipboard | python krcommon/keys.py krx). 같은 이름의 환경변수가 있으면 그 값이 파일보다 먼저
쓰이니, 그렇다고 알려 준다.

KRX 는 저장 뒤에 유가증권·코스닥 일별매매정보를 하나씩 불러 이용신청이 승인됐는지 확인한다.
종료 코드 0 = 저장(또는 확인) 성공, 1 = 실패.
"""

import argparse
import getpass
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
from krcommon import dart, krx, krxdata, toss  # noqa: E402


def home():
    return os.path.expanduser("~")


def paths(h=None):
    h = h or home()
    return {"krx": os.path.join(h, ".krx", "api_key"), "dart": os.path.join(h, ".dart", "api_key"),
            "toss": os.path.join(h, ".tossinvest", "credentials.json"),
            "toss_token": os.path.join(h, ".tossinvest", "token.json")}


ENV = {"krx": ["KRX_API_KEY"], "dart": ["DART_API_KEY"], "toss": ["TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET"]}
NAMES = {"krx": "KRX Open API", "dart": "OpenDART", "toss": "토스증권 Open API"}


def write_private(path, text):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def read_secret(prompt, stdin=None, getpass_fn=getpass.getpass):
    """숨김 입력. 표준입력이 터미널이 아니면 한 줄을 읽는다."""
    stdin = stdin or sys.stdin
    if stdin is not None and hasattr(stdin, "isatty") and not stdin.isatty():
        return (stdin.readline() or "").strip()
    return (getpass_fn(prompt) or "").strip()


def where(kind, env=None, h=None):
    """(어디서 찾았는지 설명, 찾았는지)."""
    env = os.environ if env is None else env
    p = paths(h)
    names = ENV[kind]
    if all((env.get(n) or "").strip() for n in names):
        return "환경변수 %s" % ", ".join(names), True
    if kind == "toss" and (env.get("TOSS_CREDENTIALS") or "").strip() and os.path.exists(env["TOSS_CREDENTIALS"]):
        return "TOSS_CREDENTIALS 가 가리키는 파일 %s" % env["TOSS_CREDENTIALS"], True
    if os.path.exists(p[kind]):
        return "파일 %s" % p[kind], True
    return "없음", False


def cmd_status(a, out=print, env=None, h=None):
    for kind in ("toss", "krx", "dart"):
        desc, found = where(kind, env, h)
        need = {"toss": "필수 (시세)", "krx": "선택 (장 전 스캔의 KRX 공식 종가)", "dart": "선택 (그날 공시 경고)"}[kind]
        out("%-18s %s  %s" % (NAMES[kind], "있음" if found else "없음", "(%s)" % desc if found else "- " + need))
    out("키 값은 찍지 않았습니다. 등록: python krcommon/keys.py krx | dart | toss")
    return 0


def report_krx(key, out, transport=None, now_kst=None):
    ok = True
    for r in krxdata.check_key(key, now_kst, transport):
        out("  %s %s" % ("통과" if r["ok"] else "실패", r["message"]))
        ok = ok and r["ok"]
    if not ok:
        out("  openapi.krx.co.kr 의 서비스 이용 > 주식 에서 '유가증권 일별매매정보', '코스닥 일별매매정보' 를 각각 "
            "이용신청하고 승인을 기다리세요.")
    return ok


def cmd_krx(a, out=print, env=None, h=None, stdin=None, getpass_fn=getpass.getpass, transport=None, now_kst=None):
    env = os.environ if env is None else env
    p = paths(h)
    if a.check:
        key = krxdata.api_key(env, h)
        if not key:
            out("KRX 키가 없습니다. python krcommon/keys.py krx 로 등록하세요.")
            return 1
        out("지금 KRX 키(%s)로 서비스 승인을 확인합니다." % where("krx", env, h)[0])
        return 0 if report_krx(key, out, transport, now_kst) else 1
    if a.from_env:
        key = (env.get("KRX_API_KEY") or "").strip()
        if not key:
            out("KRX_API_KEY 환경변수가 없습니다.")
            return 1
    else:
        out("openapi.krx.co.kr 마이페이지의 API 인증키를 붙여 넣으세요 (화면에 보이지 않습니다).")
        key = read_secret("KRX API 인증키: ", stdin, getpass_fn)
    if not key or len(key) < 8 or any(ch.isspace() for ch in key):
        out("키 형식이 이상합니다 (비었거나 너무 짧거나 공백이 있음). 저장하지 않았습니다.")
        return 1
    write_private(p["krx"], key + "\n")
    out("저장했습니다: %s" % p["krx"])
    if (env.get("KRX_API_KEY") or "").strip() and not a.from_env and env.get("KRX_API_KEY").strip() != key:
        out("참고: KRX_API_KEY 환경변수가 따로 있어서 그 값이 이 파일보다 먼저 쓰입니다.")
    if a.no_verify:
        return 0
    out("서비스 승인을 확인합니다 (시장마다 한 번 호출, 몇 초 걸림).")
    return 0 if report_krx(key, out, transport, now_kst) else 1


def cmd_dart(a, out=print, env=None, h=None, stdin=None, getpass_fn=getpass.getpass):
    env = os.environ if env is None else env
    out("opendart.fss.or.kr 에서 받은 인증키(40자)를 붙여 넣으세요 (화면에 보이지 않습니다).")
    key = read_secret("OpenDART 인증키: ", stdin, getpass_fn)
    if not key or len(key) < 20 or any(ch.isspace() for ch in key):
        out("키 형식이 이상합니다. 저장하지 않았습니다.")
        return 1
    p = paths(h)
    write_private(p["dart"], key + "\n")
    out("저장했습니다: %s" % p["dart"])
    if (env.get("DART_API_KEY") or "").strip():
        out("참고: DART_API_KEY 환경변수가 따로 있어서 그 값이 이 파일보다 먼저 쓰입니다.")
    return 0


def cmd_toss(a, out=print, env=None, h=None, stdin=None, getpass_fn=getpass.getpass):
    env = os.environ if env is None else env
    out("토스증권 PC 웹 설정 > Open API 의 client_id 와 client_secret 을 차례로 붙여 넣으세요 (화면에 보이지 않습니다).")
    cid = read_secret("client_id: ", stdin, getpass_fn)
    sec = read_secret("client_secret: ", stdin, getpass_fn)
    if not cid or not sec or any(ch.isspace() for ch in cid + sec):
        out("값이 비었거나 공백이 있습니다. 저장하지 않았습니다.")
        return 1
    if not cid.startswith("tsck_"):
        out("참고: client_id 가 tsck_ 로 시작하지 않습니다. 토스증권에서 받은 값인지 확인하세요.")
    p = paths(h)
    write_private(p["toss"], json.dumps({"client_id": cid, "client_secret": sec}) + "\n")
    if os.path.exists(p["toss_token"]):
        os.remove(p["toss_token"])          # 예전 클라이언트의 토큰 캐시는 새 키와 맞지 않는다
    out("저장했습니다: %s" % p["toss"])
    if (env.get("TOSS_CLIENT_ID") or "").strip():
        out("참고: TOSS_CLIENT_ID 환경변수가 따로 있어서 그 값이 이 파일보다 먼저 쓰입니다.")
    out("연결 점검: python krcommon/doctor.py")
    return 0


def main(argv=None):
    krx.utf8_stdio()
    ap = argparse.ArgumentParser(description="API 키 등록 (값은 찍지 않음).")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("status", help="어떤 키가 어디 있는지")
    q = sub.add_parser("krx", help="KRX Open API 키 등록과 서비스 승인 확인")
    q.add_argument("--check", action="store_true", help="저장하지 않고 지금 키로 승인만 확인")
    q.add_argument("--from-env", action="store_true", help="KRX_API_KEY 환경변수 값을 파일로 저장")
    q.add_argument("--no-verify", action="store_true", help="저장만 하고 KRX 를 부르지 않음")
    sub.add_parser("dart", help="OpenDART 키 등록")
    sub.add_parser("toss", help="토스증권 Open API 키 등록")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"status": cmd_status, "krx": cmd_krx, "dart": cmd_dart, "toss": cmd_toss}[a.cmd](a)
    except (EOFError, KeyboardInterrupt):
        print("keys.py: 입력이 없어 멈췄습니다. 본인 터미널에서 다시 돌리세요.", file=sys.stderr)
        return 1
    except (krxdata.KrxDataError, toss.TossError, dart.DartError, OSError) as e:
        print("keys.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
