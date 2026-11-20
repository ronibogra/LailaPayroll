import json
from dataclasses import asdict
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Company, Employee, PayrollAdjustment, PayrollRun, PayrollLine
from payroll.engine import SalaryComponents, Adjustments, compute_pay, split_gross
from services.audit import audit
from services.policy import load_policy
from services.processing import process_month
from timeutil import now_local


class PayrollLocked(Exception):
    pass


def salary_of(e: Employee, policy) -> SalaryComponents:
    if not e.basic:
        if not e.gross:
            raise ValueError(f"{e.employee_code}: no gross salary set")
        return split_gross(e.gross, policy)
    return SalaryComponents(Decimal(e.gross), Decimal(e.basic), Decimal(e.house_rent),
                            Decimal(e.medical), Decimal(e.transport), Decimal(e.food))


def get_run(db: Session, company_id: int, year: int, month: int) -> PayrollRun | None:
    return db.scalar(select(PayrollRun).where(PayrollRun.company_id == company_id, PayrollRun.year == year,
                                              PayrollRun.month == month))


def run_payroll(db: Session, company_id: int, year: int, month: int, user: dict) -> PayrollRun:
    company = db.get(Company, company_id)
    if not company:
        raise ValueError("Unknown company")
    run = get_run(db, company_id, year, month)
    if run and run.status == "FINAL":
        raise PayrollLocked(f"{company.name} payroll {year}-{month:02d} is already finalized")
    if run:
        db.delete(run)
        db.flush()

    policy = load_policy(db, company_id)
    summaries = process_month(db, year, month, company_ids=[company_id], save=True)
    employees = {e.id: e for e in db.scalars(select(Employee).where(Employee.id.in_(list(summaries) or [0])))}
    adjustments = {a.employee_id: a for a in db.scalars(
        select(PayrollAdjustment).where(PayrollAdjustment.year == year, PayrollAdjustment.month == month))}

    pol = {k: str(v) for k, v in asdict(policy).items()}
    run = PayrollRun(company_id=company_id, year=year, month=month, status="DRAFT", created_by=user["username"],
                     policy_json=json.dumps(pol))
    db.add(run)
    errors = []
    for emp_id, att in summaries.items():
        e = employees[emp_id]
        try:
            sal = salary_of(e, policy)
            a = adjustments.get(emp_id)
            adj = Adjustments(a.other_addition, a.advance, a.other_deduction, a.tax, a.note or "") if a else None
            r = compute_pay(sal, att, policy, adj)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        run.lines.append(PayrollLine(
            employee_id=e.id, employee_code=e.employee_code, full_name=e.full_name,
            department=e.department.name if e.department else "", designation=e.designation or "",
            grade=e.grade or "", payment_mode=e.payment_mode or "CASH", bank_name=e.bank_name or "",
            account_no=e.account_no or "",
            days_in_month=r.days_in_month, employed_days=r.employed_days, present_days=att.present_days,
            payable_days=r.payable_days, absent_days=r.absent_days, paid_leave_days=att.paid_leave_days,
            offdays=att.offdays, late_days=att.late_days,
            gross=sal.gross, basic=sal.basic, house_rent=sal.house_rent, medical=sal.medical,
            transport=sal.transport, food=sal.food, earned_gross=r.earned_gross,
            absent_deduction=r.absent_deduction, ot_hours=r.ot_hours, ot_rate=r.ot_rate,
            ot_amount=r.ot_amount, attendance_bonus=r.attendance_bonus, other_addition=r.other_addition,
            advance=r.advance, other_deduction=r.other_deduction, tax=r.tax, stamp=r.stamp,
            total_earnings=r.total_earnings, total_deductions=r.total_deductions, net_pay=r.net_pay,
            warnings="; ".join(r.warnings)))
    audit(db, user, "PAYROLL_PROCESS",
          f"{company.code} {year}-{month:02d}: {len(run.lines)} employees" + (f"; skipped: {'; '.join(errors)}" if errors else ""),
          commit=False)
    db.commit()
    run.errors = errors  # not stored; shown once on screen
    return run


def finalize_payroll(db: Session, company_id: int, year: int, month: int, user: dict) -> PayrollRun:
    run = get_run(db, company_id, year, month)
    if not run:
        raise ValueError("Process the payroll first")
    if run.status == "FINAL":
        raise PayrollLocked("Already finalized")
    run.status, run.finalized_by, run.finalized_at = "FINAL", user["username"], now_local()
    audit(db, user, "PAYROLL_FINALIZE", f"{run.company.code} {year}-{month:02d}", commit=False)
    db.commit()
    return run
