"""주문 실행: 세 겹 승인, 모의투자 매수와 목표가 주문, watch 의 손절·목표·청산 시각, 일지와 위험 점검 연결."""

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

from _load import load
from fake_kis import FakeKis
from krcommon import kis, krx

broker = load("trader-kr-broker", "broker")
risk = load("trader-kr-risk", "check")

ORDER = {"symbol": "SYN001", "name": "합성종목", "side": "buy", "qty": 119, "type": "market", "time_in_force": "day",
         "exit": {"stop_price": 30100, "take_profit_price": 31150, "flat_by": "15:15"},
         "plan": {"entry": 30450, "stop": 30100, "target": 31150, "max_loss": 49893}}
PLAN = {"symbol": "SYN001", "name": "합성종목", "side": "buy", "date": "2026-10-28", "setup": "orb", "entry": 30450,
        "entry_time": "10:15", "stop": 30100, "target": 31150, "flat_by": "15:15"}
NOW = "2026-10-28 10:16"


class Broker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.getcwd()
        os.chdir(self.tmp)
        os.makedirs("trader-kr/state")
        krx.write_json("trader-kr/config.json", {"budget": 5000000, "send_orders": True})
        krx.write_json("trader-kr/plan.json", PLAN)
        krx.write_json("trader-kr/orders.json", {"orders": [ORDER]})
        krx.write_json("trader-kr/state/risk.json", {
            "date": "2026-10-28", "now_kst": "2026-10-28 10:16",
            "results": [{"index": 1, "fingerprint": risk.fingerprint(ORDER), "verdict": "PASS", "max_loss": 49893,
                         "reasons": [], "warnings": []}]})
        self.fake = FakeKis({"SYN001": 30500})
        self.client = kis.Client(credentials=("appkey-test-AB12", "s", ("50123456", "01")), transport=self.fake,
                                 sleep=lambda s: None, token_path=os.path.join(self.tmp, "token.json"))

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = broker.main(list(argv), client=self.client)
        return code, out.getvalue()

    def sent(self, tr_id):
        return [x for x in self.fake.log if x["tr_id"] == tr_id]

    def approve_and_place(self, reply="네", now=NOW):
        code, text = self.call("approve", "--index", "1", "--reply", reply, "--now", now)
        self.assertEqual(code, 0, text)
        return self.call("place", "--index", "1", "--now", now)

    # ------------------------------------------------------------ 승인
    def test_show_marks_ready_and_explains_the_stop(self):
        code, text = self.call("show", "--now", NOW)
        self.assertEqual(code, 0, text)
        self.assertIn("READY: 위험 점검 PASS", text)
        self.assertIn("broker.py watch 가 돌고 있을 때만", text)

    def test_only_a_plain_yes_for_this_order_counts(self):
        for reply in ("좋아", "ok", "ㅇㅇ", "네 모두", "yes to all", "네 근데 잠깐", "네 200주", "네 000660", "아니요"):
            code, text = self.call("approve", "--index", "1", "--reply", reply, "--now", NOW)
            self.assertEqual(code, 1, reply)
            self.assertIn("기록하지 않았습니다", text, reply)
        code, text = self.call("place", "--index", "1", "--now", NOW)
        self.assertEqual(code, 1)
        self.assertIn("yes 기록이 없습니다", text)
        self.assertEqual(self.sent("VTTC0012U"), [])
        for reply in ("네", "예", "yes", "네, SYN001 119주"):
            self.assertTrue(broker.is_yes(reply, ORDER)[0], reply)

    def test_send_orders_false_blocks_everything(self):
        krx.write_json("trader-kr/config.json", {"budget": 5000000, "send_orders": False})
        code, text = self.approve_and_place()
        self.assertEqual(code, 1)
        self.assertIn("send_orders", text)
        self.assertEqual(self.sent("VTTC0012U"), [])

    def test_an_order_changed_after_the_risk_check_is_not_ready(self):
        krx.write_json("trader-kr/orders.json", {"orders": [dict(ORDER, qty=200)]})
        code, text = self.call("approve", "--index", "1", "--reply", "네", "--now", NOW)
        self.assertEqual(code, 1)
        self.assertIn("NOT READY", text)

    def test_a_yes_expires_and_is_used_once(self):
        self.call("approve", "--index", "1", "--reply", "네", "--now", NOW)
        code, text = self.call("place", "--index", "1", "--now", "2026-10-28 10:27")
        self.assertEqual(code, 1)
        self.assertIn("10분 안의 yes", text)
        code, text = self.approve_and_place(now="2026-10-28 10:28")
        self.assertEqual(code, 0, text)
        code, text = self.call("place", "--index", "1", "--now", "2026-10-28 10:28")
        self.assertEqual(code, 1)
        self.assertIn("오늘 이미 보낸 주문", text)
        self.assertEqual(len(self.sent("VTTC0012U")), 1)

    # ------------------------------------------------------------ 주문과 감시
    def test_place_buys_then_rests_the_target_and_journals(self):
        code, text = self.approve_and_place()
        self.assertEqual(code, 0, text)
        buy = self.sent("VTTC0012U")
        self.assertEqual(len(buy), 1)
        self.assertEqual(buy[0]["body"]["ORD_DVSN"], "01")
        sell = self.sent("VTTC0011U")
        self.assertEqual(len(sell), 1)
        self.assertEqual((sell[0]["body"]["ORD_DVSN"], sell[0]["body"]["ORD_UNPR"], sell[0]["body"]["ORD_QTY"]),
                         ("00", "31150", "119"))
        self.assertIn("체결: SYN001 119주, 평균 30,500", text)
        with open("trader-kr/journal.jsonl", encoding="utf-8") as fh:
            rows = [json.loads(x) for x in fh if x.strip()]
        self.assertEqual((rows[0]["entry_price"], rows[0]["qty"], rows[0]["stop"]), (30500, 119, 30100))
        st = broker.load_state()
        self.assertEqual([r["kind"] for r in st["orders"]], ["entry", "exit"])
        self.assertEqual(st["orders"][0]["max_loss"], 49893)
        # 위험 점검이 오늘 낸 진입을 센다
        res = risk.check([dict(ORDER, symbol="SYN002")], {"budget": 5000000, "max_trades_per_day": 1}, None, st,
                         {}, krx.parse_now("2026-10-28 10:20"), krx.Session(krx.parse_now(NOW).date(), source="t"))
        self.assertEqual(res["today"]["entries_placed"], 1)

    def test_watch_sells_at_the_stop(self):
        self.approve_and_place()
        self.fake.set_price("SYN001", 30000)
        code, text = self.call("watch", "--once", "--now", "2026-10-28 10:40")
        self.assertEqual(code, 0, text)
        self.assertEqual(len(self.sent("VTTC0013U")), 1)                     # 목표가 주문 취소
        market_sells = [x for x in self.sent("VTTC0011U") if x["body"]["ORD_DVSN"] == "01"]
        self.assertEqual(len(market_sells), 1)
        self.assertEqual(market_sells[0]["body"]["ORD_QTY"], "119")
        self.assertIn("손절가 이하", text)
        with open("trader-kr/journal.jsonl", encoding="utf-8") as fh:
            row = json.loads(fh.read().strip().splitlines()[-1])
        self.assertEqual((row["reason"], row["exit_price"]), ("stop", 30000))
        self.assertEqual(broker.load_state()["positions"][0]["status"], "closed")

    def test_watch_records_the_target_fill(self):
        self.approve_and_place()
        self.fake.set_price("SYN001", 31200)                                  # 걸어 둔 지정가가 체결됨
        code, text = self.call("watch", "--once", "--now", "2026-10-28 11:05")
        self.assertEqual(code, 0, text)
        self.assertIn("목표가 체결", text)
        self.assertEqual(self.sent("VTTC0013U"), [])
        with open("trader-kr/journal.jsonl", encoding="utf-8") as fh:
            row = json.loads(fh.read().strip().splitlines()[-1])
        self.assertEqual((row["reason"], row["exit_price"]), ("target", 31150))

    def test_watch_flattens_at_the_flat_by_time(self):
        self.approve_and_place()
        code, text = self.call("watch", "--once", "--now", "2026-10-28 15:16")
        self.assertEqual(code, 0, text)
        self.assertIn("청산 시각", text)
        self.assertEqual(broker.load_state()["positions"][0]["exit_reason"], "flat")

    def test_flatten_needs_a_yes(self):
        self.approve_and_place()
        code, text = self.call("flatten", "SYN001", "--reply", "팔아", "--now", "2026-10-28 11:00")
        self.assertEqual(code, 1)
        self.assertIn("팔지 않았습니다", text)
        code, text = self.call("flatten", "SYN001", "--reply", "네", "--now", "2026-10-28 11:00")
        self.assertEqual(code, 0, text)
        self.assertIn("직접 청산", text)

    def test_status_reads_only(self):
        self.approve_and_place()
        n = len(self.fake.log)
        code, text = self.call("status", "--now", NOW)
        self.assertEqual(code, 0, text)
        self.assertIn("SYN001", text)
        self.assertFalse(any(x["path"].endswith(("order-cash", "order-rvsecncl")) for x in self.fake.log[n:]))
        self.assertNotIn("50123456", text)                                   # 계좌번호는 찍지 않는다


if __name__ == "__main__":
    unittest.main()
