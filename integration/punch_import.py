"""Import punch files exported from machines or other software: Excel (.xlsx), CSV, ZKTeco attlog.dat/.txt.

Columns are found by name, so most exports work as they are:
  ID:   card_no, employee_code, emp id, employee id, user id, ac-no, enroll no, person id, employee no, id, pin
  Time: one date-time column (punch_time, datetime, time, check time, date time, timestamp ...)
        or a date column + a time column.
  Optional: device / machine / terminal; in/out / state / direction.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, time, timedelta

from integration.dtparse import parse_dt
from integration.zk_adms import parse_attlog

ID_NAMES = ["card_no", "cardno", "card no", "card", "employee_code", "emp id", "empid", "employee id",
            "employeeid", "emp code", "empcode", "employee no", "employee no.", "employeeno", "user id", "userid",
            "ac-no", "ac-no.", "ac no", "acno", "enroll no", "enrollno", "enroll number", "person id", "personid",
            "staff id", "id no", "id", "pin", "code", "badge", "badgenumber"]
DT_NAMES = ["punch_time", "punch time", "datetime", "date time", "date/time", "date_time", "check time",
            "checktime", "time stamp", "timestamp", "attendance time", "log time", "logtime", "authentication time",
            "access time", "event time", "punch"]
DATE_NAMES = ["date", "punch date", "attendance date", "log date", "day"]
TIME_NAMES = ["time", "punch time only", "log time only", "clock"]
DEV_NAMES = ["device", "machine", "terminal", "device name", "machine name", "terminal name", "device_serial",
             "sn", "serial", "reader", "door", "machine no", "device id"]
DIR_NAMES = ["in/out", "in out", "inout", "direction", "state", "status", "check type", "checktype",
             "attendance status", "punch state", "type"]


class ImportProblem(ValueError):
    pass


def _norm(h) -> str:
    return re.sub(r"\s+", " ", str(h or "").strip().lower().replace("_", " ").replace("﻿", ""))


def _find(headers: list[str], names: list[str]) -> int | None:
    norm = [_norm(h) for h in headers]
    for n in names:                                   # exact name first
        n = _norm(n)
        if n in norm:
            return norm.index(n)
    return None


def _to_date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, (int, float)):                   # Excel serial date
        return (datetime(1899, 12, 30) + timedelta(days=float(v))).date()
    return parse_dt(f"{str(v).strip()} 00:00:00").date()


def _to_time(v):
    if isinstance(v, datetime):
        return v.time().replace(microsecond=0)
    if isinstance(v, time):
        return v.replace(microsecond=0)
    if isinstance(v, timedelta):
        return (datetime.min + v).time()
    if isinstance(v, (int, float)):                   # Excel fraction of a day
        secs = round(float(v) % 1 * 86400)
        return (datetime.min + timedelta(seconds=secs)).time()
    s = str(v).strip()
    for fmt in ("%H:%M:%S", "%H:%M", "%I:%M:%S %p", "%I:%M %p", "%I:%M:%S%p", "%I:%M%p"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            pass
    raise ValueError(f"unrecognised time {s!r}")


def _to_dt(v):
    if isinstance(v, datetime):
        return v.replace(microsecond=0, tzinfo=None)
    if isinstance(v, (int, float)):
        return (datetime(1899, 12, 30) + timedelta(days=float(v))).replace(microsecond=0)
    return parse_dt(v)


def _dir(v) -> str | None:
    s = _norm(v)
    if s in ("in", "check in", "checkin", "c/in", "0", "i", "entry", "duty on"):
        return "IN"
    if s in ("out", "check out", "checkout", "c/out", "1", "o", "exit", "duty off"):
        return "OUT"
    return None


def _id(v) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v or "").strip()


def rows_from_table(headers: list, data: list[list]) -> tuple[list[tuple], list[str]]:
    i_id = _find(headers, ID_NAMES)
    i_dt = _find(headers, DT_NAMES)
    i_d = _find(headers, DATE_NAMES)
    i_t = _find(headers, TIME_NAMES)
    i_dev = _find(headers, DEV_NAMES)
    i_dir = _find(headers, DIR_NAMES)
    if i_id is None:
        raise ImportProblem("no employee ID column found. Name it e.g. 'card_no', 'Employee ID', 'User ID' or 'AC-No'. "
                            f"Columns in the file: {', '.join(str(h) for h in headers if h)}")
    if i_dt is None and (i_d is None or i_t is None):
        if i_d is not None and i_t is None:
            i_dt, i_d = i_d, None             # a single 'Date' column holding date + time
        else:
            raise ImportProblem("no date/time column found. Use one 'Punch Time' column or 'Date' + 'Time' columns. "
                                f"Columns in the file: {', '.join(str(h) for h in headers if h)}")
    rows, errors = [], []
    for n, r in enumerate(data, start=2):
        get = lambda i: r[i] if i is not None and i < len(r) else None  # noqa: E731
        card = _id(get(i_id))
        if not card:
            continue
        try:
            if i_dt is not None:
                t = _to_dt(get(i_dt))
            else:
                t = datetime.combine(_to_date(get(i_d)), _to_time(get(i_t)))
        except (ValueError, TypeError) as exc:
            errors.append(f"row {n}: {exc}")
            continue
        rows.append((card, t, _id(get(i_dev)) or None, _dir(get(i_dir)) if i_dir is not None else None))
    return rows, errors


def read_punch_file(filename: str, content: bytes) -> tuple[list[tuple], list[str]]:
    """Returns (rows, row_errors). rows = (card_no, punch_time, device, direction)."""
    name = filename.lower()
    if name.endswith((".dat", ".txt")):
        punches, bad = parse_attlog(content.decode("utf-8", "replace"))
        return [(p, t, "USB", None) for p, t in punches], ([f"{bad} unreadable lines"] if bad else [])
    if name.endswith(".xls"):
        raise ImportProblem("old .xls files are not supported - open it in Excel and 'Save As' .xlsx")
    if name.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
        best = None
        for ws in wb.worksheets:              # first sheet that has an ID and a time column
            it = ws.iter_rows(values_only=True)
            for _ in range(10):               # header may be below a title
                try:
                    head = list(next(it))
                except StopIteration:
                    break
                if _find(head, ID_NAMES) is not None and (_find(head, DT_NAMES) is not None or _find(head, DATE_NAMES) is not None):
                    best = (head, [list(r) for r in it])
                    break
            if best:
                break
        if not best:
            first = wb.worksheets[0]
            head = next(first.iter_rows(values_only=True), [])
            return rows_from_table(list(head), [])
        return rows_from_table(*best)
    text = content.decode("utf-8-sig", "replace")
    dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t") if text.strip() else csv.excel
    table = list(csv.reader(io.StringIO(text), dialect))
    if not table:
        raise ImportProblem("the file is empty")
    return rows_from_table(table[0], table[1:])
