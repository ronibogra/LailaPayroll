"""Attendance rules engine (pure Python, no database).

Turns raw machine punches into one result per employee per work day:
in/out time, status, late minutes, worked minutes and overtime.

Night shifts: every work day owns a 24-hour window that starts
`early_window_minutes` before the shift start. A night-shift worker who
punches in at 20:00 and out at 05:00 next morning gets one work day,
not two half days.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Iterable

# Day types
WORKDAY, WEEKEND, HOLIDAY = "WORKDAY", "WEEKEND", "HOLIDAY"

# Statuses
PRESENT = "PRESENT"
LATE = "LATE"
MISSING_OUT = "MISSING_OUT"      # only one punch - HR should check
ABSENT = "ABSENT"
LEAVE_PAID = "LEAVE_PAID"
LEAVE_UNPAID = "LEAVE_UNPAID"
OFFDAY = "OFFDAY"                # weekend / holiday, not worked
OFFDAY_WORK = "OFFDAY_WORK"      # worked on weekend / holiday
NOT_EMPLOYED = "NOT_EMPLOYED"    # before joining / after leaving

PAYABLE = {PRESENT, LATE, MISSING_OUT, LEAVE_PAID, OFFDAY, OFFDAY_WORK}
PRESENT_LIKE = {PRESENT, LATE, MISSING_OUT, OFFDAY_WORK}


@dataclass(frozen=True)
class ShiftRule:
    code: str = "GENERAL"
    start: time = time(8, 0)
    end: time = time(17, 0)
    grace_minutes: int = 10           # in after start+grace = late
    break_minutes: int = 60           # unpaid meal break
    break_after_minutes: int = 300    # break only deducted when span > 5h
    early_window_minutes: int = 180   # work-day window opens 3h before start
    ot_round_minutes: int = 30        # OT counted in full blocks (rounded down)
    max_ot_minutes: int = 120         # compliance OT per day; the rest is "extra OT"
    duplicate_punch_minutes: int = 2  # punches closer than this are one punch
    offday_work_is_ot: bool = True    # all hours on weekend/holiday count as OT

    @property
    def overnight(self) -> bool:
        return self.end <= self.start

    def start_dt(self, day: date) -> datetime:
        return datetime.combine(day, self.start)

    def end_dt(self, day: date) -> datetime:
        end = datetime.combine(day, self.end)
        return end + timedelta(days=1) if self.overnight else end

    def window(self, day: date) -> tuple[datetime, datetime]:
        opens = self.start_dt(day) - timedelta(minutes=self.early_window_minutes)
        return opens, opens + timedelta(days=1)

    def work_day_of(self, punch: datetime) -> date:
        """Which work day a punch belongs to."""
        for day in (punch.date() + timedelta(days=1), punch.date(), punch.date() - timedelta(days=1)):
            opens, closes = self.window(day)
            if opens <= punch < closes:
                return day
        raise ValueError(f"punch {punch} fits no window")  # unreachable: windows tile time


@dataclass
class DayResult:
    work_date: date
    status: str
    in_time: datetime | None = None
    out_time: datetime | None = None
    punch_count: int = 0
    late_minutes: int = 0
    early_leave_minutes: int = 0
    worked_minutes: int = 0
    ot_minutes: int = 0
    extra_ot_minutes: int = 0
    shift_code: str = ""
    day_type: str = WORKDAY

    @property
    def payable(self) -> bool:
        return self.status in PAYABLE


def _minutes(delta: timedelta) -> int:
    return max(0, int(delta.total_seconds() // 60))


def _floor_block(minutes: int, block: int) -> int:
    return (minutes // block) * block if block > 0 else minutes


def dedupe_punches(punches: Iterable[datetime], within_minutes: int) -> list[datetime]:
    out: list[datetime] = []
    for p in sorted(punches):
        if out and (p - out[-1]) < timedelta(minutes=within_minutes):
            continue
        out.append(p)
    return out


def group_by_work_day(punches: Iterable[datetime], shift: ShiftRule) -> dict[date, list[datetime]]:
    days: dict[date, list[datetime]] = {}
    for p in punches:
        days.setdefault(shift.work_day_of(p), []).append(p)
    return days


def evaluate_day(work_date: date, punches: list[datetime], shift: ShiftRule,
                 day_type: str = WORKDAY, leave: str | None = None,
                 employed: bool = True) -> DayResult:
    """leave: None, "PAID" or "UNPAID"."""
    res = DayResult(work_date=work_date, status=ABSENT, shift_code=shift.code, day_type=day_type)
    if not employed:
        res.status = NOT_EMPLOYED
        return res
    punches = dedupe_punches(punches, shift.duplicate_punch_minutes)
    res.punch_count = len(punches)

    if not punches:
        if day_type != WORKDAY:
            res.status = OFFDAY
        elif leave == "PAID":
            res.status = LEAVE_PAID
        elif leave == "UNPAID":
            res.status = LEAVE_UNPAID
        else:
            res.status = ABSENT
        return res

    res.in_time = punches[0]
    if len(punches) == 1:
        # One punch: present but no out time, so no worked hours or OT until HR fixes it.
        res.status = MISSING_OUT if day_type == WORKDAY else OFFDAY_WORK
        if day_type == WORKDAY:
            late = _minutes(res.in_time - shift.start_dt(work_date))
            if late > shift.grace_minutes:
                res.late_minutes = late
        return res

    res.out_time = punches[-1]
    span = _minutes(res.out_time - res.in_time)
    worked = span - (shift.break_minutes if span > shift.break_after_minutes else 0)
    res.worked_minutes = max(0, worked)

    if day_type == WORKDAY:
        late = _minutes(res.in_time - shift.start_dt(work_date))
        res.late_minutes = late if late > shift.grace_minutes else 0
        res.early_leave_minutes = _minutes(shift.end_dt(work_date) - res.out_time)
        raw_ot = _minutes(res.out_time - shift.end_dt(work_date))
        res.status = LATE if res.late_minutes else PRESENT
    else:
        raw_ot = res.worked_minutes if shift.offday_work_is_ot else 0
        res.status = OFFDAY_WORK

    ot = _floor_block(raw_ot, shift.ot_round_minutes)
    res.ot_minutes = min(ot, shift.max_ot_minutes)
    res.extra_ot_minutes = ot - res.ot_minutes
    return res


@dataclass
class MonthSummary:
    days_in_month: int = 0
    employed_days: int = 0
    present_days: int = 0
    late_days: int = 0
    late_minutes: int = 0
    missing_out_days: int = 0
    absent_days: int = 0
    paid_leave_days: int = 0
    unpaid_leave_days: int = 0
    offdays: int = 0
    offday_work_days: int = 0
    ot_minutes: int = 0
    extra_ot_minutes: int = 0
    days: list[DayResult] = field(default_factory=list)

    @property
    def payable_days(self) -> int:
        return self.employed_days - self.absent_days - self.unpaid_leave_days


def summarize(days: list[DayResult]) -> MonthSummary:
    s = MonthSummary(days_in_month=len(days), days=days)
    for d in days:
        if d.status == NOT_EMPLOYED:
            continue
        s.employed_days += 1
        if d.status in PRESENT_LIKE:
            s.present_days += 1
        if d.late_minutes:
            s.late_days += 1
            s.late_minutes += d.late_minutes
        s.missing_out_days += d.status == MISSING_OUT
        s.absent_days += d.status == ABSENT
        s.paid_leave_days += d.status == LEAVE_PAID
        s.unpaid_leave_days += d.status == LEAVE_UNPAID
        s.offdays += d.status == OFFDAY
        s.offday_work_days += d.status == OFFDAY_WORK
        s.ot_minutes += d.ot_minutes
        s.extra_ot_minutes += d.extra_ot_minutes
    return s


def evaluate_month(year: int, month: int, punches: Iterable[datetime], shift: ShiftRule,
                   weekend_days: set[int], holidays: set[date],
                   leave_by_date: dict[date, str] | None = None,
                   joined_on: date | None = None, left_on: date | None = None) -> MonthSummary:
    """weekend_days uses Python weekday numbers (Mon=0 ... Fri=4 ... Sun=6)."""
    leave_by_date = leave_by_date or {}
    first = date(year, month, 1)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    grouped = group_by_work_day(punches, shift)
    days: list[DayResult] = []
    d = first
    while d < nxt:
        if d in holidays:
            day_type = HOLIDAY
        elif d.weekday() in weekend_days:
            day_type = WEEKEND
        else:
            day_type = WORKDAY
        employed = (joined_on is None or d >= joined_on) and (left_on is None or d <= left_on)
        days.append(evaluate_day(d, grouped.get(d, []), shift, day_type, leave_by_date.get(d), employed))
        d += timedelta(days=1)
    return summarize(days)
