import csv
import io
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import AttendanceLog

FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
           "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p")


def parse_dt(value) -> datetime:
    if isinstance(value, datetime):
        return value.replace(microsecond=0, tzinfo=None)
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text).replace(microsecond=0, tzinfo=None)
    except ValueError:
        pass
    for fmt in FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"unrecognised date/time: {text!r}")


def insert_punches(db: Session, rows: list[tuple[str, datetime, str | None]], source: str,
                   user: str | None = None) -> tuple[int, int]:
    """Insert (card_no, punch_time, device) rows, skipping ones already stored. Returns (inserted, skipped)."""
    if not rows:
        return 0, 0
    lo = min(r[1] for r in rows)
    hi = max(r[1] for r in rows)
    existing = set(db.execute(select(AttendanceLog.card_no, AttendanceLog.punch_time)
                              .where(AttendanceLog.punch_time >= lo, AttendanceLog.punch_time <= hi)).all())
    new, seen = [], set()
    for card, t, device in rows:
        key = (card, t)
        if key in existing or key in seen:
            continue
        seen.add(key)
        new.append(AttendanceLog(card_no=card, punch_time=t, device=device, source=source, created_by=user))
    db.add_all(new)
    db.commit()
    return len(new), len(rows) - len(new)


def import_attendance_csv(db: Session, content: bytes, user: str | None = None) -> dict:
    """CSV columns: card_no (or employee_code), punch_time, optional device.
    The whole file is checked first - nothing is saved if any row is bad."""
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    cols = {c.strip().lower(): c for c in (reader.fieldnames or [])}
    card_col = cols.get("card_no") or cols.get("employee_code")
    time_col = cols.get("punch_time")
    dev_col = cols.get("device") or cols.get("device_serial")
    if not card_col or not time_col:
        raise ValueError("CSV needs columns card_no (or employee_code) and punch_time")
    rows, errors = [], []
    for n, row in enumerate(reader, start=2):
        card = (row.get(card_col) or "").strip()
        if not card:
            continue
        try:
            device = ((row.get(dev_col) or "").strip() or None) if dev_col else None
            rows.append((card, parse_dt(row[time_col]), device))
        except ValueError as exc:
            errors.append(f"line {n}: {exc}")
    if errors:
        raise ValueError("Nothing imported. Fix these rows: " + "; ".join(errors[:20]))
    inserted, skipped = insert_punches(db, rows, "CSV", user)
    return {"inserted": inserted, "skipped_duplicates": skipped}
