import datetime
import unittest

import _load  # noqa: F401
from krcommon import bars, krx


class Ticks(unittest.TestCase):
    def test_tick_table_boundaries(self):
        cases = [(1999, 1), (2000, 5), (4995, 5), (5000, 10), (19990, 10), (20000, 50), (49950, 50),
                 (50000, 100), (199900, 100), (200000, 500), (499500, 500), (500000, 1000), (1500000, 1000)]
        for price, tick in cases:
            self.assertEqual(krx.tick_size(price), tick, price)

    def test_rounding_stays_on_the_grid_across_bands(self):
        self.assertEqual(krx.floor_tick(30120), 30100)
        self.assertEqual(krx.ceil_tick(30120), 30150)
        self.assertEqual(krx.round_tick(30125), 30150)
        self.assertEqual(krx.round_tick(30124), 30100)
        self.assertEqual(krx.ceil_tick(1999.5), 2000)
        self.assertEqual(krx.floor_tick(2003), 2000)
        self.assertEqual(krx.ceil_tick(49980), 50000)
        self.assertTrue(krx.on_tick(72300))
        self.assertFalse(krx.on_tick(72350))
        self.assertFalse(krx.on_tick(30100.5))
        # 2026-10-29 삼성전자 체결가 245,250원은 중간가 체결이라 주문 호가(500원 단위)가 아니다
        self.assertFalse(krx.on_tick(245250))
        self.assertEqual(krx.ceil_tick(245250), 245500)

    def test_price_limits_are_thirty_percent_on_ticks(self):
        self.assertEqual(krx.price_limits(10000), (7000, 13000))
        lo, hi = krx.price_limits(30450)
        self.assertEqual((lo, hi), (21350, 39550))     # 폭 9,135 -> 50원 단위로 버려 9,100
        self.assertTrue(krx.on_tick(lo) and krx.on_tick(hi))
        # 2026-10-29 토스증권이 준 삼성전자 상·하한가 (기준가 242,000원)
        self.assertEqual(krx.price_limits(242000), (169500, 314500))


class Costs(unittest.TestCase):
    def test_round_trip_costs_floor_to_the_won(self):
        c = krx.trade_costs(30450, 30100, 100, 0.00015, 0.002)
        self.assertEqual(c["buy_fee"], 456)       # 3,045,000 x 0.015% = 456.75
        self.assertEqual(c["sell_fee"], 451)      # 3,010,000 x 0.015% = 451.5
        self.assertEqual(c["sell_tax"], 6020)     # 3,010,000 x 0.20%
        self.assertEqual(c["total"], 456 + 451 + 6020)

    def test_cost_per_share(self):
        self.assertAlmostEqual(krx.cost_per_share(30450, 30100, 0.00015, 0.002), 69.2825, places=4)


class Time(unittest.TestCase):
    def test_kst_is_utc_plus_nine_without_dst(self):
        dt = datetime.datetime(2026, 7, 1, 9, 0)
        self.assertEqual(krx.kst_of(krx.ts_of(dt)), dt)
        self.assertEqual(krx.ts_of(datetime.datetime(1970, 1, 1, 9, 0)), 0)

    def test_parse_now_and_iso(self):
        self.assertEqual(krx.parse_now("2026-10-28 10:15"), datetime.datetime(2026, 10, 28, 10, 15))
        self.assertEqual(krx.parse_iso("2026-03-25T09:30:00.123+09:00"), datetime.datetime(2026, 3, 25, 9, 30))
        self.assertEqual(krx.parse_iso("2026-03-25T00:30:00Z"), datetime.datetime(2026, 3, 25, 9, 30))

    def test_late_open_day_shifts_entry_times(self):
        s = krx.Session(datetime.date(2026, 11, 19), datetime.time(10, 0), datetime.time(16, 20), datetime.time(16, 30),
                        "toss-calendar")
        self.assertFalse(s.standard)
        self.assertEqual(s.shift(datetime.time(9, 15)), datetime.time(10, 15))
        self.assertEqual(s.shift(datetime.time(14, 50)), datetime.time(15, 50))
        self.assertEqual(s.shift(datetime.time(15, 15)), datetime.time(16, 15))
        self.assertEqual(krx.Session(datetime.date(2026, 10, 28)).slots(), 76)

    def test_calendar_from_toss_response(self):
        res = {"previousBusinessDay": {"date": "2026-10-27", "integrated": {"regularMarket": {
                   "startTime": "2026-10-27T09:00:00+09:00", "singlePriceAuctionStartTime": "2026-10-27T15:20:00+09:00",
                   "endTime": "2026-10-27T15:30:00+09:00"}}},
               "today": {"date": "2026-10-28", "integrated": None},
               "nextBusinessDay": {"date": "2026-10-29", "integrated": {"regularMarket": {
                   "startTime": "2026-10-29T10:00:00+09:00", "singlePriceAuctionStartTime": "2026-10-29T15:20:00+09:00",
                   "endTime": "2026-10-29T15:30:00+09:00"}}}}
        cal = krx.Calendar()
        cal.add_response(res)
        self.assertTrue(cal.session(datetime.date(2026, 10, 27)).standard)
        self.assertIsNone(cal.session(datetime.date(2026, 10, 28)))          # 휴장
        self.assertEqual(cal.session(datetime.date(2026, 10, 29)).open_t, datetime.time(10, 0))
        self.assertEqual(cal.session(datetime.date(2026, 10, 30)).source, "default")
        self.assertIsNone(cal.session(datetime.date(2026, 10, 31)))          # 토요일
        again = krx.Calendar.from_doc(cal.to_doc())
        self.assertEqual(again.session(datetime.date(2026, 10, 29)).open_t, datetime.time(10, 0))

    def test_a_weekday_between_business_days_is_closed(self):
        res = {"previousBusinessDay": {"date": "2026-10-08", "integrated": {"regularMarket": {
                   "startTime": "2026-10-08T09:00:00+09:00", "endTime": "2026-10-08T15:30:00+09:00"}}},
               "today": None,
               "nextBusinessDay": {"date": "2026-10-12T00:00:00+09:00", "integrated": {"regularMarket": {
                   "startTime": "2026-10-12T09:00:00+09:00", "endTime": "2026-10-12T15:30:00+09:00"}}}}
        cal = krx.Calendar()
        cal.add_response(res, asked=datetime.date(2026, 10, 9))
        self.assertIsNone(cal.session(datetime.date(2026, 10, 9)))          # 한글날
        self.assertTrue(cal.known(datetime.date(2026, 10, 12)))             # 시각이 붙은 날짜도 읽음
        self.assertEqual(krx.parse_day("20261028"), datetime.date(2026, 10, 28))
        self.assertIsNone(krx.parse_day("10/28/2026"))

    def test_symbols(self):
        self.assertEqual(krx.norm_symbol("A005930"), "005930")
        self.assertEqual(krx.norm_symbol("0101n0"), "0101N0")
        with self.assertRaises(krx.KrxError):
            krx.norm_symbol("TSLA")


class FiveMinuteBars(unittest.TestCase):
    def test_toss_candle_end_time_becomes_bar_start(self):
        end, row = bars.candle_row({"timestamp": "2026-10-28T09:01:00+09:00", "openPrice": "30000",
                                    "highPrice": "30100", "lowPrice": "29950", "closePrice": "30050",
                                    "volume": "1200", "currency": "KRW"})
        self.assertEqual(krx.kst_of(end), datetime.datetime(2026, 10, 28, 9, 1))
        b = bars.rows_to_bars({end: row})[0]
        self.assertEqual(krx.kst_of(b["t"]), datetime.datetime(2026, 10, 28, 9, 0))

    def test_slots_need_all_five_minutes_and_a_finished_clock(self):
        day = datetime.date(2026, 10, 28)
        s = krx.Session(day)
        mk = lambda m, c: {"t": krx.ts_of(datetime.datetime(2026, 10, 28, 9, 0) + datetime.timedelta(minutes=m)),
                           "o": c, "h": c + 50, "l": c - 50, "c": c, "v": 100}
        one = [mk(m, 30000 + 50 * m) for m in range(8)]
        out = bars.session_slots(one, s, 300, datetime.datetime(2026, 10, 28, 9, 8, 30))
        self.assertEqual(len(out), 2)
        self.assertTrue(out[0]["complete"])
        self.assertEqual(out[0]["h"], 30000 + 50 * 4 + 50)
        self.assertEqual(out[0]["c"], 30200)
        self.assertFalse(out[1]["complete"])              # 09:05~09:10 은 3분만 지남
        self.assertEqual(out[1]["n"], 3)



class Pad(unittest.TestCase):
    def test_korean_counts_two_columns(self):
        self.assertEqual(krx.text_width("ORB15 저가손절"), 14)
        self.assertEqual(krx.pad("규칙", 6), "규칙  ")
        self.assertEqual(krx.pad("너무 긴 이름", 4), "너무 긴 이름")


if __name__ == "__main__":
    unittest.main()
