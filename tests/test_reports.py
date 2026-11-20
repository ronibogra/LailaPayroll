import io
from datetime import date, datetime, time
from decimal import Decimal
from types import SimpleNamespace as NS

from openpyxl import load_workbook

from reports.engine import Col, Report, fmt, to_excel, to_pdf

COLS = [Col("code", "Code", lambda r: r.code), Col("name", "Name", lambda r: r.name),
        Col("day", "Date", lambda r: r.day, kind="date"), Col("t", "Time", lambda r: r.t, kind="time"),
        Col("net", "Net pay", lambda r: r.net, kind="money"), Col("h", "Hours", lambda r: r.h, kind="hours"),
        Col("broken", "Broken", lambda r: r.missing.attr)]
ROWS = [NS(code=f"E{i}", name="Md. Rahim & Sons <x>", day=date(2026, 11, i), t=datetime(2026, 11, i, 8, 1, 5),
           net=Decimal("12345.5"), h=7.5) for i in range(1, 31)] * 2
REP = Report("t", "Test report", "x", COLS, lambda *a: (ROWS, "", len(ROWS)), totals=["net"])


def test_fmt():
    assert fmt(Decimal("1234.5"), "money") == "1,234.50"
    assert fmt(date(2026, 1, 2), "date") == "02/01/2026"
    assert fmt(datetime(2026, 1, 2, 8, 1, 5), "time") == "08:01:05"
    assert fmt(time(9, 0), "time") == "09:00"
    assert fmt(True) == "yes" and fmt(None) == ""
    assert COLS[-1].text(ROWS[0]) == ""          # a broken column never crashes a report


def test_excel_has_title_columns_rows_and_totals():
    data = to_excel(REP, COLS[:5], ROWS, "Nov 2026")
    ws = load_workbook(io.BytesIO(data)).active
    assert ws["B1"].value == "LAILA GROUP" and "Test report - Nov 2026" in ws["B2"].value
    assert [ws.cell(5, i).value for i in range(1, 6)] == ["Code", "Name", "Date", "Time", "Net pay"]
    assert ws.cell(6, 1).value == "E1" and ws.cell(6, 5).value == 12345.5
    assert ws.cell(5 + len(ROWS) + 1, 1).value == "TOTAL"
    assert ws.cell(5 + len(ROWS) + 1, 5).value.startswith("=SUM(E6:")


def test_pdf_is_made_for_few_and_many_columns():
    for cols in (COLS[:3], COLS * 3):
        pdf = to_pdf(REP, cols, ROWS, "sub")
        assert pdf[:4] == b"%PDF" and len(pdf) > 2000
    assert to_pdf(REP, COLS, [], "")[:4] == b"%PDF"      # empty report still a valid PDF


def test_generic_import_templates_open():
    import pytest
    pytest.importorskip("sqlalchemy")
    from integration.generic_import import TEMPLATES, template, read_table
    for kind in TEMPLATES:
        rows = read_table(f"{kind}.xlsx", template(kind))
        assert rows and rows[0]["_row"] == 2
    rows = read_table("h.csv", b"date,name\n2026-12-16,Victory Day\n\n")
    assert rows == [{"date": "2026-12-16", "name": "Victory Day", "_row": 2}]
