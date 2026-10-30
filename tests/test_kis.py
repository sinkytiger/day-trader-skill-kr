"""한국투자증권 모의투자 클라이언트: 모의 서버만, 토큰 재사용, 주문 본문, 호출 한도 재시도."""

import json
import os
import shutil
import tempfile
import unittest

from _load import ROOT  # noqa: F401
from fake_kis import FakeKis
from krcommon import kis

CRED = ("appkey-test-AB12", "secret", ("50123456", "01"))


class Client(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fake = FakeKis({"SYN001": 30500})
        self.slept = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def client(self, cred=CRED):
        return kis.Client(credentials=cred, transport=self.fake, sleep=self.slept.append,
                          token_path=os.path.join(self.tmp, "token.json"))

    def test_mock_server_only(self):
        c = self.client()
        self.assertEqual(c.base, "https://openapivts.koreainvestment.com:29443")
        c.price("SYN001")
        self.assertTrue(all("openapivts" not in x["path"] for x in self.fake.log))   # 경로만 기록
        with open(os.path.join(ROOT, "krcommon", "kis.py"), encoding="utf-8") as fh:
            self.assertNotIn("openapi." + "koreainvestment.com", fh.read())

    def test_token_is_issued_once_and_reused(self):
        self.client().price("SYN001")
        self.client().price("SYN001")
        self.assertEqual(self.fake.tokens, 1)
        with open(os.path.join(self.tmp, "token.json"), encoding="utf-8") as fh:
            tok = json.load(fh)
        self.assertEqual(tok["key_tail"], "AB12")
        self.assertNotIn("secret", json.dumps(tok))
        self.client(("another-key-ZZ99", "s", ("50123456", "01"))).price("SYN001")
        self.assertEqual(self.fake.tokens, 2)                  # 앱키가 바뀌면 새로 받는다

    def test_buy_market_body(self):
        out = self.client().order("buy", "SYN001", 119)
        self.assertEqual(out["odno"], "0000001000")
        req = [x for x in self.fake.log if x["path"].endswith("order-cash")][0]
        self.assertEqual(req["tr_id"], "VTTC0012U")
        self.assertEqual(req["body"]["CANO"], "50123456")
        self.assertEqual(req["body"]["ACNT_PRDT_CD"], "01")
        self.assertEqual((req["body"]["ORD_DVSN"], req["body"]["ORD_UNPR"], req["body"]["ORD_QTY"]), ("01", "0", "119"))
        self.assertEqual(req["body"]["EXCG_ID_DVSN_CD"], "KRX")
        self.assertEqual(req["headers"]["custtype"], "P")
        self.assertTrue(req["headers"]["authorization"].startswith("Bearer "))

    def test_sell_limit_and_cancel_body(self):
        c = self.client()
        s = c.order("sell", "SYN001", 10, 31150)
        req = [x for x in self.fake.log if x["path"].endswith("order-cash")][-1]
        self.assertEqual(req["tr_id"], "VTTC0011U")
        self.assertEqual((req["body"]["ORD_DVSN"], req["body"]["ORD_UNPR"], req["body"]["SLL_TYPE"]), ("00", "31150", "01"))
        c.cancel(s["orgno"], s["odno"])
        req = [x for x in self.fake.log if x["path"].endswith("order-rvsecncl")][-1]
        self.assertEqual(req["tr_id"], "VTTC0013U")
        self.assertEqual((req["body"]["RVSE_CNCL_DVSN_CD"], req["body"]["QTY_ALL_ORD_YN"]), ("02", "Y"))
        self.assertEqual(req["body"]["ORGN_ODNO"], s["odno"])

    def test_rate_limit_is_retried(self):
        c = self.client()
        c.token()
        self.fake.script = [(500, {}, json.dumps({"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과하였습니다."}))]
        self.assertEqual(c.price("SYN001")["price"], 30500)
        self.assertIn(1.0, self.slept)

    def test_gateway_errors_are_read_too(self):
        c = self.client()
        c.token()
        self.fake.script = [(403, {}, json.dumps({"error_code": "EGW00102", "error_description": "AppKey는 필수입니다."}))] * 2
        with self.assertRaises(kis.KisError) as cm:
            c.price("SYN001")
        self.assertEqual(cm.exception.code, "EGW00102")
        self.assertIn("AppKey", str(cm.exception))

    def test_errors_carry_the_code(self):
        c = self.client()
        c.token()
        self.fake.script = [(200, {}, json.dumps({"rt_cd": "1", "msg_cd": "APBK0919", "msg1": "주문가능금액을 초과했습니다"}))]
        with self.assertRaises(kis.KisError) as cm:
            c.order("buy", "SYN001", 1)
        self.assertEqual(cm.exception.code, "APBK0919")
        self.assertIn("주문가능금액", str(cm.exception))

    def test_orders_page_through(self):
        c = self.client()
        c.order("buy", "SYN001", 1)
        page1 = (200, {"tr_cont": "M"}, json.dumps({"rt_cd": "0", "ctx_area_fk100": "F1", "ctx_area_nk100": "N1",
                                                     "output1": [FakeKis._row(self.fake.orders[0])]}))
        self.fake.script = [page1]
        rows = c.orders(__import__("datetime").date(2026, 10, 28))
        self.assertEqual(len(rows), 2)
        last = [x for x in self.fake.log if x["path"].endswith("inquire-daily-ccld")][-1]
        self.assertEqual(last["headers"]["tr_cont"], "N")
        self.assertEqual(last["query"]["CTX_AREA_FK100"], "F1")

    def test_credentials(self):
        env = {"KIS_APP_KEY": "k", "KIS_APP_SECRET": "s", "KIS_ACCOUNT": "50123456-01"}
        self.assertEqual(kis.find_credentials(env), ("k", "s", ("50123456", "01")))
        with self.assertRaises(kis.KisError):
            kis.split_account("123-45")
        old = kis.home_dir
        try:
            kis.home_dir = lambda: self.tmp
            with open(os.path.join(self.tmp, "credentials.json"), "w", encoding="utf-8") as fh:
                json.dump({"app_key": "k", "app_secret": "s", "account": "5012345601", "mock": False}, fh)
            with self.assertRaises(kis.KisError) as cm:
                kis.find_credentials({})
            self.assertIn("실전 계좌는", str(cm.exception))
        finally:
            kis.home_dir = old


if __name__ == "__main__":
    unittest.main()
