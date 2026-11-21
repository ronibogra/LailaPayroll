"""Excel/CSV import for leave, holidays, shifts, machines and companies (+ templates for each).
Every file is checked completely first; if any row is wrong nothing is saved and all problems are listed."""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, time

from openpyxl import Workbook, load_workbook
from sqlalchemy import select

from models import Company, Device, Employee, Holiday, Leave, Shift

TEMPLATES = {
    "leaves": (["employee_code", "from", "to", "type", "note"],
               [["12127", "2026-11-05", "2026-11-06", "CASUAL", "family"], ["12160", "2026-11-10", "2026-11-10", "UNPAID", ""]],
               "type: a leave type code from the Leave policy page (CASUAL, SICK, EARNED, MATERNITY, UNPAID ...). "
               "Leave without pay is deducted like absence. Imported leave is approved at once."),
    "holidays": (["date", "name"], [["2026-12-16", "Victory Day"], ["2026-12-25", "Christmas"]], "One row per holiday date."),
    "shifts": (["code", "name", "start", "end", "grace_minutes", "break_minutes", "ot_block_minutes", "ot_cap_minutes",
                "offday_work_is_ot", "early_window_minutes"],
               [["GENERAL", "General 09:00-18:00", "09:00", "18:00", 10, 60, 30, 120, "yes", 180],
                ["NIGHT", "Night 20:00-05:00", "20:00", "05:00", 10, 60, 30, 120, "yes", 180]],
               "End earlier than start = night shift. Existing codes are updated."),
    "machines": (["type", "ip_address", "port", "machine_no", "name", "location", "in_out", "comm_key_or_password",
                  "username", "serial_number", "active"],
                 [["ZKTECO", "192.168.10.17", 4370, 17, "Main Gate 1", "Head Office", "IN", 0, "", "", "yes"],
                  ["HIKVISION", "192.168.10.30", 80, 30, "Lobby", "Head Office", "BOTH", "password", "admin", "", "yes"]],
                 "type: ZKTECO (download by IP), HIKVISION, or ZKTECO_PUSH (needs serial_number). in_out: IN, OUT or BOTH. "
                 "Existing machines (same type + IP) are updated."),
    "companies": (["code", "name", "address"], [["LAILA", "Laila", ""], ["BAND", "Band", ""], ["LAUNDRY", "Laundry Factory", ""]],
                  "Companies under the Head Office. Existing codes are updated."),
    "punches": (["card_no", "punch_time", "device"], [["12127", "2026-11-20 08:01:00", "Gate-1"], ["12127", "2026-11-20 18:05:00", "Gate-1"]],
                "Also accepted: machine exports with columns like AC-No / Employee ID + Date + Time, and ZKTeco attlog.dat."),
}


def template(kind: str) -> bytes:
    head, rows, help_text = TEMPLATES[kind]
    wb = Workbook()
    ws = wb.active
    ws.title = kind.title()
    ws.append(head)
    for r in rows:
        ws.append(r)
    for i in range(1, len(head) + 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = 18
    wb.create_sheet("Help").append([help_text])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def read_table(filename: str, content: bytes) -> list[dict]:
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        ws = load_workbook(io.BytesIO(content), data_only=True, read_only=True).worksheets[0]
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
    elif name.endswith(".csv"):
        text = content.decode("utf-8-sig", "replace")
        rows = list(csv.reader(io.StringIO(text)))
    else:
        raise ValueError("use an Excel (.xlsx) or CSV file - download the template")
    if not rows:
        raise ValueError("the file is empty")
    head = [str(h or "").strip().lower().replace(" ", "_").replace("/", "_") for h in rows[0]]
    out = []
    for n, r in enumerate(rows[1:], start=2):
        if not any(v not in (None, "") for v in r):
            continue
        d = {h: r[i] if i < len(r) else None for i, h in enumerate(head) if h}
        d["_row"] = n
        out.append(d)
    return out


def _s(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _date(v) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    t = _s(v)
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d-%b-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(t[:11].strip(), fmt).date()
        except ValueError:
            pass
    raise ValueError(f"not a date: {t!r}")


def _time(v) -> time:
    if isinstance(v, datetime):
        return v.time().replace(second=0, microsecond=0)
    if isinstance(v, time):
        return v.replace(second=0, microsecond=0)
    if isinstance(v, float):
        mins = round(v % 1 * 1440)
        return time(mins // 60 % 24, mins % 60)
    t = _s(v)
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p"):
        try:
            return datetime.strptime(t, fmt).time()
        except ValueError:
            pass
    raise ValueError(f"not a time: {t!r}")


def _int(v, default: int) -> int:
    t = _s(v)
    return int(float(t)) if t else default


def _yes(v, default=True) -> bool:
    t = _s(v).lower()
    return default if not t else t in ("1", "yes", "y", "true", "active", "on")


def _apply(kind: str, rows: list[dict], fn) -> tuple[int, int, list[str]]:
    errors, done = [], []
    for r in rows:
        try:
            done.append(fn(r))
        except (ValueError, TypeError, KeyError) as exc:
            errors.append(f"row {r['_row']}: {exc}")
    return sum(1 for d in done if d == "added"), sum(1 for d in done if d == "updated"), errors


def import_kind(db, kind: str, filename: str, content: bytes, user: dict) -> dict:
    if kind not in TEMPLATES or kind == "punches":
        raise ValueError("unknown import")
    rows = read_table(filename, content)
    scope = user.get("company_id")

    if kind == "leaves":
        from services import leave as leave_svc
        emps = {e.employee_code: e for e in db.scalars(select(Employee))}
        ltypes = {t.code: t for t in leave_svc.types(db)}

        def fn(r):
            code = _s(r.get("employee_code"))
            e = emps.get(code)
            if not e:
                raise ValueError(f"unknown employee_code {code!r}")
            if scope and e.company_id != scope:
                raise ValueError(f"{code} belongs to another company")
            s, t = _date(r.get("from")), _date(r.get("to") or r.get("from"))
            typ = (_s(r.get("type")) or "CASUAL").upper()
            lt = ltypes.get(typ)
            if not lt:
                raise ValueError(f"unknown leave type {typ} - use one of {', '.join(ltypes)}")
            # imported leave is HR-granted history: documents are kept on paper, yearly balance not enforced
            days = leave_svc.validate(db, e, lt, s, t, has_document=True, ignore_balance=True)
            r["_obj"] = Leave(employee_id=e.id, start_date=s, end_date=t, leave_type=typ, paid=lt.paid, days=days,
                              status="APPROVED", source="HR", requested_by=user["username"], requested_at=datetime.now(),
                              note=_s(r.get("note")) or None)
            return "added"
    elif kind == "holidays":
        have = {h.day: h for h in db.scalars(select(Holiday))}

        def fn(r):
            d = _date(r.get("date"))
            name = _s(r.get("name")) or "Holiday"
            if d in have:
                have[d].name = name
                return "updated"
            r["_obj"] = Holiday(day=d, name=name)
            have[d] = r["_obj"]
            return "added"
    elif kind == "shifts":
        have = {s.code: s for s in db.scalars(select(Shift))}

        def fn(r):
            code = _s(r.get("code")).upper()
            if not code:
                raise ValueError("code is empty")
            s = have.get(code)
            new = s is None
            if new:
                s = Shift(code=code)
                r["_obj"] = s
                have[code] = s
            s.name = _s(r.get("name")) or code
            s.start, s.end = _time(r.get("start")), _time(r.get("end"))
            s.grace_minutes = _int(r.get("grace_minutes"), 10)
            s.break_minutes = _int(r.get("break_minutes"), 60)
            s.break_after_minutes = 300
            s.ot_round_minutes = max(1, _int(r.get("ot_block_minutes"), 30))
            s.max_ot_minutes = _int(r.get("ot_cap_minutes"), 120)
            s.offday_work_is_ot = _yes(r.get("offday_work_is_ot"))
            s.early_window_minutes = min(360, _int(r.get("early_window_minutes"), 180))
            return "added" if new else "updated"
    elif kind == "machines":
        from services.devices import encrypt
        have = {(d.brand, d.ip_address or d.serial_number): d for d in db.scalars(select(Device))}

        def fn(r):
            brand = (_s(r.get("type")) or "ZKTECO").upper().replace(" ", "_")
            if brand not in ("ZKTECO", "HIKVISION", "ZKTECO_PUSH"):
                raise ValueError(f"type must be ZKTECO, HIKVISION or ZKTECO_PUSH, not {brand}")
            ip, serial = _s(r.get("ip_address")), _s(r.get("serial_number"))
            if brand != "ZKTECO_PUSH" and not ip:
                raise ValueError("ip_address is empty")
            if brand == "ZKTECO_PUSH" and not serial:
                raise ValueError("serial_number is needed for ZKTECO_PUSH")
            key = (brand, ip or serial)
            d = have.get(key)
            new = d is None
            if new:
                d = Device(brand=brand, approved=True)
                r["_obj"] = d
                have[key] = d
            d.ip_address, d.serial_number = ip or None, serial or d.serial_number
            d.port = _int(r.get("port"), 4370 if brand == "ZKTECO" else 80)
            d.machine_no = _int(r.get("machine_no"), 0) or None
            d.name = _s(r.get("name")) or f"Machine {d.machine_no or ip or serial}"
            d.location = _s(r.get("location")) or None
            io_ = (_s(r.get("in_out")) or "BOTH").upper()
            d.direction = io_ if io_ in ("IN", "OUT", "BOTH") else "BOTH"
            pw = _s(r.get("comm_key_or_password"))
            if brand == "ZKTECO" and pw and not pw.isdigit():
                raise ValueError("Comm Key must be a number")
            if pw:
                d.password_enc = encrypt(pw)
            d.username = _s(r.get("username")) or d.username
            d.enabled = _yes(r.get("active"))
            d.approved = True
            return "added" if new else "updated"
    else:  # companies
        if scope:
            raise ValueError("only Head Office users can import companies")
        have = {c.code: c for c in db.scalars(select(Company))}

        def fn(r):
            code = _s(r.get("code")).upper()[:20]
            if not code or not _s(r.get("name")):
                raise ValueError("code and name are required")
            c = have.get(code)
            new = c is None
            if new:
                c = Company(code=code, active=True)
                r["_obj"] = c
                have[code] = c
            c.name = _s(r.get("name"))[:150]
            c.address = _s(r.get("address")) or c.address
            return "added" if new else "updated"

    added, updated, errors = _apply(kind, rows, fn)
    if errors:
        db.rollback()
        raise ValueError("Nothing imported. Fix these rows: " + " | ".join(errors[:30])
                         + (f" | ... and {len(errors) - 30} more" if len(errors) > 30 else ""))
    for r in rows:
        if r.get("_obj") is not None:
            db.add(r["_obj"])
    db.commit()
    return {"added": added, "updated": updated}
