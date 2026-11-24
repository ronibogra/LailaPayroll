"""Local (Asia/Dhaka) time helpers. Punch times from machines are local
wall-clock times, so the whole app stores naive local datetimes."""
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("Asia/Dhaka")
except Exception:  # Windows without tzdata: Bangladesh has no DST, fixed +06:00
    _TZ = timezone(timedelta(hours=6))


def now_local() -> datetime:
    return datetime.now(_TZ).replace(tzinfo=None, microsecond=0)


# ---- dates typed by people -------------------------------------------------------------------------
# A browser without a date/month picker shows a plain text box, so people type 01/01/2026, 1-1-2026,
# 2026-01-01, 1 Jan 2026 ... Day comes before month (Bangladesh), unless the first number cannot be a month.
import re as _re
from calendar import month_name as _mn
from datetime import date as _date

_MONTHS = {n.lower(): i for i, n in enumerate(_mn) if n} | {n.lower()[:3]: i for i, n in enumerate(_mn) if n} \
    | {"sept": 9}


def _parts(v: str) -> tuple[list[int], list[int]]:
    parts = [p for p in _re.split(r"[\s/.,_\-]+|T", v) if p]
    nums = [int(p) for p in parts if p.isdigit()]
    names = [_MONTHS[p.lower()] for p in parts if p.lower() in _MONTHS]
    return nums, names


def parse_day(value) -> _date:
    """Any common way of writing a date -> date. Raises ValueError with a readable message."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, _date):
        return value
    v = str(value or "").strip()
    if not v:
        raise ValueError("Date is empty")
    if _re.fullmatch(r"\d{8}", v):                        # 20260101
        v = f"{v[:4]}-{v[4:6]}-{v[6:]}"
    nums, names = _parts(v[:10] if _re.match(r"\d{4}-\d\d-\d\dT", v) else v)
    try:
        if names and len(nums) >= 2:                      # 1 Jan 2026 / Jan 1, 2026
            y = next(n for n in nums if n > 31)
            d = next(n for n in nums if n != y)
            return _date(_year(y), names[0], d)
        if len(nums) >= 3:
            a, b, c = nums[:3]
            if a > 31:                                    # 2026-01-31
                return _date(a, b, c)
            if b > 12 and a <= 12:                        # 01/31/2026 (month first)
                return _date(_year(c), a, b)
            return _date(_year(c), b, a)                  # 31/01/2026 (day first)
    except (StopIteration, ValueError):
        pass
    raise ValueError(f"Date '{v}' not understood - type it like 31/01/2026 or 2026-01-31")


def _year(y: int) -> int:
    return y + 2000 if y < 100 else y


def parse_month(value) -> tuple[int, int]:
    """2026-11, 11/2026, Nov 2026, 2026-11-05, 01/11/2026 ... -> (year, month). Empty -> this month."""
    t = now_local()
    v = str(value or "").strip()
    if not v:
        return t.year, t.month
    nums, names = _parts(v)
    if len(nums) >= 3 or (names and len(nums) >= 2):      # a whole date was typed: use its month
        try:
            d = parse_day(v)
            return d.year, d.month
        except ValueError:
            pass
    year = next((n for n in nums if 1900 < n < 2200), None)
    month = names[0] if names else next((n for n in nums if 1 <= n <= 12 and n != year), None)
    if not names and len(nums) >= 2 and nums[0] == year:
        month = nums[1] if 1 <= nums[1] <= 12 else None
    if year and month:
        return year, month
    raise ValueError(f"Month '{v}' not understood - choose it in the box or type it like {t:%Y-%m}")


def next_due(base, today):
    """Yearly increment date after `base` (last increment / joining); after several missed years, the latest one."""
    from datetime import timedelta

    def plus_year(d):
        try:
            return d.replace(year=d.year + 1)
        except ValueError:          # 29 Feb
            return d.replace(year=d.year + 1, day=28)
    due = plus_year(base)
    while due + timedelta(days=366) <= today:
        due = plus_year(due)
    return due
