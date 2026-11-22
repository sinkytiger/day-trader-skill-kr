#!/usr/bin/env python3
"""시장 노트 자료 만들기 (trader-kr-notes). 숫자만 모으고, 사라·팔라는 말은 하지 않는다. 주문을 내지 않는다.

    python notes.py markets        # 24개 나라 ETF·원자재·국내 ETF 비교 (iShares, Yahoo, 네이버)
    python notes.py us-watch       # 미국 관심 종목 주간 점검 (Yahoo 수정 종가, SEC 공시, 최근 4개 분기 PER)
    python notes.py study          # 공부용 회사 재무제표 5년과 지표 (미국 SEC, 국내 DART)
    python notes.py share          # 공유용 공부 노트 자료 묶기 (공개 자료만)
    python notes.py basics         # 주식 기초편 예시 숫자 (KRX, 네이버, Yahoo)
    python notes.py tg setup|status|send [--dry-run]   # 텔레그램으로 나에게 알림

결과는 모두 프로젝트의 trader-kr/notes/ 아래에 쓴다 (저장소에 올리지 않는 폴더). 단계마다 하나라도 못 읽거나 건수가
모자라면 그 단계의 report.json 에 ok:false 와 이유를 적고 종료 코드 2 로 멈춘다. 이전 결과 파일은 그대로 둔다.
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PARTS = {"markets": "markets.py", "us-watch": "uswatch.py", "study": "study.py", "share": "share.py",
         "basics": "basics.py", "tg": "tg.py"}


def load(name):
    spec = importlib.util.spec_from_file_location("trader_kr_notes_" + name.replace("-", "_"), os.path.join(HERE, PARTS[name]))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    if not argv or argv[0] not in PARTS:
        print(__doc__.strip())
        return 1
    mod = load(argv[0])
    if argv[0] == "tg":
        return mod.main(argv[1:])
    return mod.main()


if __name__ == "__main__":
    sys.exit(main())
