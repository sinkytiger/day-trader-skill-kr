import datetime
import json
import os
import shutil
import tempfile
import unittest

import _load  # noqa: F401
import synth
from fake_toss import FakeToss
from krcommon import doctor, toss


class Doctor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cred = os.path.join(self.tmp, "cred.json")
        with open(self.cred, "w") as fh:
            json.dump({"client_id": "tsck_live_fake-id-only", "client_secret": "s3cr3t-value"}, fh)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def client(self, fake):
        return toss.Client(credentials=None, transport=fake, sleep=lambda s: None,
                           token_path=os.path.join(self.tmp, "token.json"))

    def test_all_steps_pass_and_no_secret_is_printed(self):
        fake = FakeToss({"SYN001": synth.make_history()["bars"]})
        d = {"date": "2026-10-28", "integrated": {"regularMarket": {
            "startTime": "2026-10-28T09:00:00+09:00", "singlePriceAuctionStartTime": "2026-10-28T15:20:00+09:00",
            "endTime": "2026-10-28T15:30:00+09:00"}}}
        fake.calendar = {"2026-10-28": {"previousBusinessDay": d, "today": d, "nextBusinessDay": d}}
        fake.orderbook = {"SYN001": {"asks": [{"price": str(p)} for p in (30500, 30550, 30600)],
                                     "bids": [{"price": str(p)} for p in (30450, 30400, 30350)]}}
        lines = []
        env = {"TOSS_CREDENTIALS": self.cred}
        old = os.environ.get("TOSS_CREDENTIALS")
        os.environ["TOSS_CREDENTIALS"] = self.cred
        try:
            ok = doctor.run(self.client(fake), "SYN001", lines.append, env, datetime.date(2026, 10, 28))
        finally:
            if old is None:
                del os.environ["TOSS_CREDENTIALS"]
            else:
                os.environ["TOSS_CREDENTIALS"] = old
        text = "\n".join(lines)
        self.assertTrue(ok, text)
        self.assertIn("09:00 개장, 접속매매 15:20 까지", text)
        self.assertIn("SYN001 마지막 봉 2026-10-28 15:30 종료", text)
        self.assertIn("호가창 간격 50원, 표 50원. 맞습니다", text)
        self.assertNotIn("fake-id-only", text)
        self.assertNotIn("s3cr3t", text)

    def test_ip_block_is_explained(self):
        def blocked(method, url, headers, body):
            return 403, {}, json.dumps({"error": {"code": "access_denied", "message": ""}})
        c = toss.Client(credentials=("tsck_live_x", "y"), transport=blocked, sleep=lambda s: None,
                        token_path=os.path.join(self.tmp, "token.json"))
        lines = []
        ok = doctor.run(c, "SYN001", lines.append, {"TOSS_CLIENT_ID": "tsck_live_x", "TOSS_CLIENT_SECRET": "y"})
        self.assertFalse(ok)
        self.assertIn("허용 IP", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
