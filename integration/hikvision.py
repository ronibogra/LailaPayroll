"""Hikvision face terminals (DS-K1T341, DS-K1T671 etc.) via ISAPI, polled by the server.

On each machine: enable ISAPI / HTTP (Configuration > Network > Advanced > Integration Protocol),
create a user (admin is fine) and enter its IP, port 80, username and password on the Devices page.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from config import settings

PAGE = 30          # most firmware returns max 30 events per page
MAJOR_EVENT = 5    # 5 = event (access) records; minor 0 = all minors


def _tz() -> timezone:
    return timezone(timedelta(hours=settings.timezone_offset_hours))


def iso(t: datetime) -> str:
    return t.replace(microsecond=0, tzinfo=_tz()).isoformat()


def build_query(start: datetime, end: datetime, position: int, search_id: str = "laila") -> dict:
    return {"AcsEventCond": {"searchID": search_id, "searchResultPosition": position, "maxResults": PAGE,
                             "major": MAJOR_EVENT, "minor": 0, "startTime": iso(start), "endTime": iso(end)}}


def to_local(text: str) -> datetime:
    t = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if t.tzinfo is not None:
        t = t.astimezone(_tz()).replace(tzinfo=None)
    return t.replace(microsecond=0)


def parse_events(payload: dict) -> tuple[list[tuple[str, datetime]], bool, int]:
    """Returns (punches, more_pages, number_of_events_in_page). Events without an employee number
    (door opened by button, stranger, failed face) are skipped."""
    ev = payload.get("AcsEvent", {})
    info = ev.get("InfoList", []) or []
    out = []
    for i in info:
        emp = str(i.get("employeeNoString") or i.get("employeeNo") or "").strip()
        if not emp or not i.get("time"):
            continue
        try:
            out.append((emp, to_local(i["time"])))
        except ValueError:
            continue
    more = str(ev.get("responseStatusStrg", "")).upper() == "MORE"
    return out, more, len(info)


def _opener(host: str, user: str, password: str):
    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, host, user, password)
    return urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(mgr),
                                       urllib.request.HTTPBasicAuthHandler(mgr))


def fetch(ip: str, port: int, user: str, password: str, start: datetime, end: datetime,
          timeout: int = 15) -> list[tuple[str, datetime]]:
    host = f"http://{ip}:{port}"
    url = f"{host}/ISAPI/AccessControl/AcsEvent?format=json"
    opener = _opener(host, user, password)
    punches: list[tuple[str, datetime]] = []
    pos = 0
    for _ in range(2000):  # safety stop (60,000 events)
        body = json.dumps(build_query(start, end, pos)).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
        try:
            with opener.open(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise RuntimeError("wrong username or password") from exc
            raise RuntimeError(f"machine answered HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"cannot reach {ip}:{port} ({getattr(exc, 'reason', exc)})") from exc
        page, more, n = parse_events(payload)
        punches += page
        pos += n
        if not more or n == 0:
            break
    return punches


def device_info(ip: str, port: int, user: str, password: str, timeout: int = 8) -> str:
    """Connection test: returns model + serial number text."""
    host = f"http://{ip}:{port}"
    try:
        with _opener(host, user, password).open(f"{host}/ISAPI/System/deviceInfo", timeout=timeout) as resp:
            xml = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError("wrong username or password" if exc.code == 401 else f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"cannot reach {ip}:{port} ({getattr(exc, 'reason', exc)})") from exc
    import re
    model = re.search(r"<model>([^<]*)</model>", xml)
    serial = re.search(r"<serialNumber>([^<]*)</serialNumber>", xml)
    return f"{model.group(1) if model else '?'} / SN {serial.group(1) if serial else '?'}"
