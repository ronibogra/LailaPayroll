"""Festival bonus (Eid-ul-Fitr, Eid-ul-Adha, Durga Puja, Christmas, Buddha Purnima ...).

The bonus is added to that month's salary as a named addition (shown on the payslip and salary sheet).
Rules: % of basic or gross, no bonus below a minimum service, optional pro-rata up to 12 months, optional
religion filter (e.g. Eid bonus for Muslim staff, Puja bonus for Hindu staff)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

NAMES = ["Eid-ul-Fitr bonus", "Eid-ul-Adha bonus", "Durga Puja bonus", "Christmas bonus", "Buddha Purnima bonus",
         "Pohela Boishakh bonus", "Performance bonus"]


@dataclass
class BonusRow:
    employee: object
    months: int
    amount: Decimal
    reason: str


def service_months(joined: date | None, as_of: date) -> int:
    if not joined or joined > as_of:
        return 0
    m = (as_of.year - joined.year) * 12 + (as_of.month - joined.month)
    return m - (1 if as_of.day < joined.day else 0)


def compute(emps, as_of: date, basis: str, percent: Decimal, min_months: int, prorate: bool,
            religion: str = "") -> list[BonusRow]:
    out = []
    for e in emps:
        base = Decimal(e.basic or 0) if basis == "basic" else Decimal(e.gross or 0)
        months = service_months(e.joined_on, as_of)
        if religion and (e.religion or "") != religion:
            out.append(BonusRow(e, months, Decimal(0), f"religion {e.religion or 'not set'}"))
            continue
        if not base:
            out.append(BonusRow(e, months, Decimal(0), "no salary set"))
            continue
        if e.joined_on is None and min_months:
            out.append(BonusRow(e, months, Decimal(0), "joining date not set"))
            continue
        if months < min_months:
            out.append(BonusRow(e, months, Decimal(0), f"service {months} month(s) < {min_months}"))
            continue
        amt = base * percent / 100
        reason = f"{percent:g}% of {basis}"
        if prorate and months < 12:
            amt = amt * Decimal(months) / 12
            reason += f", pro-rata {months}/12"
        out.append(BonusRow(e, months, amt.quantize(Decimal(1), ROUND_HALF_UP), reason))
    return out


def set_item(adj, name: str, amount: Decimal) -> None:
    """Puts (or replaces / removes) a named bonus line in the employee's additions for the month."""
    items = [i for i in json.loads(adj.items_json or "[]") if i.get("name") != name]
    if amount and amount > 0:
        items.append({"name": name, "kind": "ADD", "amount": str(amount)})
    adj.items_json = json.dumps(items) if items else None


def existing(adjs, name: str) -> dict[int, Decimal]:
    out = {}
    for a in adjs:
        for i in json.loads(a.items_json or "[]"):
            if i.get("name") == name:
                out[a.employee_id] = Decimal(str(i.get("amount") or 0))
    return out


def names_in(adjs) -> dict[str, tuple[int, Decimal]]:
    """Bonus-like named additions already entered this month: name -> (people, total)."""
    out: dict[str, list] = {}
    for a in adjs:
        for i in json.loads(a.items_json or "[]"):
            if i.get("kind") == "ADD":
                x = out.setdefault(i["name"], [0, Decimal(0)])
                x[0] += 1
                x[1] += Decimal(str(i.get("amount") or 0))
    return {k: (v[0], v[1]) for k, v in out.items()}
