"""One-page employee profile PDF: Head Office logo, photo, job, personal, IDs, salary (if allowed), leave
balance, custom pay components and the list of documents on file."""
import io
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from services.branding import BRAND, PRODUCT_LOGO, logo_path

RED = colors.HexColor("#E8332F")


def _d(v):
    return v.strftime("%d %b %Y") if v else "-"


def _money(v):
    return f"Tk {float(v or 0):,.0f}"


def profile_pdf(e, photo, balances, documents, show_salary: bool, components=()) -> bytes:
    from config import settings
    buf = io.BytesIO()
    W, H = A4
    cell = ParagraphStyle("c", fontName="Helvetica", fontSize=9, leading=11.5)
    lab = ParagraphStyle("l", parent=cell, textColor=colors.HexColor("#6b7585"))
    h2 = ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=11, textColor=RED, spaceBefore=8, spaceAfter=4)
    name = ParagraphStyle("n", fontName="Helvetica-Bold", fontSize=17, leading=21)
    sub = ParagraphStyle("s", parent=cell, fontSize=10, textColor=colors.HexColor("#3b4453"))

    def esc(t):
        return str(t if t not in (None, "") else "-").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def kv(rows, cols=2):
        data, line = [], []
        for k, v in rows:
            line += [Paragraph(esc(k), lab), Paragraph(esc(v), cell)]
            if len(line) == cols * 2:
                data.append(line)
                line = []
        if line:
            data.append(line + [""] * (cols * 2 - len(line)))
        t = Table(data, colWidths=[30 * mm, 60 * mm] * cols)
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                               ("TOPPADDING", (0, 0), (-1, -1), 2),
                               ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#e3e6eb"))]))
        return t

    story = []
    head_txt = [Paragraph(esc(e.full_name), name),
                Paragraph(esc(f"{e.designation or ''}{', ' + e.department.name if e.department else ''}"), sub),
                Paragraph(f"<b>{esc(e.company.name if e.company else '')}</b>", sub), Spacer(1, 4),
                Paragraph(f"Employee code <b>{esc(e.employee_code)}</b> &nbsp;&nbsp; Card no. <b>{esc(e.card_no)}</b>"
                          f" &nbsp;&nbsp; Status <b>{esc(e.employment_status)}</b>", cell)]
    pic = Image(str(photo), width=32 * mm, height=40 * mm, kind="proportional") if photo else Paragraph("No photo", lab)
    top = Table([[pic, head_txt]], colWidths=[38 * mm, 142 * mm])
    top.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOX", (0, 0), (0, 0), 0.4, colors.HexColor("#e3e6eb"))]))
    story += [top, Spacer(1, 4)]
    story += [Paragraph("Job", h2), kv([
        ("Company", f"{e.company.code} - {e.company.name}" if e.company else "-"),
        ("Department", e.department.name if e.department else "-"), ("Designation", e.designation),
        ("Section / line", e.section), ("Grade", e.grade),
        ("Shift", f"{e.shift.code} ({e.shift.start:%H:%M}-{e.shift.end:%H:%M})" if e.shift else "GENERAL"),
        ("Joined", _d(e.joined_on)), ("Left", _d(e.left_on)),
        ("Overtime", "eligible" if e.ot_eligible is not False else "not eligible"),
        ("Mobile punch", "allowed" if e.allow_mobile else "no")])]
    story += [Paragraph("Personal", h2), kv([
        ("Father", e.father_name), ("Mother", e.mother_name), ("Spouse", e.spouse_name),
        ("Date of birth", _d(e.date_of_birth)), ("Gender", e.gender), ("Blood group", e.blood_group),
        ("Religion", e.religion), ("Marital status", e.marital_status), ("Mobile", e.phone), ("Email", e.email),
        ("Emergency contact", e.emergency_contact), ("Education", e.education),
        ("Present address", e.present_address), ("Permanent address", e.permanent_address)])]
    story += [Paragraph("Identity", h2), kv([("NID", e.nid), ("Birth certificate", e.birth_certificate_no),
                                             ("Passport", e.passport_no)])]
    if show_salary:
        story += [Paragraph("Salary", h2), kv([
            ("Gross", _money(e.gross)), ("Basic", _money(e.basic)), ("House rent", _money(e.house_rent)),
            ("Medical", _money(e.medical)), ("Transport", _money(e.transport)), ("Food", _money(e.food)),
            ("Pay mode", e.payment_mode), ("Bank / account", f"{e.bank_name or ''} {e.account_no or ''}".strip())]
            + [(c.name, f"{'+' if c.kind == 'ADD' else '-'} {c.value} ({c.calc.replace('_', ' ').lower()})")
               for c in components])]
    if balances:
        story.append(Paragraph(f"Leave {datetime.now():%Y}", h2))
        data = [["Leave type", "Days / year", "Used", "Waiting", "Left"]] + [
            [b["type"].name, b["entitled"] or "no limit", b["used"], b["pending"], "-" if b["left"] is None else b["left"]]
            for b in balances]
        t = Table(data, colWidths=[70 * mm, 28 * mm, 25 * mm, 25 * mm, 25 * mm])
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), RED), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                               ("FONTSIZE", (0, 0), (-1, -1), 8.5), ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#BBBBBB")),
                               ("ALIGN", (1, 0), (-1, -1), "CENTER")]))
        story.append(t)
    if documents:
        story.append(Paragraph("Documents on file", h2))
        from services.documents import DOC_TYPES
        story.append(kv([(DOC_TYPES.get(d.doc_type, d.doc_type), f"{d.title} ({d.uploaded_at:%d/%m/%Y})")
                         for d in documents], cols=1))

    def decorate(canv, doc):
        canv.saveState()
        try:
            canv.drawImage(str(logo_path()), 15 * mm, H - 22 * mm, 14 * mm, 14 * mm, mask="auto", preserveAspectRatio=True)
        except Exception:  # noqa: BLE001
            pass
        canv.setFillColor(RED)
        canv.setFont("Helvetica-Bold", 14)
        canv.drawString(32 * mm, H - 14 * mm, BRAND["name"])
        canv.setFillColor(colors.black)
        canv.setFont("Helvetica-Bold", 10)
        canv.drawString(32 * mm, H - 19.5 * mm, "Employee profile")
        canv.setFont("Helvetica", 7.5)
        canv.setFillColor(colors.HexColor("#666666"))
        canv.drawRightString(W - 15 * mm, H - 14 * mm, (BRAND.get("address") or "")[:80])
        canv.drawRightString(W - 15 * mm, H - 19.5 * mm, f"Printed {datetime.now():%d/%m/%Y %H:%M}")
        try:
            canv.drawImage(str(PRODUCT_LOGO), 15 * mm, 6 * mm, 13 * mm, 8 * mm, mask="auto", preserveAspectRatio=True)
        except Exception:  # noqa: BLE001
            pass
        canv.drawString(29 * mm, 8.5 * mm, f"SPhere Payroll · {settings.developer_credit}")
        canv.drawRightString(W - 15 * mm, 8.5 * mm, f"Page {doc.page}")
        canv.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=27 * mm,
                            bottomMargin=17 * mm, title=f"Profile {e.employee_code}", author=BRAND["name"])
    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
    return buf.getvalue()
