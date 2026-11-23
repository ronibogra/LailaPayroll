import io
from calendar import month_name

from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from openpyxl.utils import get_column_letter

from config import settings  # noqa: F401
from services.branding import BRAND

HEAD_FILL = PatternFill("solid", fgColor="E8332F")
HEAD_FONT = Font(bold=True, color="FFFFFF")
THIN = Side(style="thin", color="BBBBBB")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def _title(ws, title: str, ncols: int, company: str = "") -> None:
    ws.append([company or BRAND["name"]])
    ws.append([title])
    ws["A1"].font = Font(bold=True, size=14, color="E8332F")
    ws.cell(1, ncols + 2, f"SPhere Payroll · {settings.developer_credit}").font = Font(size=8, color="888888")
    ws["A2"].font = Font(bold=True, size=11)
    for r in (1, 2):
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=ncols)
    ws.append([])


def _table(ws, headers: list[str], rows: list[list], money_cols=(), total_cols=()) -> None:
    ws.append(headers)
    hr = ws.max_row
    for c in range(1, len(headers) + 1):
        cell = ws.cell(hr, c)
        cell.fill, cell.font, cell.border = HEAD_FILL, HEAD_FONT, BOX
        cell.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
    for row in rows:
        ws.append(row)
    last = ws.max_row
    for r in range(hr + 1, last + 1):
        for c in range(1, len(headers) + 1):
            ws.cell(r, c).border = BOX
            if c in money_cols:
                ws.cell(r, c).number_format = "#,##0.00"
    if total_cols and rows:
        ws.append(["TOTAL"] + [None] * (len(headers) - 1))
        tr = ws.max_row
        for c in total_cols:
            L = get_column_letter(c)
            ws.cell(tr, c, f"=SUM({L}{hr + 1}:{L}{last})").number_format = "#,##0.00"
        for c in range(1, len(headers) + 1):
            ws.cell(tr, c).font = Font(bold=True)
            ws.cell(tr, c).border = BOX
    ws.freeze_panes = ws.cell(hr + 1, 3)
    for c in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(c)].width = 13 if c > 3 else 20
    ws.row_dimensions[hr].height = 32


def _save(wb) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


SALARY_HEAD = ["Emp Code", "Name", "Department", "Designation", "Grade", "Days", "Present", "Absent",
               "Paid Leave", "Off Days", "Late Days", "Gross", "Basic", "House Rent", "Medical",
               "Transport", "Food", "Earned Gross", "Absent Ded.", "Late Ded.", "OT Hrs", "OT Rate", "OT Amount",
               "Att. Bonus", "Allowances / Bonus", "Other Add.", "Advance", "Other Ded.", "Custom Ded.", "Tax", "Stamp",
               "Net Pay", "To Bank / Wallet", "Cash", "Pay Mode", "Bank", "Account No", "Remarks", "Signature"]
SALARY_MONEY = set(range(12, 35))
SALARY_TOTAL = (12, 18, 19, 20, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34)


def _salary_row(l) -> list:
    from services.disbursement import bank_part, cash_part
    f = float
    return [l.employee_code, l.full_name, l.department, l.designation, l.grade, l.employed_days,
            l.present_days, l.absent_days, l.paid_leave_days, l.offdays, l.late_days, f(l.gross),
            f(l.basic), f(l.house_rent), f(l.medical), f(l.transport), f(l.food), f(l.earned_gross),
            f(l.absent_deduction), f(l.late_deduction or 0), f(l.ot_hours), f(l.ot_rate), f(l.ot_amount),
            f(l.attendance_bonus), f(l.custom_additions or 0), f(l.other_addition), f(l.advance),
            f(l.other_deduction), f(l.custom_deductions or 0), f(l.tax), f(l.stamp), f(l.net_pay),
            f(bank_part(l)), f(cash_part(l)), l.payment_mode, l.bank_name, l.account_no, l.warnings, ""]


def salary_sheet(run) -> bytes:
    period = f"{month_name[run.month]} {run.year}"
    status = "FINAL" if run.status == "FINAL" else "DRAFT - not final"
    wb = Workbook()
    ws = wb.active
    ws.title = "Salary Sheet"
    cname = run.company.name
    _title(ws, f"Salary Sheet - {period} ({status})", len(SALARY_HEAD), cname)
    lines = sorted(run.lines, key=lambda l: (l.department, l.employee_code))
    _table(ws, SALARY_HEAD, [_salary_row(l) for l in lines], SALARY_MONEY, SALARY_TOTAL)

    from services.disbursement import bank_part, cash_part
    for mode, label in (("BANK", "Bank Advice"), ("CASH", "Cash Payment"), ("MOBILE", "Mobile Banking")):
        if mode == "CASH":
            sel = [(l, cash_part(l)) for l in lines if cash_part(l) > 0]
        else:
            sel = [(l, bank_part(l)) for l in lines if l.payment_mode == mode and bank_part(l) > 0]
        if not sel:
            continue
        ws2 = wb.create_sheet(label)
        head = ["Emp Code", "Name", "Department", "Bank / Wallet", "Account / Number", "Amount", "Signature"]
        _title(ws2, f"{label} - {period} ({status})", len(head), cname)
        _table(ws2, head, [[l.employee_code, l.full_name + (" (part)" if mode == "CASH" and bank_part(l) else ""),
                            l.department, "" if mode == "CASH" else l.bank_name, "" if mode == "CASH" else l.account_no,
                            float(a), ""] for l, a in sel], {6}, (6,))
        ws2.column_dimensions["E"].width = 22

    ws3 = wb.create_sheet("Department Summary")
    head = ["Department", "Employees", "Earned Gross", "OT Amount", "Deductions", "Net Pay"]
    _title(ws3, f"Department Summary - {period}", len(head), cname)
    agg: dict[str, list] = {}
    for l in lines:
        a = agg.setdefault(l.department or "-", [0, 0.0, 0.0, 0.0, 0.0])
        a[0] += 1
        a[1] += float(l.earned_gross); a[2] += float(l.ot_amount)
        a[3] += float(l.total_deductions + l.absent_deduction + (l.late_deduction or 0)); a[4] += float(l.net_pay)
    _table(ws3, head, [[k] + v for k, v in sorted(agg.items())], {3, 4, 5, 6}, (2, 3, 4, 5, 6))
    return _save(wb)


def attendance_month(year: int, month: int, employees, daily_by_emp) -> bytes:
    """Job-card style grid: one row per employee, one column per day, plus totals."""
    from services.processing import month_bounds
    first, last = month_bounds(year, month)
    days = list(range(1, last.day + 1))
    code = {"PRESENT": "P", "LATE": "L", "MISSING_OUT": "P*", "ABSENT": "A", "LEAVE_PAID": "LV",
            "LEAVE_UNPAID": "LW", "OFFDAY": "W", "OFFDAY_WORK": "WP"}
    wb = Workbook()
    ws = wb.active
    ws.title = "Monthly Attendance"
    head = ["Company", "Emp Code", "Name", "Department"] + [str(d) for d in days] + \
           ["Present", "Late", "Absent", "Leave", "Off", "OT Hrs", "Extra OT Hrs"]
    _title(ws, f"Monthly Attendance - {month_name[month]} {year}   "
               "(P present, L late, P* single punch, A absent, LV leave, LW leave w/o pay, W off, WP worked off-day)",
           len(head))
    rows = []
    for e in employees:
        recs = {r.work_date.day: r for r in daily_by_emp.get(e.id, [])}
        cells = [code.get(recs[d].status, "") if d in recs else "" for d in days]
        rs = list(recs.values())
        rows.append([e.company.code if e.company else "", e.employee_code, e.full_name,
                     e.department.name if e.department else ""] + cells + [
            sum(r.status in ("PRESENT", "LATE", "MISSING_OUT", "OFFDAY_WORK") for r in rs),
            sum(r.late_minutes > 0 for r in rs), sum(r.status == "ABSENT" for r in rs),
            sum(r.status in ("LEAVE_PAID", "LEAVE_UNPAID") for r in rs), sum(r.status == "OFFDAY" for r in rs),
            round(sum(r.ot_minutes for r in rs) / 60, 2), round(sum(r.extra_ot_minutes for r in rs) / 60, 2)])
    _table(ws, head, rows)
    for i in range(5, 5 + len(days)):
        ws.column_dimensions[get_column_letter(i)].width = 4.5
    return _save(wb)


def attendance_day(day, records) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Daily Attendance"
    head = ["Company", "Emp Code", "Name", "Department", "Shift", "Status", "In", "Out", "Late (min)",
            "Worked (h)", "OT (h)", "Extra OT (h)"]
    _title(ws, f"Daily Attendance - {day:%d %B %Y}", len(head))
    _table(ws, head, [[r.employee.company.code if r.employee.company else "", r.employee.employee_code, r.employee.full_name,
                       r.employee.department.name if r.employee.department else "", r.shift_code, r.status,
                       f"{r.in_time:%d-%m %H:%M}" if r.in_time else "", f"{r.out_time:%d-%m %H:%M}" if r.out_time else "",
                       r.late_minutes, round(r.worked_minutes / 60, 2), round(r.ot_minutes / 60, 2),
                       round(r.extra_ot_minutes / 60, 2)] for r in records])
    return _save(wb)


def punches_sheet(rows, emps: dict, start, end) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Punches"
    head = ["Card / ID", "Employee code", "Name", "Company", "Department", "Date", "Time", "Machine", "In/Out", "Source"]
    _title(ws, f"Punch records {start:%d/%m/%Y} - {end:%d/%m/%Y}", len(head))
    data = []
    for p in rows:
        e = emps.get(p.card_no)
        data.append([p.card_no, e.employee_code if e else "", e.full_name if e else "(not an employee)",
                     e.company.code if e and e.company else "", e.department.name if e and e.department else "",
                     p.punch_time.date(), p.punch_time.strftime("%H:%M:%S"), p.device or "", p.direction or "",
                     p.source])
    _table(ws, head, data)
    for r in ws.iter_rows(min_row=5, min_col=6, max_col=6):
        for c in r:
            c.number_format = "dd/mm/yyyy"
    return _save(wb)


def first_last_sheet(rows, start, end) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "First & Last"
    head = ["Date", "Card / ID", "Employee code", "Name", "Company", "Department", "Designation", "First punch",
            "Last punch", "Punches", "Hours", "First machine", "Last machine"]
    _title(ws, f"First & last punch {start:%d/%m/%Y} - {end:%d/%m/%Y}", len(head))
    data = []
    for r in rows:
        e = r["employee"]
        data.append([r["date"], r["card_no"], e.employee_code if e else "", e.full_name if e else "(not an employee)",
                     e.company.code if e and e.company else "", e.department.name if e and e.department else "",
                     e.designation if e else "", r["first"].strftime("%H:%M:%S"),
                     r["last"].strftime("%H:%M:%S") if r["count"] > 1 else "", r["count"], r["hours"] or "",
                     r["first_device"] or "", r["last_device"] or ""])
    _table(ws, head, data)
    for row in ws.iter_rows(min_row=5, min_col=1, max_col=1):
        for c in row:
            c.number_format = "dd/mm/yyyy"
    ws.freeze_panes = "E5"
    return _save(wb)


def combined_salary(runs, year: int, month: int) -> bytes:
    """All companies of the head office in one workbook: summary + one full sheet per company."""
    period = f"{month_name[month]} {year}"
    wb = Workbook()
    ws = wb.active
    ws.title = "HO Summary"
    head = ["Company", "Status", "Employees", "Earned Gross", "OT Amount", "Deductions", "Net Pay", "Bank", "Cash", "Mobile"]
    _title(ws, f"Head Office salary summary - {period}", len(head), BRAND["name"])
    data = []
    for run in runs:
        ls = run.lines
        by = {m: float(sum(l.net_pay for l in ls if l.payment_mode == m)) for m in ("BANK", "CASH", "MOBILE")}
        data.append([f"{run.company.code} - {run.company.name}", run.status, len(ls),
                     float(sum(l.earned_gross for l in ls)), float(sum(l.ot_amount for l in ls)),
                     float(sum(l.total_deductions + l.absent_deduction for l in ls)), float(sum(l.net_pay for l in ls)),
                     by["BANK"], by["CASH"], by["MOBILE"]])
    _table(ws, head, data, set(range(4, 11)), tuple(range(3, 11)))
    ws.column_dimensions["A"].width = 40
    for run in runs:
        sh = wb.create_sheet(run.company.code[:28])
        status = "FINAL" if run.status == "FINAL" else "DRAFT - not final"
        _title(sh, f"Salary Sheet - {period} ({status})", len(SALARY_HEAD), run.company.name)
        _table(sh, SALARY_HEAD, [_salary_row(l) for l in sorted(run.lines, key=lambda l: (l.department, l.employee_code))],
               SALARY_MONEY, SALARY_TOTAL)
    return _save(wb)
