import csv
import io

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import AttendanceLog

from integration.dtparse import parse_dt  # noqa: F401  (re-exported)


def insert_punches(db: Session, rows: list[tuple], source: str, user: str | None = None,
                   device_id: int | None = None, direction: str | None = None) -> tuple[int, int]:
    """Insert (card_no, punch_time, device[, direction]) rows, skipping ones already stored.
    Returns (inserted, skipped). Works in batches so 100,000+ rows from a machine are fine."""
    if not rows:
        return 0, 0
    lo = min(r[1] for r in rows)
    hi = max(r[1] for r in rows)
    existing = set(db.execute(select(AttendanceLog.card_no, AttendanceLog.punch_time)
                              .where(AttendanceLog.punch_time >= lo, AttendanceLog.punch_time <= hi)).all())
    new, seen = [], set()
    for r in rows:
        card, t, device = str(r[0]).strip(), r[1], r[2]
        key = (card, t)
        if not card or key in existing or key in seen:
            continue
        seen.add(key)
        new.append(AttendanceLog(card_no=card[:50], punch_time=t, device=(device or None) and str(device)[:100],
                                 source=source, created_by=user, device_id=device_id,
                                 direction=(r[3] if len(r) > 3 and r[3] else direction)))
        if len(new) >= 5000:
            db.add_all(new)
            db.flush()
            new = []
    db.add_all(new)
    db.commit()
    inserted = len(seen)
    return inserted, len(rows) - inserted


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
