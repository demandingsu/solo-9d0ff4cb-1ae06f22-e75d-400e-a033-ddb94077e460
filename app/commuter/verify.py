"""实到核对：按时刻表还原计划到站时刻，并与到站实录做一对一配对。

该模块不依赖网络，方便单元测试。分页抓取由 :mod:`commuter.scraper` 负责。

核对规则：
  * 计划到站时刻按两站停站顺序与始发服务日还原，兼容 00:xx / 24:xx 跨日写法；
  * 实到允许比计划早 :data:`EARLY_TOLERANCE`（15 分钟）、晚
    :data:`LATE_TOLERANCE`（120 分钟）；
  * 一对一配对：某条实录或某个计划若存在多种配对可能，整组标记“待核实”，
    绝不按页面顺序任选一条。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable

from .times import StopRecord, service_runs_on

# 实到相对计划的允许偏差：最早提前 15 分钟，最晚晚点 120 分钟
EARLY_TOLERANCE = timedelta(minutes=15)
LATE_TOLERANCE = timedelta(minutes=120)

_ACTUAL_DT_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
)


def parse_actual_datetime(text: str) -> datetime | None:
    """把实录中的实际到站时间解析为 ``datetime``，无法解析时返回 ``None``。"""
    if not text:
        return None
    normalized = " ".join(str(text).split())
    for fmt in _ACTUAL_DT_FORMATS:
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    return None


@dataclass
class ActualRecord:
    """到站实录中的一条记录（只有车次号与实际到站日期时间）。"""

    trip_no: str
    actual_dt: datetime
    raw: str = ""           # 原始时间文本
    source_url: str = ""    # 抓取到该记录的实录分页地址


@dataclass
class PlannedArrival:
    """还原出的一个计划到站时刻（某车次 + 某个始发服务日）。"""

    trip_no: str
    service_kind: str
    service_day: date
    planned_dt: datetime    # 计划到站的真实日期时间（已做跨日换算）
    board_clock: str
    alight_clock: str
    board_seq: int
    alight_seq: int
    board_source: str = ""
    alight_source: str = ""


@dataclass
class ConfirmedPair:
    """一对一确定配对：计划班次 ↔ 实录记录。"""

    planned: PlannedArrival
    actual: ActualRecord
    delay_minutes: int      # 实到 - 计划；正数晚点，负数提前


@dataclass
class AmbiguousActual:
    """存在多个候选计划的实录记录（待核实）。"""

    actual: ActualRecord
    candidates: list[PlannedArrival]


@dataclass
class AmbiguousPlan:
    """存在多个候选实录的计划班次（待核实）。"""

    planned: PlannedArrival
    candidates: list[ActualRecord]


@dataclass
class Verification:
    """实到核对结果。"""

    confirmed: list[ConfirmedPair] = field(default_factory=list)
    ambiguous_actuals: list[AmbiguousActual] = field(default_factory=list)
    ambiguous_plans: list[AmbiguousPlan] = field(default_factory=list)
    unmatched_plans: list[PlannedArrival] = field(default_factory=list)   # 当日计划未见实到
    unmatched_actuals: list[ActualRecord] = field(default_factory=list)   # 当日实录无匹配计划
    problems: list[str] = field(default_factory=list)


def _station_key(name: str) -> str:
    return "".join(str(name).split())


def reconstruct_planned_arrivals(
    board_records: Iterable[StopRecord],
    alight_records: Iterable[StopRecord],
    board_station: str,
    alight_station: str,
    verify_date: date,
) -> tuple[list[PlannedArrival], list[str]]:
    """按两站停站顺序与始发服务日还原计划到站时刻。

    只保留计划到站落在 ``[verify_date 前一日 22:00, verify_date 次日 00:15)``
    内的班次——只有这些班次的实到（提前 ≤15 分钟 / 晚点 ≤120 分钟）可能
    落在 ``verify_date`` 当天。为此始发服务日最多向前取 3 天（时刻表允许
    最晚 47:59 的跨日写法）、向后取 1 天。

    返回 ``(计划列表, 数据问题列表)``。同一车次在同一站出现多条记录时
    全部参与组合，不按页面顺序取舍；组合得到的重复计划按
    ``(车次号, 始发服务日, 计划到站时刻)`` 去重。
    """
    board_key, alight_key = _station_key(board_station), _station_key(alight_station)
    board_map: dict[str, list[StopRecord]] = {}
    alight_map: dict[str, list[StopRecord]] = {}
    for rec in board_records:
        if _station_key(rec.station) == board_key:
            board_map.setdefault(rec.trip_no, []).append(rec)
    for rec in alight_records:
        if _station_key(rec.station) == alight_key:
            alight_map.setdefault(rec.trip_no, []).append(rec)

    problems: list[str] = []
    if not board_map:
        problems.append(f"上车站「{board_station}」没有解析到任何车次记录。")
    if not alight_map:
        problems.append(f"下车站「{alight_station}」没有解析到任何车次记录。")
    # 上车站能看到、下车站却没有记录的车次，无法还原其计划到站
    for trip_no in sorted(set(board_map) - set(alight_map)):
        problems.append(
            f"车次 {trip_no} 在「{alight_station}」站时刻表中没有记录，无法还原该班次的计划到站。"
        )

    day_zero = datetime.combine(verify_date, datetime.min.time())
    window_start = day_zero - LATE_TOLERANCE
    window_end = day_zero + timedelta(days=1) + EARLY_TOLERANCE
    service_days = [verify_date + timedelta(days=d) for d in range(-3, 2)]

    planned: dict[tuple[str, date, datetime], PlannedArrival] = {}
    for trip_no in sorted(set(board_map) & set(alight_map)):
        combo_ok = False
        reasons: list[str] = []
        for b_rec in board_map[trip_no]:
            for a_rec in alight_map[trip_no]:
                if b_rec.stop_seq is None or a_rec.stop_seq is None:
                    reasons.append("缺少停站顺序")
                    continue
                if b_rec.stop_seq >= a_rec.stop_seq:
                    reasons.append(
                        f"停站顺序不满足「{board_station}」先于「{alight_station}」"
                    )
                    continue
                if b_rec.service_kind != a_rec.service_kind:
                    reasons.append("两站页面标注的服务日类型不一致")
                    continue
                b_delta, a_delta = b_rec.clock_delta(), a_rec.clock_delta()
                if b_delta is None or a_delta is None:
                    reasons.append("缺少关键时刻")
                    continue
                # 00:xx 到站早于上车发车且未用 24:xx 书写时，按次日处理
                if a_delta < b_delta:
                    a_delta += timedelta(hours=24)
                if a_delta < b_delta:
                    reasons.append("两站时刻矛盾")
                    continue
                combo_ok = True
                for service_day in service_days:
                    if not service_runs_on(b_rec.service_kind, service_day):
                        continue
                    base = datetime.combine(service_day, datetime.min.time())
                    planned_dt = base + a_delta
                    if not (window_start <= planned_dt < window_end):
                        continue
                    key = (trip_no, service_day, planned_dt)
                    planned.setdefault(
                        key,
                        PlannedArrival(
                            trip_no=trip_no,
                            service_kind=b_rec.service_kind,
                            service_day=service_day,
                            planned_dt=planned_dt,
                            board_clock=b_rec.clock.strip(),
                            alight_clock=a_rec.clock.strip(),
                            board_seq=b_rec.stop_seq,
                            alight_seq=a_rec.stop_seq,
                            board_source=b_rec.source_url,
                            alight_source=a_rec.source_url,
                        ),
                    )
        if not combo_ok:
            uniq = "、".join(sorted(set(reasons))) or "时刻数据不完整"
            problems.append(f"车次 {trip_no} 无法还原计划到站（{uniq}），已跳过。")

    ordered = sorted(planned.values(), key=lambda p: (p.planned_dt, p.trip_no))
    return ordered, problems


def verify_arrivals(
    board_records: Iterable[StopRecord],
    alight_records: Iterable[StopRecord],
    actual_records: Iterable[ActualRecord],
    board_station: str,
    alight_station: str,
    verify_date: date,
) -> Verification:
    """把当日到站实录与还原出的计划到站做一对一配对。

    配对窗口为「计划到站 -15 分钟 ~ +120 分钟」。某条实录与某个计划只有在
    彼此都是对方唯一候选时才算确定配对；凡是有多种配对可能的实录 / 计划
    全部进入“待核实”，不按页面顺序任选一条。

    报告范围以 ``verify_date`` 为准：确定配对与待核实组合只要涉及当日计划
    或当日实录即列出；未见实到的计划只列出计划到站在当日的；无匹配计划的
    实录只列出实到时间在当日的（前一日深夜计划、其他日期的实录属于对应
    日期的核对范围，这里不重复下结论）。
    """
    planned, problems = reconstruct_planned_arrivals(
        board_records, alight_records, board_station, alight_station, verify_date
    )
    actuals = list(actual_records)

    plan_matches: list[list[int]] = [[] for _ in planned]
    actual_matches: list[list[int]] = [[] for _ in actuals]
    for pi, plan in enumerate(planned):
        for ai, actual in enumerate(actuals):
            if actual.trip_no != plan.trip_no:
                continue
            delta = actual.actual_dt - plan.planned_dt
            if -EARLY_TOLERANCE <= delta <= LATE_TOLERANCE:
                plan_matches[pi].append(ai)
                actual_matches[ai].append(pi)

    result = Verification(problems=problems)

    def on_date_plan(p: PlannedArrival) -> bool:
        return p.planned_dt.date() == verify_date

    def on_date_actual(a: ActualRecord) -> bool:
        return a.actual_dt.date() == verify_date

    confirmed_pi: set[int] = set()
    confirmed_ai: set[int] = set()
    for pi, plan in enumerate(planned):
        ais = plan_matches[pi]
        if len(ais) != 1 or actual_matches[ais[0]] != [pi]:
            continue
        ai = ais[0]
        actual = actuals[ai]
        if not (on_date_plan(plan) or on_date_actual(actual)):
            continue
        delay = actual.actual_dt - plan.planned_dt
        result.confirmed.append(
            ConfirmedPair(
                planned=plan,
                actual=actual,
                delay_minutes=int(round(delay.total_seconds() / 60)),
            )
        )
        confirmed_pi.add(pi)
        confirmed_ai.add(ai)

    for pi, plan in enumerate(planned):
        if pi in confirmed_pi:
            continue
        candidates = [actuals[ai] for ai in plan_matches[pi]]
        if not candidates:
            if on_date_plan(plan):
                result.unmatched_plans.append(plan)
            continue
        if on_date_plan(plan) or any(on_date_actual(a) for a in candidates):
            result.ambiguous_plans.append(AmbiguousPlan(planned=plan, candidates=candidates))

    for ai, actual in enumerate(actuals):
        if ai in confirmed_ai:
            continue
        candidates = [planned[pi] for pi in actual_matches[ai]]
        if not candidates:
            if on_date_actual(actual):
                result.unmatched_actuals.append(actual)
            continue
        if on_date_actual(actual) or any(on_date_plan(p) for p in candidates):
            result.ambiguous_actuals.append(
                AmbiguousActual(actual=actual, candidates=candidates)
            )

    result.confirmed.sort(key=lambda c: (c.planned.planned_dt, c.planned.trip_no))
    result.ambiguous_plans.sort(key=lambda ap: (ap.planned.planned_dt, ap.planned.trip_no))
    result.ambiguous_actuals.sort(key=lambda aa: (aa.actual.actual_dt, aa.actual.trip_no))
    result.unmatched_plans.sort(key=lambda p: (p.planned_dt, p.trip_no))
    result.unmatched_actuals.sort(key=lambda a: (a.actual_dt, a.trip_no))
    return result
