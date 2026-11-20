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
