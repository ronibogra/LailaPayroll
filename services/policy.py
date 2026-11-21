"""Settings stored in the database.

* Group settings (company_id 0): weekend days, factory location for mobile punch, setup flag.
* Pay policy: group default (company_id 0) with optional per-company overrides."""
from dataclasses import fields
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Setting
from payroll.engine import PayPolicy

LABELS = {
    "medical": "Medical allowance (Tk)",
    "transport": "Transport allowance (Tk)",
    "food": "Food allowance (Tk)",
    "house_rent_pct": "House rent as share of basic (0.50 = 50%)",
    "ot_divisor": "OT hourly divisor (208 = 26 days x 8h)",
    "ot_multiplier": "OT multiplier (2 = double basic)",
    "pay_extra_ot": "Pay OT above daily cap (true/false)",
    "absent_basis": "Absent deduction from (basic / gross)",
    "absent_divisor": "Absent divisor (30 / month_days)",
    "attendance_bonus": "Attendance bonus (Tk, 0 = off)",
    "bonus_max_late_days": "Late days allowed for attendance bonus",
    "stamp_charge": "Revenue stamp deduction (Tk)",
    "round_net_to_taka": "Round net pay to whole taka (true/false)",
    "ot_enabled": "Overtime applies (true/false) - false for Head Office",
    "late_days_per_deduction": "Late days that cost a deduction (e.g. 3; 0 = off)",
    "late_deduction_days": "Days deducted each time (e.g. 1)",
    "half_day_late_minutes": "Late more than N minutes = half day deducted (0 = off)",
}
GROUP_DEFAULTS = {"weekend_days": "4", "geofence_lat": "0", "geofence_lon": "0", "geofence_radius_m": "200"}
POLICY_PREFIX = "pay."


# ------------------------------------------------------------ generic
def get_setting(db: Session, key: str, company_id: int = 0, default: str | None = None) -> str | None:
    row = db.scalar(select(Setting).where(Setting.company_id == company_id, Setting.key == key))
    if row is not None:
        return row.value
    return default if default is not None else GROUP_DEFAULTS.get(key)


def set_setting(db: Session, key: str, value: str, company_id: int = 0, commit: bool = True) -> None:
    row = db.scalar(select(Setting).where(Setting.company_id == company_id, Setting.key == key))
    if row:
        row.value = str(value)
    else:
        db.add(Setting(company_id=company_id, key=key, value=str(value)))
    if commit:
        db.commit()


def delete_setting(db: Session, key: str, company_id: int) -> None:
    row = db.scalar(select(Setting).where(Setting.company_id == company_id, Setting.key == key))
    if row:
        db.delete(row)


def weekend_set(db: Session) -> set[int]:
    raw = get_setting(db, "weekend_days") or ""
    return {int(x) for x in raw.split(",") if x.strip().isdigit()}


def geofence(db: Session) -> tuple[float, float, int]:
    return (float(get_setting(db, "geofence_lat") or 0), float(get_setting(db, "geofence_lon") or 0),
            int(get_setting(db, "geofence_radius_m") or 200))


# ------------------------------------------------------------ pay policy
def _parse(kind, raw: str):
    if kind in (bool, "bool"):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if kind in (int, "int"):
        return int(raw)
    if kind in (Decimal, "Decimal"):
        return Decimal(str(raw))
    return str(raw).strip()


def _validate(key: str, raw: str, kind) -> None:
    _parse(kind, raw)
    if key == "absent_basis" and raw not in ("basic", "gross"):
        raise ValueError("absent_basis must be basic or gross")
    if key == "absent_divisor" and raw not in ("30", "month_days"):
        raise ValueError("absent_divisor must be 30 or month_days")


def _stored(db: Session, company_id: int) -> dict[str, str]:
    return {s.key[len(POLICY_PREFIX):]: s.value for s in db.scalars(
        select(Setting).where(Setting.company_id == company_id, Setting.key.startswith(POLICY_PREFIX)))}


def load_policy(db: Session, company_id: int = 0) -> PayPolicy:
    """Group default, then the company's own overrides on top."""
    policy = PayPolicy()
    stored = _stored(db, 0)
    if company_id:
        stored.update(_stored(db, company_id))
    for f in fields(PayPolicy):
        if f.name in stored:
            setattr(policy, f.name, _parse(f.type, stored[f.name]))
    return policy


def save_policy(db: Session, values: dict[str, str], company_id: int = 0) -> None:
    """For a company, a value equal to the group default is stored as 'no override'."""
    known = {f.name: f.type for f in fields(PayPolicy)}
    group = load_policy(db, 0)
    for key, raw in values.items():
        if key not in known:
            continue
        raw = str(raw).strip()
        _validate(key, raw, known[key])
        if company_id and _parse(known[key], raw) == getattr(group, key):
            delete_setting(db, POLICY_PREFIX + key, company_id)
        else:
            set_setting(db, POLICY_PREFIX + key, raw, company_id, commit=False)
    db.commit()


def policy_rows(db: Session, company_id: int = 0) -> list[tuple[str, str, str, bool]]:
    """(key, label, effective value, overridden_for_company)"""
    policy = load_policy(db, company_id)
    own = _stored(db, company_id) if company_id else {}
    return [(f.name, LABELS.get(f.name, f.name), str(getattr(policy, f.name)), f.name in own)
            for f in fields(PayPolicy)]
