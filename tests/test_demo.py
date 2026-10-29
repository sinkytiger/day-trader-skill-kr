"""키 없이 도는 데모: 8단계가 모두 끝나고, 실제 키를 읽지 않고, 남의 폴더를 건드리지 않는다."""

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

from _load import ROOT  # noqa: F401  (sys.path 와 키 환경변수 정리)
from krcommon import demo


class Demo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dir = os.path.join(self.tmp, "demo")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_demo(self, folder=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = demo.main(["--dir", folder or self.dir, "--replays", "300"])
        return code, out.getvalue()

    def test_all_eight_steps_run_offline(self):
        code, text = self.run_demo()
        self.assertEqual(code, 0, text)
        for n in range(1, 9):
            self.assertIn("== %d/8 " % n, text)
        self.assertIn("PASS  #1 매수 SYN001", text)
        self.assertIn("== 한 번에", text)
        self.assertIn("1. PASS      SYN001 합성종목  신호 10:15 (방금)", text)
        self.assertIn("저장된 파일", text)
        self.assertNotIn("토스증권. 후보", text)
        for name in ("watchlist", "entry", "plan", "timemachine", "orders", "lab"):
            self.assertTrue(os.path.exists(os.path.join(self.dir, "trader-kr", name + ".json")), name)
        with open(os.path.join(self.dir, "trader-kr", "orders.json"), encoding="utf-8") as fh:
            self.assertIs(json.load(fh)["send_orders"], False)

    def test_rerun_starts_a_fresh_journal(self):
        self.assertEqual(self.run_demo()[0], 0)
        self.assertEqual(self.run_demo()[0], 0)
        with open(os.path.join(self.dir, "trader-kr", "journal.jsonl"), encoding="utf-8") as fh:
            self.assertEqual(len([x for x in fh if x.strip()]), 1)

    def test_refuses_a_folder_it_did_not_make(self):
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, "mine.txt"), "w") as fh:
            fh.write("x")
        code, text = self.run_demo()
        self.assertEqual(code, 1)
        self.assertIn("데모가 만든 폴더가 아닙니다", text)
        self.assertEqual(os.listdir(self.dir), ["mine.txt"])

    def test_child_processes_cannot_see_real_keys(self):
        os.environ["TOSS_CLIENT_ID"] = "x"
        try:
            env = demo.child_env(self.dir)
        finally:
            os.environ.pop("TOSS_CLIENT_ID", None)
        for k in demo.KEY_ENV:
            self.assertNotIn(k, env)
        self.assertEqual(env["HOME"], os.path.abspath(self.dir))
        self.assertEqual(env["USERPROFILE"], os.path.abspath(self.dir))


if __name__ == "__main__":
    unittest.main()
