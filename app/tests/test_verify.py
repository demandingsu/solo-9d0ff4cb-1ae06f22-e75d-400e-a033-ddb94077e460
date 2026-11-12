"""实到核对逻辑单元测试：计划还原、配对窗口、一对一与待核实。"""

import unittest
from datetime import date, datetime

from commuter.times import DAILY, WEEKDAY, WEEKEND, StopRecord
from commuter.verify import (
    ActualRecord,
    parse_actual_datetime,
    reconstruct_planned_arrivals,
    verify_arrivals,
)

BOARD = "滨河园区"
ALIGHT = "中央厂区"
# 2026-11-12 是周四；2026-11-14 是周六
THU = date(2026, 11, 12)
SAT = date(2026, 11, 14)


def rec(trip_no, seq, station, clock, kind):
    return StopRecord(trip_no, seq, station, clock, kind)


def act(trip_no, dt_str):
    return ActualRecord(trip_no, datetime.strptime(dt_str, "%Y-%m-%d %H:%M"), dt_str)


class ParseActualDatetimeTests(unittest.TestCase):
    def test_common_formats(self):
        self.assertEqual(
            parse_actual_datetime("2026-11-12 00:30"), datetime(2026, 11, 12, 0, 30)
        )
        self.assertEqual(
            parse_actual_datetime("2026/11/12 00:30"), datetime(2026, 11, 12, 0, 30)
        )
        self.assertEqual(
            parse_actual_datetime("2026-11-12T00:30"), datetime(2026, 11, 12, 0, 30)
        )
        self.assertEqual(
            parse_actual_datetime("2026-11-12 00:30:05"), datetime(2026, 11, 12, 0, 30, 5)
        )

    def test_invalid(self):
        self.assertIsNone(parse_actual_datetime(""))
        self.assertIsNone(parse_actual_datetime("25:10"))
        self.assertIsNone(parse_actual_datetime("昨天半夜"))


class ReconstructTests(unittest.TestCase):
    def test_cross_day_24xx_plan(self):
        # 24:25 → 始发服务日次日 00:25；只还原落在核对日前后窗口内的计划
        board = [rec("N202", 1, BOARD, "23:15", DAILY)]
        alight = [rec("N202", 3, ALIGHT, "24:25", DAILY)]
        plans, problems = reconstruct_planned_arrivals(board, alight, BOARD, ALIGHT, THU)
        self.assertEqual(problems, [])
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].planned_dt, datetime(2026, 11, 12, 0, 25))
        self.assertEqual(plans[0].service_day, date(2026, 11, 11))

    def test_00xx_and_24xx_are_equivalent(self):
        # 00:25（到站早于发车，按次日处理）与 24:25 应还原出同一计划时刻
        board = [rec("A", 1, BOARD, "23:15", DAILY)]
        p24, _ = reconstruct_planned_arrivals(
            board, [rec("A", 3, ALIGHT, "24:25", DAILY)], BOARD, ALIGHT, THU
        )
        p00, _ = reconstruct_planned_arrivals(
            board, [rec("A", 3, ALIGHT, "00:25", DAILY)], BOARD, ALIGHT, THU
        )
        self.assertEqual(
            [p.planned_dt for p in p24], [datetime(2026, 11, 12, 0, 25)]
        )
        self.assertEqual([p.planned_dt for p in p24], [p.planned_dt for p in p00])

    def test_service_kind_filters_service_days(self):
        # 周末班次在工作日（周四）前后没有可还原的计划
        board = [rec("N204", 1, BOARD, "23:50", WEEKEND)]
        alight = [rec("N204", 3, ALIGHT, "01:00", WEEKEND)]
        plans, _ = reconstruct_planned_arrivals(board, alight, BOARD, ALIGHT, THU)
        self.assertEqual(plans, [])

    def test_board_only_trip_reported(self):
        board = [rec("W9", 1, BOARD, "23:00", DAILY)]
        plans, problems = reconstruct_planned_arrivals(board, [], BOARD, ALIGHT, THU)
        self.assertEqual(plans, [])
        self.assertTrue(any("W9" in p and "没有记录" in p for p in problems))

    def test_alight_only_trip_ignored(self):
        # 只经下车站、不经上车站的车次属于其他线路，不算数据问题
        alight = [rec("X1", 2, ALIGHT, "23:30", DAILY)]
        plans, problems = reconstruct_planned_arrivals([], alight, BOARD, ALIGHT, THU)
        self.assertEqual(plans, [])
        self.assertFalse(any("X1" in p for p in problems))

    def test_missing_stop_seq_reported(self):
        board = [rec("W2", None, BOARD, "23:00", DAILY)]
        alight = [rec("W2", 3, ALIGHT, "23:40", DAILY)]
        plans, problems = reconstruct_planned_arrivals(board, alight, BOARD, ALIGHT, THU)
        self.assertEqual(plans, [])
        self.assertTrue(any("W2" in p and "停站顺序" in p for p in problems))

    def test_kind_mismatch_reported(self):
        board = [rec("W3", 1, BOARD, "23:00", WEEKDAY)]
        alight = [rec("W3", 3, ALIGHT, "23:40", DAILY)]
        plans, problems = reconstruct_planned_arrivals(board, alight, BOARD, ALIGHT, THU)
        self.assertEqual(plans, [])
        self.assertTrue(any("W3" in p and "不一致" in p for p in problems))


class MatchTests(unittest.TestCase):
    def setUp(self):
        # N202：23:15 上车，24:25 到中央厂区 → 计划 2026-11-12 00:25
        self.board = [rec("N202", 1, BOARD, "23:15", DAILY)]
        self.alight = [rec("N202", 3, ALIGHT, "24:25", DAILY)]

    def _verify(self, actuals, day=THU):
        return verify_arrivals(
            self.board, self.alight, actuals, BOARD, ALIGHT, day
        )

    def test_confirmed_pair_with_delay(self):
        v = self._verify([act("N202", "2026-11-12 00:30")])
        self.assertEqual(len(v.confirmed), 1)
        pair = v.confirmed[0]
        self.assertEqual(pair.planned.planned_dt, datetime(2026, 11, 12, 0, 25))
        self.assertEqual(pair.actual.actual_dt, datetime(2026, 11, 12, 0, 30))
        self.assertEqual(pair.delay_minutes, 5)
        self.assertEqual(v.unmatched_plans, [])
        self.assertEqual(v.unmatched_actuals, [])

    def test_early_boundary_inclusive(self):
        # 早 15 分钟整仍可配对
        v = self._verify([act("N202", "2026-11-12 00:10")])
        self.assertEqual(len(v.confirmed), 1)
        self.assertEqual(v.confirmed[0].delay_minutes, -15)

    def test_early_beyond_tolerance_unmatched(self):
        # 早 16 分钟：实录无匹配计划，计划未见实到
        v = self._verify([act("N202", "2026-11-12 00:09")])
        self.assertEqual(v.confirmed, [])
        self.assertEqual(len(v.unmatched_actuals), 1)
        self.assertEqual(len(v.unmatched_plans), 1)

    def test_late_boundary_inclusive(self):
        # 晚 120 分钟整仍可配对
        v = self._verify([act("N202", "2026-11-12 02:25")])
        self.assertEqual(len(v.confirmed), 1)
        self.assertEqual(v.confirmed[0].delay_minutes, 120)

    def test_late_beyond_tolerance_unmatched(self):
        v = self._verify([act("N202", "2026-11-12 02:26")])
        self.assertEqual(v.confirmed, [])
        self.assertEqual(len(v.unmatched_actuals), 1)
        self.assertEqual(len(v.unmatched_plans), 1)

    def test_one_plan_two_actuals_is_ambiguous(self):
        v = self._verify([act("N202", "2026-11-12 00:26"),
                          act("N202", "2026-11-12 00:28")])
        self.assertEqual(v.confirmed, [])
        self.assertEqual(len(v.ambiguous_plans), 1)
        self.assertEqual(len(v.ambiguous_plans[0].candidates), 2)
        self.assertEqual(len(v.ambiguous_actuals), 2)

    def test_unmatched_plan_on_verify_date_reported(self):
        v = self._verify([])
        self.assertEqual(len(v.unmatched_plans), 1)
        self.assertEqual(
            v.unmatched_plans[0].planned_dt, datetime(2026, 11, 12, 0, 25)
        )

    def test_late_actual_matches_previous_day_plan(self):
        # 计划 23:30（前一日）晚点 70 分钟，实到落在当日 00:40
        board = [rec("W1", 1, BOARD, "22:30", DAILY)]
        alight = [rec("W1", 2, ALIGHT, "23:30", DAILY)]
        v = verify_arrivals(
            board, alight, [act("W1", "2026-11-12 00:40")], BOARD, ALIGHT, THU
        )
        self.assertEqual(len(v.confirmed), 1)
        self.assertEqual(v.confirmed[0].planned.planned_dt, datetime(2026, 11, 11, 23, 30))
        self.assertEqual(v.confirmed[0].delay_minutes, 70)
        # 当日 23:30 的计划仍无实到，单独列为未见实到
        self.assertEqual(
            [p.planned_dt for p in v.unmatched_plans],
            [datetime(2026, 11, 12, 23, 30)],
        )

    def test_pair_outside_verify_date_not_reported(self):
        # 前一日计划与前一日实到的配对不属于当日核对范围
        board = [rec("W1", 1, BOARD, "22:30", DAILY)]
        alight = [rec("W1", 2, ALIGHT, "23:30", DAILY)]
        v = verify_arrivals(
            board, alight, [act("W1", "2026-11-11 23:33")], BOARD, ALIGHT, THU
        )
        self.assertEqual(v.confirmed, [])
        self.assertEqual(v.unmatched_actuals, [])
        self.assertEqual(
            [p.planned_dt for p in v.unmatched_plans],
            [datetime(2026, 11, 12, 23, 30)],
        )


class AdjacentServiceDayAmbiguityTests(unittest.TestCase):
    """相邻服务日同号班次：一条实到同时落在两个计划的窗口内。"""

    def setUp(self):
        # N210 工作日 23:45 → 25:35；周末 00:20 → 01:30
        self.board = [
            rec("N210", 1, BOARD, "23:45", WEEKDAY),
            rec("N210", 1, BOARD, "00:20", WEEKEND),
        ]
        self.alight = [
            rec("N210", 3, ALIGHT, "25:35", WEEKDAY),
            rec("N210", 3, ALIGHT, "01:30", WEEKEND),
        ]

    def test_two_plans_on_saturday_early_morning(self):
        plans, problems = reconstruct_planned_arrivals(
            self.board, self.alight, BOARD, ALIGHT, SAT
        )
        self.assertEqual(problems, [])
        # 周五（工作日）始发 → 周六 01:35；周六（周末）始发 → 周六 01:30
        self.assertEqual(
            [p.planned_dt for p in plans],
            [datetime(2026, 11, 14, 1, 30), datetime(2026, 11, 14, 1, 35)],
        )
        self.assertEqual(
            {p.service_day for p in plans},
            {date(2026, 11, 13), date(2026, 11, 14)},
        )

    def test_single_actual_is_ambiguous_not_picked_by_order(self):
        v = verify_arrivals(
            self.board, self.alight, [act("N210", "2026-11-14 01:40")],
            BOARD, ALIGHT, SAT,
        )
        # 不能按页面顺序任选一条：没有确定配对，全部待核实
        self.assertEqual(v.confirmed, [])
        self.assertEqual(len(v.ambiguous_actuals), 1)
        self.assertEqual(len(v.ambiguous_actuals[0].candidates), 2)
        self.assertEqual(len(v.ambiguous_plans), 2)
        self.assertEqual(v.unmatched_plans, [])
        self.assertEqual(v.unmatched_actuals, [])

    def test_two_distinct_actuals_still_ambiguous(self):
        # 两条实到都在两个计划窗口内：依然不能确定一一对应
        v = verify_arrivals(
            self.board, self.alight,
            [act("N210", "2026-11-14 01:31"), act("N210", "2026-11-14 01:36")],
            BOARD, ALIGHT, SAT,
        )
        self.assertEqual(v.confirmed, [])
        self.assertEqual(len(v.ambiguous_actuals), 2)
        self.assertEqual(len(v.ambiguous_plans), 2)

    def test_actual_matching_only_one_plan_is_confirmed(self):
        # 01:16 距 01:30 计划 -14 分钟（可配），距 01:35 计划 -19 分钟（超出
        # 提前 15 分钟窗口），因此只与周末计划唯一配对
        v = verify_arrivals(
            self.board, self.alight, [act("N210", "2026-11-14 01:16")],
            BOARD, ALIGHT, SAT,
        )
        self.assertEqual(len(v.confirmed), 1)
        self.assertEqual(v.confirmed[0].planned.planned_dt, datetime(2026, 11, 14, 1, 30))
        self.assertEqual(v.confirmed[0].delay_minutes, -14)
        # 另一个计划（01:35）未见实到
        self.assertEqual(
            [p.planned_dt for p in v.unmatched_plans],
            [datetime(2026, 11, 14, 1, 35)],
        )


if __name__ == "__main__":
    unittest.main()
