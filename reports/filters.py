"""Filters for the Reports centre. Each report lists the filter keys it supports; the page builds the form
from these definitions and the fetchers apply them with emp_conditions()."""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import or_, select

from models import Company, Department, Device, Employee, Shift


@dataclass
class Filter:
    key: str
    label: str
    kind: str = "select"          # select / multi / text / date / month / check / time
    options: str = ""             # name of an option source (see options())
    choices: list = field(default_factory=list)   # fixed (value, label) list
    help: str = ""


FILTERS: dict[str, Filter] = {f.key: f for f in [
    Filter("start", "From", "date"), Filter("end", "To", "date"),
    Filter("month", "Month", "month"), Filter("month_to", "To month", "month"),
    Filter("company", "Company", options="companies"),
    Filter("department", "Department", options="departments"),
    Filter("designation", "Designation", options="designations"),
    Filter("section", "Section / line", options="sections"),
    Filter("shift", "Shift", options="shifts"),
    Filter("status", "Employee status", choices=[("ACTIVE", "Active"), ("INACTIVE", "Inactive")]),
    Filter("q", "Employee (code, card or name)", "text"),
    Filter("gender", "Gender", choices=[("Male", "Male"), ("Female", "Female"), ("Other", "Other")]),
    Filter("blood", "Blood group", choices=[(b, b) for b in ("A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-")]),
    Filter("payment_mode", "Pay mode", choices=[("BANK", "Bank"), ("CASH", "Cash"), ("MOBILE", "Mobile banking")]),
    Filter("mobile", "Mobile punch", choices=[("1", "Allowed"), ("0", "Not allowed")]),
    Filter("joined_from", "Joined from", "date"), Filter("joined_to", "Joined to", "date"),
    Filter("machine", "Machine", options="machines"),
    Filter("source", "Source", choices=[(s, s) for s in ("ZKTECO", "HIKVISION", "FILE", "USB", "MANUAL", "MOBILE")]),
    Filter("direction", "In / Out", choices=[("IN", "In"), ("OUT", "Out")]),
    Filter("unknown", "Only IDs that are not employees", "check"),
    Filter("single", "Only days with a single punch", "check"),
    Filter("late_after", "First punch after", "time", help="e.g. 09:10 = came late"),
    Filter("early_before", "Last punch before", "time", help="e.g. 17:00 = left early"),
    Filter("att_status", "Attendance status", "multi", choices=[
        ("CAME", "Present - everyone who came"), ("PRESENT", "On time"), ("LATE", "Late"),
        ("MISSING_OUT", "In only / single punch"), ("ABSENT", "Absent"),
        ("LEAVE_PAID", "Leave (paid)"), ("LEAVE_UNPAID", "Leave (unpaid)"), ("OFFDAY", "Off day"),
        ("OFFDAY_WORK", "Worked off day")]),
    Filter("late_only", "Only late", "check"), Filter("ot_only", "Only with overtime", "check"),
    Filter("leave_type", "Leave type", options="leave_types"),
    Filter("leave_status", "Leave status", choices=[("APPROVED", "Approved"), ("PENDING", "Waiting for approval"),
                                                    ("REJECTED", "Rejected")]),
    Filter("paid", "Paid / unpaid", choices=[("1", "Paid"), ("0", "Unpaid")]),
    Filter("change_kind", "Type", choices=[("INCREMENT", "Increment"), ("PROMOTION", "Promotion")]),
    Filter("sep_type", "Leaving type", choices=[("RESIGNATION", "Resignation"), ("TERMINATION", "Termination"),
                                                 ("DISMISSAL", "Dismissal"), ("RETIREMENT", "Retirement"),
                                                 ("CONTRACT_END", "End of contract"), ("DISCHARGE", "Discharge"),
                                                 ("DEATH", "Death"), ("ABSCONDING", "Absence without notice")]),
    Filter("sep_status", "Status", choices=[("LEFT", "Left"), ("LEAVING", "Leaving (in progress)")]),
    Filter("asset_status", "Asset status", choices=[("OUT", "With employee"), ("OUT_LEFT", "Still with staff who left"),
                                                     ("RETURNED", "Returned"), ("DAMAGED", "Returned damaged"),
                                                     ("LOST", "Lost")]),
    Filter("asset_type", "Asset type", choices=[("LAPTOP", "Laptop / computer"), ("MOBILE", "Mobile phone"),
                                                 ("SIM", "SIM card"), ("IDCARD", "ID card"), ("ACCESS", "Access / RFID card"),
                                                 ("KEYS", "Keys"), ("UNIFORM", "Uniform"), ("PPE", "Safety gear (PPE)"),
                                                 ("TOOLS", "Tools / equipment"), ("VEHICLE", "Vehicle / motorbike"),
                                                 ("FURNITURE", "Furniture"), ("OTHER", "Other")]),
    Filter("asset_q", "Item, serial or asset tag", "text"),
    Filter("run_status", "Payroll status", choices=[("FINAL", "Final"), ("DRAFT", "Draft")]),
    Filter("warnings", "Only with remarks / warnings", "check"),
]}

EMP_FILTER_KEYS = ("company", "department", "designation", "section", "shift", "status", "q", "gender", "blood",
                   "payment_mode", "mobile", "joined_from", "joined_to")


def options(db, user: dict, source: str) -> list[tuple[str, str]]:
    if source == "companies":
        stmt = select(Company).order_by(Company.code)
        if user.get("company_id"):
            stmt = stmt.where(Company.id == user["company_id"])
        return [(str(c.id), f"{c.code} - {c.name}") for c in db.scalars(stmt)]
    if source == "departments":
        return [(str(d.id), d.name) for d in db.scalars(select(Department).order_by(Department.name))]
    if source in ("designations", "sections"):
        col = Employee.designation if source == "designations" else Employee.section
        return [(v, v) for (v,) in db.execute(select(col).where(col.is_not(None)).distinct().order_by(col)) if v]
    if source == "shifts":
        return [("GENERAL", "GENERAL (default)")] + [(str(s.id), f"{s.code} - {s.name}")
                                                     for s in db.scalars(select(Shift).order_by(Shift.code))
                                                     if s.code != "GENERAL"]
    if source == "leave_types":
        from models import LeaveType
        return [(t.code, t.name) for t in db.scalars(select(LeaveType).order_by(LeaveType.sort, LeaveType.code))]
    if source == "machines":
        return [(str(d.id), d.name) for d in db.scalars(select(Device).order_by(Device.name))]
    return []


def has_emp_filter(p: dict, user: dict) -> bool:
    return bool(user.get("company_id")) or any(str(p.get(k) or "").strip() for k in EMP_FILTER_KEYS)


def emp_conditions(p: dict, user: dict) -> list:
    """SQL conditions on Employee for the common employee filters (and the user's company limit)."""
    from datetime import date
    c = []
    cid = user.get("company_id") or (int(p["company"]) if str(p.get("company") or "").isdigit() else None)
    if cid:
        c.append(Employee.company_id == cid)
    if str(p.get("department") or "").isdigit():
        c.append(Employee.department_id == int(p["department"]))
    if p.get("designation"):
        c.append(Employee.designation == p["designation"])
    if p.get("section"):
        c.append(Employee.section == p["section"])
    sh = str(p.get("shift") or "")
    if sh == "GENERAL":
        c.append(or_(Employee.shift_id.is_(None), Employee.shift.has(Shift.code == "GENERAL")))
    elif sh.isdigit():
        c.append(Employee.shift_id == int(sh))
    if p.get("status"):
        c.append(Employee.employment_status == p["status"])
    if str(p.get("q") or "").strip():
        q = p["q"].strip()
        like = f"%{q}%"
        c.append(or_(Employee.employee_code == q, Employee.card_no == q, Employee.full_name.ilike(like),
                     Employee.employee_code.ilike(like)))
    for k, col in (("gender", Employee.gender), ("blood", Employee.blood_group), ("payment_mode", Employee.payment_mode)):
        if p.get(k):
            c.append(col == p[k])
    if p.get("mobile") in ("0", "1"):
        c.append(Employee.allow_mobile == (p["mobile"] == "1"))
    if p.get("joined_from"):
        c.append(Employee.joined_on >= date.fromisoformat(p["joined_from"]))
    if p.get("joined_to"):
        c.append(Employee.joined_on <= date.fromisoformat(p["joined_to"]))
    return c


def describe(p: dict, db, user: dict) -> str:
    """Short text of the active filters, printed under the report title."""
    parts = []
    for k, f in FILTERS.items():
        v = p.get(k) if f.kind != "multi" else ",".join(p.get(k) or []) if isinstance(p.get(k), list) else p.get(k)
        if not v or k in ("start", "end", "month", "month_to"):
            continue
        if f.kind == "check":
            parts.append(f.label.lower())
            continue
        label = v
        src = dict(options(db, user, f.options)) if f.options else dict(f.choices)
        if f.kind == "multi":
            label = ", ".join(src.get(x, x) for x in str(v).split(","))
        else:
            label = src.get(v, v)
        parts.append(f"{f.label}: {label}")
    return " · ".join(parts)
