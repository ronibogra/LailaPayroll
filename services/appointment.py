"""Appointment letter: an editable template (Settings > Appointment letter) filled from the employee's data.

Fields are written like {name} in the text. A line with only {salary_table} becomes the salary breakdown table."""
from __future__ import annotations

import io
import re
from datetime import date
from decimal import Decimal

DEFAULT_SUBJECT = "Letter of Appointment - {designation}"
DEFAULT_BODY = """Dear {name},

With reference to your application and the interview held with you, the management of {company} is pleased to appoint you as {designation} in the {department} department on the following terms and conditions:

1. Joining: Your appointment is effective from {joining_date}. Your employee ID is {employee_code}.

2. Probation: You will be on probation for {probation_months} months from the date of joining. On satisfactory completion of probation your service will be confirmed in writing.

3. Salary: Your monthly gross salary will be Tk {gross} ({gross_words}), as follows:
{salary_table}

4. Working hours: Your shift is {shift} ({shift_time}). Your weekly holiday is {weekly_off}. Overtime: {overtime}.

5. Attendance: You must record your attendance at the punch machine (or the company's mobile attendance, if allowed) when you come in and when you leave.

6. Leave and holidays: You will get leave as per the company's leave policy and the Bangladesh Labour Act 2006: {leave_summary}. Festival and government holidays as declared by the management.

7. Termination: During probation either party may end this appointment by giving notice as per law. After confirmation, termination and resignation will be governed by the Bangladesh Labour Act 2006 and the company's service rules.

8. Conduct: You will follow the company's service rules, policies and lawful instructions of the management, and keep all company information confidential.

9. Documents: Please submit copies of your NID, educational certificates and two passport-size photographs on the day of joining.

Please sign and return the duplicate copy of this letter as a token of your acceptance of the above terms and conditions.

We welcome you to {group} and wish you a successful career with us."""

FIELDS = {
    "name": "Employee name", "father_name": "Father's name", "mother_name": "Mother's name",
    "present_address": "Present address", "permanent_address": "Permanent address", "employee_code": "Employee ID",
    "designation": "Designation", "department": "Department", "section": "Section / line", "grade": "Grade",
    "company": "Company name", "company_code": "Company code", "group": "Head Office / group name",
    "joining_date": "Joining date", "letter_date": "Letter date", "ref": "Reference no.",
    "gross": "Gross salary", "gross_words": "Gross salary in words", "basic": "Basic", "house_rent": "House rent",
    "medical": "Medical", "transport": "Transport", "food": "Food", "salary_table": "Salary table (own line)",
    "shift": "Shift name", "shift_time": "Shift time", "weekly_off": "Weekly holiday", "overtime": "Overtime rule",
    "leave_summary": "Leave entitlement", "probation_months": "Probation months", "nid": "NID no.",
    "phone": "Mobile", "signatory_name": "Signed by (name)", "signatory_title": "Signed by (title)",
}
SETTING_KEYS = ("subject", "body", "probation_months", "signatory_name", "signatory_title")
DEFAULTS = {"subject": DEFAULT_SUBJECT, "body": DEFAULT_BODY, "probation_months": "6",
            "signatory_name": "", "signatory_title": "Head of HR & Admin"}

_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve",
         "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _two(n: int) -> str:
    return _ONES[n] if n < 20 else (_TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else ""))


def _three(n: int) -> str:
    h, r = divmod(n, 100)
    return ((_ONES[h] + " Hundred" + (" " if r else "")) if h else "") + (_two(r) if r else "")


def taka_words(amount) -> str:
    """Bangladeshi system: 1,25,500 -> 'Taka One Lakh Twenty Five Thousand Five Hundred only'."""
    v = Decimal(str(amount or 0)).quantize(Decimal("0.01"))
    n, paisa = int(v), int((v - int(v)) * 100)
    if paisa:
        return taka_words(n)[:-5] + f" and {_two(paisa)} Paisa only"
    if n == 0:
        return "Taka Zero only"
    parts = []
    crore, n = divmod(n, 10_000_000)
    lakh, n = divmod(n, 100_000)
    thousand, n = divmod(n, 1000)
    if crore:
        parts.append((taka_words(crore)[5:-5] if crore > 99 else _two(crore)) + " Crore")
    if lakh:
        parts.append(_two(lakh) + " Lakh")
    if thousand:
        parts.append(_two(thousand) + " Thousand")
    if n:
        parts.append(_three(n))
    return "Taka " + " ".join(parts) + " only"


def _money(v) -> str:
    return f"{Decimal(str(v or 0)):,.0f}"


def fill(text: str, values: dict) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), text or "")


def load_template(db) -> dict:
    from services.policy import get_setting
    return {k: (get_setting(db, f"appointment.{k}", default=None) or DEFAULTS[k]) for k in SETTING_KEYS}


def values_for(db, e, letter_date: date, joining: date | None, ref: str, tpl: dict) -> dict:
    from services import leave as leave_svc
    from services.branding import BRAND
    from services.policy import load_policy, weekend_set
    from services.processing import weekly_off_for
    names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    off = sorted(weekly_off_for(e, e.shift.weekly_off if e.shift else None, weekend_set(db)))
    ot_ok = load_policy(db, e.company_id).ot_enabled and e.ot_eligible is not False
    leaves = [t for t in leave_svc.types(db) if t.days_per_year and t.paid and (not t.gender or t.gender == e.gender)]
    shift = e.shift
    return {
        "name": e.full_name, "father_name": e.father_name or "", "mother_name": e.mother_name or "",
        "present_address": e.present_address or "", "permanent_address": e.permanent_address or "",
        "employee_code": e.employee_code, "designation": e.designation or "-",
        "department": e.department.name if e.department else "-", "section": e.section or "", "grade": e.grade or "",
        "company": e.company.name if e.company else BRAND["name"], "company_code": e.company.code if e.company else "",
        "group": BRAND["name"], "joining_date": f"{joining:%d %B %Y}" if joining else "the date of joining",
        "letter_date": f"{letter_date:%d %B %Y}", "ref": ref,
        "gross": _money(e.gross), "gross_words": taka_words(e.gross), "basic": _money(e.basic),
        "house_rent": _money(e.house_rent), "medical": _money(e.medical), "transport": _money(e.transport),
        "food": _money(e.food),
        "shift": shift.name if shift else "General", "shift_time": (f"{shift.start:%I:%M %p} to {shift.end:%I:%M %p}"
                                                                   if shift else "as per office time"),
        "weekly_off": " and ".join(names[d] for d in off) if off else "as per roster",
        "overtime": "applicable as per law and company policy" if ot_ok else "not applicable for this position",
        "leave_summary": ", ".join(f"{t.name} {t.days_per_year} days a year" for t in leaves) or "as per law",
        "probation_months": tpl.get("probation_months") or "6", "nid": e.nid or "", "phone": e.phone or "",
        "signatory_name": tpl.get("signatory_name") or "", "signatory_title": tpl.get("signatory_title") or "",
    }


def letter_pdf(e, values: dict, tpl: dict, components=(), acceptance: bool = True, title: str = "",
               to_block: bool = True) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_JUSTIFY
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    from config import settings
    from services.branding import BRAND, PRODUCT_LOGO, logo_path

    W, H = A4
    body = ParagraphStyle("b", fontName="Helvetica", fontSize=10.5, leading=14.5, alignment=TA_JUSTIFY, spaceAfter=6)
    bold = ParagraphStyle("bb", parent=body, fontName="Helvetica-Bold")
    small = ParagraphStyle("s", parent=body, fontSize=9.5, leading=12.5, spaceAfter=0, alignment=0)

    def esc(t):
        return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def salary_table():
        rows = [["Salary head", "Tk per month"]]
        for label, key in (("Basic salary", "basic"), ("House rent", "house_rent"), ("Medical allowance", "medical"),
                           ("Transport allowance", "transport"), ("Food allowance", "food")):
            if Decimal(str(getattr(e, key) or 0)):
                rows.append([label, values[key]])
        rows.append(["Gross salary", values["gross"]])
        n_main = len(rows)
        calc = {"FIXED": "fixed", "PCT_BASIC": "% of basic", "PCT_GROSS": "% of gross",
                "PER_PRESENT_DAY": "per present day", "PER_OT_HOUR": "per OT hour"}
        for c in components:
            v = f"{c.value:,.2f}".rstrip("0").rstrip(".")
            rows.append([f"{c.name} ({'deduction' if c.kind == 'DED' else 'allowance'})",
                         f"{v} {calc.get(c.calc, '')}".strip()])
        t = Table(rows, colWidths=[85 * mm, 45 * mm], hAlign="LEFT")
        st = [("FONT", (0, 0), (-1, -1), "Helvetica", 9.5), ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9.5),
              ("FONT", (0, n_main - 1), (-1, n_main - 1), "Helvetica-Bold", 9.5),
              ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0f2f5")),
              ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#b8bec8")), ("ALIGN", (1, 0), (1, -1), "RIGHT"),
              ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6)]
        t.setStyle(TableStyle(st))
        return [Spacer(1, 2), t, Spacer(1, 8)]

    story = []
    story.append(Table([[Paragraph(f"Ref: {esc(values['ref'])}", small),
                         Paragraph(f"Date: {esc(values['letter_date'])}", ParagraphStyle("r", parent=small, alignment=2))]],
                       colWidths=[100 * mm, 80 * mm], style=[("LEFTPADDING", (0, 0), (-1, -1), 0),
                                                             ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story.append(Spacer(1, 8))
    to = [f"<b>{esc(values['name'])}</b>"]
    if values["father_name"]:
        to.append(f"Father: {esc(values['father_name'])}")
    addr = values["present_address"] or values["permanent_address"]
    if addr:
        to.append(esc(addr).replace("\n", "<br/>"))
    if values["phone"]:
        to.append(f"Mobile: {esc(values['phone'])}")
    if to_block:
        story.append(Paragraph("To<br/>" + "<br/>".join(to), small))
    story.append(Spacer(1, 10))
    story.append(Paragraph(f"<u>Subject: {esc(fill(tpl['subject'], values))}</u>", bold))
    story.append(Spacer(1, 4))
    text = fill(tpl["body"], {k: v for k, v in values.items() if k != "salary_table"})
    para: list[str] = []

    def flush():
        if para:
            story.append(Paragraph("<br/>".join(esc(x) for x in para), body))
            para.clear()

    for line in text.replace("\r\n", "\n").split("\n"):
        if line.strip() == "{salary_table}":
            flush()
            story.extend(salary_table())
        elif not line.strip():
            flush()
        else:
            para.append(line.strip())
    flush()
    sign = [Spacer(1, 26), Paragraph("_______________________", small),
            Paragraph(f"<b>{esc(values['signatory_name'] or 'Authorised signatory')}</b>", small),
            Paragraph(esc(values["signatory_title"]), small), Paragraph(esc(values["company"]), small)]
    story.append(KeepTogether(sign))
    accept = [Spacer(1, 18), Paragraph("<b>Acceptance</b>", small), Spacer(1, 3),
              Paragraph(f"I, {esc(values['name'])}, have read and understood the above terms and conditions and accept "
                        f"them. I will join on {esc(values['joining_date'])}.", small),
              Spacer(1, 24),
              Table([["Signature: ____________________", "Date: ______________"]], colWidths=[100 * mm, 80 * mm],
                    style=[("FONT", (0, 0), (-1, -1), "Helvetica", 9.5), ("LEFTPADDING", (0, 0), (-1, -1), 0)])]
    if acceptance:
        story.append(KeepTogether(accept))

    company_name = values["company"]
    company_addr = (e.company.address if e.company and e.company.address else BRAND.get("address")) or ""

    def decorate(canv, doc):
        canv.saveState()
        try:
            canv.drawImage(str(logo_path()), 15 * mm, H - 27 * mm, 17 * mm, 17 * mm, mask="auto", preserveAspectRatio=True)
        except Exception:  # noqa: BLE001
            pass
        canv.setFillColor(colors.HexColor("#E8332F"))
        canv.setFont("Helvetica-Bold", 15)
        canv.drawString(35 * mm, H - 16 * mm, company_name[:60])
        canv.setFillColor(colors.HexColor("#444444"))
        canv.setFont("Helvetica", 8.5)
        line2 = f"A concern of {BRAND['name']}" if BRAND["name"] not in company_name else ""
        canv.drawString(35 * mm, H - 21 * mm, " · ".join(x for x in (line2, company_addr[:90]) if x))
        contact = " · ".join(x for x in (BRAND.get("phone"), BRAND.get("email")) if x)
        if contact:
            canv.drawString(35 * mm, H - 25.5 * mm, contact[:100])
        canv.setStrokeColor(colors.HexColor("#E8332F"))
        canv.setLineWidth(1.2)
        canv.line(15 * mm, H - 30 * mm, W - 15 * mm, H - 30 * mm)
        canv.setFont("Helvetica", 7)
        canv.setFillColor(colors.HexColor("#888888"))
        try:
            canv.drawImage(str(PRODUCT_LOGO), 15 * mm, 6 * mm, 11 * mm, 7 * mm, mask="auto", preserveAspectRatio=True)
        except Exception:  # noqa: BLE001
            pass
        canv.drawString(27 * mm, 8.5 * mm, f"SPhere Payroll · {settings.developer_credit}")
        canv.drawRightString(W - 15 * mm, 8.5 * mm, f"{values['ref']} · page {doc.page}")
        canv.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=36 * mm,
                            bottomMargin=18 * mm, title=f"{title or 'Appointment letter'} {e.employee_code}",
                            author=company_name)
    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
    return buf.getvalue()
