import argparse
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest

import _load  # noqa: F401
from krcommon import dart, keys, krxdata
from test_krxdata import FakeKrx

NOW = datetime.datetime(2026, 10, 29, 9, 0)
KEY = "krx-test-key-1234567890"


def args(**kw):
    base = {"check": False, "from_env": False, "no_verify": False}
    base.update(kw)
    return argparse.Namespace(**base)


class Keys(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.lines = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def text(self):
        return "\n".join(self.lines)

    def test_krx_key_is_saved_hidden_and_checked(self):
        fake = FakeKrx(datetime.date(2026, 10, 28))
        code = keys.cmd_krx(args(), self.lines.append, env={}, h=self.tmp, stdin=_Tty(),
                            getpass_fn=lambda prompt: KEY, transport=fake, now_kst=NOW)
        self.assertEqual(code, 0, self.text())
        with open(os.path.join(self.tmp, ".krx", "api_key")) as fh:
            self.assertEqual(fh.read().strip(), KEY)
        self.assertNotIn(KEY, self.text())
        self.assertIn("통과 유가증권 일별매매정보", self.text())
        self.assertIn("통과 코스닥 일별매매정보", self.text())
        self.assertEqual(krxdata.api_key({}, home=self.tmp), KEY)        # 스캐너가 이 파일을 읽는다

    def test_piped_input_and_a_rejected_service(self):
        fake = FakeKrx(datetime.date(2026, 10, 28), status=403)
        code = keys.cmd_krx(args(), self.lines.append, env={}, h=self.tmp, stdin=_Pipe(KEY + "\n"),
                            transport=fake, now_kst=NOW)
        self.assertEqual(code, 1)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, ".krx", "api_key")))   # 저장은 됐고
        self.assertIn("이용신청", self.text())                                         # 승인 안내

    def test_bad_input_is_not_saved(self):
        for bad in ("", "short", "has space inside"):
            code = keys.cmd_krx(args(no_verify=True), self.lines.append, env={}, h=self.tmp, stdin=_Pipe(bad + "\n"))
            self.assertEqual(code, 1, bad)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, ".krx", "api_key")))

    def test_from_env_and_the_env_wins_note(self):
        code = keys.cmd_krx(args(from_env=True, no_verify=True), self.lines.append, env={"KRX_API_KEY": KEY},
                            h=self.tmp)
        self.assertEqual(code, 0)
        self.lines = []
        keys.cmd_krx(args(no_verify=True), self.lines.append, env={"KRX_API_KEY": KEY}, h=self.tmp,
                     stdin=_Pipe("another-key-0987654321\n"))
        self.assertIn("환경변수가 따로 있어서", self.text())

    def test_check_only_does_not_write(self):
        code = keys.cmd_krx(args(check=True), self.lines.append, env={"KRX_API_KEY": KEY}, h=self.tmp,
                            transport=FakeKrx(datetime.date(2026, 10, 28)), now_kst=NOW)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, ".krx")))

    def test_dart_and_toss(self):
        dkey = "d" * 40
        self.assertEqual(keys.cmd_dart(args(), self.lines.append, env={}, h=self.tmp, stdin=_Pipe(dkey + "\n")), 0)
        self.assertEqual(dart.api_key({}, home=self.tmp), dkey)
        token = os.path.join(self.tmp, ".tossinvest", "token.json")
        os.makedirs(os.path.dirname(token))
        with open(token, "w") as fh:
            fh.write("{}")
        code = keys.cmd_toss(args(), self.lines.append, env={}, h=self.tmp,
                             stdin=_Pipe("tsck_live_fake-id-only\nsecret-value\n"))
        self.assertEqual(code, 0)
        with open(os.path.join(self.tmp, ".tossinvest", "credentials.json")) as fh:
            self.assertEqual(json.load(fh)["client_id"], "tsck_live_fake-id-only")
        self.assertFalse(os.path.exists(token))                         # 예전 토큰 캐시는 지움
        self.assertNotIn("secret-value", self.text())

    def test_status_never_prints_values(self):
        keys.cmd_status(args(), self.lines.append, env={"KRX_API_KEY": KEY, "DART_API_KEY": "x" * 40}, h=self.tmp)
        t = self.text()
        self.assertIn("KRX Open API", t)
        self.assertIn("환경변수 KRX_API_KEY", t)
        self.assertNotIn(KEY, t)


class _Pipe(io.StringIO):
    def isatty(self):
        return False


class _Tty(io.StringIO):
    """터미널인 척: 숨김 입력(getpass) 경로를 탄다."""
    def isatty(self):
        return True


if __name__ == "__main__":
    unittest.main()
