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
    attendance_bonus: Decimal
    other_addition: Decimal
    advance: Decimal
    other_deduction: Decimal
    tax: Decimal
    stamp: Decimal
    total_earnings: Decimal
    total_deductions: Decimal
    net_pay: Decimal
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}


def compute_pay(salary: SalaryComponents, att: MonthSummary, policy: PayPolicy,
                adj: Adjustments | None = None) -> PayResult:
    adj = adj or Adjustments()
    salary.check()
    warnings: list[str] = []
    dim = att.days_in_month
    if att.employed_days == 0:
        warnings.append("not employed during this month")

    # Pro-rate for joiners / leavers.
    factor = D(att.employed_days) / D(dim) if dim else ZERO
    earned_gross = money(salary.gross * factor)

    absent_days = att.absent_days + att.unpaid_leave_days
    basis = salary.basic if policy.absent_basis == "basic" else salary.gross
    divisor = D(dim) if policy.absent_divisor == "month_days" else D("30")
    absent_deduction = money(basis / divisor * absent_days)
    if absent_deduction > earned_gross:
        absent_deduction = earned_gross

    ot_minutes = att.ot_minutes + (att.extra_ot_minutes if policy.pay_extra_ot else 0)
    ot_hours = money(D(ot_minutes) / 60)
    ot_rate = money(salary.basic / policy.ot_divisor * policy.ot_multiplier)
    ot_amount = money(ot_rate * ot_hours)

    bonus = ZERO
    if (policy.attendance_bonus > 0 and att.employed_days == dim and absent_days == 0
            and att.late_days <= policy.bonus_max_late_days):
        bonus = money(policy.attendance_bonus)

    if att.missing_out_days:
        warnings.append(f"{att.missing_out_days} day(s) with a single punch - check before paying OT")

    total_earnings = earned_gross - absent_deduction + ot_amount + bonus + D(adj.other_addition)
    stamp = money(policy.stamp_charge) if total_earnings > 0 else ZERO
    total_deductions = D(adj.advance) + D(adj.other_deduction) + D(adj.tax) + stamp
    net = total_earnings - total_deductions
    net = taka(net) if policy.round_net_to_taka else money(net)
    if net < 0:
        warnings.append("deductions exceed earnings - net pay set to 0")
        net = ZERO

    return PayResult(
        days_in_month=dim, employed_days=att.employed_days, payable_days=att.payable_days,
        absent_days=absent_days, gross=money(salary.gross), earned_gross=earned_gross,
        basic=money(salary.basic), absent_deduction=absent_deduction, ot_hours=ot_hours,
        ot_rate=ot_rate, ot_amount=ot_amount, attendance_bonus=bonus,
        other_addition=money(adj.other_addition), advance=money(adj.advance),
        other_deduction=money(adj.other_deduction), tax=money(adj.tax), stamp=stamp,
        total_earnings=money(total_earnings), total_deductions=money(total_deductions),
        net_pay=net, warnings=warnings,
    )
