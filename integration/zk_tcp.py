"""ZKTeco machines downloaded by IP over the standard ZK protocol (TCP/UDP port 4370), using pyzk.
No ZK SDK is needed. Comm Key = Menu > Comm. > Connection on the machine (0 = none)."""
from __future__ import annotations

from datetime import datetime


class ZKError(RuntimeError):
    pass


def _connect(ip: str, port: int, comm_key: int, timeout: int):
    try:
        from zk import ZK
    except ImportError as exc:  # pragma: no cover
        raise ZKError("pyzk library missing") from exc
    last = None
    for force_udp in (False, True):          # most machines speak TCP; some older ones only UDP
        try:
            return ZK(ip, port=port, timeout=timeout, password=comm_key, force_udp=force_udp,
                      ommit_ping=True).connect()
        except Exception as exc:  # noqa: BLE001 - pyzk raises plain exceptions
            last = exc
    msg = str(last)
    if "unauth" in msg.lower():
        raise ZKError("wrong Comm Key (check Menu > Comm. > Connection on the machine)")
    if "timed out" in msg.lower() or "can't reach" in msg.lower() or "refused" in msg.lower():
        raise ZKError(f"cannot reach {ip}:{port} - check the IP, cable and that the machine is on")
    raise ZKError(f"cannot connect to {ip}:{port} ({msg})")


def device_info(ip: str, port: int = 4370, comm_key: int = 0, timeout: int = 10) -> str:
    conn = _connect(ip, port, comm_key, timeout)
    try:
        parts = []
        for label, fn in (("", "get_device_name"), ("SN ", "get_serialnumber"), ("firmware ", "get_firmware_version")):
            try:
                parts.append(f"{label}{getattr(conn, fn)()}")
            except Exception:  # noqa: BLE001
                pass
        try:
            conn.read_sizes()
            parts.append(f"{conn.users} users, {conn.records} punches stored")
        except Exception:  # noqa: BLE001
            pass
        try:
            parts.append(f"clock {conn.get_time():%d/%m/%Y %H:%M}")
        except Exception:  # noqa: BLE001
            pass
        return " / ".join(parts) or "connected"
    finally:
        try:
            conn.disconnect()
        except Exception:  # noqa: BLE001
            pass


def fetch(ip: str, port: int = 4370, comm_key: int = 0, since: datetime | None = None,
          timeout: int = 30) -> list[tuple[str, datetime, int | None]]:
    """All punches stored on the machine (optionally only newer than `since`): (user_id, time, punch)."""
    conn = _connect(ip, port, comm_key, timeout)
    try:
        try:
            conn.disable_device()             # stops new punches mixing into the read; re-enabled below
        except Exception:  # noqa: BLE001
            pass
        records = conn.get_attendance() or []
    except ZKError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ZKError(f"download from {ip} failed: {exc}") from exc
    finally:
        try:
            conn.enable_device()
        except Exception:  # noqa: BLE001
            pass
        try:
            conn.disconnect()
        except Exception:  # noqa: BLE001
            pass
    out = []
    for r in records:
        uid = str(getattr(r, "user_id", "") or "").strip()
        t = getattr(r, "timestamp", None)
        if not uid or not isinstance(t, datetime):
            continue
        t = t.replace(microsecond=0, tzinfo=None)
        if since and t < since:
            continue
        out.append((uid, t, getattr(r, "punch", None)))
    return out
