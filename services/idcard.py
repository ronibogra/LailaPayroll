"""Staff ID cards (CR80 size 54 x 85.6 mm, portrait) as PDF.

Layouts: 'a4' = 9 cards per A4 sheet with cut marks (back pages mirrored for double-sided printing);
'card' = one card per page in card size, for PVC card printers (Evolis, Zebra, Fargo, Magicard ...)."""
from __future__ import annotations

import io
from datetime import date

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode import qr
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

CW, CH = 54 * mm, 85.6 * mm
RED, GREEN, DARK = colors.HexColor("#E8332F"), colors.HexColor("#00a651"), colors.HexColor("#1f2733")


def _fit(c, text, font, size, max_w, min_size=5.5):
    while size > min_size and stringWidth(text, font, size) > max_w:
        size -= 0.25
    c.setFont(font, size)
    return size


def _wrap(text, font, size, max_w, lines=2):
    words, out, cur = (text or "").split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if stringWidth(t, font, size) <= max_w or not cur:
            cur = t
        else:
            out.append(cur)
            cur = w
    if cur:
        out.append(cur)
    if len(out) > lines:
        out = out[:lines]
        out[-1] = out[-1].rstrip(".") + "…"
    return out


def _photo(path):
    """Crop to 4:5 portrait so every face fills the box the same way."""
    if not path:
        return None
    try:
        from PIL import Image
        im = Image.open(path).convert("RGB")
        w, h = im.size
        tw = min(w, int(h * 0.8))
        th = int(tw / 0.8)
        left, top = (w - tw) // 2, max(0, (h - th) // 3)
        im = im.crop((left, top, left + tw, top + th)).resize((360, 450))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=90)
        buf.seek(0)
        return ImageReader(buf)
    except Exception:  # noqa: BLE001 - a broken photo must not stop the whole print
        return None


def _qr(c, text, x, y, size):
    w = qr.QrCodeWidget(text, barLevel="M")
    b = w.getBounds()
    d = Drawing(size, size, transform=[size / (b[2] - b[0]), 0, 0, size / (b[3] - b[1]), 0, 0])
    d.add(w)
    renderPDF.draw(d, c, x, y)


def front(c, e, x, y, opt):
    c.saveState()
    c.translate(x, y)
    p = c.beginPath()
    p.roundRect(0, 0, CW, CH, 3 * mm)
    c.clipPath(p, stroke=0)
    c.setFillColor(colors.white)
    c.rect(0, 0, CW, CH, stroke=0, fill=1)
    # header band
    c.setFillColor(RED)
    c.rect(0, CH - 18 * mm, CW, 18 * mm, stroke=0, fill=1)
    c.setFillColor(GREEN)
    c.rect(0, CH - 19.2 * mm, CW, 1.2 * mm, stroke=0, fill=1)
    try:
        c.setFillColor(colors.white)
        c.circle(8.5 * mm, CH - 9 * mm, 6 * mm, stroke=0, fill=1)
        c.drawImage(opt["logo"], 3.5 * mm, CH - 14 * mm, 10 * mm, 10 * mm, mask="auto", preserveAspectRatio=True)
    except Exception:  # noqa: BLE001
        pass
    c.setFillColor(colors.white)
    comp = e.company.name if e.company else opt["group"]
    lines = _wrap(comp.upper(), "Helvetica-Bold", 7.6, CW - 17.5 * mm)
    ty = CH - 8 * mm if len(lines) == 1 else CH - 6.6 * mm
    for i, ln in enumerate(lines):
        _fit(c, ln, "Helvetica-Bold", 7.6, CW - 17.5 * mm)
        c.drawString(16 * mm, ty - i * 3.3 * mm, ln)
    c.setFont("Helvetica", 5.6)
    if opt["group"].lower() not in comp.lower():
        c.drawString(16 * mm, CH - 15.6 * mm, f"A concern of {opt['group']}"[:48])
    # photo
    pw, ph = 24 * mm, 30 * mm
    px, py = (CW - pw) / 2, CH - 21.5 * mm - ph
    c.setFillColor(colors.HexColor("#eef0f3"))
    c.setStrokeColor(GREEN)
    c.setLineWidth(1.2)
    img = _photo(opt["photo"](e))
    if img:
        c.drawImage(img, px, py, pw, ph)
    else:
        c.rect(px, py, pw, ph, stroke=0, fill=1)
        c.setFillColor(colors.HexColor("#9aa3af"))
        c.setFont("Helvetica", 7)
        c.drawCentredString(CW / 2, py + ph / 2, "PHOTO")
    c.roundRect(px, py, pw, ph, 1.2 * mm, stroke=1, fill=0)
    # name, designation, department
    yy = py - 5 * mm
    c.setFillColor(DARK)
    for ln in _wrap(e.full_name, "Helvetica-Bold", 9.5, CW - 6 * mm):
        _fit(c, ln, "Helvetica-Bold", 9.5, CW - 6 * mm, 7)
        c.drawCentredString(CW / 2, yy, ln)
        yy -= 3.9 * mm
    c.setFillColor(RED)
    _fit(c, (e.designation or "").upper(), "Helvetica-Bold", 7, CW - 6 * mm)
    c.drawCentredString(CW / 2, yy + 0.4 * mm, (e.designation or "").upper())
    yy -= 3.3 * mm
    c.setFillColor(colors.HexColor("#4a5361"))
    dept = e.department.name if e.department else ""
    _fit(c, dept, "Helvetica", 6.8, CW - 6 * mm)
    c.drawCentredString(CW / 2, yy + 0.6 * mm, dept)
    # ID + blood group
    by = 7.5 * mm
    c.setFillColor(colors.HexColor("#f3f4f6"))
    c.roundRect(3 * mm, by, CW - 6 * mm, 8 * mm, 1.5 * mm, stroke=0, fill=1)
    c.setFillColor(colors.HexColor("#6b7585"))
    c.setFont("Helvetica", 5.5)
    c.drawString(5 * mm, by + 5.4 * mm, "ID NO.")
    c.setFillColor(DARK)
    _fit(c, e.employee_code, "Helvetica-Bold", 10.5, 28 * mm)
    c.drawString(5 * mm, by + 1.6 * mm, e.employee_code)
    if e.blood_group:
        c.setFillColor(RED)
        c.circle(CW - 9 * mm, by + 4 * mm, 3.6 * mm, stroke=0, fill=1)
        c.setFillColor(colors.white)
        _fit(c, e.blood_group, "Helvetica-Bold", 7.5, 6.4 * mm)
        c.drawCentredString(CW - 9 * mm, by + 2.9 * mm, e.blood_group)
        c.setFillColor(colors.HexColor("#6b7585"))
        c.setFont("Helvetica", 4.6)
        c.drawRightString(CW - 13.4 * mm, by + 5.4 * mm, "BLOOD")
    # footer strip
    c.setFillColor(GREEN)
    c.rect(0, 0, CW, 5 * mm, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 6)
    c.drawCentredString(CW / 2, 1.8 * mm, "EMPLOYEE IDENTITY CARD")
    c.restoreState()
    _border(c, x, y)


def back(c, e, x, y, opt):
    c.saveState()
    c.translate(x, y)
    p = c.beginPath()
    p.roundRect(0, 0, CW, CH, 3 * mm)
    c.clipPath(p, stroke=0)
    c.setFillColor(colors.white)
    c.rect(0, 0, CW, CH, stroke=0, fill=1)
    c.setFillColor(GREEN)
    c.rect(0, CH - 6 * mm, CW, 6 * mm, stroke=0, fill=1)
    c.setFillColor(colors.white)
    _fit(c, (e.company.name if e.company else opt["group"]).upper(), "Helvetica-Bold", 6.6, CW - 6 * mm)
    c.drawCentredString(CW / 2, CH - 4 * mm, (e.company.name if e.company else opt["group"]).upper())
    rows = [("Joined", e.joined_on.strftime("%d %b %Y") if e.joined_on else "-"),
            ("Blood group", e.blood_group or "-"), ("Mobile", e.phone or "-"),
            ("Emergency", e.emergency_contact or "-"), ("NID", e.nid or "-") if opt.get("show_nid") else None,
            ("Issued", opt["issued"].strftime("%d %b %Y")),
            ("Valid until", opt["valid"].strftime("%d %b %Y") if opt.get("valid") else "while in service")]
    yy = CH - 10.5 * mm
    for r in [r for r in rows if r]:
        c.setFillColor(colors.HexColor("#6b7585"))
        c.setFont("Helvetica", 5.8)
        c.drawString(3.5 * mm, yy, r[0])
        c.setFillColor(DARK)
        lines = _wrap(str(r[1]), "Helvetica-Bold", 6.2, CW - 22 * mm, 2)
        for i, ln in enumerate(lines):
            _fit(c, ln, "Helvetica-Bold", 6.2, CW - 22 * mm)
            c.drawString(18.5 * mm, yy - i * 2.7 * mm, ln)
        yy -= 3.6 * mm + 2.7 * mm * (len(lines) - 1)
    # QR (employee code) for quick lookup
    q = 17 * mm
    _qr(c, f"{opt['group']}|{e.company.code if e.company else ''}|{e.employee_code}|{e.full_name}", (CW - q) / 2,
        yy - q - 0.5 * mm, q)
    yy -= q + 3.5 * mm
    c.setStrokeColor(colors.HexColor("#9aa3af"))
    c.setLineWidth(0.4)
    c.line(14 * mm, yy, CW - 14 * mm, yy)
    c.setFillColor(colors.HexColor("#4a5361"))
    c.setFont("Helvetica", 5.6)
    c.drawCentredString(CW / 2, yy - 2.6 * mm, opt.get("signatory") or "Authorised signature")
    # return notice
    c.setFillColor(colors.HexColor("#f3f4f6"))
    c.rect(0, 0, CW, 13.5 * mm, stroke=0, fill=1)
    c.setFillColor(DARK)
    c.setFont("Helvetica", 5.2)
    note = ["This card is the property of the company and must be", "shown on request. If found, please return to:"]
    for i, ln in enumerate(note):
        c.drawCentredString(CW / 2, 10.6 * mm - i * 2.3 * mm, ln)
    c.setFont("Helvetica-Bold", 5.4)
    addr = _wrap(opt["address"] or opt["group"], "Helvetica-Bold", 5.4, CW - 5 * mm, 2)
    for i, ln in enumerate(addr):
        c.drawCentredString(CW / 2, 5.6 * mm - i * 2.3 * mm, ln)
    if opt.get("phone"):
        c.setFont("Helvetica", 5.2)
        c.drawCentredString(CW / 2, 1.2 * mm, f"Phone: {opt['phone']}"[:60])
    c.restoreState()
    _border(c, x, y)


def _border(c, x, y):
    c.saveState()
    c.setStrokeColor(colors.HexColor("#c9ced6"))
    c.setLineWidth(0.3)
    c.roundRect(x, y, CW, CH, 3 * mm, stroke=1, fill=0)
    c.restoreState()


def _cut_marks(c, x, y):
    c.saveState()
    c.setStrokeColor(colors.HexColor("#999999"))
    c.setLineWidth(0.25)
    m = 2.5 * mm
    for cx in (x, x + CW):
        for cy in (y, y + CH):
            c.line(cx - m if cx == x else cx + 1 * mm, cy, cx - 1 * mm if cx == x else cx + m, cy)
            c.line(cx, cy - m if cy == y else cy + 1 * mm, cx, cy - 1 * mm if cy == y else cy + m)
    c.restoreState()


def cards_pdf(employees, opt: dict) -> bytes:
    buf = io.BytesIO()
    layout, sides = opt.get("layout", "a4"), opt.get("sides", "both")
    opt.setdefault("issued", date.today())
    if layout == "card":
        c = canvas.Canvas(buf, pagesize=(CW, CH))
        c.setTitle("Staff ID cards")
        for e in employees:
            if sides in ("both", "front"):
                front(c, e, 0, 0, opt)
                c.showPage()
            if sides in ("both", "back"):
                back(c, e, 0, 0, opt)
                c.showPage()
    else:
        W, H = A4
        cols, rows_ = 3, 3
        gx, gy = 6 * mm, 6 * mm
        ox = (W - cols * CW - (cols - 1) * gx) / 2
        oy = (H - rows_ * CH - (rows_ - 1) * gy) / 2
        c = canvas.Canvas(buf, pagesize=A4)
        c.setTitle("Staff ID cards")
        per = cols * rows_
        for start in range(0, len(employees), per):
            chunk = employees[start:start + per]
            for side in (("front", "back") if sides == "both" else (sides,)):
                for i, e in enumerate(chunk):
                    r, col = divmod(i, cols)
                    if side == "back":
                        col = cols - 1 - col          # mirrored so it lines up when printed on both sides
                    x = ox + col * (CW + gx)
                    y = H - oy - (r + 1) * CH - r * gy
                    (front if side == "front" else back)(c, e, x, y, opt)
                    _cut_marks(c, x, y)
                c.setFont("Helvetica", 7)
                c.setFillColor(colors.HexColor("#888888"))
                c.drawString(ox, 8 * mm, f"Staff ID cards - {side.upper()} side - cut along the marks"
                             + (" - print the next page on the back of this sheet (flip on long edge)"
                                if side == "front" and sides == "both" else ""))
                c.drawString(ox, 4.5 * mm, f"SPhere Payroll · {opt.get('credit', '')}")
                c.showPage()
    c.save()
    return buf.getvalue()
