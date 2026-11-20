"""Turns stored punches into daily attendance and monthly summaries."""
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import select, delete
from sqlalchemy.orm import Session

from attendance.rules import ShiftRule, evaluate_month, MonthSummary
from models import Employee, Shift, AttendanceLog, Holiday, Leave, DailyAttendance, PayrollRun
from services.policy import weekend_set

DEFAULT_SHIFT = ShiftRule()


def month_bounds(year: int, month: int) -> tuple[date, date]:
    first = date(year, month, 1)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return first, nxt - timedelta(days=1)


def shift_rule(s: Shift | None) -> ShiftRule:
    if s is None:
        return DEFAULT_SHIFT
    return ShiftRule(code=s.code, start=s.start, end=s.end, grace_minutes=s.grace_minutes,
                     break_minutes=s.break_minutes, break_after_minutes=s.break_after_minutes,
                     early_window_minutes=s.early_window_minutes, ot_round_minutes=s.ot_round_minutes,
                     max_ot_minutes=s.max_ot_minutes, offday_work_is_ot=s.offday_work_is_ot)


def final_company_ids(db: Session, year: int, month: int) -> set[int]:
    return set(db.scalars(select(PayrollRun.company_id).where(PayrollRun.year == year, PayrollRun.month == month,
                                                              PayrollRun.status == "FINAL")))


def employees_for_month(db: Session, first: date, last: date, company_ids=None) -> list[Employee]:
    """Everyone employed at any point in the month (includes people who left mid-month)."""
    stmt = select(Employee).where(
        (Employee.joined_on.is_(None)) | (Employee.joined_on <= last),
        (Employee.left_on.is_(None)) | (Employee.left_on >= first),
    ).order_by(Employee.employee_code)
    if company_ids is not None:
        stmt = stmt.where(Employee.company_id.in_(list(company_ids) or [0]))
    return [e for e in db.scalars(stmt) if e.employment_status != "INACTIVE" or e.left_on]


def process_month(db: Session, year: int, month: int, company_ids=None, save: bool = True) -> dict[int, MonthSummary]:
    """company_ids: None = all companies. Companies whose payroll for the month is FINAL are skipped (locked)."""
    first, last = month_bounds(year, month)
    locked = final_company_ids(db, year, month)
    employees = [e for e in employees_for_month(db, first, last, company_ids) if e.company_id not in locked]
    all_shifts = list(db.scalars(select(Shift)))
    shifts = {s.id: shift_rule(s) for s in all_shifts}
    # employees without a shift use the GENERAL shift (created on first start, editable on the Shifts page)
    default = next((shift_rule(s) for s in all_shifts if s.code == "GENERAL"), DEFAULT_SHIFT)
    weekend = weekend_set(db)
    # One query for all punches; widen by a day each side for night shifts.
    lo = datetime.combine(first - timedelta(days=1), datetime.min.time())
    hi = datetime.combine(last + timedelta(days=2), datetime.min.time())
    punches: dict[str, list[datetime]] = defaultdict(list)
    for card, t in db.execute(select(AttendanceLog.card_no, AttendanceLog.punch_time)
                              .where(AttendanceLog.punch_time >= lo, AttendanceLog.punch_time < hi)):
        punches[card].append(t)

    holidays = {h.day for h in db.scalars(select(Holiday).where(Holiday.day >= first, Holiday.day <= last))}
    leave_map: dict[int, dict[date, str]] = defaultdict(dict)
    for lv in db.scalars(select(Leave).where(Leave.start_date <= last, Leave.end_date >= first)):
        d = max(lv.start_date, first)
        while d <= min(lv.end_date, last):
            leave_map[lv.employee_id][d] = "PAID" if lv.paid else "UNPAID"
            d += timedelta(days=1)

    results: dict[int, MonthSummary] = {}
    for e in employees:
        rule = shifts.get(e.shift_id, default)
        results[e.id] = evaluate_month(year, month, punches.get(e.card_no, []), rule,
                                       weekend, holidays, leave_map.get(e.id),
                                       e.joined_on, e.left_on)

    if save:
        ids = list(results)
        for i in range(0, len(ids), 500):   # SQL Server limits parameters per statement
            db.execute(delete(DailyAttendance).where(DailyAttendance.work_date >= first,
                                                     DailyAttendance.work_date <= last,
                                                     DailyAttendance.employee_id.in_(ids[i:i + 500])))
        rows = []
        for emp_id, summary in results.items():
            for d in summary.days:
                if d.status == "NOT_EMPLOYED":
                    continue
                rows.append(DailyAttendance(
                    employee_id=emp_id, work_date=d.work_date, shift_code=d.shift_code,
                    day_type=d.day_type, status=d.status, in_time=d.in_time, out_time=d.out_time,
                    punch_count=d.punch_count, late_minutes=d.late_minutes,
                    early_leave_minutes=d.early_leave_minutes, worked_minutes=d.worked_minutes,
                    ot_minutes=d.ot_minutes, extra_ot_minutes=d.extra_ot_minutes))
        db.add_all(rows)
        db.commit()
    return results
