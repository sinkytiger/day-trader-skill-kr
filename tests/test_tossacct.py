import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

from _load import load
from fake_toss import FakeToss, holding, order
from krcommon import toss, tossacct

journal = load("trader-kr-journal", "journal")
PLAN = {"symbol": "SYN001", "name": "합성종목", "side": "buy", "date": "2026-10-28", "setup": "orb", "entry": 30450,
        "entry_time": "10:15", "stop": 30100, "target": 31150, "flat_by": "15:15"}
DAY = "2026-10-28"


class Clock(object):
    def __init__(self):
        self.t = 1000000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def at(hm):
    return "%sT%s:00+09:00" % (DAY, hm)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.getcwd()
        os.chdir(self.tmp)
        os.makedirs("trader-kr")
        self.write("plan", PLAN)
        self.write("config", {"max_loss_per_trade_krw": 50000})
        self.fake = FakeToss()
        self.real_make = journal.make_account
        clock = Clock()
        cl = toss.Client(credentials=("id", "secret"), transport=self.fake, sleep=clock.sleep, clock=clock,
                         token_path=os.path.join(self.tmp, "token.json"))
        journal.make_account = lambda seq=None: tossacct.Account(client=cl, account_seq=seq)
        self.client = cl

    def tearDown(self):
        journal.make_account = self.real_make
        os.chdir(self.old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, doc):
        with open("trader-kr/%s.json" % name, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)

    def call(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = journal.main(argv)
        return code, out.getvalue() + err.getvalue()

    def sync(self, *extra):
        return self.call(["sync", "--date", DAY, "--now", DAY + " 15:30"] + list(extra))

    def rows(self):
        return journal.load()


class AccountReads(Base):
    def test_one_brokerage_account_is_picked_and_masked(self):
        acct = tossacct.Account(client=self.client)
        self.assertEqual(acct.seq(), 7)
        self.assertEqual(acct.masked, "*******8901")

    def test_several_accounts_need_a_choice_and_never_show_the_full_number(self):
        self.fake.account_list.append({"accountNo": "99988877766", "accountSeq": 9, "accountType": "BROKERAGE"})
        with self.assertRaises(tossacct.AccountError) as cm:
            tossacct.Account(client=self.client).seq()
        self.assertIn("--account-seq", str(cm.exception))
        self.assertNotIn("12345678901", str(cm.exception))
        self.assertEqual(tossacct.Account(client=self.client, account_seq=9).seq(), 9)

    def test_closed_orders_follow_the_cursor(self):
        self.fake.order_book = [order("C%d" % i, "SYN001", "BUY", 1, 30450, at("10:%02d" % (10 + i))) for i in range(5)]
        self.fake.page_size = 2
        got = tossacct.Account(client=self.client).orders("CLOSED", DAY, DAY)
        self.assertEqual([o["orderId"] for o in got], ["C0", "C1", "C2", "C3", "C4"])
        pages = [q for m, path, q in self.fake.log if path == "/api/v1/orders"]
        self.assertEqual(len(pages), 3)
        self.assertEqual(pages[0]["limit"], "100")
        self.assertEqual([q.get("cursor") for q in pages], [None, "2", "4"])

    def test_missing_permission_is_explained(self):
        self.fake.script = [(403, {}, json.dumps({"error": {"code": "forbidden", "message": ""}}))]
        with self.assertRaises(tossacct.AccountError) as cm:
            tossacct.Account(client=self.client).accounts()
        self.assertIn("권한", str(cm.exception))

    def test_fill_of_keeps_only_korean_fills(self):
        f = tossacct.fill_of(order("A", "SYN001", "SELL", 4, 30100, at("10:40"), commission=18, tax=240))
        self.assertEqual((f["qty"], f["avg"], f["fees"]), (4.0, 30100.0, 258.0))
        self.assertIsNone(tossacct.fill_of(order("B", "AAPL", "BUY", 1, 200, at("10:40"), currency="USD")))
        self.assertIsNone(tossacct.fill_of(order("C", "SYN001", "BUY", 4, 30100, at("10:40"), status="CANCELED",
                                                 filled=0)))
        part = tossacct.fill_of(order("D", "SYN001", "BUY", 4, 30100, at("10:40"), status="CANCELED", filled=1))
        self.assertEqual(part["qty"], 1.0)
        self.assertIsNone(tossacct.fill_of(order("E", "SYN001", "BUY", 1, 30100, at("10:40")))["fees"])


class Sync(Base):
    def test_a_planned_trade_is_written_with_toss_costs(self):
        self.fake.order_book = [order("B1", "SYN001", "BUY", 4, 30450, at("10:16"), price=30450, commission=18),
                                order("S1", "SYN001", "SELL", 4, 30100, at("10:40"), commission=18, tax=240)]
        code, text = self.sync()
        self.assertEqual(code, 0, text)
        self.assertIn("끝난 주문 2건 중 국내 체결 2건", text)
        self.assertIn("손절로 봤습니다", text)
        self.assertIn("손익 -1,676원 (비용 276원, 토스증권 값), 비용 뺀 R -1.20", text)
        [r] = self.rows()
        self.assertEqual((r["qty"], r["entry_time"], r["exit_time"], r["reason"]), (4, "10:16", "10:40", "stop"))
        self.assertEqual((r["signal_time"], r["stop"], r["setup"]), ("10:15", 30100, "orb"))
        self.assertEqual(r["toss_orders"], ["B1", "S1"])

    def test_running_twice_writes_nothing_new(self):
        self.fake.order_book = [order("B1", "SYN001", "BUY", 4, 30450, at("10:16"), commission=18)]
        self.sync()
        code, text = self.sync()
        self.assertEqual(code, 0, text)
        self.assertIn("새로 적을 체결이 없습니다", text)
        self.assertEqual(len(self.rows()), 1)

    def test_split_buys_are_averaged_and_a_partial_sell_splits_the_record(self):
        self.fake.order_book = [order("B1", "SYN001", "BUY", 2, 30450, at("10:16"), commission=9),
                                order("B2", "SYN001", "BUY", 2, 30500, at("10:20"), commission=9),
                                order("S1", "SYN001", "SELL", 1, 31150, at("11:05"), commission=4, tax=62)]
        code, text = self.sync()
        self.assertEqual(code, 0, text)
        self.assertIn("합쳐서 4주, 평균 30,475", text)
        closed = [r for r in self.rows() if r.get("exit_price") is not None]
        still = [r for r in self.rows() if r.get("exit_price") is None]
        self.assertEqual([(r["qty"], r["reason"]) for r in closed], [(1, "target")])
        self.assertEqual([r["qty"] for r in still], [3])
        self.assertAlmostEqual(closed[0]["buy_fees"] + still[0]["buy_fees"], 18.0)
        self.assertNotEqual(closed[0]["id"], still[0]["id"])

    def test_a_typed_record_is_matched_to_the_toss_fill(self):
        self.call(["open", "--entry-price", "30450", "--qty", "4", "--entry-time", "10:15", "--now", DAY + " 10:16"])
        self.fake.order_book = [order("B1", "SYN001", "BUY", 4, 30460, at("10:16"), commission=18)]
        code, text = self.sync()
        self.assertEqual(code, 0, text)
        self.assertIn("적어 둔 30,450 (10:15) 에서 고침", text)
        [r] = self.rows()
        self.assertEqual((r["entry_price"], r["entry_time"], r["typed_entry"]["price"]), (30460, "10:16", 30450))

    def test_plans_from_today_candidates_are_used(self):
        os.makedirs("trader-kr/today/SYN002")
        with open("trader-kr/today/SYN002/plan.json", "w", encoding="utf-8") as fh:
            json.dump(dict(PLAN, symbol="SYN002", entry=9000, stop=8900, target=9200, entry_time="10:30"), fh)
        self.fake.order_book = [order("B1", "SYN002", "BUY", 10, 9010, at("10:31"))]
        code, text = self.sync()
        self.assertEqual(code, 0, text)
        self.assertIn("계획과 묶음, 손절 8,900", text)
        [r] = self.rows()
        self.assertEqual((r["signal_time"], r["plan_entry"], r["target"]), ("10:30", 9000, 9200))

    def test_mock_trades_are_left_alone(self):
        self.call(["open", "--entry-price", "30450", "--qty", "4", "--entry-time", "10:16", "--now", DAY + " 10:16",
                   "--note", "KIS 모의 0000123"])
        self.fake.order_book = [order("S1", "SYN001", "SELL", 4, 30100, at("10:40"), commission=18, tax=240)]
        code, text = self.sync()
        self.assertEqual(code, 0, text)
        self.assertIn("일지에 없는 SYN001 4주", text)
        [r] = self.rows()
        self.assertIsNone(r.get("exit_price"))

    def test_without_a_plan_the_percent_stop_rule_sets_the_stop(self):
        os.remove("trader-kr/plan.json")
        self.write("config", {"stop_rule": "pct:5"})
        self.fake.order_book = [order("B1", "SYN002", "BUY", 4, 30450, at("10:16"), commission=18)]
        code, text = self.sync()
        self.assertEqual(code, 0, text)
        self.assertIn("손절 28,900 (설정 pct:5)", text)
        [r] = self.rows()
        self.assertEqual((r["stop"], r["flat_by"], r["signal_time"]), (28900, None, None))

    def test_dry_run_writes_nothing(self):
        self.fake.order_book = [order("B1", "SYN001", "BUY", 4, 30450, at("10:16"))]
        code, text = self.sync("--dry-run")
        self.assertEqual(code, 0, text)
        self.assertIn("산 것: SYN001 4주 30,450 (10:16), 계획과 묶음", text)
        self.assertFalse(os.path.exists("trader-kr/journal.jsonl"))

    def test_waiting_orders_are_listed_and_symbol_filter_applies(self):
        self.fake.order_book = [order("B1", "SYN001", "BUY", 4, 30400, at("10:16"), status="PARTIAL_FILLED",
                                      price=30400, filled=2),
                                order("B2", "SYN002", "BUY", 1, 9000, at("10:17"))]
        code, text = self.sync("--symbol", "SYN001")
        self.assertEqual(code, 0, text)
        self.assertIn("체결 대기: SYN001 매수 4주 지정가 30,400 (2주 체결)", text)
        self.assertIn("국내 체결 0건 (SYN001 만)", text)
        self.assertEqual(self.rows(), [])

    def test_only_get_requests_reach_toss(self):
        self.fake.order_book = [order("B1", "SYN001", "BUY", 4, 30450, at("10:16")),
                                order("S1", "SYN001", "SELL", 4, 30100, at("10:40"))]
        self.fake.holding_items = [holding("SYN002", 1, 9000, 9100)]
        self.sync()
        self.call(["holdings", "--now", DAY + " 15:30"])
        self.assertTrue(self.fake.log)
        for method, path, _ in self.fake.log:
            if path == "/oauth2/token":
                self.assertEqual(method, "POST")
            else:
                self.assertEqual(method, "GET", path)
            for bad in ("conditional-orders", "/modify", "/cancel", "buying-power", "sellable-quantity"):
                self.assertNotIn(bad, path)
        for method, path, h in self.fake.headers_log:
            if path in ("/api/v1/holdings", "/api/v1/orders"):
                self.assertEqual(h.get("X-Tossinvest-Account"), "7")


class Holdings(Base):
    def test_holdings_show_the_stop_gap_and_what_to_check(self):
        self.write("config", {"stop_rule": "pct:5"})
        self.call(["open", "--entry-price", "30450", "--qty", "4", "--entry-time", "10:16", "--now", DAY + " 10:16"])
        self.call(["open", "--symbol", "SYN003", "--entry-price", "5000", "--qty", "10", "--stop", "4800",
                   "--now", DAY + " 10:30"])
        self.fake.holding_items = [holding("SYN001", 4, 30450, 30000), holding("SYN002", 2, 10000, 10100),
                                   holding("AAPL", 1, 200, 210, country="US", currency="USD")]
        code, text = self.call(["holdings", "--now", DAY + " 15:20"])
        self.assertEqual(code, 0, text)
        self.assertIn("국내 보유 2종목", text)
        self.assertIn("해외 1종목", text)
        self.assertIn("SYN001 현재가(30,000)가 손절가(30,100) 이하입니다", text)
        self.assertIn("SYN001 계획 청산 시각(15:15)이 지났습니다", text)
        self.assertIn("9,500*", text)
        self.assertIn("일지에 없는 보유: SYN002.", text)
        self.assertIn("SYN003 (20261028-SYN003-1): 일지에는 열려 있는데 계좌에 없습니다", text)
        self.assertNotIn("12345678901", text)

    def test_long_held_stock_below_the_rule_stop_is_only_in_the_table(self):
        self.write("config", {"stop_rule": "pct:5"})
        self.fake.holding_items = [holding("SYN002", 2, 10000, 9000), holding("SYN004", 1, 5000, 5100)]
        code, text = self.call(["holdings", "--now", DAY + " 11:00", "--symbol", "SYN002"])
        self.assertEqual(code, 0, text)
        self.assertIn("국내 보유 1종목", text)
        self.assertIn("9,500*  닿음", text)
        self.assertNotIn("이하입니다", text)
        self.assertNotIn("SYN004", text)


if __name__ == "__main__":
    unittest.main()
