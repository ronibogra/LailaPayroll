"""Bulk employee upload from Excel. Matches on employee_code: existing employees are updated."""
import io
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from openpyxl import Workbook, load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Company, Employee, Department, Shift
from payroll.engine import split_gross
from services.policy import load_policy

COLUMNS = ["company", "employee_code", "card_no", "full_name", "department", "designation", "section", "grade",
           "shift_code", "joined_on", "left_on", "gross", "basic", "house_rent", "medical", "transport",
           "food", "payment_mode", "bank_name", "account_no", "status"]
REQUIRED = {"company", "employee_code", "card_no", "full_name", "gross"}
SALARY = ("basic", "house_rent", "medical", "transport", "food")


def template_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Employees"
    ws.append(COLUMNS)
    ws.append(["HO", "LG0001", "1001", "Md. Example", "Sewing", "Operator", "Line-1", "4", "GENERAL",
               "2024-01-15", None, 12500, None, None, None, None, None, "BANK", "Dutch-Bangla Bank",
               "1234567890", "ACTIVE"])
    help_ws = wb.create_sheet("Help")
    for line in [
        "company, employee_code, card_no, full_name and gross are required.",
        "company = the company code or name from Settings > Companies (e.g. HO).",
        "card_no must be unique across ALL companies: the head-office machines are shared.",
        "card_no = the user ID enrolled on the ZKTeco / Hikvision machine.",
        "Leave basic..food blank to split gross automatically with the Settings policy.",
        "If you fill basic, also fill house_rent, medical, transport, food: they must add up to gross.",
        "payment_mode: CASH, BANK or MOBILE. status: ACTIVE or INACTIVE.",
        "Dates: YYYY-MM-DD. shift_code must exist on the Shifts page (blank = GENERAL shift).",
    ]:
        help_ws.append([line])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _date(v):
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v).strip()[:10])


def _money(v):
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v).replace(",", "").strip())
    except InvalidOperation:
        raise ValueError(f"not a number: {v!r}")


def _text(v):
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip()
    return s or None


def import_employees(db: Session, content: bytes, only_company_id: int | None = None) -> dict:
    """only_company_id: set for users limited to one company - other companies' rows are refused."""
    ws = load_workbook(io.BytesIO(content), data_only=True).worksheets[0]
    rows = ws.iter_rows(values_only=True)
    header = [(_text(h) or "").lower() for h in next(rows)]
    missing = REQUIRED - set(header)
    if missing:
        raise ValueError(f"missing columns: {', '.join(sorted(missing))}")
    idx = {h: i for i, h in enumerate(header) if h}

    companies = {}
    for c in db.scalars(select(Company)):
        companies[c.code.lower()] = c
        companies[c.name.lower()] = c
    policies = {}
    depts = {d.name.lower(): d for d in db.scalars(select(Department))}
    shifts = {s.code.upper(): s for s in db.scalars(select(Shift))}
    existing = {e.employee_code: e for e in db.scalars(select(Employee))}
    cards = {e.card_no: e.employee_code for e in existing.values()}

    parsed, errors = [], []
    for n, row in enumerate(rows, start=2):
        get = lambda c: row[idx[c]] if c in idx and idx[c] < len(row) else None
        code = _text(get("employee_code"))
        if not code:
            continue
        try:
            comp = companies.get((_text(get("company")) or "").lower())
            if comp is None:
                raise ValueError(f"unknown company {_text(get('company'))!r} - add it under Settings > Companies")
            if only_company_id and comp.id != only_company_id:
                raise ValueError("you can only import employees of your own company")
            if comp.id not in policies:
                policies[comp.id] = load_policy(db, comp.id)
            policy = policies[comp.id]
            card = _text(get("card_no"))
            name = _text(get("full_name"))
            gross = _money(get("gross"))
            if not card or not name or not gross:
                raise ValueError("card_no, full_name and gross are required")
            if cards.get(card, code) != code:
                raise ValueError(f"card_no {card} already belongs to {cards[card]}")
            cards[card] = code
            parts = {c: _money(get(c)) for c in SALARY}
            if parts["basic"] is None:
                split = split_gross(gross, policy)
                parts = {c: getattr(split, c) for c in SALARY}
            elif any(v is None for v in parts.values()):
                raise ValueError("when basic is filled, house_rent, medical, transport and food are required")
            elif sum(parts.values()) != gross:
                raise ValueError(f"basic..food add up to {sum(parts.values())}, not gross {gross}")
            shift_code = (_text(get("shift_code")) or "").upper()
            if shift_code and shift_code not in shifts:
                raise ValueError(f"unknown shift_code {shift_code}")
            mode = (_text(get("payment_mode")) or "CASH").upper()
            if mode not in ("CASH", "BANK", "MOBILE"):
                raise ValueError("payment_mode must be CASH, BANK or MOBILE")
            parsed.append(dict(
                company_id=comp.id, employee_code=code, card_no=card, full_name=name, dept=_text(get("department")),
                designation=_text(get("designation")), section=_text(get("section")),
                grade=_text(get("grade")), shift=shifts.get(shift_code), joined_on=_date(get("joined_on")),
                left_on=_date(get("left_on")), gross=gross, payment_mode=mode,
                bank_name=_text(get("bank_name")), account_no=_text(get("account_no")),
                status=(_text(get("status")) or "ACTIVE").upper(), **parts))
        except (ValueError, TypeError) as exc:
            errors.append(f"row {n} ({code}): {exc}")
    if errors:
        raise ValueError("Nothing imported. Fix these rows: " + " | ".join(errors[:30]))

    added = updated = 0
    for p in parsed:
        dept_name = p.pop("dept")
        dept = None
        if dept_name:
            dept = depts.get(dept_name.lower())
            if dept is None:
                dept = Department(name=dept_name)
                db.add(dept)
                depts[dept_name.lower()] = dept
        shift = p.pop("shift")
        status = p.pop("status")
        e = existing.get(p["employee_code"])
        if e is not None and only_company_id and e.company_id != only_company_id:
            raise ValueError(f"{p['employee_code']} belongs to another company")
        if e is None:
            e = Employee(employee_code=p["employee_code"])
            db.add(e)
            added += 1
        else:
            updated += 1
        for k, v in p.items():
            setattr(e, k, v)
        e.department, e.shift, e.employment_status = dept, shift, status
    db.commit()
    return {"added": added, "updated": updated}
