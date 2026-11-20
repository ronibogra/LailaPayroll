"""Machine handling: ZKTeco push, Hikvision polling, device password encryption."""
import base64
import hashlib
import threading
import time as _time
from datetime import timedelta

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from config import settings
from database import SessionLocal
from integration import hikvision
from integration.csv_import import insert_punches
from integration.zk_adms import parse_attlog
from models import Device
from timeutil import now_local

_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(("dev-pw:" + settings.secret_key).encode()).digest()))
_poll_lock = threading.Lock()


def encrypt(pw: str) -> str:
    return _fernet.encrypt(pw.encode()).decode()


def decrypt(token: str | None) -> str:
    if not token:
        return ""
    try:
        return _fernet.decrypt(token.encode()).decode()
    except InvalidToken:
        return ""


# ------------------------------------------------------------ ZKTeco (push)
def zk_device(db: Session, sn: str, ip: str | None) -> Device:
    """Find the machine by serial number; an unknown machine is registered as PENDING (not approved)."""
    sn = (sn or "").strip()[:100]
    dev = db.scalar(select(Device).where(Device.serial_number == sn, Device.brand == "ZKTECO"))
    if dev is None:
        dev = Device(name=f"ZKTeco {sn}", brand="ZKTECO", serial_number=sn, ip_address=ip, approved=False,
                     last_status="New machine - waiting for approval on the Devices page")
        db.add(dev)
    dev.last_seen = now_local()
    if ip:
        dev.ip_address = ip
    db.commit()
    return dev


def zk_receive(db: Session, dev: Device, body: str) -> tuple[int, int]:
    """Store ATTLOG lines. Returns (lines_received, inserted). Unapproved machines are ignored."""
    punches, bad = parse_attlog(body)
    if not dev.approved or not dev.enabled:
        dev.last_status = f"Ignored {len(punches)} punches - machine not approved/enabled"
        db.commit()
        return len(punches), 0
    ins, dup = insert_punches(db, [(p, t, dev.name) for p, t in punches], "ZKTECO")
    dev.last_status = f"{now_local():%d/%m %H:%M} received {len(punches)}, new {ins}" + (f", bad lines {bad}" if bad else "")
    db.commit()
    return len(punches), ins


# ------------------------------------------------------------ Hikvision (poll)
def hik_poll_device(db: Session, dev: Device, days_back: int | None = None) -> dict:
    end = now_local() + timedelta(minutes=5)
    if days_back:
        start = end - timedelta(days=days_back)
    elif dev.cursor_time:
        start = dev.cursor_time - timedelta(minutes=10)   # overlap: duplicates are skipped
    else:
        start = end - timedelta(days=31)
    try:
        punches = hikvision.fetch(dev.ip_address, dev.port or 80, dev.username or "admin",
                                  decrypt(dev.password_enc), start, end)
    except RuntimeError as exc:
        dev.last_status = f"{now_local():%d/%m %H:%M} ERROR: {exc}"
        db.commit()
        return {"ok": False, "message": str(exc)}
    ins, dup = insert_punches(db, [(p, t, dev.name) for p, t in punches], "HIKVISION")
    if punches:
        newest = max(t for _, t in punches)
        dev.cursor_time = max(dev.cursor_time or newest, newest)
    dev.last_seen = now_local()
    dev.last_status = f"{now_local():%d/%m %H:%M} read {len(punches)}, new {ins}"
    db.commit()
    return {"ok": True, "read": len(punches), "inserted": ins}


def hik_poll_all(days_back: int | None = None) -> list[str]:
    if not _poll_lock.acquire(blocking=False):
        return ["a poll is already running"]
    db = SessionLocal()
    try:
        out = []
        for dev in db.scalars(select(Device).where(Device.brand == "HIKVISION", Device.approved == True,  # noqa: E712
                                                   Device.enabled == True)).all():  # noqa: E712
            r = hik_poll_device(db, dev, days_back)
            out.append(f"{dev.name}: " + (f"{r['inserted']} new" if r["ok"] else r["message"]))
        return out
    finally:
        db.close()
        _poll_lock.release()


def start_poller() -> None:
    def loop():
        while True:
            try:
                hik_poll_all()
            except Exception:  # never let the thread die
                pass
            _time.sleep(max(15, settings.device_poll_seconds))
    threading.Thread(target=loop, name="hik-poller", daemon=True).start()
