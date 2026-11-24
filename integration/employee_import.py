"""Bulk employee upload/update from Excel. Matches on employee_code.

Only employee_code is required. card_no defaults to employee_code, full_name to the code, company to the
only company (or the importing user's company). For an existing employee, empty cells keep the old value."""
import io
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from openpyxl import Workbook, load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Company, Employee, Department, Shift
from payroll.engine import split_gross
from services.policy import load_policy

_DAYS = {d: i for i, d in enumerate(("mon", "tue", "wed", "thu", "fri", "sat", "sun"))}


def parse_weekly_off(text: str) -> str:
    """'Fri' / 'Friday' / 'Sat, Sun' / 'none' -> '4' / '4' / '5,6' / 'NONE'."""
    t = text.strip().lower()
    if t in ("none", "no", "0", "-"):
        return "NONE"
    days = set()
    for part in t.replace("/", ",").replace("+", ",").replace(" ", ",").split(","):
        if not part:
            continue
        if part[:3] not in _DAYS:
            raise ValueError(f"weekly_off: '{text}' - use day names like Fri or Sat,Sun, or NONE")
        days.add(_DAYS[part[:3]])
    return ",".join(str(d) for d in sorted(days))


COLUMNS = ["company", "employee_code", "card_no", "full_name", "department", "designation", "section", "grade",
           "shift_code", "joined_on", "left_on", "gross", "basic", "house_rent", "medical", "transport", "food",
           "payment_mode", "bank_name", "account_no", "status",
           "father_name", "mother_name", "spouse_name", "date_of_birth", "gender", "blood_group", "religion",
           "marital_status", "nid", "birth_certificate_no", "passport_no", "phone", "email", "emergency_contact", "present_address", "permanent_address",
           "education", "notes", "weekly_off", "bank_part", "monthly_tax"]
TEXT_PROFILE = ("father_name", "mother_name", "spouse_name", "gender", "blood_group", "religion", "marital_status",
                "nid", "birth_certificate_no", "passport_no", "phone", "email", "emergency_contact", "present_address", "permanent_address", "education",
                "notes", "designation", "section", "grade", "bank_name", "account_no")
SALARY = ("basic", "house_rent", "medical", "transport", "food")
ALIASES = {"code": "employee_code", "emp code": "employee_code", "employee id": "employee_code",
           "emp id": "employee_code", "id": "employee_code", "card": "card_no", "card no": "card_no",
           "machine id": "card_no", "user id": "card_no", "name": "full_name", "employee name": "full_name",
           "dept": "department", "joining date": "joined_on", "join date": "joined_on", "dob": "date_of_birth",
           "birth date": "date_of_birth", "mobile": "phone", "phone no": "phone", "gross salary": "gross",
           "salary": "gross", "weekly off": "weekly_off", "off day": "weekly_off", "weekend": "weekly_off", "nid no": "nid", "national id": "nid", "father": "father_name", "mother": "mother_name"}


def template_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Employees"
    ws.append(COLUMNS)
    ws.append(["HO", "12127", "12127", "Md. Example", "Accounts", "Officer", "", "", "GENERAL", "2024-01-15", None,
               25000, None, None, None, None, None, "BANK", "Dutch-Bangla Bank", "1234567890", "ACTIVE",
               "Father Name", "Mother Name", "", "1990-05-01", "Male", "B+", "Islam", "Married", "", "", "", "01700000000",
               "", "Wife: 01800000000", "Dhaka", "Bogura", "BBA", "", "Fri", "", ""])
    help_ws = wb.create_sheet("Help")
    for line in [
        "Only employee_code is required. Fill whatever else you have - you can upload again later to add more.",
        "card_no = the user ID on the punch machines. If empty, the employee_code is used.",
        "company = company code (e.g. HO, LAILA, BAND, LAUNDRY). Can be empty if there is only one company.",
        "Existing employees (same employee_code) are updated; EMPTY cells keep the old value.",
        "gross: salary is split automatically into basic/house rent/medical/transport/food (Settings > pay policy).",
        "If you fill basic, also fill house_rent, medical, transport, food: they must add up to gross.",
        "payment_mode: CASH, BANK or MOBILE. status: ACTIVE or INACTIVE. Dates: YYYY-MM-DD or real Excel dates.",
        "shift_code must exist on the Shifts page (empty = GENERAL shift). Photos are added on the employee page.",
        "weekly_off: the employee's own day(s) off, e.g. Fri, or Sat,Sun, or NONE (works every day). Empty = shift / Settings.",
        "monthly_tax: income tax (TDS) in Tk deducted every month (Payroll > Tax can change one month).",
        "bank_part: salary partly by bank / wallet, rest in cash: 5000 = Tk 5,000 to bank, 60% = 60% to bank, NONE = all by pay mode.",
        "Column names like 'Name', 'Employee ID', 'Card No', 'Joining Date', 'Mobile', 'Gross Salary' also work.",
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
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d-%b-%Y", "%d %b %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s[:11].strip(), fmt).date()
        except ValueError:
            pass
    raise ValueError(f"not a date: {s!r}")


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


def _header(h) -> str:
    k = (_text(h) or "").lower().replace("_", " ").strip()
    k = ALIASES.get(k, k)
    return k.replace(" ", "_")


def import_employees(db: Session, content: bytes, only_company_id: int | None = None, allow_salary: bool = True) -> dict:
    """only_company_id: set for users limited to one company - other companies' rows are refused."""
    try:
        ws = load_workbook(io.BytesIO(content), data_only=True).worksheets[0]
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"cannot read the Excel file ({exc}). Save it as .xlsx and try again")
    rows = ws.iter_rows(values_only=True)
    header = [_header(h) for h in next(rows, [])]
    if "employee_code" not in header:
        raise ValueError("no employee_code column. Download the template to see the column names")
    idx = {h: i for i, h in enumerate(header) if h}

    all_companies = list(db.scalars(select(Company)))
    companies = {}
    for c in all_companies:
        companies[c.code.lower()] = c
        companies[c.name.lower()] = c
    default_company = (db.get(Company, only_company_id) if only_company_id
                       else (all_companies[0] if len(all_companies) == 1 else None))
    policies = {}
    depts = {d.name.lower(): d for d in db.scalars(select(Department))}
    shifts = {s.code.upper(): s for s in db.scalars(select(Shift))}
    existing = {e.employee_code: e for e in db.scalars(select(Employee))}
    cards = {e.card_no: e.employee_code for e in existing.values()}

    parsed, errors = [], []
    for n, row in enumerate(rows, start=2):
        get = lambda c: row[idx[c]] if c in idx and idx[c] < len(row) else None  # noqa: E731, B023
        code = _text(get("employee_code"))
        if not code:
            continue
        old = existing.get(code)
        try:
            cname = _text(get("company"))
            if cname:
                comp = companies.get(cname.lower())
                if comp is None:
                    raise ValueError(f"unknown company {cname!r} - add it under Settings > Companies "
                                     f"(known: {', '.join(c.code for c in all_companies)})")
            else:
                comp = old.company if old else default_company
                if comp is None:
                    raise ValueError("company is empty - fill the company code "
                                     f"({', '.join(c.code for c in all_companies)})")
            if only_company_id and comp.id != only_company_id:
                raise ValueError("you can only import employees of your own company")
            if old and only_company_id and old.company_id != only_company_id:
                raise ValueError("this employee belongs to another company")
            card = _text(get("card_no")) or (old.card_no if old else code)
            if cards.get(card, code) != code:
                raise ValueError(f"card_no {card} already belongs to {cards[card]}")
            cards[card] = code
            p = {"company_id": comp.id, "employee_code": code, "card_no": card,
                 "full_name": _text(get("full_name")) or (old.full_name if old else code)}
            if comp.id not in policies:
                policies[comp.id] = load_policy(db, comp.id)
            gross = _money(get("gross")) if allow_salary else None
            parts = {c: _money(get(c)) for c in SALARY} if allow_salary else {c: None for c in SALARY}
            if gross is not None:
                if parts["basic"] is None:
                    split = split_gross(gross, policies[comp.id]) if gross > 0 else None
                    parts = {c: (getattr(split, c) if split else Decimal(0)) for c in SALARY}
                elif any(v is None for v in parts.values()):
                    raise ValueError("when basic is filled, house_rent, medical, transport and food are required")
                elif sum(parts.values()) != gross:
                    raise ValueError(f"basic..food add up to {sum(parts.values())}, not gross {gross}")
                p["gross"] = gross
                p.update(parts)
            elif not old:
                p.update({"gross": Decimal(0), **{c: Decimal(0) for c in SALARY}})
            shift_code = (_text(get("shift_code")) or "").upper()
            if shift_code:
                if shift_code not in shifts:
                    raise ValueError(f"unknown shift_code {shift_code}")
                p["shift"] = shifts[shift_code]
            mode = (_text(get("payment_mode")) or "").upper() if allow_salary else ""
            if mode:
                if mode not in ("CASH", "BANK", "MOBILE"):
                    raise ValueError("payment_mode must be CASH, BANK or MOBILE")
                p["payment_mode"] = mode
            mt = _money(get("monthly_tax")) if allow_salary else None
            if mt is not None:
                if mt < 0:
                    raise ValueError("monthly_tax cannot be negative")
                p["monthly_tax"] = mt
            bp = (_text(get("bank_part")) or "").strip().replace(",", "") if allow_salary else ""
            if bp:
                if bp.upper() in ("NONE", "NO", "0"):
                    p["split_mode"], p["split_value"] = "", Decimal(0)
                else:
                    pct = bp.endswith("%")
                    try:
                        v = Decimal(bp.rstrip("%").strip())
                    except Exception:  # noqa: BLE001
                        raise ValueError(f"bank_part '{bp}' - use an amount like 5000 or a percent like 60%")
                    if v <= 0 or (pct and v >= 100):
                        raise ValueError(f"bank_part '{bp}' is out of range")
                    p["split_mode"], p["split_value"] = ("PERCENT" if pct else "FIXED"), v
            wo = (_text(get("weekly_off")) or "").strip()
            if wo:
                p["weekly_off"] = parse_weekly_off(wo)
            status = (_text(get("status")) or "").upper()
            if status:
                if status not in ("ACTIVE", "INACTIVE"):
                    raise ValueError("status must be ACTIVE or INACTIVE")
                p["employment_status"] = status
            for f in ("joined_on", "left_on", "date_of_birth"):
                v = _date(get(f))
                if v:
                    p[f] = v
            for f in TEXT_PROFILE:
                if not allow_salary and f in ("bank_name", "account_no"):
                    continue
                v = _text(get(f))
                if v:
                    p[f] = v[:300]
            dname = _text(get("department"))
            if dname:
                p["dept_name"] = dname
            parsed.append(p)
        except (ValueError, TypeError) as exc:
            errors.append(f"row {n} ({code}): {exc}")
    if errors:
        raise ValueError("Nothing imported. Fix these rows: " + " | ".join(errors[:30])
                         + (f" | ... and {len(errors) - 30} more" if len(errors) > 30 else ""))

    added = updated = 0
    for p in parsed:
        dname = p.pop("dept_name", None)
        e = existing.get(p["employee_code"])
        if e is None:
            e = Employee(employee_code=p["employee_code"], employment_status="ACTIVE", payment_mode="CASH")
            db.add(e)
            existing[e.employee_code] = e
            added += 1
        else:
            updated += 1
        if dname:
            dept = depts.get(dname.lower())
            if dept is None:
                dept = Department(name=dname)
                db.add(dept)
                depts[dname.lower()] = dept
            e.department = dept
        for k, v in p.items():
            setattr(e, k, v)
    db.commit()
    return {"added": added, "updated": updated}
