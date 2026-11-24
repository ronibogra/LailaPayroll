"""Required working hours (from office hours) compared with hours actually worked.

Required = every working day the person is employed (not weekly off, not a public holiday) x the day's office
hours (shift length minus the unpaid break). Approved leave days are not required. Worked = time between first
and last punch minus the break, on every day (also off days worked)."""
from __future__ import annotations

from datetime import date, datetime


def shift_minutes(start, end, brk: int, brk_after: int) -> int:
    a = start.hour * 60 + start.minute
    b = end.hour * 60 + end.minute
    span = (b - a) % (24 * 60) or 24 * 60            # night shift crosses midnight
    return span - (brk if span > brk_after else 0)


def shift_table(db) -> dict[str, int]:
    from sqlalchemy import select
    from models import Shift
    out = {"GENERAL": shift_minutes(datetime(2000, 1, 1, 9).time(), datetime(2000, 1, 1, 18).time(), 60, 300)}
    for s in db.scalars(select(Shift)):
        out[s.code] = shift_minutes(s.start, s.end, s.break_minutes or 0, s.break_after_minutes or 0)
    return out


def blank() -> dict:
    return {"work_days": 0, "leave_days": 0, "req_min": 0, "worked_min": 0, "ot_min": 0, "present": 0, "late": 0,
            "absent": 0}


def add(acc: dict, r, shifts: dict[str, int]) -> None:
    if r.status == "NOT_EMPLOYED":
        return
    if r.day_type == "WORKDAY":
        acc["work_days"] += 1
        if r.status in ("LEAVE_PAID", "LEAVE_UNPAID"):
            acc["leave_days"] += 1
        else:
            acc["req_min"] += shifts.get(r.shift_code or "GENERAL", shifts["GENERAL"])
    acc["worked_min"] += r.worked_minutes or 0
    acc["ot_min"] += (r.ot_minutes or 0) + (r.extra_ot_minutes or 0)
    acc["present"] += r.status in ("PRESENT", "LATE", "MISSING_OUT", "OFFDAY_WORK")
    acc["late"] += (r.late_minutes or 0) > 0
    acc["absent"] += r.status == "ABSENT"


def finish(acc: dict) -> dict:
    req, wk = acc["req_min"] / 60, acc["worked_min"] / 60
    acc.update(required=round(req, 1), worked=round(wk, 1), diff=round(wk - req, 1),
               pct=round(wk * 100 / req, 1) if req else None, ot=round(acc["ot_min"] / 60, 1))
    return acc


def by_employee(db, employee_ids: list[int], first: date, last: date) -> dict[int, dict]:
    from sqlalchemy import select
    from models import DailyAttendance
    shifts = shift_table(db)
    out: dict[int, dict] = {}
    ids = list(employee_ids)
    for i in range(0, len(ids), 500):
        for r in db.scalars(select(DailyAttendance).where(DailyAttendance.employee_id.in_(ids[i:i + 500]),
                                                          DailyAttendance.work_date >= first,
                                                          DailyAttendance.work_date <= last)):
            add(out.setdefault(r.employee_id, blank()), r, shifts)
    return {k: finish(v) for k, v in out.items()}
