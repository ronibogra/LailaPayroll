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


DEFAULT_DESIGN = {
    "style": "LAILA",                  # LAILA = Laila Group card (swoosh corners); LABELS; CLASSIC
    "header_color": "#E8332F", "accent_color": "#00a651", "text_color": "#1f2733",
    "title": "EMPLOYEE IDENTITY CARD",
    "front_fields": ["full_name", "employee_code", "designation", "department", "joined_on", "blood_group"],
    "back_fields": ["joined_on", "blood_group", "phone", "emergency_contact", "issued", "valid"],
    "labels": {},
    "show_photo": True, "show_holder_sign": True, "show_auth_sign": True,
    "holder_sign_label": "Card holder Signature", "auth_sign_label": "Authorized Signature",
    "show_qr": True, "show_address": True,
    "back_note": "This card is the property of the company and must be shown on request. If found, please return to:",
}
FIELDS = {"full_name": "Name", "employee_code": "ID No.", "card_no": "Card No.", "designation": "Designation",
          "department": "Department", "section": "Section", "grade": "Grade", "company": "Company",
          "joined_on": "DOJ", "blood_group": "Blood Group", "phone": "Mobile", "emergency_contact": "Emergency",
          "nid": "NID", "date_of_birth": "Date of Birth", "father_name": "Father", "present_address": "Address",
          "issued": "Issued", "valid": "Valid until"}


def design_of(opt) -> dict:
    d = dict(DEFAULT_DESIGN)
    d.update({k: v for k, v in (opt.get("design") or {}).items() if v is not None})
    d["labels"] = {**FIELDS, **(d.get("labels") or {})}
    return d


def _colors(opt):
    d = design_of(opt)
    try:
        return (colors.HexColor(d["header_color"]), colors.HexColor(d["accent_color"]), colors.HexColor(d["text_color"]))
    except Exception:  # noqa: BLE001 - a typed colour that is not valid falls back to the defaults
        return colors.HexColor("#E8332F"), colors.HexColor("#00a651"), colors.HexColor("#1f2733")


def value_of(e, key, opt) -> str:
    if key == "department":
        return e.department.name if e.department else ""
    if key == "company":
        return e.company.name if e.company else ""
    if key in ("joined_on", "date_of_birth"):
        v = getattr(e, key, None)
        return v.strftime("%d-%m-%Y") if v else ""
    if key == "issued":
        return opt["issued"].strftime("%d-%m-%Y")
    if key == "valid":
        return opt["valid"].strftime("%d-%m-%Y") if opt.get("valid") else "While in service"
    return str(getattr(e, key, "") or "")


def _header(c, e, opt, RED, GREEN, h=15 * mm):
    c.setFillColor(RED)
    c.rect(0, CH - h, CW, h, stroke=0, fill=1)
    c.setFillColor(GREEN)
    c.rect(0, CH - h - 1.1 * mm, CW, 1.1 * mm, stroke=0, fill=1)
    try:
        c.setFillColor(colors.white)
        c.circle(7.8 * mm, CH - h / 2, 5.4 * mm, stroke=0, fill=1)
        c.drawImage(opt["logo"], 3.3 * mm, CH - h / 2 - 4.5 * mm, 9 * mm, 9 * mm, mask="auto", preserveAspectRatio=True)
    except Exception:  # noqa: BLE001
        pass
    c.setFillColor(colors.white)
    comp = e.company.name if e.company else opt["group"]
    lines = _wrap(comp.upper(), "Helvetica-Bold", 7.4, CW - 16.5 * mm)
    sub = opt["group"].lower() not in comp.lower()
    ty = CH - h / 2 + (1.2 * mm if len(lines) == 1 else 2.6 * mm) + (1 * mm if sub else -1 * mm)
    for i, ln in enumerate(lines):
        _fit(c, ln, "Helvetica-Bold", 7.4, CW - 16.5 * mm)
        c.drawString(14.8 * mm, ty - i * 3.2 * mm, ln)
    if sub:
        c.setFont("Helvetica", 5.3)
        c.drawString(14.8 * mm, ty - len(lines) * 3.2 * mm + 0.4 * mm, f"A concern of {opt['group']}"[:48])


def _sign_img(c, path, x, y, w, h):
    if not path:
        return
    try:
        c.drawImage(str(path), x, y, w, h, mask="auto", preserveAspectRatio=True, anchor="s")
    except Exception:  # noqa: BLE001
        pass


def front_labels(c, e, x, y, opt):
    """Head Office sample: photo, then Name / ID No. / Designation / Department / DOJ / Blood Group rows,
    card holder signature (from the profile) and authorized signature."""
    RED, GREEN, DARK = _colors(opt)
    d = design_of(opt)
    c.saveState()
    c.translate(x, y)
    p = c.beginPath()
    p.roundRect(0, 0, CW, CH, 3 * mm)
    c.clipPath(p, stroke=0)
    c.setFillColor(colors.white)
    c.rect(0, 0, CW, CH, stroke=0, fill=1)
    _header(c, e, opt, RED, GREEN)
    top = CH - 18 * mm
    if d["show_photo"]:
        pw, ph = 20 * mm, 25 * mm
        px, py = (CW - pw) / 2, top - ph
        img = _photo(opt["photo"](e))
        if img:
            c.drawImage(img, px, py, pw, ph)
        else:
            c.setFillColor(colors.HexColor("#eef0f3"))
            c.rect(px, py, pw, ph, stroke=0, fill=1)
            c.setFillColor(colors.HexColor("#9aa3af"))
            c.setFont("Helvetica", 6.5)
            c.drawCentredString(CW / 2, py + ph / 2, "PHOTO")
        c.setStrokeColor(GREEN)
        c.setLineWidth(1)
        c.rect(px, py, pw, ph, stroke=1, fill=0)
        top = py - 2.2 * mm
    signs = d["show_holder_sign"] or d["show_auth_sign"]
    bottom = (16.5 if signs else 7.5) * mm
    fields = [f for f in d["front_fields"] if f in FIELDS]
    lab_w = max([stringWidth(d["labels"][f], "Helvetica", 6.2) for f in fields] or [0]) + 1.2 * mm
    lab_w = min(lab_w, 19 * mm)
    val_x, val_w = 3.5 * mm + lab_w + 2 * mm, CW - (3.5 * mm + lab_w + 2 * mm) - 3 * mm
    rows = []
    for f in fields:
        v = value_of(e, f, opt)
        font = "Helvetica-Bold"
        rows.append((d["labels"][f], _wrap(v or "-", font, 6.6, val_w, 2)))
    n_lines = sum(len(r[1]) for r in rows) or 1
    step = min(3.7 * mm, max(2.6 * mm, (top - bottom) / n_lines))
    yy = top - step + 0.6 * mm
    for label, vals in rows:
        c.setFillColor(colors.HexColor("#4a5361"))
        c.setFont("Helvetica", 6.2)
        c.drawString(3.5 * mm, yy, label)
        c.drawString(3.5 * mm + lab_w, yy, ":")
        c.setFillColor(DARK)
        for i, v in enumerate(vals):
            _fit(c, v, "Helvetica-Bold", 6.6, val_w, 5)
            c.drawString(val_x, yy - i * step * 0.85, v)
        yy -= step * (1 + 0.85 * (len(vals) - 1))
    if signs:
        half = (CW - 7 * mm) / 2
        line_y = 9.3 * mm
        c.setStrokeColor(colors.HexColor("#6b7585"))
        c.setLineWidth(0.4)
        for i, (show, label, path) in enumerate([(d["show_holder_sign"], d["holder_sign_label"], opt["signature"](e)),
                                                 (d["show_auth_sign"], d["auth_sign_label"], opt.get("auth_sign"))]):
            if not show:
                continue
            sx = 3.5 * mm + i * (half + 0 * mm) + (0 if i == 0 else 0)
            _sign_img(c, path, sx + 1 * mm, line_y + 0.4 * mm, half - 2 * mm, 6 * mm)
            c.line(sx + 1 * mm, line_y, sx + half - 1 * mm, line_y)
            c.setFillColor(colors.HexColor("#4a5361"))
            _fit(c, label, "Helvetica", 5, half - 1 * mm, 4)
            c.drawCentredString(sx + half / 2, line_y - 2.3 * mm, label)
    c.setFillColor(GREEN)
    c.rect(0, 0, CW, 4.5 * mm, stroke=0, fill=1)
    c.setFillColor(colors.white)
    _fit(c, d["title"], "Helvetica-Bold", 6, CW - 4 * mm)
    c.drawCentredString(CW / 2, 1.6 * mm, d["title"])
    c.restoreState()
    _border(c, x, y)


def _swoosh(c, RED, GREEN):
    """Green and red curved bands in the top-left and bottom-right corners (Laila Group card)."""
    def band(color, pts):
        p = c.beginPath()
        p.moveTo(*pts[0])
        for a, b, d in zip(pts[1::3], pts[2::3], pts[3::3]):
            p.curveTo(a[0], a[1], b[0], b[1], d[0], d[1])
        p.close()
        c.setFillColor(color)
        c.drawPath(p, stroke=0, fill=1)
    W, H = CW, CH
    # top-left: wide green, thin red edge
    band(GREEN, [(0, H), (0, H - 30 * mm), (6 * mm, H - 14 * mm), (22 * mm, H - 4 * mm),
                 (30 * mm, H), (0, H), (0, H)])
    band(colors.white, [(0, H - 22 * mm), (4 * mm, H - 12 * mm), (14 * mm, H - 5 * mm), (24 * mm, H),
                        (0, H), (0, H), (0, H - 22 * mm)])
    band(RED, [(0, H - 19 * mm), (3 * mm, H - 11 * mm), (11 * mm, H - 5 * mm), (20 * mm, H),
               (0, H), (0, H), (0, H - 19 * mm)])
    band(GREEN, [(0, H - 14 * mm), (2 * mm, H - 8 * mm), (7 * mm, H - 3 * mm), (13 * mm, H),
                 (0, H), (0, H), (0, H - 14 * mm)])
    # bottom-right: mirror, red outside, green inside (kept small so it stays clear of the signature)
    band(RED, [(W, 0), (W, 17 * mm), (W - 3.5 * mm, 6 * mm), (W - 15 * mm, 0), (W, 0), (W, 0), (W, 0)])
    band(colors.white, [(W, 12 * mm), (W - 2 * mm, 5 * mm), (W - 6.5 * mm, 2 * mm), (W - 12 * mm, 0),
                        (W, 0), (W, 0), (W, 12 * mm)])
    band(GREEN, [(W, 10 * mm), (W - 1.6 * mm, 4.2 * mm), (W - 5 * mm, 1.6 * mm), (W - 9.5 * mm, 0),
                 (W, 0), (W, 0), (W, 10 * mm)])


def front_laila(c, e, x, y, opt):
    """Laila Group card: cream card, swoosh corners, logo + group name top-right, photo, bold rows,
    card holder and authorised signatures at the bottom."""
    RED, GREEN, DARK = _colors(opt)
    d = design_of(opt)
    c.saveState()
    c.translate(x, y)
    p = c.beginPath()
    p.roundRect(0, 0, CW, CH, 3 * mm)
    c.clipPath(p, stroke=0)
    c.setFillColor(colors.HexColor("#fbfaf4"))
    c.rect(0, 0, CW, CH, stroke=0, fill=1)
    _swoosh(c, RED, GREEN)
    # logo + names, top right
    lx, ly, ls = CW - 17.5 * mm, CH - 15 * mm, 12 * mm
    try:
        c.drawImage(opt["logo"], lx, ly, ls, ls, mask="auto", preserveAspectRatio=True)
    except Exception:  # noqa: BLE001
        pass
    c.setFillColor(DARK)
    c.setFont("Times-Bold", 6.6)
    gname = opt["group"].upper()
    _fit(c, gname, "Times-Bold", 6.6, 26 * mm)
    c.drawCentredString(lx + ls / 2, ly - 2.6 * mm, gname)
    comp = e.company.name if e.company else ""
    if comp and opt["group"].lower() not in comp.lower():
        c.setFillColor(RED)
        _fit(c, comp.upper(), "Helvetica-Bold", 5, 30 * mm)
        c.drawRightString(CW - 3 * mm, ly - 5.4 * mm, comp.upper())
    top = CH - 25 * mm
    if d["show_photo"]:
        pw, ph = 21 * mm, 24 * mm
        px, py = (CW - pw) / 2, top - ph
        img = _photo(opt["photo"](e))
        c.setFillColor(colors.white)
        c.rect(px - 0.8 * mm, py - 0.8 * mm, pw + 1.6 * mm, ph + 1.6 * mm, stroke=0, fill=1)
        if img:
            c.drawImage(img, px, py, pw, ph)
        else:
            c.setFillColor(colors.HexColor("#eef0f3"))
            c.rect(px, py, pw, ph, stroke=0, fill=1)
            c.setFillColor(colors.HexColor("#9aa3af"))
            c.setFont("Helvetica", 6.5)
            c.drawCentredString(CW / 2, py + ph / 2, "PHOTO")
        c.setStrokeColor(colors.HexColor("#8a8f98"))
        c.setLineWidth(0.6)
        c.rect(px - 0.8 * mm, py - 0.8 * mm, pw + 1.6 * mm, ph + 1.6 * mm, stroke=1, fill=0)
        top = py - 4.5 * mm
    signs = d["show_holder_sign"] or d["show_auth_sign"]
    bottom = (15 if signs else 6) * mm
    fields = [f for f in d["front_fields"] if f in FIELDS]
    lab_w = min(max([stringWidth(d["labels"][f], "Helvetica-Bold", 6.8) for f in fields] or [0]) + 1.5 * mm, 19 * mm)
    lx0 = 5 * mm
    val_x, val_w = lx0 + lab_w + 2.2 * mm, CW - (lx0 + lab_w + 2.2 * mm) - 3 * mm
    rows = [(d["labels"][f], _wrap(value_of(e, f, opt) or "-", "Helvetica-Bold", 6.8, val_w, 2)) for f in fields]
    n_lines = sum(len(r[1]) for r in rows) or 1
    step = min(4.1 * mm, max(2.6 * mm, (top - bottom) / n_lines))
    yy = top - step + 1 * mm
    for label, vals in rows:
        c.setFillColor(DARK)
        c.setFont("Helvetica-Bold", 6.8)
        c.drawString(lx0, yy, label)
        c.drawString(lx0 + lab_w, yy, ":")
        for i, v in enumerate(vals):
            _fit(c, v, "Helvetica-Bold", 6.8, val_w, 5)
            c.drawString(val_x, yy - i * step * 0.82, v)
        yy -= step * (1 + 0.82 * (len(vals) - 1))
    if signs:
        half = 18 * mm
        line_y = 9 * mm
        c.setStrokeColor(DARK)
        c.setLineWidth(0.4)
        for show, label, path, sx in ((d["show_holder_sign"], d["holder_sign_label"], opt["signature"](e), 3.5 * mm),
                                      (d["show_auth_sign"], d["auth_sign_label"], opt.get("auth_sign"),
                                       CW - half - 8 * mm)):
            if not show:
                continue
            _sign_img(c, path, sx, line_y + 0.4 * mm, half, 5.6 * mm)
            c.line(sx, line_y, sx + half, line_y)
            c.setFillColor(DARK)
            _fit(c, label.upper(), "Helvetica-Bold", 3.9, half + 2 * mm, 3)
            c.drawCentredString(sx + half / 2, line_y - 2 * mm, label.upper())
    c.restoreState()
    _border(c, x, y)


def front(c, e, x, y, opt):
    style = design_of(opt)["style"]
    if style == "LAILA":
        return front_laila(c, e, x, y, opt)
    if style != "CLASSIC":
        return front_labels(c, e, x, y, opt)
    RED, GREEN, DARK = _colors(opt)
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
    c.drawCentredString(CW / 2, 1.8 * mm, design_of(opt)["title"])
    c.restoreState()
    _border(c, x, y)


def back(c, e, x, y, opt):
    RED, GREEN, DARK = _colors(opt)
    d = design_of(opt)
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
    rows = [(d["labels"][f], value_of(e, f, opt) or "-") for f in d["back_fields"] if f in FIELDS]
    if opt.get("show_nid") and "nid" not in d["back_fields"]:
        rows.append((d["labels"]["nid"], e.nid or "-"))
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
    if d["show_qr"]:        # QR (employee code) for quick lookup
        q = 17 * mm
        _qr(c, f"{opt['group']}|{e.company.code if e.company else ''}|{e.employee_code}|{e.full_name}", (CW - q) / 2,
            yy - q - 0.5 * mm, q)
        yy -= q + 3.5 * mm
    if d["style"] == "CLASSIC":    # the LABELS front already has the signatures
        yy -= 6 * mm
        _sign_img(c, opt.get("auth_sign"), 14 * mm, yy + 0.4 * mm, CW - 28 * mm, 6 * mm)
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
    note = _wrap(d["back_note"], "Helvetica", 5.2, CW - 5 * mm, 2)
    for i, ln in enumerate(note):
        c.drawCentredString(CW / 2, 10.6 * mm - i * 2.3 * mm, ln)
    c.setFont("Helvetica-Bold", 5.4)
    addr = _wrap(opt["address"] or opt["group"], "Helvetica-Bold", 5.4, CW - 5 * mm, 2) if d["show_address"] else []
    for i, ln in enumerate(addr):
        c.drawCentredString(CW / 2, 5.6 * mm - i * 2.3 * mm, ln)
    if opt.get("phone") and d["show_address"]:
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
