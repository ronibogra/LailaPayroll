"""Delete centre (Super Admin): every kind of stored data, system by system. Each section knows how to list,
filter and delete its rows - including files on disk and rows that depend on them."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Callable

from sqlalchemy import delete, func, or_, select

from config import DATA_DIR
from models import (AttendanceLog, AuditLog, Company, DailyAttendance, Department, Device, Employee, EmployeeAsset,
                    EmployeeChange, EmployeeComponent, EmployeeDocument, EmployeeSeparation, ErrorLog, Holiday, Leave,
                    LeaveType, PayComponent, PayrollAdjustment, PayrollLine, PayrollRun, Shift, SpecialHoliday, User)
from services import documents as docs


# ------------------------------------------------------------------ files
def unlink_photo(name: str | None) -> int:
    from services.photos import PHOTO_DIR
    if name and "/" not in name and "\\" not in name and ".." not in name:
        (PHOTO_DIR / name).unlink(missing_ok=True)
        return 1
    return 0


def unlink_punch_photo(name: str | None) -> int:
    if name and ".." not in name:
        (DATA_DIR / "punch_photos" / name).unlink(missing_ok=True)
        return 1
    return 0


def wipe_employee(db, emp: Employee, with_punches: bool = False, with_final: bool = False) -> dict:
    """Remove an employee and everything stored for them. Not done (final > 0) when they are on a finalized payroll,
    unless with_final (their payslips go too). The caller commits."""
    res = {"code": emp.employee_code, "name": emp.full_name, "files": 0, "punches": 0, "final": 0}
    final = db.scalar(select(func.count(PayrollLine.id)).join(PayrollRun, PayrollRun.id == PayrollLine.run_id)
                      .where(PayrollLine.employee_id == emp.id, PayrollRun.status == "FINAL")) or 0
    if final and not with_final:
        res["final"] = final
        return res
    for d in db.scalars(select(EmployeeDocument).where(EmployeeDocument.employee_id == emp.id)):
        docs.delete_file(d.stored_name)
        res["files"] += 1
        db.delete(d)
    for lv in db.scalars(select(Leave).where(Leave.employee_id == emp.id)):
        if lv.document:
            docs.delete_file(lv.document)
        db.delete(lv)
    for model in (DailyAttendance, EmployeeAsset, EmployeeSeparation, PayrollAdjustment, EmployeeComponent,
                  EmployeeChange, PayrollLine):
        db.execute(delete(model).where(model.employee_id == emp.id))
    for u in db.scalars(select(User).where(User.employee_id == emp.id)):
        if u.role == "STAFF":
            db.delete(u)
        else:
            u.employee_id = None
    if with_punches and emp.card_no:
        for lg in db.scalars(select(AttendanceLog).where(AttendanceLog.card_no == emp.card_no)):
            unlink_punch_photo(lg.photo)
            db.delete(lg)
            res["punches"] += 1
    unlink_photo(emp.photo)
    unlink_photo(emp.signature)
    db.delete(emp)
    db.flush()
    return res


# ------------------------------------------------------------------ sections
@dataclass
class Section:
    key: str
    label: str
    group: str
    model: Any
    cols: list[tuple[str, Callable[[Any], Any]]]
    order: Any = None
    date_col: Any = None            # filter From / To on this column
    emp_on: Any = None              # join condition to Employee (search by code / name, company filter)
    search: list = field(default_factory=list)   # own text columns searched by "q"
    company_col: Any = None         # own company column (when there is no employee)
    where: Any = None               # fixed condition (e.g. only rows with a photo)
    delete_one: Callable | None = None   # (db, row, opts) -> "" done / reason it was kept
    clears: bool = False            # "delete" only clears a file (photos), the row stays
    note: str = ""


def _d(v):
    return v.strftime("%d/%m/%Y") if isinstance(v, (date, datetime)) else v


def _emp(r):
    e = getattr(r, "employee", None)
    return f"{e.employee_code} {e.full_name}" if e else ""


def _money(v):
    return f"{v:,.0f}" if v is not None else ""


# delete hooks: return "" when deleted, else the reason the row was kept
def _del_employee(db, e, o):
    res = wipe_employee(db, e, with_punches=o.get("with_punches"), with_final=o.get("with_final"))
    return f"{e.employee_code}: on a finalized payroll" if res["final"] else ""


def _del_punch(db, lg, o):
    unlink_punch_photo(lg.photo)
    db.delete(lg)
    return ""


def _clear_punch_photo(db, lg, o):
    unlink_punch_photo(lg.photo)
    lg.photo = None
    return ""


def _clear_photos(db, e, o):
    unlink_photo(e.photo)
    unlink_photo(e.signature)
    e.photo = e.signature = None
    return ""


def _del_document(db, d, o):
    docs.delete_file(d.stored_name)
    db.delete(d)
    return ""


def _del_leave(db, lv, o):
    if lv.document:
        docs.delete_file(lv.document)
    db.delete(lv)
    return ""


def _del_run(db, r, o):
    if r.status == "FINAL" and not o.get("with_final"):
        return f"{r.company.code if r.company else ''} {r.year}-{r.month:02d}: finalized"
    db.delete(r)
    return ""


def _del_line(db, ln, o):
    run = db.get(PayrollRun, ln.run_id)
    if run and run.status == "FINAL" and not o.get("with_final"):
        return f"{ln.employee_code} {run.year}-{run.month:02d}: finalized"
    db.delete(ln)
    db.flush()
    if run and not db.scalar(select(func.count(PayrollLine.id)).where(PayrollLine.run_id == run.id)):
        db.delete(run)                                  # nobody left on that payroll
    return ""


def _del_shift(db, s, o):
    for e in db.scalars(select(Employee).where(Employee.shift_id == s.id)):
        e.shift_id = None                               # back to the default shift
    db.delete(s)
    return ""


def _del_department(db, d, o):
    for e in db.scalars(select(Employee).where(Employee.department_id == d.id)):
        e.department_id = None
    db.delete(d)
    return ""


def _del_company(db, c, o):
    n = db.scalar(select(func.count(Employee.id)).where(Employee.company_id == c.id)) or 0
    if n:
        return f"{c.code}: still has {n} employee(s) - delete or move them first"
    for r in db.scalars(select(PayrollRun).where(PayrollRun.company_id == c.id)):
        if r.status == "FINAL" and not o.get("with_final"):
            return f"{c.code}: has finalized payrolls"
        db.delete(r)
    for u in db.scalars(select(User).where(User.company_id == c.id)):
        u.company_id = None
    db.delete(c)
    return ""


def _del_leave_type(db, t, o):
    n = db.scalar(select(func.count(Leave.id)).where(Leave.leave_type == t.code)) or 0
    if n:
        return f"{t.code}: used by {n} leave record(s) - delete those first or set the type inactive"
    db.delete(t)
    return ""


def _del_component(db, c, o):
    db.execute(delete(EmployeeComponent).where(EmployeeComponent.component_id == c.id))
    db.delete(c)
    return ""


def _del_user(db, u, o):
    me = o.get("me")
    if u.username == me:
        return f"{u.username}: you cannot delete yourself"
    if u.super_admin:
        left = db.scalar(select(func.count(User.id)).where(User.super_admin == True, User.active == True,  # noqa: E712
                                                           User.id != u.id)) or 0
        if not left:
            return f"{u.username}: the last user who may delete"
    db.delete(u)
    return ""


def _status(v):
    return (v or "").replace("_", " ").title()


EMP = Employee
SECTIONS: list[Section] = [
    # ---- People
    Section("employees", "Employees", "People", Employee,
            [("Code", lambda r: r.employee_code), ("Name", lambda r: r.full_name),
             ("Company", lambda r: r.company.code if r.company else ""),
             ("Department", lambda r: r.department.name if r.department else ""),
             ("Status", lambda r: "Active" if r.employment_status == "ACTIVE" else "Left"),
             ("Joined", lambda r: _d(r.joined_on)), ("Left", lambda r: _d(r.left_on))],
            order=(Employee.company_id, Employee.employee_code), date_col=Employee.joined_on,
            search=[Employee.employee_code, Employee.full_name, Employee.card_no], company_col=Employee.company_id,
            delete_one=_del_employee,
            note="Removes the employee with documents, leave, assets, increments, attendance and draft payslips."),
    Section("photos", "Photos & signatures", "People", Employee,
            [("Code", lambda r: r.employee_code), ("Name", lambda r: r.full_name),
             ("Photo", lambda r: "✓" if r.photo else ""), ("Signature", lambda r: "✓" if r.signature else ""),
             ("Status", lambda r: "Active" if r.employment_status == "ACTIVE" else "Left")],
            order=(Employee.employee_code,), search=[Employee.employee_code, Employee.full_name, Employee.card_no],
            company_col=Employee.company_id, where=or_(Employee.photo.is_not(None), Employee.signature.is_not(None)),
            delete_one=_clear_photos, clears=True, note="Deletes the picture files; the employee stays."),
    Section("documents", "Documents & letters", "People", EmployeeDocument,
            [("Employee", _emp), ("Type", lambda r: docs.DOC_TYPES.get(r.doc_type, r.doc_type)),
             ("Title", lambda r: r.title), ("File", lambda r: r.original_name),
             ("Size KB", lambda r: round((r.size or 0) / 1024)), ("Uploaded", lambda r: _d(r.uploaded_at))],
            order=(EmployeeDocument.uploaded_at.desc(),), date_col=EmployeeDocument.uploaded_at,
            emp_on=EmployeeDocument.employee_id == EMP.id, search=[EmployeeDocument.title], delete_one=_del_document),
    Section("assets", "Assets with staff", "People", EmployeeAsset,
            [("Employee", _emp), ("Item", lambda r: r.name), ("Serial", lambda r: r.serial_no),
             ("Status", lambda r: _status(r.status)), ("Issued", lambda r: _d(r.issued_on))],
            order=(EmployeeAsset.issued_on.desc(),), date_col=EmployeeAsset.issued_on,
            emp_on=EmployeeAsset.employee_id == EMP.id, search=[EmployeeAsset.name, EmployeeAsset.serial_no]),
    Section("changes", "Increments & promotions", "People", EmployeeChange,
            [("Employee", _emp), ("Type", lambda r: r.kind.title()), ("Effective", lambda r: _d(r.effective_date)),
             ("Gross before", lambda r: _money(r.old_gross)), ("New gross", lambda r: _money(r.new_gross)),
             ("Status", lambda r: _status(r.status))],
            order=(EmployeeChange.effective_date.desc(),), date_col=EmployeeChange.effective_date,
            emp_on=EmployeeChange.employee_id == EMP.id,
            note="Deletes the record only - the employee's current salary is not changed back."),
    Section("separations", "Leaving records", "People", EmployeeSeparation,
            [("Employee", _emp), ("Type", lambda r: _status(r.sep_type)), ("Last day", lambda r: _d(r.last_working_day)),
             ("Status", lambda r: _status(r.status))],
            order=(EmployeeSeparation.last_working_day.desc(),), date_col=EmployeeSeparation.last_working_day,
            emp_on=EmployeeSeparation.employee_id == EMP.id),
    Section("users", "User logins", "People", User,
            [("Username", lambda r: r.username), ("Name", lambda r: r.full_name), ("Role", lambda r: r.role),
             ("Active", lambda r: "Yes" if r.active else "No")],
            order=(User.role, User.username), search=[User.username, User.full_name, User.role], delete_one=_del_user,
            note="You cannot delete yourself or the last Super Admin."),
    # ---- Attendance
    Section("punches", "Punch records", "Attendance", AttendanceLog,
            [("Time", lambda r: r.punch_time.strftime("%d/%m/%Y %H:%M")), ("Card / ID", lambda r: r.card_no),
             ("Source", lambda r: r.source), ("Machine", lambda r: r.device), ("Photo", lambda r: "✓" if r.photo else "")],
            order=(AttendanceLog.punch_time.desc(),), date_col=AttendanceLog.punch_time,
            emp_on=AttendanceLog.card_no == EMP.card_no, search=[AttendanceLog.card_no, AttendanceLog.device],
            delete_one=_del_punch, note="Process the month again afterwards so attendance matches."),
    Section("punch_photos", "Mobile punch photos", "Attendance", AttendanceLog,
            [("Time", lambda r: r.punch_time.strftime("%d/%m/%Y %H:%M")), ("Card / ID", lambda r: r.card_no),
             ("File", lambda r: r.photo)],
            order=(AttendanceLog.punch_time.desc(),), date_col=AttendanceLog.punch_time,
            emp_on=AttendanceLog.card_no == EMP.card_no, search=[AttendanceLog.card_no],
            where=AttendanceLog.photo.is_not(None), delete_one=_clear_punch_photo, clears=True,
            note="Deletes the photo files; the punches stay."),
    Section("attendance", "Processed attendance", "Attendance", DailyAttendance,
            [("Employee", _emp), ("Date", lambda r: _d(r.work_date)), ("Status", lambda r: r.status),
             ("In", lambda r: r.in_time.strftime("%H:%M") if r.in_time else ""),
             ("Out", lambda r: r.out_time.strftime("%H:%M") if r.out_time else "")],
            order=(DailyAttendance.work_date.desc(),), date_col=DailyAttendance.work_date,
            emp_on=DailyAttendance.employee_id == EMP.id,
            note="Made again from the punches by Process month."),
    Section("leave", "Leave", "Attendance", Leave,
            [("Employee", _emp), ("Type", lambda r: r.leave_type), ("From", lambda r: _d(r.start_date)),
             ("To", lambda r: _d(r.end_date)), ("Days", lambda r: r.days), ("Status", lambda r: _status(r.status))],
            order=(Leave.start_date.desc(),), date_col=Leave.start_date, emp_on=Leave.employee_id == EMP.id,
            search=[Leave.leave_type], delete_one=_del_leave),
    Section("holidays", "Holidays", "Attendance", Holiday,
            [("Date", lambda r: _d(r.day)), ("Name", lambda r: r.name)],
            order=(Holiday.day.desc(),), date_col=Holiday.day, search=[Holiday.name]),
    Section("special_holidays", "Special holidays", "Attendance", SpecialHoliday,
            [("Date", lambda r: _d(r.day)), ("Name", lambda r: r.name), ("For", lambda r: _status(r.scope))],
            order=(SpecialHoliday.day.desc(),), date_col=SpecialHoliday.day, search=[SpecialHoliday.name]),
    Section("shifts", "Shifts", "Attendance", Shift,
            [("Code", lambda r: r.code), ("Name", lambda r: r.name),
             ("Time", lambda r: f"{r.start:%H:%M} - {r.end:%H:%M}")],
            order=(Shift.code,), search=[Shift.code, Shift.name], delete_one=_del_shift,
            note="Employees on a deleted shift go back to the default shift."),
    Section("leave_types", "Leave types", "Attendance", LeaveType,
            [("Code", lambda r: r.code), ("Name", lambda r: r.name), ("Days / year", lambda r: r.days_per_year)],
            order=(LeaveType.sort, LeaveType.code), search=[LeaveType.code, LeaveType.name], delete_one=_del_leave_type),
    # ---- Payroll
    Section("payroll", "Payrolls (salary sheets)", "Payroll", PayrollRun,
            [("Company", lambda r: r.company.code if r.company else ""), ("Month", lambda r: f"{r.year}-{r.month:02d}"),
             ("Status", lambda r: r.status.title()), ("Payslips", lambda r: len(r.lines)),
             ("Made by", lambda r: r.created_by)],
            order=(PayrollRun.year.desc(), PayrollRun.month.desc()), company_col=PayrollRun.company_id,
            delete_one=_del_run, note="Deletes the whole month's salary sheet with every payslip in it."),
    Section("payslips", "Payslips (one per employee)", "Payroll", PayrollLine,
            [("Code", lambda r: r.employee_code), ("Name", lambda r: r.full_name),
             ("Month", lambda r: f"{r.run.year}-{r.run.month:02d}" if r.run else ""),
             ("Net pay", lambda r: _money(r.net_pay)), ("Payroll", lambda r: r.run.status.title() if r.run else "")],
            order=(PayrollLine.run_id.desc(), PayrollLine.employee_code), emp_on=PayrollLine.employee_id == EMP.id,
            search=[PayrollLine.employee_code, PayrollLine.full_name], delete_one=_del_line),
    Section("adjustments", "Advances, deductions & tax", "Payroll", PayrollAdjustment,
            [("Employee", _emp), ("Month", lambda r: f"{r.year}-{r.month:02d}"), ("Advance", lambda r: _money(r.advance)),
             ("Other ded.", lambda r: _money(r.other_deduction)), ("Addition", lambda r: _money(r.other_addition)),
             ("Tax", lambda r: _money(r.tax))],
            order=(PayrollAdjustment.year.desc(), PayrollAdjustment.month.desc()),
            emp_on=PayrollAdjustment.employee_id == EMP.id),
    Section("components", "Pay components", "Payroll", PayComponent,
            [("Code", lambda r: r.code), ("Name", lambda r: r.name), ("Kind", lambda r: r.kind),
             ("Value", lambda r: _money(r.value))],
            order=(PayComponent.sort, PayComponent.code), search=[PayComponent.code, PayComponent.name],
            delete_one=_del_component),
    # ---- Setup
    Section("companies", "Companies", "Setup", Company,
            [("Code", lambda r: r.code), ("Name", lambda r: r.name)],
            order=(Company.code,), search=[Company.code, Company.name], delete_one=_del_company,
            note="Only a company without employees can be deleted."),
    Section("departments", "Departments", "Setup", Department,
            [("Name", lambda r: r.name), ("Code", lambda r: r.code), ("Employees", lambda r: len(r.employees))],
            order=(Department.name,), search=[Department.name, Department.code], delete_one=_del_department,
            note="Employees of a deleted department are left without a department."),
    Section("machines", "Machines", "Setup", Device,
            [("Name", lambda r: r.name), ("Brand", lambda r: r.brand), ("IP / serial", lambda r: r.ip_address or r.serial_number),
             ("Location", lambda r: r.location)],
            order=(Device.name,), search=[Device.name, Device.ip_address, Device.serial_number],
            note="The punches already downloaded stay."),
    # ---- Logs
    Section("audit", "Activity log", "Logs", AuditLog,
            [("Time", lambda r: r.created_at.strftime("%d/%m/%Y %H:%M")), ("User", lambda r: r.username),
             ("Action", lambda r: r.action), ("Detail", lambda r: (r.detail or "")[:120])],
            order=(AuditLog.created_at.desc(),), date_col=AuditLog.created_at,
            search=[AuditLog.username, AuditLog.action, AuditLog.detail]),
    Section("errors", "Error log", "Logs", ErrorLog,
            [("Time", lambda r: r.created_at.strftime("%d/%m/%Y %H:%M")), ("Source", lambda r: r.source),
             ("Message", lambda r: (r.message or "")[:140])],
            order=(ErrorLog.created_at.desc(),), date_col=ErrorLog.created_at,
            search=[ErrorLog.source, ErrorLog.message]),
]
BY_KEY = {s.key: s for s in SECTIONS}
GROUPS = list(dict.fromkeys(s.group for s in SECTIONS))


def _day(v: str | None, end=False):
    from timeutil import parse_day
    if not v:
        return None
    d = parse_day(v)
    return d + timedelta(days=1) if end else d


def query(sec: Section, p: dict, allowed_companies: list[int] | None = None):
    """select() of the section's rows that match the filters (q, start, end, company)."""
    stmt = select(sec.model)
    if sec.where is not None:
        stmt = stmt.where(sec.where)
    joined = False
    import re
    codes = [c for c in re.split(r"[\s,;]+", str(p.get("codes") or "")) if c]
    q = str(p.get("q") or "").strip()
    company = int(p["company"]) if str(p.get("company") or "").isdigit() else None
    if sec.emp_on is not None and (q or company or codes or allowed_companies is not None):
        stmt = stmt.outerjoin(EMP, sec.emp_on)
        joined = True
    if q:
        like = f"%{q}%"
        conds = [c.ilike(like) for c in sec.search]
        if joined:
            conds += [EMP.employee_code.ilike(like), EMP.full_name.ilike(like), EMP.card_no.ilike(like)]
        if conds:
            stmt = stmt.where(or_(*conds))
    if codes:                                   # a hand-picked group: Employee IDs / card numbers pasted from Excel
        if sec.model is Employee:
            stmt = stmt.where(or_(Employee.employee_code.in_(codes), Employee.card_no.in_(codes)))
        elif joined:
            stmt = stmt.where(or_(EMP.employee_code.in_(codes), EMP.card_no.in_(codes)))
    ccol = sec.company_col if sec.company_col is not None else (EMP.company_id if joined else None)
    if ccol is not None:
        if company:
            stmt = stmt.where(ccol == company)
        if allowed_companies is not None:
            stmt = stmt.where(or_(ccol.in_(allowed_companies or [0]), ccol.is_(None)) if joined else
                              ccol.in_(allowed_companies or [0]))
    if sec.date_col is not None and (p.get("start") or p.get("end")):
        s, e = _day(p.get("start")), _day(p.get("end"), end=True)
        is_dt = getattr(sec.date_col.type, "python_type", date) is datetime
        if s:
            stmt = stmt.where(sec.date_col >= (datetime.combine(s, time.min) if is_dt else s))
        if e:
            stmt = stmt.where(sec.date_col < (datetime.combine(e, time.min) if is_dt else e))
    return stmt


def count(db, sec: Section, p: dict | None = None, allowed=None) -> int:
    stmt = query(sec, p or {}, allowed).with_only_columns(func.count(func.distinct(sec.model.id)))
    return db.scalar(stmt.order_by(None)) or 0


def rows(db, sec: Section, p: dict, allowed=None, page: int = 1, per: int = 100) -> list:
    stmt = query(sec, p, allowed)
    if sec.order is not None:
        stmt = stmt.order_by(*sec.order)
    return list(db.scalars(stmt.offset((page - 1) * per).limit(per)).unique())


def run_delete(db, sec: Section, ids: list[int] | None, p: dict, opts: dict, allowed=None) -> tuple[int, list[str]]:
    """Delete the chosen rows (ids) or, with ids=None, every row matching the filters. Returns (done, kept reasons)."""
    if ids is None:
        targets = list(db.scalars(query(sec, p, allowed)).unique())
    else:
        targets = list(db.scalars(query(sec, {}, allowed).where(sec.model.id.in_(ids or [0]))).unique())
    done, kept = 0, []
    for r in targets:
        why = sec.delete_one(db, r, opts) if sec.delete_one else (db.delete(r) or "")
        if why:
            kept.append(why)
        else:
            done += 1
    return done, kept
