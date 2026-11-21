"""Turns stored punches into daily attendance and monthly summaries."""
import logging
import threading
import time as _time
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import select, delete
from sqlalchemy.orm import Session

from attendance.rules import ShiftRule, evaluate_month, MonthSummary
from dataclasses import replace

from models import Employee, Shift, AttendanceLog, Holiday, Leave, DailyAttendance, PayrollRun, SpecialHoliday
from services.policy import weekend_set, load_policy

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
                     max_ot_minutes=s.max_ot_minutes, offday_work_is_ot=s.offday_work_is_ot,
                     late_count_from=s.late_count_from or "START")


def parse_days(raw: str | None) -> set[int] | None:
    """'4' -> {4}; '5,6' -> {5, 6}; 'NONE' -> set() (works every day); '' -> None (not set)."""
    raw = (raw or "").strip().upper()
    if not raw:
        return None
    if raw == "NONE":
        return set()
    return {int(x) for x in raw.split(",") if x.strip().isdigit() and 0 <= int(x) <= 6}


def weekly_off_for(e, shift_off: str | None, default: set[int]) -> set[int]:
    """Employee's own weekly off day(s) first, then the shift's, then the Settings default."""
    for raw in (getattr(e, "weekly_off", None), shift_off):
        days = parse_days(raw)
        if days is not None:
            return days
    return default


def holiday_applies(h: SpecialHoliday, e: Employee) -> bool:
    v = (h.scope_value or "").strip()
    if h.scope == "COMPANY":
        return v.isdigit() and e.company_id == int(v)
    if h.scope == "DEPARTMENT":
        return v.isdigit() and e.department_id == int(v)
    if h.scope == "EMPLOYEES":
        codes = {c.strip().lower() for c in v.replace("\n", ",").split(",") if c.strip()}
        return e.employee_code.lower() in codes or e.card_no.lower() in codes
    return False


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


_lock = threading.RLock()       # manual and automatic processing never run at the same time


def process_month(db: Session, year: int, month: int, company_ids=None, save: bool = True) -> dict[int, MonthSummary]:
    """company_ids: None = all companies. Companies whose payroll for the month is FINAL are skipped (locked)."""
    with _lock:
        return _process_month(db, year, month, company_ids, save)


def _process_month(db: Session, year: int, month: int, company_ids=None, save: bool = True) -> dict[int, MonthSummary]:
    first, last = month_bounds(year, month)
    locked = final_company_ids(db, year, month)
    employees = [e for e in employees_for_month(db, first, last, company_ids) if e.company_id not in locked]
    all_shifts = list(db.scalars(select(Shift)))
    shifts = {s.id: shift_rule(s) for s in all_shifts}
    # employees without a shift use the GENERAL shift (created on first start, editable on the Shifts page)
    default = next((shift_rule(s) for s in all_shifts if s.code == "GENERAL"), DEFAULT_SHIFT)
    weekend = weekend_set(db)
    shift_off = {s.id: s.weekly_off for s in all_shifts}
    # One query for all punches; widen by a day each side for night shifts.
    lo = datetime.combine(first - timedelta(days=1), datetime.min.time())
    hi = datetime.combine(last + timedelta(days=2), datetime.min.time())
    punches: dict[str, list[datetime]] = defaultdict(list)
    for card, t in db.execute(select(AttendanceLog.card_no, AttendanceLog.punch_time)
                              .where(AttendanceLog.punch_time >= lo, AttendanceLog.punch_time < hi)):
        punches[card].append(t)

    holidays = {h.day for h in db.scalars(select(Holiday).where(Holiday.day >= first, Holiday.day <= last))}
    specials = list(db.scalars(select(SpecialHoliday).where(SpecialHoliday.day >= first, SpecialHoliday.day <= last)))
    ot_on = {}
    leave_map: dict[int, dict[date, str]] = defaultdict(dict)
    for lv in db.scalars(select(Leave).where(Leave.start_date <= last, Leave.end_date >= first,
                                             Leave.status == "APPROVED")):
        d = max(lv.start_date, first)
        while d <= min(lv.end_date, last):
            leave_map[lv.employee_id][d] = "PAID" if lv.paid else "UNPAID"
            d += timedelta(days=1)

    results: dict[int, MonthSummary] = {}
    for e in employees:
        rule = shifts.get(e.shift_id, default)
        if e.company_id not in ot_on:
            ot_on[e.company_id] = load_policy(db, e.company_id).ot_enabled
        if not ot_on[e.company_id] or e.ot_eligible is False:
            rule = replace(rule, ot_allowed=False)
        own = holidays | {h.day for h in specials if holiday_applies(h, e)}
        results[e.id] = evaluate_month(year, month, punches.get(e.card_no, []), rule,
                                       weekly_off_for(e, shift_off.get(e.shift_id), weekend), own, leave_map.get(e.id),
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


def start_auto_refresh(every_seconds: int = 300) -> None:
    """Keeps daily attendance up to date: when new punches have arrived (machines, mobile, files), the current month
    (and the previous one during the first 5 days) is processed again in the background."""
    from database import SessionLocal
    from sqlalchemy import func
    from timeutil import now_local
    log = logging.getLogger("laila.autoprocess")
    state = {"last_id": None}

    def loop():
        _time.sleep(60)
        while True:
            try:
                with SessionLocal() as db:
                    top = db.scalar(select(func.max(AttendanceLog.id))) or 0
                    today = now_local().date()
                    if top != state["last_id"] or state.get("day") != today:
                        months = [(today.year, today.month)]
                        if today.day <= 5:
                            prev = today.replace(day=1) - timedelta(days=1)
                            months.append((prev.year, prev.month))
                        for y, m in months:
                            process_month(db, y, m)
                        state["last_id"], state["day"] = top, today
            except Exception as exc:  # noqa: BLE001 - never let the thread die
                log.warning("automatic processing failed: %s", exc)
                try:
                    from services.errors import log_error
                    log_error("PROCESS", f"automatic attendance processing failed: {exc}", exc=exc)
                except Exception:  # noqa: BLE001
                    pass
            _time.sleep(every_seconds)
    threading.Thread(target=loop, name="auto-process", daemon=True).start()
