"""Custom pay components (Admin): which ones apply to an employee and with what value."""
from decimal import Decimal

from sqlalchemy import select

from models import EmployeeComponent, PayComponent
from payroll.engine import ComponentSpec

CALCS = {"FIXED": "Fixed amount (Tk per month)", "PCT_BASIC": "% of basic", "PCT_GROSS": "% of gross",
         "PER_PRESENT_DAY": "Tk per present day", "PER_OT_HOUR": "Tk per OT hour"}


def load_all(db):
    comps = list(db.scalars(select(PayComponent).where(PayComponent.active == True).order_by(PayComponent.sort, PayComponent.id)))  # noqa: E712
    per_emp: dict[int, dict[int, EmployeeComponent]] = {}
    for ec in db.scalars(select(EmployeeComponent)):
        per_emp.setdefault(ec.employee_id, {})[ec.component_id] = ec
    return comps, per_emp


def for_employee(e, comps, per_emp) -> list[ComponentSpec]:
    out = []
    own = per_emp.get(e.id, {})
    for c in comps:
        ec = own.get(c.id)
        if ec is not None and ec.excluded:
            continue
        if c.assigned_only and ec is None:
            continue
        if ec is None and ((c.company_id and c.company_id != e.company_id) or
                           (c.department_id and c.department_id != e.department_id)):
            continue
        value = ec.value if ec is not None and ec.value is not None else c.value
        out.append(ComponentSpec(c.code, c.name, c.kind, c.calc, Decimal(value or 0), bool(c.prorate)))
    return out
