"""스킬 폴더(이름에 하이픈이 있음)의 스크립트를 불러온다."""

import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 시험은 네트워크 없이 돈다: 실제 키를 읽어 진짜 서버를 부르는 일이 없게 지운다
for _k in ("DART_API_KEY", "TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TOSS_CREDENTIALS"):
    os.environ.pop(_k, None)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def load(folder, name):
    path = os.path.join(ROOT, folder, name + ".py")
    spec = importlib.util.spec_from_file_location("trader_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
