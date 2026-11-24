"""Increments and promotions: record, apply on the effective date, and describe for letters."""
from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select

from models import Department, Employee, EmployeeChange
from payroll.engine import split_gross
from services.policy import load_policy
from timeutil import next_due, now_local

KINDS = {"INCREMENT": "Increment", "PROMOTION": "Promotion"}


def new_gross(old: Decimal, how: str, value: Decimal) -> Decimal:
    """how: AMOUNT (Tk added), PERCENT (% added) or GROSS (new gross typed)."""
    old = Decimal(old or 0)
    if how == "PERCENT":
        g = old + old * value / 100
    elif how == "GROSS":
        g = value
    else:
        g = old + value
    return g.quantize(Decimal(1), ROUND_HALF_UP)


def apply(db, ch: EmployeeChange) -> None:
    """Puts the new designation / grade / department / salary on the employee."""
    e: Employee = ch.employee
    if ch.new_designation:
        e.designation = ch.new_designation
    if ch.new_grade:
        e.grade = ch.new_grade
    if ch.new_department and (not e.department or e.department.name != ch.new_department):
        d = db.scalar(select(Department).where(Department.name == ch.new_department))
        if not d:
            d = Department(name=ch.new_department)
            db.add(d)
            db.flush()
        e.department_id = d.id
    if ch.new_gross and Decimal(ch.new_gross) != Decimal(e.gross or 0):
        parts = split_gross(Decimal(ch.new_gross), load_policy(db, e.company_id))
        e.gross = parts.gross
        e.basic, e.house_rent, e.medical, e.transport, e.food = (parts.basic, parts.house_rent, parts.medical,
                                                                  parts.transport, parts.food)
    ch.status, ch.applied_at = "APPLIED", datetime.now()


def apply_due(db) -> int:
    """Applies every pending change whose effective date has come (runs in the background and on page views)."""
    today = now_local().date()
    n = 0
    for ch in db.scalars(select(EmployeeChange).where(EmployeeChange.status == "PENDING",
                                                      EmployeeChange.effective_date <= today)
                         .order_by(EmployeeChange.effective_date, EmployeeChange.id)):
        apply(db, ch)
        n += 1
    if n:
        db.commit()
    return n


def increase(ch) -> tuple[Decimal, Decimal]:
    old, new = Decimal(ch.old_gross or 0), Decimal(ch.new_gross or 0)
    amt = new - old
    pct = (amt * 100 / old).quantize(Decimal("0.01")) if old else Decimal(0)
    return amt, pct


def due_list(db, days_ahead: int = 30, company_ids=None) -> list[dict]:
    """Active staff whose yearly increment is due: 12 months after the last increment / promotion (or joining),
    within `days_ahead` days or already overdue. Also lists changes already saved for a future date."""
    from datetime import date, timedelta
    today = now_local().date()
    last: dict[int, date] = {}
    upcoming: dict[int, EmployeeChange] = {}
    for ch in db.scalars(select(EmployeeChange).where(EmployeeChange.status != "CANCELLED")):
        if ch.status == "PENDING":
            upcoming[ch.employee_id] = ch
        if ch.new_gross and ch.new_gross != ch.old_gross:
            last[ch.employee_id] = max(last.get(ch.employee_id, ch.effective_date), ch.effective_date)
    stmt = select(Employee).where(Employee.employment_status == "ACTIVE")
    if company_ids is not None:
        stmt = stmt.where(Employee.company_id.in_(list(company_ids) or [0]))
    out = []
    for e in db.scalars(stmt):
        base = last.get(e.id) or e.joined_on
        if not base:
            continue
        due = next_due(base, today)
        pend = upcoming.get(e.id)
        if due <= today + timedelta(days=days_ahead) or pend:
            out.append({"e": e, "due": due, "last": last.get(e.id), "overdue": (today - due).days if due < today else 0,
                        "pending": pend})
    out.sort(key=lambda x: (x["pending"] is None and 0 or 1, x["due"]))
    return out


def notices_for(db, emp: Employee, days: int = 60) -> list[EmployeeChange]:
    """Increments / promotions to show to the employee (approved in the last `days` days)."""
    from datetime import timedelta
    since = now_local() - timedelta(days=days)
    return list(db.scalars(select(EmployeeChange).where(EmployeeChange.employee_id == emp.id,
                                                        EmployeeChange.notify == True,  # noqa: E712
                                                        EmployeeChange.status != "CANCELLED",
                                                        EmployeeChange.created_at >= since)
                           .order_by(EmployeeChange.effective_date.desc())))
