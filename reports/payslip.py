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
    from reportlab.lib import colors
    from reportlab.platypus import Table, TableStyle

    def grid(head, vals, widths):
        t = Table([head, vals], colWidths=widths, rowHeights=[4.6 * mm, 5 * mm])
        t.setStyle(TableStyle([
            ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7.8), ("FONT", (0, 1), (-1, 1), "Helvetica", 8.6),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0f2f5")), ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#b8bec8")),
            ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
        w, h = t.wrapOn(c, x1 - x0, 20 * mm)
        t.drawOn(c, x0, y - h + 2.5 * mm)
        return h

    full = x1 - x0
    h = grid(["Days in month", "Employed", "Present", "Absent", "Leave", "Off days", "Late"],
             [l.days_in_month, l.employed_days, l.present_days, l.absent_days, l.paid_leave_days, l.offdays, l.late_days],
             [full / 7] * 7)
    y -= h + 2.5 * mm
    if (l.gross or 0) > 0:
        h = grid(["Basic", "House rent", "Medical", "Transport", "Food", "Gross salary"],
                 [_tk(l.basic), _tk(l.house_rent), _tk(l.medical), _tk(l.transport), _tk(l.food), _tk(l.gross)],
                 [full / 6] * 6)
        c.setFont("Helvetica-Bold", 8.6)
        y -= h + 4.5 * mm
    c.setFont("Helvetica", 9)
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
    rows = max(len(earn), len(ded))
    step = 4.6 * mm
    c.setFont("Helvetica-Bold", 9)
    c.drawString(x0, y, "Earnings"); c.drawRightString(mid - 6 * mm, y, "Tk")
    c.drawString(mid, y, "Deductions"); c.drawRightString(x1, y, "Tk")
    c.line(x0, y - 1.5 * mm, x1, y - 1.5 * mm)
    c.setFont("Helvetica", 9)
    for i, (k, v) in enumerate(earn):
        yy = y - (i + 1) * step
        c.drawString(x0, yy, k); c.drawRightString(mid - 6 * mm, yy, _tk(v))
    for i, (k, v) in enumerate(ded):
        yy = y - (i + 1) * step
        c.drawString(mid, yy, k); c.drawRightString(x1, yy, _tk(v))
    y -= (rows + 1) * step
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
    from services.disbursement import bank_part, cash_part
    b, cash = bank_part(l), cash_part(l)
    where = {"BANK": f"bank {l.bank_name or ''} {l.account_no or ''}".strip(),
             "MOBILE": f"{l.bank_name or 'mobile wallet'} {l.account_no or ''}".strip()}.get(l.payment_mode, "")
    if b and cash:
        paid = f"Paid: Tk {_tk(b)} to {where} + Tk {_tk(cash)} in cash"
    elif b:
        paid = f"Paid to {where}"
    else:
        paid = "Paid in cash"
    c.drawRightString(x1, y - 7 * mm, paid[:95])
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


def _rows(l) -> int:
    """How many earning / deduction lines the slip will have (same rules as _slip)."""
    try:
        items = json.loads(l.items_json or "[]")
    except ValueError:
        items = []
    earn = 1 + sum(1 for v in (l.ot_amount, l.attendance_bonus, l.other_addition) if v) + \
        sum(1 for i in items if i["kind"] == "ADD")
    ded = sum(1 for v in (l.absent_deduction, l.late_deduction, l.advance, l.other_deduction, l.tax, l.stamp) if v) + \
        sum(1 for i in items if i["kind"] == "DED")
    return max(earn, ded)


def payslips_pdf(run, lines=None) -> bytes:
    """run=None: lines from several companies (each line's own run gives its company name)."""
    lines = sorted(lines if lines is not None else run.lines, key=lambda l: (l.department, l.employee_code))
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4
    slot = 0                      # 0 = top half free, 1 = bottom half free
    for l in lines:
        big = _rows(l) > 8        # many additions / deductions: the slip gets a whole page
        if big and slot == 1:
            c.showPage()
            slot = 0
        _slip(c, l, run or l.run, H - slot * H / 2)
        if big:
            c.showPage()
            slot = 0
            continue
        if slot == 0:
            c.setDash(3, 3); c.setStrokeColorRGB(0.6, 0.6, 0.6)
            c.line(8 * mm, H / 2, W - 8 * mm, H / 2)
            c.setDash(); c.setStrokeColorRGB(0, 0, 0)
            slot = 1
        else:
            c.showPage()
            slot = 0
    if slot == 1:
        c.showPage()
    if not lines:
        c.drawString(20 * mm, H - 20 * mm, "No payroll lines.")
        c.showPage()
    c.save()
    return buf.getvalue()
