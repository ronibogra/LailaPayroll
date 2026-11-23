"""Payslips as PDF: two payslips per A4 page (cut along the dashed line)."""
import io
import json
from decimal import Decimal
from calendar import month_name

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from services.branding import BRAND, logo_path

RED = (0.91, 0.20, 0.18)


def _tk(x) -> str:
    return f"{float(x):,.2f}"


def _slip(c: canvas.Canvas, l, run, top: float) -> None:
    W, _ = A4
    x0, x1 = 15 * mm, W - 15 * mm
    y = top - 12 * mm
    c.drawImage(str(logo_path()), x0, y - 8 * mm, 14 * mm, 14 * mm, mask="auto", preserveAspectRatio=True)
    c.setFillColorRGB(*RED)
    c.setFont("Helvetica-Bold", 15)
    c.drawString(x0 + 17 * mm, y, run.company.name)
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica", 10)
    c.drawString(x0 + 17 * mm, y - 5 * mm, f"Payslip - {month_name[run.month]} {run.year}"
                 + ("" if run.status == "FINAL" else "   (DRAFT)"))
    c.setFont("Helvetica", 8)
    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.drawRightString(x1, y, f"A {BRAND['name']} concern")
    addr = run.company.address or BRAND.get("address")      # all companies sit at the Head Office
    if addr:
        c.drawRightString(x1, y - 4 * mm, addr[:70])
    c.setFillColorRGB(0, 0, 0)
    y -= 15 * mm
    c.setFont("Helvetica", 9)
    info = [("Employee ID", l.employee_code), ("Name", l.full_name), ("Department", l.department),
            ("Designation", l.designation), ("Grade", l.grade), ("Payment", f"{l.payment_mode} {l.account_no}".strip())]
    for i, (k, v) in enumerate(info):
        cx = x0 + (i % 2) * 90 * mm
        cy = y - (i // 2) * 5 * mm
        c.drawString(cx, cy, f"{k}:")
        c.drawString(cx + 24 * mm, cy, str(v or "-")[:45])
    y -= 17 * mm
    c.drawString(x0, y, f"Days in month: {l.days_in_month}   Employed: {l.employed_days}   Present: {l.present_days}   "
                        f"Absent: {l.absent_days}   Leave: {l.paid_leave_days}   Off days: {l.offdays}   Late: {l.late_days}")
    y -= 7 * mm

    c.drawString(x0, y, f"Salary structure:  Basic {_tk(l.basic)} + House rent {_tk(l.house_rent)} + Medical {_tk(l.medical)}"
                        f" + Transport {_tk(l.transport)} + Food {_tk(l.food)} = Gross {_tk(l.gross)}")
    y -= 7 * mm
    items = []
    try:
        items = json.loads(l.items_json or "[]")
    except ValueError:
        pass
    late = l.late_deduction or 0
    earn = [(f"Earned gross ({l.employed_days}/{l.days_in_month} days)", l.earned_gross),
            (f"Overtime {l.ot_hours} h x {l.ot_rate}", l.ot_amount), ("Attendance bonus", l.attendance_bonus),
            ("Other additions", l.other_addition)]
    earn += [(i["name"][:38], Decimal(i["amount"])) for i in items if i["kind"] == "ADD"]
    ded = [(f"Absent / unpaid leave ({l.absent_days} days)", l.absent_deduction), (f"Late ({l.late_days} days)", late),
           ("Advance", l.advance), ("Other deductions", l.other_deduction), ("Tax", l.tax)]
    ded += [(i["name"][:38], Decimal(i["amount"])) for i in items if i["kind"] == "DED"] + [("Revenue stamp", l.stamp)]
    # keep the slip short: hide empty lines (the first earning line always shows)
    earn = earn[:1] + [x for x in earn[1:] if x[1]]
    ded = [x for x in ded if x[1]] or [("-", 0)]
    mid = x0 + 92 * mm
    c.setFont("Helvetica-Bold", 9)
    c.drawString(x0, y, "Earnings"); c.drawRightString(mid - 6 * mm, y, "Tk")
    c.drawString(mid, y, "Deductions"); c.drawRightString(x1, y, "Tk")
    c.line(x0, y - 1.5 * mm, x1, y - 1.5 * mm)
    c.setFont("Helvetica", 9)
    for i, (k, v) in enumerate(earn):
        yy = y - (i + 1) * 4.6 * mm
        c.drawString(x0, yy, k); c.drawRightString(mid - 6 * mm, yy, _tk(v))
    for i, (k, v) in enumerate(ded):
        yy = y - (i + 1) * 4.6 * mm
        c.drawString(mid, yy, k); c.drawRightString(x1, yy, _tk(v))
    y -= (max(len(earn), len(ded)) + 1) * 4.6 * mm
    c.line(x0, y + 2.5 * mm, x1, y + 2.5 * mm)
    c.setFont("Helvetica-Bold", 9)
    gross_earn = sum((v for _, v in earn), Decimal(0))
    c.drawString(x0, y - 1 * mm, "Total"); c.drawRightString(mid - 6 * mm, y - 1 * mm, _tk(gross_earn))
    c.drawString(mid, y - 1 * mm, "Total"); c.drawRightString(x1, y - 1 * mm, _tk(sum((v for _, v in ded), Decimal(0))))
    y -= 9 * mm
    c.setFont("Helvetica-Bold", 11)
    c.drawString(x0, y - 2 * mm, f"NET PAY:  Tk {_tk(l.net_pay)}")
    from services.appointment import taka_words
    c.setFont("Helvetica-Oblique", 8.5)
    c.drawString(x0 + 62 * mm, y - 2 * mm, f"({taka_words(l.net_pay)})"[:95])
    c.setFont("Helvetica", 8.5)
    c.drawRightString(x1, y - 7 * mm, {"BANK": f"Paid to bank: {l.bank_name or ''} {l.account_no or ''}",
                                       "MOBILE": f"Paid to {l.bank_name or 'mobile wallet'}: {l.account_no or ''}",
                                       "CASH": "Paid in cash"}.get(l.payment_mode, "")[:70])
    c.setFont("Helvetica", 8)
    c.drawString(x0, y - 15 * mm, "Employee signature: ____________________")
    c.drawRightString(x1, y - 15 * mm, "Authorised signature: ____________________")
    c.setFillColorRGB(0.45, 0.45, 0.45)
    from config import settings
    from services.branding import PRODUCT_LOGO
    try:
        c.drawImage(str(PRODUCT_LOGO), x0, y - 26.5 * mm, 12 * mm, 7 * mm, mask="auto", preserveAspectRatio=True)
    except Exception:  # noqa: BLE001
        pass
    c.drawString(x0 + 13 * mm, y - 24 * mm, f"SPhere Payroll · {settings.developer_credit}")
    c.setFillColorRGB(0, 0, 0)


def payslips_pdf(run, lines=None) -> bytes:
    lines = sorted(lines if lines is not None else run.lines, key=lambda l: (l.department, l.employee_code))
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4
    for i, l in enumerate(lines):
        half = i % 2
        _slip(c, l, run, H - half * H / 2)
        if half == 0:
            c.setDash(3, 3); c.setStrokeColorRGB(0.6, 0.6, 0.6)
            c.line(8 * mm, H / 2, W - 8 * mm, H / 2)
            c.setDash(); c.setStrokeColorRGB(0, 0, 0)
        if half == 1 or i == len(lines) - 1:
            c.showPage()
    if not lines:
        c.drawString(20 * mm, H - 20 * mm, "No payroll lines.")
        c.showPage()
    c.save()
    return buf.getvalue()
