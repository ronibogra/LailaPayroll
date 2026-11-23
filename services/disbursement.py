"""Salary disbursement: bank advice letters, bank / mobile-banking upload files and the cash payment sheet.

Each employee has a pay mode (BANK, MOBILE, CASH) and bank / wallet + account no. in the profile.
The company's own salary account (debited by the bank) is set on the Payroll page."""
from __future__ import annotations

import io
from calendar import month_name
from collections import OrderedDict
from datetime import date
from decimal import Decimal

from services.appointment import taka_words

MODES = {"BANK": "Bank transfer", "MOBILE": "Mobile banking (bKash / Nagad / Rocket)", "CASH": "Cash"}
BANK_KEYS = ("bank_name", "branch", "account_name", "account_no", "signatory1", "signatory2")


def split(net, mode: str, split_mode: str, split_value) -> tuple[Decimal, Decimal]:
    """(to bank / wallet, in cash) for one net pay."""
    net = Decimal(str(net or 0))
    if net <= 0:
        return Decimal(0), Decimal(0)
    if mode not in ("BANK", "MOBILE"):
        return Decimal(0), net
    v = Decimal(str(split_value or 0))
    if split_mode == "FIXED" and v > 0:
        bank = min(v, net)
    elif split_mode == "PERCENT" and 0 < v < 100:
        bank = (net * v / 100).quantize(Decimal(1))
    else:
        bank = net
    return bank, net - bank


def bank_part(l) -> Decimal:
    if l.bank_amount is not None:
        return Decimal(l.bank_amount)
    return Decimal(l.net_pay) if l.payment_mode in ("BANK", "MOBILE") and l.net_pay > 0 else Decimal(0)


def cash_part(l) -> Decimal:
    if l.cash_amount is not None:
        return Decimal(l.cash_amount)
    return Decimal(l.net_pay) if l.payment_mode not in ("BANK", "MOBILE") and l.net_pay > 0 else Decimal(0)


def totals(lines) -> dict:
    """Amounts per channel (a split employee counts in both)."""
    out = {"BANK": Decimal(0), "MOBILE": Decimal(0), "CASH": Decimal(0)}
    people = {"BANK": 0, "MOBILE": 0, "CASH": 0}
    for l in lines:
        b, c = bank_part(l), cash_part(l)
        if b > 0 and l.payment_mode in ("BANK", "MOBILE"):
            out[l.payment_mode] += b
            people[l.payment_mode] += 1
        if c > 0:
            out["CASH"] += c
            people["CASH"] += 1
    return {"amount": out, "people": people}


def company_bank(db, company_id: int) -> dict:
    from services.policy import get_setting
    return {k: get_setting(db, f"salary_bank.{k}", company_id, default="") or "" for k in BANK_KEYS}


def group_by(lines, mode: str) -> "OrderedDict[str, list]":
    out: OrderedDict[str, list] = OrderedDict()
    for l in sorted((l for l in lines if l.payment_mode == mode and bank_part(l) > 0),
                    key=lambda l: ((l.bank_name or "").strip().lower(), l.employee_code)):
        out.setdefault((l.bank_name or "(bank not set)").strip() or "(bank not set)", []).append(l)
    return out


def problems(lines) -> list[str]:
    """Things to fix before paying: missing account numbers, duplicates."""
    out, seen = [], {}
    for l in lines:
        if l.payment_mode in ("BANK", "MOBILE") and bank_part(l) > 0:
            if not (l.account_no or "").strip():
                out.append(f"{l.employee_code} {l.full_name}: {l.payment_mode.lower()} pay but no account / wallet no.")
            elif l.payment_mode == "MOBILE" and not (l.account_no.strip().isdigit() and len(l.account_no.strip()) == 11):
                out.append(f"{l.employee_code} {l.full_name}: wallet number {l.account_no} is not 11 digits")
            if l.payment_mode == "BANK" and not (l.bank_name or "").strip():
                out.append(f"{l.employee_code} {l.full_name}: bank name missing")
            acc = (l.account_no or "").strip()
            if acc and acc in seen:
                out.append(f"{l.employee_code} and {seen[acc]} have the same account no. {acc}")
            seen.setdefault(acc, l.employee_code)
    return out


def transfer_xlsx(run) -> bytes:
    """One sheet per bank (to upload / send to the bank) and one per mobile wallet provider."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    wb.remove(wb.active)
    period = f"{month_name[run.month]} {run.year}"
    for mode, prefix in (("BANK", ""), ("MOBILE", "Mobile ")):
        for bank, ls in group_by(run.lines, mode).items():
            ws = wb.create_sheet((prefix + bank)[:31].replace("/", "-").replace("\\", "-") or "Sheet")
            ws.append([f"{run.company.name} - salary {period} - {bank}"])
            ws["A1"].font = Font(bold=True, size=12)
            if mode == "BANK":
                head = ["SL", "Employee ID", "Account name", "Account no.", "Amount (Tk)", "Narration"]
                rows = [[i, l.employee_code, l.full_name, l.account_no or "", float(bank_part(l)), f"Salary {period}"]
                        for i, l in enumerate(ls, 1)]
            else:
                head = ["SL", "Wallet number", "Amount (Tk)", "Name", "Employee ID", "Reference"]
                rows = [[i, l.account_no or "", float(bank_part(l)), l.full_name, l.employee_code, f"Salary {period}"]
                        for i, l in enumerate(ls, 1)]
            ws.append(head)
            for c in ws[2]:
                c.font, c.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="E8332F")
            for r in rows:
                ws.append(r)
            amt_col = 5 if mode == "BANK" else 3
            ws.append([""] * (amt_col - 2) + ["Total", float(sum(bank_part(l) for l in ls))])
            ws.cell(ws.max_row, amt_col).font = Font(bold=True)
            for r in range(3, ws.max_row + 1):
                ws.cell(r, amt_col).number_format = "#,##0.00"
                ws.cell(r, 4 if mode == "BANK" else 2).number_format = "@"     # keep leading zeros
            for col, w in zip("ABCDEF", (6, 16, 30, 22, 16, 24)):
                ws.column_dimensions[col].width = w
    if not wb.sheetnames:
        wb.create_sheet("No bank or mobile pay").append(["Nobody in this payroll is paid by bank or mobile banking."])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def payment_pdf(run, bank: dict, letter_date: date | None = None) -> bytes:
    """Bank advice letter for every bank (to sign and send), then the mobile banking list and the cash
    payment sheet with a signature column."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    from config import settings
    from services.branding import BRAND, PRODUCT_LOGO, logo_path

    letter_date = letter_date or date.today()
    period = f"{month_name[run.month]} {run.year}"
    W, H = A4
    body = ParagraphStyle("b", fontName="Helvetica", fontSize=10, leading=14)
    h = ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=12, leading=16, spaceAfter=6)
    story = []

    def table(head, rows, widths, money_col, total):
        data = [head] + rows + [[""] * (money_col - 1) + ["Total", f"{total:,.2f}"] + [""] * (len(head) - money_col - 1)]
        t = Table(data, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([
            ("FONT", (0, 0), (-1, -1), "Helvetica", 8.8), ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8.8),
            ("FONT", (0, -1), (-1, -1), "Helvetica-Bold", 9), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8332F")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#b8bec8")),
            ("ALIGN", (money_col, 0), (money_col, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#f7f8fa")])]))
        return t

    banks = group_by(run.lines, "BANK")
    own = (bank.get("bank_name") or "").strip()
    if own and banks:      # one letter to the company's own bank; other banks are paid by BEFTN through it
        letters = [(own, [l for ls in banks.values() for l in ls], True)]
    else:                  # company bank not set: one letter per employee bank
        letters = [(name, ls, False) for name, ls in banks.items()]
    for n, (name, ls, with_bank_col) in enumerate(letters, 1):
        total = sum((bank_part(l) for l in ls), Decimal(0))
        story.append(Paragraph(f"Ref: {run.company.code}/ACC/SAL/{run.year}{run.month:02d}/{n:02d}"
                               f" &nbsp;&nbsp;&nbsp; Date: {letter_date:%d %B %Y}", body))
        story.append(Spacer(1, 10))
        story.append(Paragraph(f"To<br/>The Manager<br/><b>{name}</b><br/>{bank.get('branch') or ''}", body))
        story.append(Spacer(1, 10))
        story.append(Paragraph(f"<u><b>Subject: Request to transfer salary of {period} - Tk {total:,.2f}</b></u>", body))
        story.append(Spacer(1, 8))
        acct = bank.get("account_no") or "____________________"
        acct_name = bank.get("account_name") or run.company.name
        others = sorted({(l.bank_name or "").strip() for l in ls} - {own}) if with_bank_col else []
        story.append(Paragraph(
            f"Dear Sir / Madam,<br/>Please debit our account <b>{acct_name}</b>, A/C no. <b>{acct}</b>, "
            f"by <b>Tk {total:,.2f}</b> ({taka_words(total)}) and credit the salary for {period} to the accounts "
            f"of our {len(ls)} employee(s) listed below."
            + (" Accounts with other banks (" + ", ".join(o or "bank not set" for o in others) +
               ") may please be credited through BEFTN." if others else ""), body))
        story.append(Spacer(1, 8))
        if with_bank_col:
            rows = [[i, l.employee_code, l.full_name[:28], (l.bank_name or "")[:22], l.account_no or "",
                     f"{bank_part(l):,.2f}"] for i, l in enumerate(ls, 1)]
            story.append(table(["SL", "Emp ID", "Account name", "Bank", "Account no.", "Amount (Tk)"], rows,
                               [10 * mm, 21 * mm, 50 * mm, 37 * mm, 34 * mm, 28 * mm], 5, total))
        else:
            rows = [[i, l.employee_code, l.full_name[:34], l.account_no or "", f"{bank_part(l):,.2f}"]
                    for i, l in enumerate(ls, 1)]
            story.append(table(["SL", "Emp ID", "Account name", "Account no.", "Amount (Tk)"], rows,
                               [12 * mm, 24 * mm, 62 * mm, 45 * mm, 32 * mm], 4, total))
        story.append(Spacer(1, 30))
        sig = [[f"_____________________\n{bank.get('signatory1') or 'Authorised signatory'}",
                f"_____________________\n{bank.get('signatory2') or 'Authorised signatory'}"]]
        story.append(Table(sig, colWidths=[90 * mm, 85 * mm], style=[("FONT", (0, 0), (-1, -1), "Helvetica", 9)]))
        story.append(PageBreak())

    for mode, title, cols in (("MOBILE", "Mobile banking payment list", ["SL", "Emp ID", "Name", "Wallet",
                                                                        "Number", "Amount (Tk)"]),
                              ("CASH", "Cash salary payment sheet", ["SL", "Emp ID", "Name", "Department",
                                                                     "Amount (Tk)", "Signature"])):
        ls = [l for g in group_by(run.lines, mode).values() for l in g] if mode == "MOBILE" else \
            sorted((l for l in run.lines if cash_part(l) > 0),
                   key=lambda l: (l.department, l.employee_code))
        if not ls:
            continue
        amt = bank_part if mode == "MOBILE" else cash_part
        total = sum((amt(l) for l in ls), Decimal(0))
        story.append(Paragraph(f"{title} - {period}", h))
        story.append(Paragraph(f"{len(ls)} employee(s), total Tk {total:,.2f} ({taka_words(total)})", body))
        story.append(Spacer(1, 8))
        if mode == "MOBILE":
            rows = [[i, l.employee_code, l.full_name[:30], (l.bank_name or "")[:14], l.account_no or "",
                     f"{bank_part(l):,.2f}"] for i, l in enumerate(ls, 1)]
            story.append(table(cols, rows, [11 * mm, 22 * mm, 55 * mm, 25 * mm, 32 * mm, 30 * mm], 5, total))
        else:
            rows = [[i, l.employee_code, l.full_name[:30] + (" (part)" if bank_part(l) > 0 else ""),
                     (l.department or "")[:20], f"{cash_part(l):,.2f}", ""] for i, l in enumerate(ls, 1)]
            t = table(cols, rows, [11 * mm, 22 * mm, 55 * mm, 35 * mm, 27 * mm, 30 * mm], 4, total)
            t.setStyle(TableStyle([("BOTTOMPADDING", (0, 1), (-1, -2), 9), ("TOPPADDING", (0, 1), (-1, -2), 9)]))
            story.append(t)
        story.append(Spacer(1, 26))
        story.append(Table([["_____________________\nPrepared by", "_____________________\nChecked by",
                             "_____________________\nApproved by"]], colWidths=[60 * mm] * 3,
                           style=[("FONT", (0, 0), (-1, -1), "Helvetica", 9)]))
        story.append(PageBreak())

    if not story:
        story.append(Paragraph("Nobody in this payroll has a net pay to disburse.", body))
    elif isinstance(story[-1], PageBreak):
        story.pop()

    draft = run.status != "FINAL"

    def decorate(canv, doc):
        canv.saveState()
        try:
            canv.drawImage(str(logo_path()), 15 * mm, H - 24 * mm, 14 * mm, 14 * mm, mask="auto", preserveAspectRatio=True)
        except Exception:  # noqa: BLE001
            pass
        canv.setFillColor(colors.HexColor("#E8332F"))
        canv.setFont("Helvetica-Bold", 14)
        canv.drawString(32 * mm, H - 16 * mm, run.company.name[:60])
        canv.setFillColor(colors.HexColor("#555555"))
        canv.setFont("Helvetica", 8.5)
        canv.drawString(32 * mm, H - 21 * mm, (run.company.address or BRAND.get("address") or "")[:100])
        canv.setStrokeColor(colors.HexColor("#E8332F"))
        canv.line(15 * mm, H - 27 * mm, W - 15 * mm, H - 27 * mm)
        if draft:
            canv.setFont("Helvetica-Bold", 9)
            canv.setFillColor(colors.HexColor("#b3261e"))
            canv.drawRightString(W - 15 * mm, H - 16 * mm, "DRAFT - payroll not final")
        canv.setFont("Helvetica", 7)
        canv.setFillColor(colors.HexColor("#888888"))
        try:
            canv.drawImage(str(PRODUCT_LOGO), 15 * mm, 6 * mm, 11 * mm, 7 * mm, mask="auto", preserveAspectRatio=True)
        except Exception:  # noqa: BLE001
            pass
        canv.drawString(27 * mm, 8.5 * mm, f"SPhere Payroll · {settings.developer_credit}")
        canv.drawRightString(W - 15 * mm, 8.5 * mm, f"Salary {period} · page {doc.page}")
        canv.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=33 * mm,
                            bottomMargin=17 * mm, title=f"Salary payment {run.company.code} {period}")
    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
    return buf.getvalue()
