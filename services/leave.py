"""Leave policy, balances, document rules and the approval workflow.

Workflow (Leave policy page, Admin): DEPT_FINAL = Department Head, then final approver (e.g. CFO/HR head);
DEPT = Department Head only; FINAL = final approver only; NONE = requests are approved at once.
A department without a Department Head skips that step. HR-granted leave is approved at once."""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, select

from models import Employee, Leave, LeaveType
from services.policy import get_setting
from timeutil import now_local

DEFAULT_TYPES = [
    # code, name, days/year, paid, doc rule, doc after days, doc label, gender, self request, sort
    ("CASUAL", "Casual leave", 10, True, "NEVER", 0, "", None, True, 1),
    ("SICK", "Medical / sick leave", 14, True, "OVER_DAYS", 3, "Doctor's prescription", None, True, 2),
    ("EARNED", "Earned leave", 18, True, "NEVER", 0, "", None, True, 3),
    ("MATERNITY", "Maternity leave", 112, True, "ALWAYS", 0, "Medical certificate / maternity papers", "Female", True, 4),
    ("UNPAID", "Leave without pay", 0, False, "NEVER", 0, "", None, True, 5),
]
WORKFLOWS = {"DEPT_FINAL": "Department Head, then final approver (CFO / HR head)",
             "DEPT": "Department Head only", "FINAL": "Final approver only (CFO / HR head)",
             "NONE": "No approval - requests are approved at once"}
OPEN = ("PENDING_DEPT", "PENDING_FINAL")
STATUS_LABEL = {"PENDING_DEPT": "Waiting for Department Head", "PENDING_FINAL": "Waiting for final approval",
                "APPROVED": "Approved", "REJECTED": "Rejected", "CANCELLED": "Cancelled"}


def seed_types(db) -> None:
    if db.scalar(select(func.count(LeaveType.id))):
        return
    for code, name, days, paid, rule, after, label, gender, selfreq, sort in DEFAULT_TYPES:
        db.add(LeaveType(code=code, name=name, days_per_year=days, paid=paid, doc_rule=rule, doc_after_days=after,
                         doc_label=label or "Document", gender=gender, self_request=selfreq, sort=sort))


def types(db, active_only: bool = True) -> list[LeaveType]:
    stmt = select(LeaveType).order_by(LeaveType.sort, LeaveType.code)
    if active_only:
        stmt = stmt.where(LeaveType.active == True)  # noqa: E712
    return list(db.scalars(stmt))


def count_days(start: date, end: date) -> int:
    return (end - start).days + 1


def leave_days(lv: Leave) -> int:
    return lv.days or count_days(lv.start_date, lv.end_date)


def doc_needed(lt: LeaveType, days: int) -> bool:
    return lt.doc_rule == "ALWAYS" or (lt.doc_rule == "OVER_DAYS" and days > (lt.doc_after_days or 0))


def balances(db, emp: Employee, year: int | None = None) -> list[dict]:
    year = year or now_local().year
    y0, y1 = date(year, 1, 1), date(year, 12, 31)
    used: dict[str, int] = {}
    pending: dict[str, int] = {}
    for lv in db.scalars(select(Leave).where(Leave.employee_id == emp.id, Leave.start_date <= y1, Leave.end_date >= y0,
                                             Leave.status.in_(("APPROVED",) + OPEN))):
        s, e = max(lv.start_date, y0), min(lv.end_date, y1)
        n = count_days(s, e)
        (used if lv.status == "APPROVED" else pending)[lv.leave_type] = \
            (used if lv.status == "APPROVED" else pending).get(lv.leave_type, 0) + n
    out = []
    for lt in types(db):
        if lt.gender and emp.gender and lt.gender != emp.gender:
            continue
        u, p = used.get(lt.code, 0), pending.get(lt.code, 0)
        out.append({"type": lt, "entitled": lt.days_per_year, "used": u, "pending": p,
                    "left": (lt.days_per_year - u - p) if lt.days_per_year else None})
    return out


def validate(db, emp: Employee, lt: LeaveType | None, start: date, end: date, has_document: bool,
             exclude_id: int | None = None, ignore_balance: bool = False) -> int:
    """Raises ValueError with a clear message if the leave breaks a rule. Returns the number of days."""
    if lt is None or not lt.active:
        raise ValueError("unknown or inactive leave type")
    if end < start:
        raise ValueError("'to' date is before 'from' date")
    days = count_days(start, end)
    if days > 366:
        raise ValueError("leave longer than one year")
    if lt.gender and emp.gender and lt.gender != emp.gender:
        raise ValueError(f"{lt.name} is only for {lt.gender.lower()} employees")
    if lt.gender and not emp.gender:
        raise ValueError(f"{lt.name}: set the employee's gender in the profile first")
    if doc_needed(lt, days) and not has_document:
        if lt.doc_rule == "ALWAYS":
            raise ValueError(f"{lt.name} needs a document: {lt.doc_label}")
        raise ValueError(f"{lt.name} of more than {lt.doc_after_days} days needs a document: {lt.doc_label}")
    overlap = db.scalar(select(Leave).where(Leave.employee_id == emp.id, Leave.start_date <= end, Leave.end_date >= start,
                                            Leave.status.in_(("APPROVED",) + OPEN),
                                            Leave.id != (exclude_id or 0)))
    if overlap:
        raise ValueError(f"overlaps with {overlap.leave_type} leave {overlap.start_date:%d/%m/%Y} - "
                         f"{overlap.end_date:%d/%m/%Y} ({STATUS_LABEL.get(overlap.status, overlap.status)})")
    if lt.days_per_year and not ignore_balance:
        for year in range(start.year, end.year + 1):
            b = next((x for x in balances(db, emp, year) if x["type"].code == lt.code), None)
            y0, y1 = max(start, date(year, 1, 1)), min(end, date(year, 12, 31))
            need = count_days(y0, y1)
            if b and b["left"] is not None and need > b["left"]:
                raise ValueError(f"not enough {lt.name} left in {year}: {b['left']} day(s) left, {need} asked "
                                 f"({b['used']} used, {b['pending']} waiting for approval)")
    return days


def workflow(db) -> str:
    w = get_setting(db, "leave_approval", default="DEPT_FINAL") or "DEPT_FINAL"
    return w if w in WORKFLOWS else "DEPT_FINAL"


def first_status(db, emp: Employee) -> str:
    w = workflow(db)
    has_head = bool(emp.department and emp.department.head_username)
    if w in ("DEPT_FINAL", "DEPT") and has_head:
        return "PENDING_DEPT"
    if w in ("DEPT_FINAL", "FINAL"):
        return "PENDING_FINAL"
    if w == "DEPT" and not has_head:
        return "PENDING_FINAL"      # no head assigned: someone must still approve
    return "APPROVED"


def can_act(user: dict, lv: Leave, emp: Employee) -> bool:
    if user.get("is_admin") and lv.status in OPEN:
        return True
    if lv.status == "PENDING_DEPT":
        return "leave.approve_dept" in user["perms"] and bool(emp.department) and \
            emp.department.head_username == user["username"]
    if lv.status == "PENDING_FINAL":
        return "leave.approve_final" in user["perms"]
    return False


def approve(db, lv: Leave, user: dict) -> str:
    now = now_local()
    if lv.status == "PENDING_DEPT":
        lv.dept_approved_by, lv.dept_approved_at = user["username"], now
        lv.status = "PENDING_FINAL" if workflow(db) in ("DEPT_FINAL", "FINAL") else "APPROVED"
    elif lv.status == "PENDING_FINAL":
        lv.final_approved_by, lv.final_approved_at = user["username"], now
        lv.status = "APPROVED"
    return lv.status


def reject(lv: Leave, user: dict, reason: str) -> None:
    lv.status, lv.rejected_by, lv.reject_reason = "REJECTED", user["username"], reason.strip()[:300] or None


def pending_for(db, user: dict, company_id: int | None = None) -> list[Leave]:
    stmt = select(Leave).join(Employee, Employee.id == Leave.employee_id).where(Leave.status.in_(OPEN))
    if company_id:
        stmt = stmt.where(Employee.company_id == company_id)
    rows = list(db.scalars(stmt.order_by(Leave.requested_at, Leave.id)))
    return [lv for lv in rows if can_act(user, lv, lv.employee)]


def month_range(d: date) -> tuple[date, date]:
    first = d.replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    return first, nxt - timedelta(days=1)
