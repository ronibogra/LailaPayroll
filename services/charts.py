"""Small server-side SVG charts for the dashboard (no internet / JS library needed on the LAN).
Colours follow the data-viz rules: status colours only for present/late/absent (with labels), the fixed
categorical order for companies, one hue for single-series trends. Each mark carries data-tip for the hover
tooltip (see dashboard.html) and every chart has a table view."""
from __future__ import annotations

from html import escape
from math import ceil, log10

CAT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
STATUS = {"Present": "#0ca30c", "Late": "#fab219", "Absent": "#d03b3b", "Leave": "#2a78d6"}
INK, MUTED, GRID = "#1d2430", "#6b7585", "#e9ebef"


def nice_max(v: float) -> float:
    if v <= 0:
        return 1
    p = 10 ** int(log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * p:
            return m * p
    return 10 * p


def ticks(top: float, n: int = 4) -> list[float]:
    """Gridline values with a round step (250 -> 0,50..250, not 62.5)."""
    for k in (n, 5, 2, 3):
        step = top / k * (10 if top < 5 else 1)
        if abs(step - round(step)) < 1e-9:
            return [top * i / k for i in range(k + 1)]
    return [top * i / n for i in range(n + 1)]


def _num(v: float) -> str:
    return f"{v:,.0f}" if abs(v) >= 100 or float(v).is_integer() else f"{v:,.1f}"


def _bar_path(x, y, w, h, r=4, horizontal=False) -> str:
    """Bar with rounded data-end only (square at the baseline)."""
    r = max(0, min(r, (h if horizontal else w) / 2, (w if horizontal else h)))
    if h <= 0 or w <= 0:
        return ""
    if horizontal:   # baseline on the left, round the right end
        return (f"M{x},{y}h{w - r}a{r},{r} 0 0 1 {r},{r}v{h - 2 * r}a{r},{r} 0 0 1 {-r},{r}h{-(w - r)}z")
    return (f"M{x},{y + h}v{-(h - r)}a{r},{r} 0 0 1 {r},{-r}h{w - 2 * r}a{r},{r} 0 0 1 {r},{r}v{h - r}z")


def columns(labels: list[str], values: list[float], color: str = CAT[0], unit: str = "", height: int = 220) -> str:
    W, H, L, B, T = 640, height, 40, 26, 14
    top = nice_max(max(values or [0]))
    pw, ph = W - L - 8, H - B - T
    n = max(len(values), 1)
    slot = pw / n
    bw = max(3, min(28, slot * 0.62))
    out = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img">']
    for t in ticks(top):
        y = T + ph - ph * t / top
        out.append(f'<line x1="{L}" x2="{W - 8}" y1="{y:.1f}" y2="{y:.1f}" stroke="{GRID}"/>'
                   f'<text x="{L - 6}" y="{y + 4:.1f}" text-anchor="end" class="ax">{_num(t)}</text>')
    step = max(1, ceil(n / 12))
    for i, (lab, v) in enumerate(zip(labels, values)):
        x = L + i * slot + (slot - bw) / 2
        h = ph * v / top
        out.append(f'<path class="mark" d="{_bar_path(x, T + ph - h, bw, h)}" fill="{color}" '
                   f'data-tip="{escape(lab)}: {_num(v)}{unit}"/>')
        if i % step == 0:
            out.append(f'<text x="{x + bw / 2:.1f}" y="{H - 8}" text-anchor="middle" class="ax">{escape(lab)}</text>')
    out.append(f'<line x1="{L}" x2="{W - 8}" y1="{T + ph}" y2="{T + ph}" stroke="#c9ced6"/></svg>')
    return "".join(out)


def line(labels: list[str], values: list[float | None], color: str = CAT[0], unit: str = "", top: float | None = None,
         height: int = 220) -> str:
    W, H, L, B, T = 640, height, 40, 26, 16
    vals = [v for v in values if v is not None]
    top = top or nice_max(max(vals or [0]))
    pw, ph = W - L - 16, H - B - T
    n = max(len(values) - 1, 1)
    out = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img">']
    for t in ticks(top):
        y = T + ph - ph * t / top
        out.append(f'<line x1="{L}" x2="{W - 16}" y1="{y:.1f}" y2="{y:.1f}" stroke="{GRID}"/>'
                   f'<text x="{L - 6}" y="{y + 4:.1f}" text-anchor="end" class="ax">{_num(t)}{unit}</text>')
    pts = [(L + pw * i / n, T + ph - ph * v / top, lab, v) for i, (lab, v) in enumerate(zip(labels, values)) if v is not None]
    if pts:
        area = f"M{pts[0][0]:.1f},{T + ph} " + " ".join(f"L{x:.1f},{y:.1f}" for x, y, *_ in pts) + f" L{pts[-1][0]:.1f},{T + ph}z"
        out.append(f'<path d="{area}" fill="{color}" opacity=".12"/>')
        out.append('<polyline fill="none" stroke="{}" stroke-width="2" stroke-linejoin="round" points="{}"/>'.format(
            color, " ".join(f"{x:.1f},{y:.1f}" for x, y, *_ in pts)))
        for x, y, lab, v in pts:
            out.append(f'<circle class="mark" cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="{color}" stroke="#fff" stroke-width="2" '
                       f'data-tip="{escape(lab)}: {_num(v)}{unit}"/>')
        x, y, lab, v = pts[-1]
        out.append(f'<text x="{x - 6:.1f}" y="{y - 10:.1f}" text-anchor="end" class="lbl">{_num(v)}{unit}</text>')
    step = max(1, ceil(len(labels) / 8))
    for i, lab in enumerate(labels):
        if i % step == 0 or i == len(labels) - 1:
            out.append(f'<text x="{L + pw * i / n:.1f}" y="{H - 8}" text-anchor="middle" class="ax">{escape(lab)}</text>')
    out.append("</svg>")
    return "".join(out)


def stacked_hbars(categories: list[str], series: list[tuple[str, str, list[float]]], height_per: int = 34) -> str:
    """One horizontal bar per category, segments per series (e.g. Present / Late / Absent / Leave per company)."""
    L, R, T = 150, 60, 6
    W = 640
    H = T + height_per * max(len(categories), 1) + 4
    totals = [sum(s[2][i] for s in series) for i in range(len(categories))]
    top = max(totals or [0]) or 1
    pw = W - L - R
    out = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img">']
    for i, cat in enumerate(categories):
        y = T + i * height_per
        bh = height_per - 12
        out.append(f'<text x="{L - 8}" y="{y + bh / 2 + 4:.1f}" text-anchor="end" class="lbl">{escape(cat[:22])}</text>')
        x = L
        segs = [(name, col, vals[i]) for name, col, vals in series if vals[i] > 0]
        for j, (name, col, v) in enumerate(segs):
            w = pw * v / top
            last = j == len(segs) - 1
            gap = 0 if last else 2
            d = _bar_path(x, y, max(w - gap, 0.5), bh, 4 if last else 0, horizontal=True)
            out.append(f'<path class="mark" d="{d}" fill="{col}" data-tip="{escape(cat)} · {escape(name)}: {_num(v)}"/>')
            x += w
        out.append(f'<text x="{x + 6:.1f}" y="{y + bh / 2 + 4:.1f}" class="ax">{_num(totals[i])}</text>')
    out.append("</svg>")
    return "".join(out)


def stacked_columns(labels: list[str], series: list[tuple[str, str, list[float]]], unit: str = "", height: int = 230,
                    width: int = 640) -> str:
    W, H, L, B, T = width, height, 56, 26, 14
    n = max(len(labels), 1)
    totals = [sum(s[2][i] for s in series) for i in range(len(labels))]
    top = nice_max(max(totals or [0]))
    pw, ph = W - L - 8, H - B - T
    slot = pw / n
    bw = max(6, min(64, slot * 0.5))
    out = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img">']
    for t in ticks(top):
        y = T + ph - ph * t / top
        lab = f"{t / 1e6:.1f}M" if top >= 1e6 else f"{t / 1e3:.0f}k" if top >= 1e4 else _num(t)
        out.append(f'<line x1="{L}" x2="{W - 8}" y1="{y:.1f}" y2="{y:.1f}" stroke="{GRID}"/>'
                   f'<text x="{L - 6}" y="{y + 4:.1f}" text-anchor="end" class="ax">{lab}</text>')
    for i, lab in enumerate(labels):
        x = L + i * slot + (slot - bw) / 2
        y = T + ph
        segs = [(name, col, vals[i]) for name, col, vals in series if vals[i] > 0]
        for j, (name, col, v) in enumerate(segs):
            h = ph * v / top
            last = j == len(segs) - 1
            gap = 0 if last else 2
            out.append(f'<path class="mark" d="{_bar_path(x, y - h, bw, max(h - gap, 0.5), 4 if last else 0)}" fill="{col}" '
                       f'data-tip="{escape(lab)} · {escape(name)}: {_num(v)}{unit}"/>')
            y -= h
        out.append(f'<text x="{x + bw / 2:.1f}" y="{H - 8}" text-anchor="middle" class="ax">{escape(lab)}</text>')
    out.append(f'<line x1="{L}" x2="{W - 8}" y1="{T + ph}" y2="{T + ph}" stroke="#c9ced6"/></svg>')
    return "".join(out)


def hbars(labels: list[str], values: list[float], color: str = CAT[0], height_per: int = 28) -> str:
    L, R, T, W = 150, 50, 4, 640
    H = T + height_per * max(len(labels), 1)
    top = max(values or [0]) or 1
    pw = W - L - R
    out = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img">']
    for i, (lab, v) in enumerate(zip(labels, values)):
        y = T + i * height_per
        bh = height_per - 10
        w = pw * v / top
        out.append(f'<text x="{L - 8}" y="{y + bh / 2 + 4:.1f}" text-anchor="end" class="lbl">{escape(lab[:22])}</text>'
                   f'<path class="mark" d="{_bar_path(L, y, max(w, 1), bh, 4, horizontal=True)}" fill="{color}" '
                   f'data-tip="{escape(lab)}: {_num(v)}"/>'
                   f'<text x="{L + w + 6:.1f}" y="{y + bh / 2 + 4:.1f}" class="ax">{_num(v)}</text>')
    out.append("</svg>")
    return "".join(out)


def legend(items: list[tuple[str, str]]) -> str:
    return '<div class="legend">' + "".join(
        f'<span><i style="background:{c}"></i>{escape(n)}</span>' for n, c in items) + "</div>"
