"""核心时刻逻辑单元测试：跨日时刻、服务日、配对筛选。"""

import unittest
from datetime import datetime

from commuter.times import (
    DAILY,
    WEEKDAY,
    WEEKEND,
    StopRecord,
    determine_last_trip,
    parse_clock,
    service_kind_for_day,
    service_runs_on,
)


def rec(trip_no, seq, station, clock, kind):
    return StopRecord(trip_no, seq, station, clock, kind)


class ClockTests(unittest.TestCase):
    def test_normal_and_overmidnight(self):
        from datetime import timedelta

        self.assertEqual(parse_clock("23:40"), timedelta(hours=23, minutes=40))
        self.assertEqual(parse_clock("24:25"), timedelta(hours=24, minutes=25))
        self.assertEqual(parse_clock("25:10"), timedelta(hours=25, minutes=10))

    def test_bad_clock(self):
        self.assertIsNone(parse_clock(""))
        self.assertIsNone(parse_clock("abc"))
        self.assertIsNone(parse_clock("48:00"))
        self.assertIsNone(parse_clock("12:60"))


class ServiceDayTests(unittest.TestCase):
    def test_weekday_weekend_split(self):
        # 2026-11-11 是周三，2026-11-14 是周六
        self.assertEqual(service_kind_for_day(datetime(2026, 11, 11).date()), WEEKDAY)
        self.assertEqual(service_kind_for_day(datetime(2026, 11, 14).date()), WEEKEND)

    def test_runs_on(self):
        sat = datetime(2026, 11, 14).date()
        wed = datetime(2026, 11, 11).date()
        self.assertTrue(service_runs_on(DAILY, sat))
        self.assertTrue(service_runs_on(WEEKEND, sat))
        self.assertFalse(service_runs_on(WEEKDAY, sat))
        self.assertTrue(service_runs_on(WEEKDAY, wed))


class DetermineTests(unittest.TestCase):
    BOARD = "滨河园区"
    ALIGHT = "中央厂区"

    def setUp(self):
        self.board = [
            rec("N201", 1, self.BOARD, "22:30", WEEKDAY),
            rec("N202", 1, self.BOARD, "23:15", DAILY),
            rec("N203", 1, self.BOARD, "24:00", WEEKDAY),
            rec("N204", 1, self.BOARD, "23:50", WEEKEND),
        ]
        self.alight = [
            rec("N201", 3, self.ALIGHT, "23:40", WEEKDAY),
            rec("N202", 3, self.ALIGHT, "24:25", DAILY),
            rec("N203", 3, self.ALIGHT, "25:10", WEEKDAY),
            rec("N204", 3, self.ALIGHT, "01:00", WEEKEND),
        ]
        # 周三 22:00 出门，周四 02:00 前到岗
        self.earliest = datetime(2026, 11, 11, 22, 0)
        self.deadline = datetime(2026, 11, 12, 2, 0)

    def test_latest_is_overmidnight_weekday_trip(self):
        d = determine_last_trip(
            self.board, self.alight, self.BOARD, self.ALIGHT,
            self.earliest, self.deadline,
        )
        self.assertIsNotNone(d.latest)
        self.assertEqual(d.latest.trip_no, "N203")
        # 24:00 换算为次日 00:00，25:10 换算为次日 01:10
        self.assertEqual(d.latest.depart_dt, datetime(2026, 11, 12, 0, 0))
        self.assertEqual(d.latest.arrive_dt, datetime(2026, 11, 12, 1, 10))
        # 周末班次 N204 在周三不运行，不应出现
        self.assertNotIn("N204", [o.trip_no for o in d.options])

    def test_weekend_service_day(self):
        # 周六 22:00 出门，周日 02:00 截止：只有 N202(每日) 与 N204(周末)
        d = determine_last_trip(
            self.board, self.alight, self.BOARD, self.ALIGHT,
            datetime(2026, 11, 14, 22, 0), datetime(2026, 11, 15, 2, 0),
        )
        trip_nos = [o.trip_no for o in d.options]
        self.assertNotIn("N201", trip_nos)
        self.assertNotIn("N203", trip_nos)
        self.assertEqual(d.latest.trip_no, "N204")
        self.assertEqual(d.latest.service_day, datetime(2026, 11, 14).date())

    def test_earliest_depart_filter(self):
        # 23:30 才出门：22:30 的 N201 被排除
        d = determine_last_trip(
            self.board, self.alight, self.BOARD, self.ALIGHT,
            datetime(2026, 11, 11, 23, 30), self.deadline,
        )
        self.assertNotIn("N201", [o.trip_no for o in d.options])

    def test_latest_arrive_filter(self):
        # 截止 00:30：N202 到站 00:25 可以，N203 到站 01:10 不行
        d = determine_last_trip(
            self.board, self.alight, self.BOARD, self.ALIGHT,
            self.earliest, datetime(2026, 11, 12, 0, 30),
        )
        self.assertEqual(d.latest.trip_no, "N202")

    def test_missing_clock_record_reported(self):
        self.alight.append(rec("N206", 3, self.ALIGHT, "", DAILY))
        self.board.append(rec("N206", 1, self.BOARD, "25:30", DAILY))
        d = determine_last_trip(
            self.board, self.alight, self.BOARD, self.ALIGHT,
            self.earliest, self.deadline,
        )
        self.assertTrue(any("N206" in p and "关键时刻" in p for p in d.problems))
        self.assertNotIn("N206", [o.trip_no for o in d.options])

    def test_wrong_stop_order_rejected(self):
        self.board.append(rec("N300", 5, self.BOARD, "23:00", DAILY))
        self.alight.append(rec("N300", 2, self.ALIGHT, "23:30", DAILY))
        d = determine_last_trip(
            self.board, self.alight, self.BOARD, self.ALIGHT,
            self.earliest, self.deadline,
        )
        self.assertTrue(any("N300" in p for p in d.problems))
        self.assertIsNone(next((o for o in d.options if o.trip_no == "N300"), None))

    def test_no_match_returns_none(self):
        d = determine_last_trip(
            self.board, self.alight, self.BOARD, self.ALIGHT,
            datetime(2026, 11, 11, 6, 0), datetime(2026, 11, 11, 7, 0),
        )
        self.assertIsNone(d.latest)
        self.assertEqual(d.options, [])

    def test_plain_midnight_clock_rolled_to_next_day(self):
        # 23:50 发车、01:00 到站（未用 25:00 书写）应推断为次日 01:00
        board = [rec("W1", 1, self.BOARD, "23:50", DAILY)]
        alight = [rec("W1", 2, self.ALIGHT, "01:00", DAILY)]
        d = determine_last_trip(
            board, alight, self.BOARD, self.ALIGHT,
            datetime(2026, 11, 11, 22, 0), datetime(2026, 11, 12, 3, 0),
        )
        self.assertEqual(d.latest.depart_dt, datetime(2026, 11, 11, 23, 50))
        self.assertEqual(d.latest.arrive_dt, datetime(2026, 11, 12, 1, 0))

    def test_trip_missing_at_alight_reported(self):
        board = [rec("W9", 1, self.BOARD, "23:00", DAILY)]
        alight = [rec("W1", 2, self.ALIGHT, "23:30", DAILY)]
        d = determine_last_trip(
            board, alight, self.BOARD, self.ALIGHT,
            self.earliest, self.deadline,
        )
        self.assertTrue(any("W9" in p and "没有记录" in p for p in d.problems))


if __name__ == "__main__":
    unittest.main()
