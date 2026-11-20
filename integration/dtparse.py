"""Date/time parsing for imported punch files (no database imports, so it is easy to test)."""
from datetime import datetime

FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
           "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p")


def parse_dt(value) -> datetime:
    if isinstance(value, datetime):
        return value.replace(microsecond=0, tzinfo=None)
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text).replace(microsecond=0, tzinfo=None)
    except ValueError:
        pass
    for fmt in FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"unrecognised date/time: {text!r}")
