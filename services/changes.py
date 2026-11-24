"""Increments and promotions: record, apply on the effective date, and describe for letters."""
from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select

from models import Department, Employee, EmployeeChange
from payroll.engine import split_gross
from services.policy import load_policy
from timeutil import now_local

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
