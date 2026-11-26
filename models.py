from datetime import datetime, date, time
from decimal import Decimal

from sqlalchemy import (String, Integer, DateTime, Date, Time, Boolean, ForeignKey, Text,
                        Numeric, UniqueConstraint, Index)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base
from timeutil import now_local

Money = Numeric(12, 2)


class Company(Base):
    """A Laila Group company whose staff work at / are handled by the head office."""
    __tablename__ = "companies"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(150))
    address: Mapped[str | None] = mapped_column(String(250), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Department(Base):
    __tablename__ = "departments"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    code: Mapped[str | None] = mapped_column(String(30), nullable=True)  # not unique: SQL Server allows only one NULL in a unique column
    head_username: Mapped[str | None] = mapped_column(String(80), nullable=True)   # Department Head (leave approval)
    employees = relationship("Employee", back_populates="department")


class Shift(Base):
    __tablename__ = "shifts"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(30), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    start: Mapped[time] = mapped_column(Time)
    end: Mapped[time] = mapped_column(Time)
    grace_minutes: Mapped[int] = mapped_column(Integer, default=10)
    break_minutes: Mapped[int] = mapped_column(Integer, default=60)
    break_after_minutes: Mapped[int] = mapped_column(Integer, default=300)
    early_window_minutes: Mapped[int] = mapped_column(Integer, default=180)
    ot_round_minutes: Mapped[int] = mapped_column(Integer, default=30)
    max_ot_minutes: Mapped[int] = mapped_column(Integer, default=120)
    offday_work_is_ot: Mapped[bool] = mapped_column(Boolean, default=True)
    late_count_from: Mapped[str] = mapped_column(String(10), default="START")   # START or GRACE_END
    weekly_off: Mapped[str] = mapped_column(String(20), default="")   # "4" = Friday, "5,6" = Sat+Sun; "" = Settings default


class Employee(Base):
    __tablename__ = "employees"
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_code: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    card_no: Mapped[str] = mapped_column(String(50), unique=True, index=True)  # user ID on the machines
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    full_name: Mapped[str] = mapped_column(String(180), index=True)
    department_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id"), nullable=True)
    designation: Mapped[str | None] = mapped_column(String(120), nullable=True)
    section: Mapped[str | None] = mapped_column(String(120), nullable=True)
    grade: Mapped[str | None] = mapped_column(String(20), nullable=True)
    factory: Mapped[str | None] = mapped_column(String(120), nullable=True)
    shift_id: Mapped[int | None] = mapped_column(ForeignKey("shifts.id"), nullable=True)
    employment_status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    joined_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    left_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    gross: Mapped[Decimal] = mapped_column(Money, default=0)
    basic: Mapped[Decimal] = mapped_column(Money, default=0)
    house_rent: Mapped[Decimal] = mapped_column(Money, default=0)
    medical: Mapped[Decimal] = mapped_column(Money, default=0)
    transport: Mapped[Decimal] = mapped_column(Money, default=0)
    food: Mapped[Decimal] = mapped_column(Money, default=0)
    # profile
    photo: Mapped[str | None] = mapped_column(String(120), nullable=True)   # file name in <data>/photos
    signature: Mapped[str | None] = mapped_column(String(120), nullable=True)   # signature image in <data>/photos
    father_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    mother_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    spouse_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    gender: Mapped[str | None] = mapped_column(String(10), nullable=True)
    blood_group: Mapped[str | None] = mapped_column(String(5), nullable=True)
    religion: Mapped[str | None] = mapped_column(String(30), nullable=True)
    marital_status: Mapped[str | None] = mapped_column(String(15), nullable=True)
    nid: Mapped[str | None] = mapped_column(String(30), nullable=True)
    birth_certificate_no: Mapped[str | None] = mapped_column(String(30), nullable=True)
    passport_no: Mapped[str | None] = mapped_column(String(30), nullable=True)
    ot_eligible: Mapped[bool] = mapped_column(Boolean, default=True)
    confirmed_on: Mapped[date | None] = mapped_column(Date, nullable=True)      # service confirmed after probation
    weekly_off: Mapped[str] = mapped_column(String(20), default="")   # own weekly off day(s); "" = from shift / Settings   # officers / management: False
    phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    email: Mapped[str | None] = mapped_column(String(120), nullable=True)
    emergency_contact: Mapped[str | None] = mapped_column(String(150), nullable=True)
    present_address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    permanent_address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    education: Mapped[str | None] = mapped_column(String(200), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    allow_mobile: Mapped[bool] = mapped_column(Boolean, default=False)    # may punch from phone
    mobile_pin_hash: Mapped[str | None] = mapped_column(String(300), nullable=True)
    payment_mode: Mapped[str] = mapped_column(String(10), default="CASH")  # CASH / BANK / MOBILE
    # part to bank / wallet when the rest is paid in cash: "" = all by pay mode, FIXED = Tk amount, PERCENT = % of net
    split_mode: Mapped[str] = mapped_column(String(10), default="")
    split_value: Mapped[Decimal] = mapped_column(Money, default=0)
    monthly_tax: Mapped[Decimal] = mapped_column(Money, default=0)   # income tax (TDS) deducted every month
    bank_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    account_no: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)
    department = relationship("Department", back_populates="employees")
    shift = relationship("Shift")
    company = relationship("Company")


class EmployeeDocument(Base):
    """Files kept for an employee: education certificates, NID / birth certificate copy, CV, etc."""
    __tablename__ = "employee_documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    doc_type: Mapped[str] = mapped_column(String(30), default="OTHER")
    title: Mapped[str] = mapped_column(String(150), default="")
    stored_name: Mapped[str] = mapped_column(String(120))
    original_name: Mapped[str] = mapped_column(String(200), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    uploaded_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)
    employee = relationship("Employee", viewonly=True)


class EmployeeAsset(Base):
    """Company items given to an employee (laptop, mobile, SIM, ID card, uniform, tools ...) and their return."""
    __tablename__ = "employee_assets"
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    category: Mapped[str] = mapped_column(String(30), default="OTHER")
    name: Mapped[str] = mapped_column(String(150))
    brand_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    serial_no: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    asset_tag: Mapped[str | None] = mapped_column(String(60), nullable=True, index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    value: Mapped[Decimal] = mapped_column(Money, default=0)
    issued_on: Mapped[date] = mapped_column(Date)
    issued_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    condition_issued: Mapped[str | None] = mapped_column(String(30), nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="ISSUED")     # ISSUED / RETURNED / LOST / DAMAGED
    returned_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    returned_to: Mapped[str | None] = mapped_column(String(80), nullable=True)
    condition_returned: Mapped[str | None] = mapped_column(String(30), nullable=True)
    note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    employee = relationship("Employee")


class EmployeeChange(Base):
    """Increment and / or promotion: what changed, from when, and whether it is applied yet."""
    __tablename__ = "employee_changes"
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    kind: Mapped[str] = mapped_column(String(12), default="INCREMENT")   # INCREMENT / PROMOTION (promotion may raise pay)
    effective_date: Mapped[date] = mapped_column(Date)
    old_designation: Mapped[str | None] = mapped_column(String(120), nullable=True)
    new_designation: Mapped[str | None] = mapped_column(String(120), nullable=True)
    old_grade: Mapped[str | None] = mapped_column(String(30), nullable=True)
    new_grade: Mapped[str | None] = mapped_column(String(30), nullable=True)
    old_department: Mapped[str | None] = mapped_column(String(120), nullable=True)
    new_department: Mapped[str | None] = mapped_column(String(120), nullable=True)
    old_gross: Mapped[Decimal] = mapped_column(Money, default=0)
    new_gross: Mapped[Decimal] = mapped_column(Money, default=0)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="PENDING")   # PENDING (future date) / APPLIED / CANCELLED
    notify: Mapped[bool] = mapped_column(Boolean, default=True)           # show a notice to the employee
    created_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    employee = relationship("Employee")


class EmployeeSeparation(Base):
    """Leaving the company: resignation / termination ..., notice, last working day, clearance, release."""
    __tablename__ = "employee_separations"
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    sep_type: Mapped[str] = mapped_column(String(20), default="RESIGNATION")
    notice_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_working_day: Mapped[date] = mapped_column(Date)
    reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    checklist_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="IN_PROGRESS")   # IN_PROGRESS / COMPLETED / CANCELLED
    created_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)
    completed_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    employee = relationship("Employee")


class Device(Base):
    """A punch machine. ZKTeco machines push to /iclock (ADMS); Hikvision machines are polled (ISAPI).
    Machines that contact the server but are not approved here are listed as pending and ignored."""
    __tablename__ = "devices"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    brand: Mapped[str] = mapped_column(String(20))   # ZKTECO (download by IP) / ZKTECO_PUSH / HIKVISION
    machine_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    direction: Mapped[str] = mapped_column(String(5), default="BOTH")  # IN / OUT / BOTH
    serial_number: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    port: Mapped[int] = mapped_column(Integer, default=80)
    username: Mapped[str | None] = mapped_column(String(60), nullable=True)
    password_enc: Mapped[str | None] = mapped_column(String(300), nullable=True)   # Hikvision password or ZK Comm Key
    location: Mapped[str | None] = mapped_column(String(150), nullable=True)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(250), nullable=True)
    cursor_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # Hikvision: newest event read
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)


class AttendanceLog(Base):
    __tablename__ = "attendance_logs"
    __table_args__ = (
        UniqueConstraint("card_no", "punch_time", name="uq_attendance_card_time"),
        Index("ix_attendance_card_time", "card_no", "punch_time"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    card_no: Mapped[str] = mapped_column(String(50), index=True)
    punch_time: Mapped[datetime] = mapped_column(DateTime, index=True)
    device: Mapped[str | None] = mapped_column(String(100), nullable=True)
    device_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    direction: Mapped[str | None] = mapped_column(String(5), nullable=True)   # IN / OUT from the machine setting
    source: Mapped[str] = mapped_column(String(20), default="CSV")  # RAWDATA / CSV / MANUAL / MOBILE
    latitude: Mapped[float | None] = mapped_column(Numeric(9, 6), nullable=True)
    longitude: Mapped[float | None] = mapped_column(Numeric(9, 6), nullable=True)
    accuracy_m: Mapped[int | None] = mapped_column(Integer, nullable=True)
    client_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    photo: Mapped[str | None] = mapped_column(String(120), nullable=True)   # mobile punch selfie (punch_photos/...)
    note: Mapped[str | None] = mapped_column(String(250), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)


class Holiday(Base):
    __tablename__ = "holidays"
    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[date] = mapped_column(Date, unique=True)
    name: Mapped[str] = mapped_column(String(120))


class Leave(Base):
    __tablename__ = "leaves"
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    leave_type: Mapped[str] = mapped_column(String(20))  # code of a LeaveType (CASUAL / SICK / EARNED / MATERNITY / UNPAID ...)
    paid: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(String(250), nullable=True)
    days: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(15), default="APPROVED")  # PENDING_DEPT / PENDING_FINAL / APPROVED / REJECTED / CANCELLED
    source: Mapped[str] = mapped_column(String(10), default="HR")        # HR (granted) / SELF (requested by employee)
    reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    document: Mapped[str | None] = mapped_column(String(200), nullable=True)   # stored file (prescription, maternity papers)
    requested_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dept_approved_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    dept_approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    final_approved_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    final_approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rejected_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    employee = relationship("Employee")


class LeaveType(Base):
    """Leave policy (Admin): yearly days and document rules per leave type."""
    __tablename__ = "leave_types"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    days_per_year: Mapped[int] = mapped_column(Integer, default=0)       # 0 = no yearly limit
    paid: Mapped[bool] = mapped_column(Boolean, default=True)
    doc_rule: Mapped[str] = mapped_column(String(10), default="NEVER")   # NEVER / ALWAYS / OVER_DAYS
    doc_after_days: Mapped[int] = mapped_column(Integer, default=0)      # with OVER_DAYS: document needed above this
    doc_label: Mapped[str] = mapped_column(String(80), default="Document")
    gender: Mapped[str | None] = mapped_column(String(10), nullable=True)   # e.g. Female for maternity
    self_request: Mapped[bool] = mapped_column(Boolean, default=True)      # employees may request it themselves
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)


class SpecialHoliday(Base):
    """Holiday for only some staff: one company, one department, or listed employees."""
    __tablename__ = "special_holidays"
    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    name: Mapped[str] = mapped_column(String(120))
    scope: Mapped[str] = mapped_column(String(12))            # COMPANY / DEPARTMENT / EMPLOYEES
    scope_value: Mapped[str] = mapped_column(Text, default="")  # company id / department id / employee codes (comma)


class PayComponent(Base):
    """Custom addition or deduction in payroll (Admin), e.g. Mobile allowance, Night allowance, Loan, PF."""
    __tablename__ = "pay_components"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(3), default="ADD")       # ADD / DED
    calc: Mapped[str] = mapped_column(String(15), default="FIXED")    # FIXED / PCT_BASIC / PCT_GROSS / PER_PRESENT_DAY / PER_OT_HOUR
    value: Mapped[Decimal] = mapped_column(Money, default=0)
    prorate: Mapped[bool] = mapped_column(Boolean, default=True)      # FIXED / % : reduce for joiners / leavers
    company_id: Mapped[int] = mapped_column(Integer, default=0)       # 0 = all companies
    department_id: Mapped[int] = mapped_column(Integer, default=0)    # 0 = all departments
    assigned_only: Mapped[bool] = mapped_column(Boolean, default=False)  # only employees it is assigned to
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)


class EmployeeComponent(Base):
    """Per-employee value for a pay component (overrides the default), or switches it off for that employee."""
    __tablename__ = "employee_components"
    __table_args__ = (UniqueConstraint("employee_id", "component_id", name="uq_emp_component"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    component_id: Mapped[int] = mapped_column(ForeignKey("pay_components.id"), index=True)
    value: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)


class DailyAttendance(Base):
    __tablename__ = "daily_attendance"
    __table_args__ = (UniqueConstraint("employee_id", "work_date", name="uq_daily_employee_date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    work_date: Mapped[date] = mapped_column(Date, index=True)
    shift_code: Mapped[str] = mapped_column(String(30), default="")
    day_type: Mapped[str] = mapped_column(String(10), default="WORKDAY")
    status: Mapped[str] = mapped_column(String(20))
    in_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    out_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    punch_count: Mapped[int] = mapped_column(Integer, default=0)
    late_minutes: Mapped[int] = mapped_column(Integer, default=0)
    early_leave_minutes: Mapped[int] = mapped_column(Integer, default=0)
    worked_minutes: Mapped[int] = mapped_column(Integer, default=0)
    ot_minutes: Mapped[int] = mapped_column(Integer, default=0)
    extra_ot_minutes: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_local, onupdate=now_local)
    employee = relationship("Employee")


class Setting(Base):
    """Key/value settings. company_id 0 = whole group (default); >0 = override for one company."""
    __tablename__ = "settings"
    __table_args__ = (UniqueConstraint("company_id", "key", name="uq_setting_company_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(Integer, default=0)
    key: Mapped[str] = mapped_column(String(60))
    value: Mapped[str] = mapped_column(String(250))


class PayrollAdjustment(Base):
    __tablename__ = "payroll_adjustments"
    __table_args__ = (UniqueConstraint("employee_id", "year", "month", name="uq_adjust_emp_month"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    year: Mapped[int] = mapped_column(Integer)
    month: Mapped[int] = mapped_column(Integer)
    other_addition: Mapped[Decimal] = mapped_column(Money, default=0)
    advance: Mapped[Decimal] = mapped_column(Money, default=0)
    other_deduction: Mapped[Decimal] = mapped_column(Money, default=0)
    tax: Mapped[Decimal] = mapped_column(Money, default=0)
    note: Mapped[str | None] = mapped_column(String(250), nullable=True)
    items_json: Mapped[str | None] = mapped_column(Text, nullable=True)       # one-off items [{name, kind, amount}]
    overrides_json: Mapped[str | None] = mapped_column(Text, nullable=True)   # admin edits {absent_days, ot_hours, ...}
    edited_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    employee = relationship("Employee", viewonly=True)


class PayrollRun(Base):
    __tablename__ = "payroll_runs"
    __table_args__ = (UniqueConstraint("company_id", "year", "month", name="uq_payroll_company_month"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    year: Mapped[int] = mapped_column(Integer)
    month: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(10), default="DRAFT")  # DRAFT / FINAL
    policy_json: Mapped[str] = mapped_column(Text, default="{}")
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)
    finalized_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lines = relationship("PayrollLine", back_populates="run", cascade="all, delete-orphan")
    company = relationship("Company")


class PayrollLine(Base):
    __tablename__ = "payroll_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("payroll_runs.id"), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    # Snapshot so a finalized sheet never changes when master data changes later
    employee_code: Mapped[str] = mapped_column(String(50))
    full_name: Mapped[str] = mapped_column(String(180))
    department: Mapped[str] = mapped_column(String(120), default="")
    designation: Mapped[str] = mapped_column(String(120), default="")
    grade: Mapped[str] = mapped_column(String(20), default="")
    payment_mode: Mapped[str] = mapped_column(String(10), default="CASH")
    bank_name: Mapped[str] = mapped_column(String(80), default="")
    account_no: Mapped[str] = mapped_column(String(40), default="")
    days_in_month: Mapped[int] = mapped_column(Integer)
    employed_days: Mapped[int] = mapped_column(Integer)
    present_days: Mapped[int] = mapped_column(Integer)
    payable_days: Mapped[int] = mapped_column(Integer)
    absent_days: Mapped[int] = mapped_column(Integer)
    paid_leave_days: Mapped[int] = mapped_column(Integer)
    offdays: Mapped[int] = mapped_column(Integer)
    late_days: Mapped[int] = mapped_column(Integer)
    gross: Mapped[Decimal] = mapped_column(Money)
    basic: Mapped[Decimal] = mapped_column(Money)
    house_rent: Mapped[Decimal] = mapped_column(Money)
    medical: Mapped[Decimal] = mapped_column(Money)
    transport: Mapped[Decimal] = mapped_column(Money)
    food: Mapped[Decimal] = mapped_column(Money)
    earned_gross: Mapped[Decimal] = mapped_column(Money)
    absent_deduction: Mapped[Decimal] = mapped_column(Money)
    ot_hours: Mapped[Decimal] = mapped_column(Money)
    ot_rate: Mapped[Decimal] = mapped_column(Money)
    ot_amount: Mapped[Decimal] = mapped_column(Money)
    attendance_bonus: Mapped[Decimal] = mapped_column(Money)
    other_addition: Mapped[Decimal] = mapped_column(Money)
    advance: Mapped[Decimal] = mapped_column(Money)
    other_deduction: Mapped[Decimal] = mapped_column(Money)
    tax: Mapped[Decimal] = mapped_column(Money)
    stamp: Mapped[Decimal] = mapped_column(Money)
    late_deduction: Mapped[Decimal | None] = mapped_column(Money, nullable=True, default=0)
    custom_additions: Mapped[Decimal | None] = mapped_column(Money, nullable=True, default=0)
    custom_deductions: Mapped[Decimal | None] = mapped_column(Money, nullable=True, default=0)
    items_json: Mapped[str | None] = mapped_column(Text, nullable=True)    # [{name, kind, amount, source}]
    edited: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    total_earnings: Mapped[Decimal] = mapped_column(Money)
    total_deductions: Mapped[Decimal] = mapped_column(Money)
    net_pay: Mapped[Decimal] = mapped_column(Money)
    bank_amount: Mapped[Decimal | None] = mapped_column(Money, nullable=True)   # to bank / wallet (pay mode)
    cash_amount: Mapped[Decimal | None] = mapped_column(Money, nullable=True)   # in cash
    warnings: Mapped[str] = mapped_column(Text, default="")
    run = relationship("PayrollRun", back_populates="lines")


class Role(Base):
    """A named set of permissions (see services/permissions.py). ADMIN always has every permission."""
    __tablename__ = "roles"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(30), unique=True)
    label: Mapped[str] = mapped_column(String(120), default="")
    permissions: Mapped[str] = mapped_column(Text, default="")
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(30), default="VIEWER")  # name of a Role
    full_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True)  # None = all companies
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    employee_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)   # Staff login: own record
    must_change: Mapped[bool] = mapped_column(Boolean, default=False)                     # change password at next login
    super_admin: Mapped[bool] = mapped_column(Boolean, default=False)                     # may delete data (Delete centre)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(120))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local, index=True)


class ErrorLog(Base):
    """Every error the program meets: imports, machines, pages, payroll. Shown on the Error log page."""
    __tablename__ = "error_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local, index=True)
    source: Mapped[str] = mapped_column(String(40))          # MACHINE / IMPORT / PAGE / PAYROLL / SYSTEM ...
    message: Mapped[str] = mapped_column(String(500))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    username: Mapped[str | None] = mapped_column(String(80), nullable=True)
