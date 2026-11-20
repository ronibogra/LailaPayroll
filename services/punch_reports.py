"""Punch record queries and the first & last punch report."""
from collections import OrderedDict
from datetime import date, datetime, time, timedelta

from sqlalchemy import select, or_
from sqlalchemy.orm import Session

from models import AttendanceLog, Employee


def punch_query(db: Session, start: date, end: date, company_id: int | None = None, q: str = "",
                device_id: int | None = None, source: str = "", only_unknown: bool = False):
    stmt = select(AttendanceLog).where(AttendanceLog.punch_time >= datetime.combine(start, time.min),
                                       AttendanceLog.punch_time < datetime.combine(end + timedelta(days=1), time.min))
    if company_id:
        stmt = stmt.where(AttendanceLog.card_no.in_(select(Employee.card_no).where(Employee.company_id == company_id)))
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(AttendanceLog.card_no == q.strip(), AttendanceLog.card_no.in_(
            select(Employee.card_no).where(or_(Employee.full_name.ilike(like), Employee.employee_code == q.strip())))))
    if device_id:
        stmt = stmt.where(AttendanceLog.device_id == device_id)
    if source:
        stmt = stmt.where(AttendanceLog.source == source)
    if only_unknown:
        stmt = stmt.where(AttendanceLog.card_no.not_in(select(Employee.card_no)))
    return stmt


def employees_by_card(db: Session, cards) -> dict[str, Employee]:
    cards = list({c for c in cards})
    out = {}
    for i in range(0, len(cards), 500):
        for e in db.scalars(select(Employee).where(Employee.card_no.in_(cards[i:i + 500]))):
            out[e.card_no] = e
    return out


def first_last(db: Session, start: date, end: date, company_id: int | None = None, q: str = "",
               include_unknown: bool = True) -> list[dict]:
    """One row per employee ID per calendar day: first punch, last punch, number of punches, hours between."""
    stmt = punch_query(db, start, end, company_id, q).with_only_columns(
        AttendanceLog.card_no, AttendanceLog.punch_time, AttendanceLog.device).order_by(
        AttendanceLog.card_no, AttendanceLog.punch_time)
    groups: "OrderedDict[tuple, dict]" = OrderedDict()
    for card, t, dev in db.execute(stmt):
        key = (card, t.date())
        g = groups.get(key)
        if g is None:
            groups[key] = {"card_no": card, "date": t.date(), "first": t, "last": t, "count": 1,
                           "first_device": dev, "last_device": dev}
        else:
            g["last"], g["last_device"] = t, dev
            g["count"] += 1
    emps = employees_by_card(db, [k[0] for k in groups])
    rows = []
    for g in groups.values():
        e = emps.get(g["card_no"])
        if e is None and not include_unknown:
            continue
        g["employee"] = e
        g["hours"] = round((g["last"] - g["first"]).total_seconds() / 3600, 2) if g["count"] > 1 else 0
        rows.append(g)
    rows.sort(key=lambda r: (r["date"], (r["employee"].company.code if r["employee"] and r["employee"].company else "~"),
                             r["employee"].employee_code if r["employee"] else r["card_no"]))
    return rows
