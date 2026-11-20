"""Report engine: every list in the program is a Report with selectable columns.
The same column choice is used on screen, in Excel and in PDF. Each user's choice is remembered."""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Callable

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from services.branding import BRAND, logo_path


@dataclass
class Col:
    key: str
    label: str
    get: Callable[[Any], Any]
    default: bool = True
    kind: str = "text"            # text / num / money / date / time / datetime / hours
    perm: str | None = None       # column only for users with this permission (e.g. salary.view)
    link: Callable[[Any], str] | None = None

    def value(self, row):
        try:
            return self.get(row)
        except (AttributeError, TypeError, KeyError):
            return None

    def text(self, row) -> str:
        return fmt(self.value(row), self.kind)


@dataclass
class Report:
    id: str
    title: str
    perm: str
    cols: list[Col]
    fetch: Callable[..., tuple[list, str]]      # (db, user, params, export) -> (rows, subtitle)
    landscape: bool = True
    totals: list[str] = field(default_factory=list)

    def allowed_cols(self, user: dict) -> list[Col]:
        return [c for c in self.cols if not c.perm or c.perm in user["perms"]]


def fmt(v, kind: str = "text") -> str:
    if v is None or v == "":
        return ""
    if kind == "hours":
        return f"{float(v):.2f}" if v else ""
    if kind == "money" or isinstance(v, Decimal):
        return f"{float(v):,.2f}" if kind == "money" else f"{float(v):,.2f}".rstrip("0").rstrip(".")
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, datetime):
        return v.strftime("%H:%M:%S") if kind == "time" else v.strftime("%d/%m/%Y %H:%M")
    if isinstance(v, date):
        return v.strftime("%d/%m/%Y")
    if isinstance(v, time):
        return v.strftime("%H:%M")
    if isinstance(v, float):
        return f"{v:,.2f}"
    return str(v)


# ------------------------------------------------------------ column choice per user
def chosen_cols(db, user: dict, report: Report, override: str | None = None) -> list[Col]:
    from services.policy import get_setting
    allowed = report.allowed_cols(user)
    keys = None
    if override:
        keys = override.split(",")
    else:
        saved = get_setting(db, f"cols:{user['username']}:{report.id}", default="")
        if saved:
            keys = saved.split(",")
    if keys:
        by = {c.key: c for c in allowed}
        cols = [by[k] for k in keys if k in by]
        if cols:
            return cols
    return [c for c in allowed if c.default]


def save_cols(db, user: dict, report: Report, keys: list[str]) -> None:
    from services.policy import set_setting, delete_setting
    allowed = {c.key for c in report.allowed_cols(user)}
    keys = [k for k in keys if k in allowed]
    if keys:
        set_setting(db, f"cols:{user['username']}:{report.id}", ",".join(keys))
    else:
        delete_setting(db, f"cols:{user['username']}:{report.id}", 0)
        db.commit()


# ------------------------------------------------------------ Excel
HEAD_FILL = PatternFill("solid", fgColor="E8332F")
THIN = Side(style="thin", color="BBBBBB")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
NUMFMT = {"money": "#,##0.00", "num": "#,##0.##", "hours": "0.00", "date": "dd/mm/yyyy", "datetime": "dd/mm/yyyy hh:mm",
          "time": "hh:mm:ss"}


def _xl_value(v, kind):
    if isinstance(v, Decimal):
        return float(v)
    if kind == "time" and isinstance(v, datetime):
        return v.time()
    if isinstance(v, bool):
        return "yes" if v else "no"
    return v


def to_excel(report: Report, cols: list[Col], rows: list, subtitle: str = "") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = report.title[:30]
    try:
        img = XLImage(str(logo_path()))
        img.width, img.height = 54, 54 * img.height / max(img.width, 1)
        ws.add_image(img, "A1")
    except Exception:  # noqa: BLE001 - a broken logo must not stop the export
        pass
    ws.row_dimensions[1].height = 22
    ws.row_dimensions[2].height = 18
    ws["B1"] = BRAND["name"]
    ws["B1"].font = Font(bold=True, size=15, color="E8332F")
    ws["B2"] = report.title + (f" - {subtitle}" if subtitle else "")
    ws["B2"].font = Font(bold=True, size=11)
    ws["B3"] = f"Printed {datetime.now():%d/%m/%Y %H:%M} · {len(rows)} rows"
    ws["B3"].font = Font(size=9, color="777777")
    hr = 5
    for i, c in enumerate(cols, start=1):
        cell = ws.cell(hr, i, c.label)
        cell.fill, cell.font, cell.border = HEAD_FILL, Font(bold=True, color="FFFFFF"), BOX
        cell.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
    ws.row_dimensions[hr].height = 30
    for r_i, row in enumerate(rows, start=hr + 1):
        for c_i, c in enumerate(cols, start=1):
            cell = ws.cell(r_i, c_i, _xl_value(c.value(row), c.kind))
            cell.border = BOX
            if c.kind in NUMFMT:
                cell.number_format = NUMFMT[c.kind]
    if report.totals and rows:
        tr = hr + len(rows) + 1
        ws.cell(tr, 1, "TOTAL").font = Font(bold=True)
        for c_i, c in enumerate(cols, start=1):
            if c.key in report.totals:
                L = get_column_letter(c_i)
                cell = ws.cell(tr, c_i, f"=SUM({L}{hr + 1}:{L}{tr - 1})")
                cell.font, cell.number_format = Font(bold=True), NUMFMT.get(c.kind, "#,##0.00")
    for c_i, c in enumerate(cols, start=1):
        width = max([len(c.label)] + [len(c.text(r)) for r in rows[:300]]) + 2
        ws.column_dimensions[get_column_letter(c_i)].width = min(max(width, 8), 45)
    ws.freeze_panes = ws.cell(hr + 1, 1)
    ws.auto_filter.ref = f"A{hr}:{get_column_letter(len(cols))}{hr + max(len(rows), 1)}"
    ws.print_title_rows = f"{hr}:{hr}"
    ws.page_setup.orientation = "landscape" if report.landscape else "portrait"
    ws.page_setup.fitToWidth = 1
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------ PDF
def to_pdf(report: Report, cols: list[Col], rows: list, subtitle: str = "") -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

    from config import settings
    wide = report.landscape or len(cols) > 7
    page = landscape(A4) if wide else A4
    width = page[0] - 20 * mm
    n = max(len(cols), 1)
    size = 8.5 if n <= 8 else 7.5 if n <= 12 else 6.5 if n <= 16 else 5.5
    cell = ParagraphStyle("c", fontName="Helvetica", fontSize=size, leading=size * 1.2)
    head = ParagraphStyle("h", parent=cell, fontName="Helvetica-Bold", textColor=colors.white)
    right = ParagraphStyle("r", parent=cell, alignment=2)

    def esc(t: str) -> str:
        return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    data = [[Paragraph(esc(c.label), head) for c in cols]]
    for row in rows:
        data.append([Paragraph(esc(c.text(row)), right if c.kind in ("money", "num", "hours") else cell) for c in cols])
    if report.totals and rows:
        tot = []
        for i, c in enumerate(cols):
            if c.key in report.totals:
                s = sum(float(c.value(r) or 0) for r in rows)
                tot.append(Paragraph(f"<b>{fmt(s, 'money')}</b>", right))
            else:
                tot.append(Paragraph("<b>TOTAL</b>" if i == 0 else "", cell))
        data.append(tot)
    # column widths: proportional to content length, within limits
    lens = [max([len(c.label)] + [len(c.text(r)) for r in rows[:200]]) for c in cols]
    lens = [min(max(x, 4), 40) for x in lens]
    widths = [width * x / sum(lens) for x in lens]
    t = Table(data, colWidths=widths, repeatRows=1)
    style = [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8332F")),
             ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#BBBBBB")),
             ("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F6F7F9")]),
             ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]
    t.setStyle(TableStyle(style))
    printed = datetime.now().strftime("%d/%m/%Y %H:%M")

    def decorate(canv, doc):
        canv.saveState()
        W, H = page
        try:
            canv.drawImage(str(logo_path()), 10 * mm, H - 21 * mm, 14 * mm, 14 * mm, mask="auto",
                           preserveAspectRatio=True)
        except Exception:  # noqa: BLE001
            pass
        canv.setFillColor(colors.HexColor("#E8332F"))
        canv.setFont("Helvetica-Bold", 14)
        canv.drawString(27 * mm, H - 13 * mm, BRAND["name"])
        canv.setFillColor(colors.black)
        canv.setFont("Helvetica-Bold", 10)
        canv.drawString(27 * mm, H - 18.5 * mm, (report.title + (f" - {subtitle}" if subtitle else ""))[:140])
        canv.setFont("Helvetica", 7.5)
        canv.setFillColor(colors.HexColor("#666666"))
        if BRAND.get("address"):
            canv.drawRightString(W - 10 * mm, H - 13 * mm, BRAND["address"][:90])
        canv.drawRightString(W - 10 * mm, H - 18.5 * mm, f"Printed {printed} · {len(rows)} rows")
        canv.drawString(10 * mm, 7 * mm, settings.developer_credit)
        canv.drawRightString(W - 10 * mm, 7 * mm, f"Page {doc.page}")
        canv.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=10 * mm, rightMargin=10 * mm, topMargin=25 * mm,
                            bottomMargin=13 * mm, title=report.title, author=BRAND["name"])
    doc.build([t], onFirstPage=decorate, onLaterPages=decorate)
    return buf.getvalue()
