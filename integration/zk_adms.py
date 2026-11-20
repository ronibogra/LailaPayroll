"""ZKTeco ADMS / PUSH protocol (SpeedFace-V5L and other push-firmware terminals).

On each machine: Menu > COMM > Cloud Server Setting
    Server address = <head-office server IP>, Server port = 8585, HTTPS = off, Proxy = off
The machine then sends every punch to this server within seconds.

Endpoints used by the machine:
    GET  /iclock/cdata?SN=..&options=all      handshake -> we reply with options
    POST /iclock/cdata?SN=..&table=ATTLOG     punches, one per line, tab separated
    POST /iclock/cdata?SN=..&table=OPERLOG    user/operation log (acknowledged, ignored)
    GET  /iclock/getrequest?SN=..             command poll -> "OK" (no commands)
    POST /iclock/devicecmd?SN=..              command results -> "OK"
The same line format is used by attlog.dat files exported to USB.
"""
from __future__ import annotations

from datetime import datetime

from config import settings


def options_response(sn: str, stamp: str = "None") -> str:
    return "\r\n".join([
        f"GET OPTION FROM: {sn}",
        f"ATTLOGStamp={stamp}",
        "OPERLOGStamp=9999",
        "ATTPHOTOStamp=9999",
        "ErrorDelay=30",
        "Delay=10",
        "TransTimes=00:00;14:05",
        "TransInterval=1",
        "TransFlag=TransData AttLog",
        f"TimeZone={settings.timezone_offset_hours}",
        "Realtime=1",
        "Encrypt=None",
        "",
    ])


def parse_attlog(body: str) -> tuple[list[tuple[str, datetime]], int]:
    """Lines: PIN<TAB>YYYY-MM-DD HH:MM:SS<TAB>status<TAB>verify<TAB>... Returns (punches, bad_line_count)."""
    out: list[tuple[str, datetime]] = []
    bad = 0
    for raw in body.replace("\r", "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("\t")]
        if len(parts) < 2:
            parts = line.split()           # some USB exports use spaces
            if len(parts) >= 3:
                parts = [parts[0], f"{parts[1]} {parts[2]}"] + parts[3:]
        try:
            pin = parts[0]
            t = datetime.strptime(parts[1][:19], "%Y-%m-%d %H:%M:%S")
        except (IndexError, ValueError):
            bad += 1
            continue
        if pin:
            out.append((pin, t))
    return out, bad
