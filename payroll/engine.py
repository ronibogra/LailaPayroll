"""Payroll engine (pure Python, no database).

Every rate and rule lives in PayPolicy so HR / Accounts can set them to
match Laila Group policy. The defaults follow common Bangladesh practice
and MUST be confirmed by HR before paying anyone:

* Gross split: basic = (gross - medical - transport - food) / 1.5,
  house rent = 50% of basic (RMG wage-board structure, Dec 2023).
* OT rate per hour = basic / 208 x 2  (Labour Act s.108: twice basic).
* Absent / unpaid-leave deduction = basic / 30 per day.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from decimal import Decimal, ROUND_HALF_UP

from attendance.rules import MonthSummary

D = Decimal
ZERO = D("0")


def money(x) -> Decimal:
    return D(x).quantize(D("0.01"), rounding=ROUND_HALF_UP)


def taka(x) -> Decimal:
    return D(x).quantize(D("1"), rounding=ROUND_HALF_UP)


@dataclass
class PayPolicy:
    medical: Decimal = D("750")
    transport: Decimal = D("450")
    food: Decimal = D("1250")
    house_rent_pct: Decimal = D("0.50")
    ot_divisor: Decimal = D("208")
    ot_multiplier: Decimal = D("2")
    pay_extra_ot: bool = True              # pay OT above the daily compliance cap
    absent_basis: str = "basic"            # "basic" or "gross"
    absent_divisor: str = "30"             # "30" or "month_days"
    attendance_bonus: Decimal = D("0")     # 0 = no attendance bonus
    bonus_max_late_days: int = 0           # allowed late days and still get bonus
    stamp_charge: Decimal = D("0")         # revenue stamp deducted from net pay
    round_net_to_taka: bool = True
    ot_enabled: bool = True                # False: no overtime pay for this company (e.g. Head Office)
    late_days_per_deduction: int = 0       # e.g. 3 = every 3 late days deduct late_deduction_days; 0 = off
    late_deduction_days: Decimal = D("1")
    half_day_late_minutes: int = 0         # late more than this = half day deducted; 0 = off


@dataclass
class ComponentSpec:
    """A custom pay component as it applies to one employee (value already resolved for that employee)."""
    code: str
    name: str
    kind: str          # ADD / DED
    calc: str          # FIXED / PCT_BASIC / PCT_GROSS / PER_PRESENT_DAY / PER_OT_HOUR
    value: Decimal
    prorate: bool = True


@dataclass
class SalaryComponents:
    gross: Decimal
    basic: Decimal
    house_rent: Decimal
    medical: Decimal
    transport: Decimal
    food: Decimal

    def check(self) -> None:
        total = self.basic + self.house_rent + self.medical + self.transport + self.food
        if money(total) != money(self.gross):
            raise ValueError(f"salary components add up to {total}, not gross {self.gross}")


def split_gross(gross, policy: PayPolicy) -> SalaryComponents:
    gross = D(gross)
    fixed = policy.medical + policy.transport + policy.food
    if gross <= fixed:
        raise ValueError(f"gross {gross} must be above fixed allowances {fixed}")
    basic = taka((gross - fixed) / (1 + policy.house_rent_pct))
    house = gross - fixed - basic
    return SalaryComponents(gross, basic, house, policy.medical, policy.transport, policy.food)


@dataclass
class Adjustments:
    other_addition: Decimal = ZERO   # arrears, night allowance, etc.
    advance: Decimal = ZERO
    other_deduction: Decimal = ZERO
    tax: Decimal = ZERO
    note: str = ""
    items: list = field(default_factory=list)       # one-off [{"name", "kind": ADD/DED, "amount"}]
    overrides: dict = field(default_factory=dict)   # admin edits: absent_days, ot_hours, late_days, present_days


@dataclass
class PayResult:
    days_in_month: int
    employed_days: int
    payable_days: int
    absent_days: int
    gross: Decimal
    earned_gross: Decimal
    basic: Decimal
    absent_deduction: Decimal
    ot_hours: Decimal
    ot_rate: Decimal
    ot_amount: Decimal
    late_deduction: Decimal
    custom_additions: Decimal
    custom_deductions: Decimal
    attendance_bonus: Decimal
    other_addition: Decimal
    advance: Decimal
    other_deduction: Decimal
    tax: Decimal
    stamp: Decimal
    total_earnings: Decimal
    total_deductions: Decimal
    net_pay: Decimal
    present_days: int = 0
    late_days: int = 0
    items: list = field(default_factory=list)     # [{"name", "kind", "amount", "source"}]
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}


def component_amount(c: ComponentSpec, salary: "SalaryComponents", factor: Decimal, present_days: int,
                     ot_hours: Decimal) -> Decimal:
    v = D(c.value)
    if c.calc == "PCT_BASIC":
        amt = salary.basic * v / 100 * (factor if c.prorate else 1)
    elif c.calc == "PCT_GROSS":
        amt = salary.gross * v / 100 * (factor if c.prorate else 1)
    elif c.calc == "PER_PRESENT_DAY":
        amt = v * present_days
    elif c.calc == "PER_OT_HOUR":
        amt = v * ot_hours
    else:  # FIXED
        amt = v * (factor if c.prorate else 1)
    return money(amt)


def late_penalty_days(late_list: list[int], policy: PayPolicy) -> tuple[Decimal, int, int]:
    """(days to deduct, late days counted, half days) from the late minutes of each late day."""
    half = [m for m in late_list if policy.half_day_late_minutes and m > policy.half_day_late_minutes]
    normal = [m for m in late_list if not (policy.half_day_late_minutes and m > policy.half_day_late_minutes)]
    days = D(len(half)) / 2
    if policy.late_days_per_deduction > 0:
        days += D(len(normal) // policy.late_days_per_deduction) * D(policy.late_deduction_days)
    return days, len(normal), len(half)


def compute_pay(salary: SalaryComponents, att: MonthSummary, policy: PayPolicy,
                adj: Adjustments | None = None, components: list[ComponentSpec] | None = None) -> PayResult:
    adj = adj or Adjustments()
    ov = adj.overrides or {}
    salary.check()
    warnings: list[str] = []
    dim = att.days_in_month
    if att.employed_days == 0:
        warnings.append("not employed during this month")

    # Pro-rate for joiners / leavers.
    factor = D(att.employed_days) / D(dim) if dim else ZERO
    earned_gross = money(salary.gross * factor)

    absent_days = att.absent_days + att.unpaid_leave_days
    if ov.get("absent_days") not in (None, ""):
        absent_days = int(D(str(ov["absent_days"])))
        warnings.append(f"absent days set to {absent_days} by {ov.get('by', 'admin')}")
    present_days = att.present_days
    if ov.get("present_days") not in (None, ""):
        present_days = int(ov["present_days"])
    basis = salary.basic if policy.absent_basis == "basic" else salary.gross
    divisor = D(dim) if policy.absent_divisor == "month_days" else D("30")
    day_rate = basis / divisor
    absent_deduction = money(day_rate * D(absent_days))
    if absent_deduction > earned_gross:
        absent_deduction = earned_gross

    # late rules
    late_list = [d.late_minutes for d in att.days if d.late_minutes]
    late_days = len(late_list)
    if ov.get("late_days") not in (None, ""):
        late_days = int(ov["late_days"])
        late_list = [1] * late_days
    pen_days, _, half_days = late_penalty_days(late_list, policy)
    late_deduction = money(day_rate * pen_days)
    if late_deduction:
        warnings.append(f"late deduction {pen_days} day(s)" + (f" incl. {half_days} half day(s)" if half_days else ""))

    ot_minutes = att.ot_minutes + (att.extra_ot_minutes if policy.pay_extra_ot else 0)
    ot_hours = money(D(ot_minutes) / 60)
    if ov.get("ot_hours") not in (None, ""):
        ot_hours = money(D(str(ov["ot_hours"])))
    if not policy.ot_enabled:
        ot_hours = ZERO
    if policy.ot_divisor and policy.ot_divisor > 0:
        ot_rate = money(salary.basic / policy.ot_divisor * policy.ot_multiplier)
    else:          # 0 / empty divisor in the pay policy: no overtime pay instead of a crash
        ot_rate = ZERO
        if ot_hours:
            warnings.append("OT not paid: 'OT hourly divisor' in the pay policy is 0")
    ot_amount = money(ot_rate * ot_hours)

    bonus = ZERO
    if (policy.attendance_bonus > 0 and att.employed_days == dim and absent_days == 0
            and late_days <= policy.bonus_max_late_days):
        bonus = money(policy.attendance_bonus)

    if att.missing_out_days:
        warnings.append(f"{att.missing_out_days} day(s) with a single punch - check before paying OT")

    items = []
    for c in components or []:
        amt = component_amount(c, salary, factor, present_days, ot_hours)
        if amt:
            items.append({"name": c.name, "kind": c.kind, "amount": str(amt), "source": c.code})
    for it in adj.items or []:
        amt = money(D(str(it.get("amount") or 0)))
        if amt:
            items.append({"name": str(it.get("name") or "Item")[:60], "kind": "DED" if it.get("kind") == "DED" else "ADD",
                          "amount": str(amt), "source": "ONE-OFF"})
    custom_add = sum((D(i["amount"]) for i in items if i["kind"] == "ADD"), ZERO)
    custom_ded = sum((D(i["amount"]) for i in items if i["kind"] == "DED"), ZERO)

    total_earnings = (earned_gross - absent_deduction - late_deduction + ot_amount + bonus + D(adj.other_addition)
                      + custom_add)
    stamp = money(policy.stamp_charge) if total_earnings > 0 else ZERO
    total_deductions = D(adj.advance) + D(adj.other_deduction) + D(adj.tax) + stamp + custom_ded
    net = total_earnings - total_deductions
    net = taka(net) if policy.round_net_to_taka else money(net)
    if net < 0:
        warnings.append("deductions exceed earnings - net pay set to 0")
        net = ZERO

    return PayResult(
        days_in_month=dim, employed_days=att.employed_days, payable_days=att.payable_days,
        absent_days=absent_days, gross=money(salary.gross), earned_gross=earned_gross,
        basic=money(salary.basic), absent_deduction=absent_deduction, ot_hours=ot_hours,
        ot_rate=ot_rate, ot_amount=ot_amount, late_deduction=late_deduction, custom_additions=money(custom_add),
        custom_deductions=money(custom_ded), attendance_bonus=bonus,
        other_addition=money(adj.other_addition), advance=money(adj.advance),
        other_deduction=money(adj.other_deduction), tax=money(adj.tax), stamp=stamp,
        total_earnings=money(total_earnings), total_deductions=money(total_deductions),
        net_pay=net, present_days=present_days, late_days=late_days, items=items, warnings=warnings,
    )
