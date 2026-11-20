"""All reports (lists) of the program with their selectable columns. Screen, Excel and PDF all use these."""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, or_, select

from models import (AttendanceLog, AuditLog, Company, DailyAttendance, Device, Employee, ErrorLog, Holiday, Leave,
                    PayrollRun, Shift)
from reports.engine import Col, Report
from timeutil import now_local

DEVICE_TYPES = {"ZKTECO": "ZKTeco (by IP)", "ZKTECO_PUSH": "ZKTeco (push)", "HIKVISION": "Hikvision"}


class Problem(ValueError):
    pass


def _company(user: dict, p: dict) -> int | None:
    if user.get("company_id"):
        return user["company_id"]
    v = str(p.get("company") or "")
    return int(v) if v.isdigit() and v != "0" else None


def _dates(p: dict, default_days: int = 0) -> tuple[date, date]:
    today = now_local().date()
    s = date.fromisoformat(p["start"]) if p.get("start") else (
        date.fromisoformat(p["day"]) if p.get("day") else today - timedelta(days=default_days))
    e = date.fromisoformat(p["end"]) if p.get("end") else (date.fromisoformat(p["day"]) if p.get("day") else today)
    if e < s:
        s, e = e, s
    if (e - s).days > 92:
        raise Problem("Choose at most 3 months at a time")
    return s, e


def _comp_name(db, cid) -> str:
    c = db.get(Company, cid) if cid else None
    return f" · {c.code}" if c else ""


def _page(stmt, db, p, export, default_size):
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    if not export:
        size = default_size
        page = max(1, int(p.get("page") or 1))
        stmt = stmt.offset((page - 1) * size).limit(size)
    return db.scalars(stmt).all(), total


emp_link = lambda e: f"/employees/{e.id}"  # noqa: E731

# ------------------------------------------------------------ employees
EMP_COLS = [
    Col("company", "Company", lambda e: e.company.code if e.company else ""),
    Col("code", "Employee code", lambda e: e.employee_code, link=emp_link),
    Col("card", "Card no.", lambda e: e.card_no),
    Col("name", "Name", lambda e: e.full_name, link=emp_link),
    Col("department", "Department", lambda e: e.department.name if e.department else ""),
    Col("designation", "Designation", lambda e: e.designation),
    Col("section", "Section / line", lambda e: e.section, False),
    Col("grade", "Grade", lambda e: e.grade, False),
    Col("shift", "Shift", lambda e: e.shift.code if e.shift else "GENERAL"),
    Col("joined", "Joined", lambda e: e.joined_on, kind="date"),
    Col("left", "Left", lambda e: e.left_on, False, kind="date"),
    Col("status", "Status", lambda e: e.employment_status),
    Col("phone", "Mobile", lambda e: e.phone),
    Col("gross", "Gross", lambda e: e.gross, kind="money", perm="salary.view"),
    Col("basic", "Basic", lambda e: e.basic, False, kind="money", perm="salary.view"),
    Col("house_rent", "House rent", lambda e: e.house_rent, False, kind="money", perm="salary.view"),
    Col("medical", "Medical", lambda e: e.medical, False, kind="money", perm="salary.view"),
    Col("transport", "Transport", lambda e: e.transport, False, kind="money", perm="salary.view"),
    Col("food", "Food", lambda e: e.food, False, kind="money", perm="salary.view"),
    Col("payment", "Pay mode", lambda e: e.payment_mode, perm="salary.view"),
    Col("bank", "Bank / wallet", lambda e: e.bank_name, False, perm="salary.view"),
    Col("account", "Account no.", lambda e: e.account_no, False, perm="salary.view"),
    Col("father", "Father", lambda e: e.father_name, False),
    Col("mother", "Mother", lambda e: e.mother_name, False),
    Col("spouse", "Spouse", lambda e: e.spouse_name, False),
    Col("dob", "Date of birth", lambda e: e.date_of_birth, False, kind="date"),
    Col("gender", "Gender", lambda e: e.gender, False),
    Col("blood", "Blood group", lambda e: e.blood_group, False),
    Col("religion", "Religion", lambda e: e.religion, False),
    Col("marital", "Marital status", lambda e: e.marital_status, False),
    Col("nid", "NID", lambda e: e.nid, False),
    Col("email", "Email", lambda e: e.email, False),
    Col("emergency", "Emergency contact", lambda e: e.emergency_contact, False),
    Col("present_address", "Present address", lambda e: e.present_address, False),
    Col("permanent_address", "Permanent address", lambda e: e.permanent_address, False),
    Col("education", "Education", lambda e: e.education, False),
    Col("mobile", "Mobile punch", lambda e: e.allow_mobile, False),
    Col("photo", "Photo", lambda e: "yes" if e.photo else "", False),
]


def fetch_employees(db, user, p, export):
    cid = _company(user, p)
    stmt = select(Employee)
    if cid:
        stmt = stmt.where(Employee.company_id == cid)
    if p.get("status"):
        stmt = stmt.where(Employee.employment_status == p["status"])
    if p.get("q"):
        like = f"%{p['q'].strip()}%"
        stmt = stmt.where(or_(Employee.employee_code.ilike(like), Employee.full_name.ilike(like),
                              Employee.card_no.ilike(like)))
    rows, total = _page(stmt.order_by(Employee.company_id, Employee.employee_code), db, p, export, 50)
    return rows, f"{total} employees{_comp_name(db, cid)}", total


# ------------------------------------------------------------ punches
def _emp_map(db, rows, attr="card_no"):
    from services.punch_reports import employees_by_card
    return employees_by_card(db, [getattr(r, attr) for r in rows])


class PunchRow:
    __slots__ = ("p", "e")

    def __init__(self, p, e):
        self.p, self.e = p, e


PUNCH_COLS = [
    Col("card", "Card / ID", lambda r: r.p.card_no),
    Col("code", "Employee code", lambda r: r.e.employee_code if r.e else "", link=lambda r: emp_link(r.e) if r.e else ""),
    Col("name", "Name", lambda r: r.e.full_name if r.e else "(not an employee)", link=lambda r: emp_link(r.e) if r.e else ""),
    Col("company", "Company", lambda r: r.e.company.code if r.e and r.e.company else ""),
    Col("department", "Department", lambda r: r.e.department.name if r.e and r.e.department else "", False),
    Col("date", "Date", lambda r: r.p.punch_time.date(), kind="date"),
    Col("time", "Time", lambda r: r.p.punch_time, kind="time"),
    Col("machine", "Machine", lambda r: r.p.device),
    Col("direction", "In/Out", lambda r: r.p.direction),
    Col("source", "Source", lambda r: r.p.source),
    Col("note", "Note", lambda r: r.p.note, False),
    Col("location", "GPS", lambda r: f"{r.p.latitude},{r.p.longitude}" if r.p.latitude else "", False),
]


def fetch_punches(db, user, p, export):
    from services.punch_reports import punch_query
    s, e = _dates(p)
    cid = _company(user, p)
    stmt = punch_query(db, s, e, cid, p.get("q", ""), int(p["machine"]) if str(p.get("machine", "")).isdigit() else None,
                       p.get("source", ""), bool(p.get("unknown")))
    stmt = stmt.order_by(AttendanceLog.punch_time.desc() if not export else AttendanceLog.punch_time, AttendanceLog.card_no)
    rows, total = _page(stmt, db, p, export, 200)
    emps = _emp_map(db, rows)
    return [PunchRow(r, emps.get(r.card_no)) for r in rows], f"{s:%d/%m/%Y} - {e:%d/%m/%Y}{_comp_name(db, cid)}", total


# ------------------------------------------------------------ first & last
FL_COLS = [
    Col("date", "Date", lambda r: r["date"], kind="date"),
    Col("card", "Card / ID", lambda r: r["card_no"]),
    Col("code", "Employee code", lambda r: r["employee"].employee_code if r["employee"] else "",
        link=lambda r: emp_link(r["employee"]) if r["employee"] else ""),
    Col("name", "Name", lambda r: r["employee"].full_name if r["employee"] else "(not an employee)",
        link=lambda r: emp_link(r["employee"]) if r["employee"] else ""),
    Col("company", "Company", lambda r: r["employee"].company.code if r["employee"] and r["employee"].company else ""),
    Col("department", "Department", lambda r: r["employee"].department.name if r["employee"] and r["employee"].department else ""),
    Col("designation", "Designation", lambda r: r["employee"].designation if r["employee"] else "", False),
    Col("first", "First punch", lambda r: r["first"], kind="time"),
    Col("last", "Last punch", lambda r: r["last"] if r["count"] > 1 else None, kind="time"),
    Col("count", "Punches", lambda r: r["count"], kind="num"),
    Col("hours", "Hours", lambda r: r["hours"], kind="hours"),
    Col("first_machine", "First machine", lambda r: r["first_device"], False),
    Col("last_machine", "Last machine", lambda r: r["last_device"], False),
]


def fetch_first_last(db, user, p, export):
    from services.punch_reports import first_last
    s, e = _dates(p)
    cid = _company(user, p)
    rows = first_last(db, s, e, cid, p.get("q", ""), include_unknown=not cid)
    return (rows if export else rows[:3000]), f"{s:%d/%m/%Y} - {e:%d/%m/%Y}{_comp_name(db, cid)}", len(rows)


# ------------------------------------------------------------ daily attendance
ATT_COLS = [
    Col("date", "Date", lambda r: r.work_date, False, kind="date"),
    Col("company", "Co.", lambda r: r.employee.company.code if r.employee.company else ""),
    Col("code", "Code", lambda r: r.employee.employee_code, link=lambda r: emp_link(r.employee)),
    Col("name", "Name", lambda r: r.employee.full_name, link=lambda r: emp_link(r.employee)),
    Col("department", "Department", lambda r: r.employee.department.name if r.employee.department else ""),
    Col("designation", "Designation", lambda r: r.employee.designation, False),
    Col("shift", "Shift", lambda r: r.shift_code),
    Col("status", "Status", lambda r: r.status),
    Col("in", "In", lambda r: r.in_time, kind="time"),
    Col("out", "Out", lambda r: r.out_time, kind="time"),
    Col("late", "Late (min)", lambda r: r.late_minutes or None, kind="num"),
    Col("early", "Early leave (min)", lambda r: r.early_leave_minutes or None, False, kind="num"),
    Col("worked", "Worked h", lambda r: round(r.worked_minutes / 60, 2) if r.worked_minutes else None, kind="hours"),
    Col("ot", "OT h", lambda r: round(r.ot_minutes / 60, 2) if r.ot_minutes else None, kind="hours"),
    Col("extra_ot", "Extra OT h", lambda r: round(r.extra_ot_minutes / 60, 2) if r.extra_ot_minutes else None, kind="hours"),
    Col("punches", "Punches", lambda r: r.punch_count, False, kind="num"),
]


def fetch_attendance(db, user, p, export):
    s, e = _dates({"start": p.get("day") or p.get("start"), "end": p.get("end") or p.get("day")})
    cid = _company(user, p)
    stmt = select(DailyAttendance).join(Employee).where(DailyAttendance.work_date >= s, DailyAttendance.work_date <= e)
    if cid:
        stmt = stmt.where(Employee.company_id == cid)
    if p.get("status"):
        stmt = stmt.where(DailyAttendance.status == p["status"])
    rows = db.scalars(stmt.order_by(DailyAttendance.work_date, Employee.company_id, Employee.employee_code)).all()
    sub = f"{s:%d/%m/%Y}" + (f" - {e:%d/%m/%Y}" if e != s else "") + _comp_name(db, cid) + (f" · {p['status']}" if p.get("status") else "")
    return rows, sub, len(rows)


# ------------------------------------------------------------ leave
LEAVE_COLS = [
    Col("code", "Code", lambda r: r[1].employee_code, link=lambda r: emp_link(r[1])),
    Col("name", "Name", lambda r: r[1].full_name, link=lambda r: emp_link(r[1])),
    Col("company", "Company", lambda r: r[1].company.code if r[1].company else ""),
    Col("type", "Type", lambda r: r[0].leave_type),
    Col("from", "From", lambda r: r[0].start_date, kind="date"),
    Col("to", "To", lambda r: r[0].end_date, kind="date"),
    Col("days", "Days", lambda r: (r[0].end_date - r[0].start_date).days + 1, kind="num"),
    Col("paid", "Paid", lambda r: r[0].paid),
    Col("note", "Note", lambda r: r[0].note),
]


def fetch_leaves(db, user, p, export):
    s, e = _dates({"start": p.get("start") or str(now_local().date() - timedelta(days=60)),
                   "end": p.get("end") or str(now_local().date() + timedelta(days=30))})
    stmt = (select(Leave, Employee).join(Employee, Employee.id == Leave.employee_id)
            .where(Leave.end_date >= s, Leave.start_date <= e).order_by(Leave.start_date.desc()))
    cid = _company(user, p)
    if cid:
        stmt = stmt.where(Employee.company_id == cid)
    rows = db.execute(stmt).all()
    return rows, f"{s:%d/%m/%Y} - {e:%d/%m/%Y}{_comp_name(db, cid)}", len(rows)


# ------------------------------------------------------------ holidays, shifts, machines
HOLIDAY_COLS = [Col("date", "Date", lambda h: h.day, kind="date"), Col("weekday", "Day", lambda h: h.day.strftime("%A")),
                Col("name", "Holiday", lambda h: h.name)]


def fetch_holidays(db, user, p, export):
    year = int(p.get("year") or now_local().year)
    rows = db.scalars(select(Holiday).where(Holiday.day >= date(year - 1, 1, 1)).order_by(Holiday.day)).all()
    return rows, f"from {year - 1}", len(rows)


SHIFT_COLS = [
    Col("code", "Code", lambda s: s.code), Col("name", "Name", lambda s: s.name),
    Col("start", "Start", lambda s: s.start, kind="time"), Col("end", "End", lambda s: s.end, kind="time"),
    Col("grace", "Grace (min)", lambda s: s.grace_minutes, kind="num"),
    Col("break", "Break (min)", lambda s: s.break_minutes, kind="num"),
    Col("ot_block", "OT block (min)", lambda s: s.ot_round_minutes, kind="num"),
    Col("ot_cap", "Daily OT cap (min)", lambda s: s.max_ot_minutes, kind="num"),
    Col("offday_ot", "Off-day work = OT", lambda s: s.offday_work_is_ot),
    Col("early", "Accept punches early (min)", lambda s: s.early_window_minutes, False, kind="num"),
    Col("employees", "Employees", lambda s: getattr(s, "_count", 0), kind="num"),
]


def fetch_shifts(db, user, p, export):
    rows = db.scalars(select(Shift).order_by(Shift.code)).all()
    used = dict(db.execute(select(Employee.shift_id, func.count()).group_by(Employee.shift_id)).all())
    for s in rows:
        s._count = used.get(s.id, 0)
    return rows, "", len(rows)


DEVICE_COLS = [
    Col("no", "No.", lambda d: d.machine_no, kind="num"), Col("name", "Name", lambda d: d.name),
    Col("type", "Type", lambda d: DEVICE_TYPES.get(d.brand, d.brand)), Col("ip", "IP", lambda d: d.ip_address),
    Col("port", "Port", lambda d: d.port if d.brand != "ZKTECO_PUSH" else None, kind="num"),
    Col("serial", "Serial no.", lambda d: d.serial_number, False), Col("location", "Location", lambda d: d.location),
    Col("direction", "In/Out", lambda d: d.direction),
    Col("status", "Status", lambda d: ("ACTIVE" if d.enabled else "OFF") if d.approved else "PENDING"),
    Col("last_seen", "Last contact", lambda d: d.last_seen, kind="datetime"),
    Col("last_result", "Last result", lambda d: d.last_status),
    Col("punches", "Punches", lambda d: getattr(d, "_count", 0), kind="num"),
]


def fetch_devices(db, user, p, export):
    rows = db.scalars(select(Device).order_by(Device.approved, Device.machine_no, Device.name)).all()
    counts = dict(db.execute(select(AttendanceLog.device_id, func.count()).group_by(AttendanceLog.device_id)).all())
    for d in rows:
        d._count = counts.get(d.id, 0)
    return rows, f"{len(rows)} machines", len(rows)


# ------------------------------------------------------------ payroll lines (one company, one month)
PAY_COLS = [
    Col("code", "Code", lambda l: l.employee_code, link=lambda l: f"/employees/{l.employee_id}"),
    Col("name", "Name", lambda l: l.full_name, link=lambda l: f"/employees/{l.employee_id}"),
    Col("department", "Dept", lambda l: l.department), Col("designation", "Designation", lambda l: l.designation, False),
    Col("grade", "Grade", lambda l: l.grade, False),
    Col("days", "Days", lambda l: l.employed_days, False, kind="num"),
    Col("present", "Present", lambda l: l.present_days, kind="num"),
    Col("absent", "Absent", lambda l: l.absent_days, kind="num"),
    Col("leave", "Paid leave", lambda l: l.paid_leave_days, False, kind="num"),
    Col("off", "Off days", lambda l: l.offdays, False, kind="num"),
    Col("late", "Late", lambda l: l.late_days, kind="num"),
    Col("gross", "Gross", lambda l: l.gross, kind="money"),
    Col("basic", "Basic", lambda l: l.basic, False, kind="money"),
    Col("house_rent", "House rent", lambda l: l.house_rent, False, kind="money"),
    Col("medical", "Medical", lambda l: l.medical, False, kind="money"),
    Col("transport", "Transport", lambda l: l.transport, False, kind="money"),
    Col("food", "Food", lambda l: l.food, False, kind="money"),
    Col("earned", "Earned", lambda l: l.earned_gross, kind="money"),
    Col("absent_ded", "Absent ded.", lambda l: l.absent_deduction, kind="money"),
    Col("ot_hours", "OT h", lambda l: l.ot_hours, kind="hours"),
    Col("ot_rate", "OT rate", lambda l: l.ot_rate, False, kind="money"),
    Col("ot", "OT Tk", lambda l: l.ot_amount, kind="money"),
    Col("bonus", "Att. bonus", lambda l: l.attendance_bonus, kind="money"),
    Col("addition", "Other add.", lambda l: l.other_addition, False, kind="money"),
    Col("advance", "Advance", lambda l: l.advance, False, kind="money"),
    Col("other_ded", "Other ded.", lambda l: l.other_deduction, False, kind="money"),
    Col("tax", "Tax", lambda l: l.tax, False, kind="money"),
    Col("stamp", "Stamp", lambda l: l.stamp, False, kind="money"),
    Col("deductions", "Deductions", lambda l: l.total_deductions, kind="money"),
    Col("net", "Net pay", lambda l: l.net_pay, kind="money"),
    Col("mode", "Mode", lambda l: l.payment_mode),
    Col("bank", "Bank", lambda l: l.bank_name, False), Col("account", "Account no.", lambda l: l.account_no, False),
    Col("remarks", "Remarks", lambda l: l.warnings),
    Col("signature", "Signature", lambda l: "", False),
]


def fetch_payroll(db, user, p, export):
    from calendar import month_name
    cid = _company(user, p)
    y, m = (int(x) for x in (p.get("month") or f"{now_local():%Y-%m}").split("-")[:2])
    if not cid:
        first = db.scalar(select(Company.id).order_by(Company.code))
        cid = first
    run = db.scalar(select(PayrollRun).where(PayrollRun.company_id == cid, PayrollRun.year == y, PayrollRun.month == m))
    rows = sorted(run.lines, key=lambda l: (l.department, l.employee_code)) if run else []
    sub = f"{run.company.name if run else ''} · {month_name[m]} {y}" + (f" · {run.status}" if run else " · not processed")
    return rows, sub, len(rows)


# ------------------------------------------------------------ logs
AUDIT_COLS = [Col("time", "Time", lambda a: a.created_at, kind="datetime"), Col("user", "User", lambda a: a.username),
              Col("action", "Action", lambda a: a.action), Col("detail", "Detail", lambda a: a.detail)]


def fetch_audit(db, user, p, export):
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc())
    if p.get("q"):
        like = f"%{p['q']}%"
        stmt = stmt.where(or_(AuditLog.username.ilike(like), AuditLog.action.ilike(like), AuditLog.detail.ilike(like)))
    rows = db.scalars(stmt.limit(20000 if export else 500)).all()
    return rows, "", len(rows)


ERROR_COLS = [Col("time", "Time", lambda r: r.created_at, kind="datetime"), Col("type", "Type", lambda r: r.source),
              Col("message", "Message", lambda r: r.message), Col("user", "User", lambda r: r.username),
              Col("detail", "Details", lambda r: r.detail, False)]


def fetch_errors(db, user, p, export):
    stmt = select(ErrorLog).order_by(ErrorLog.created_at.desc())
    if p.get("source"):
        stmt = stmt.where(ErrorLog.source == p["source"])
    rows = db.scalars(stmt.limit(20000 if export else 1000)).all()
    return rows, p.get("source", ""), len(rows)


REPORTS: dict[str, Report] = {r.id: r for r in [
    Report("employees", "Employees", "employees.view", EMP_COLS, fetch_employees),
    Report("punches", "Punch records", "punches.view", PUNCH_COLS, fetch_punches),
    Report("first_last", "First & last punch", "punches.view", FL_COLS, fetch_first_last),
    Report("attendance", "Daily attendance", "attendance.view", ATT_COLS, fetch_attendance),
    Report("leaves", "Leave", "attendance.view", LEAVE_COLS, fetch_leaves, landscape=False),
    Report("holidays", "Holidays", "attendance.view", HOLIDAY_COLS, fetch_holidays, landscape=False),
    Report("shifts", "Shifts", "attendance.view", SHIFT_COLS, fetch_shifts),
    Report("machines", "Machines", "machines.manage", DEVICE_COLS, fetch_devices),
    Report("payroll", "Salary sheet", "payroll.view", PAY_COLS, fetch_payroll,
           totals=["gross", "basic", "house_rent", "medical", "transport", "food", "earned", "absent_ded", "ot",
                   "bonus", "addition", "advance", "other_ded", "tax", "stamp", "deductions", "net"]),
    Report("audit", "Activity log", "logs.view", AUDIT_COLS, fetch_audit),
    Report("errors", "Error log", "logs.view", ERROR_COLS, fetch_errors),
]}
