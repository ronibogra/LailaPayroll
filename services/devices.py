"""Punch machines: ZKTeco by IP (download), ZKTeco push (ADMS), Hikvision (ISAPI). Device passwords /
Comm Keys are stored encrypted. Every machine error goes to the Error log."""
import base64
import hashlib
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from config import settings
from database import SessionLocal
from integration import hikvision, zk_tcp
from integration.csv_import import insert_punches
from integration.zk_adms import parse_attlog
from models import Device
from services.errors import log_error
from timeutil import now_local

BRANDS = {"ZKTECO": "ZKTeco (download by IP)", "ZKTECO_PUSH": "ZKTeco (machine sends - Cloud Server)",
          "HIKVISION": "Hikvision"}
PULL_BRANDS = ("ZKTECO", "HIKVISION")
_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(("dev-pw:" + settings.secret_key).encode()).digest()))
_poll_lock = threading.Lock()
_device_locks: dict[int, threading.Lock] = {}
_next_due: dict[int, float] = {}


def encrypt(pw: str) -> str:
    return _fernet.encrypt(pw.encode()).decode()


def decrypt(token: str | None) -> str:
    if not token:
        return ""
    try:
        return _fernet.decrypt(token.encode()).decode()
    except InvalidToken:
        return ""


def comm_key(dev: Device) -> int:
    raw = decrypt(dev.password_enc).strip()
    return int(raw) if raw.isdigit() else 0


def _dir(dev: Device) -> str | None:
    return dev.direction if dev.direction in ("IN", "OUT") else None


def _lock(dev_id: int) -> threading.Lock:
    return _device_locks.setdefault(dev_id, threading.Lock())


# ------------------------------------------------------------ ZKTeco push (ADMS)
def zk_device(db: Session, sn: str, ip: str | None) -> Device:
    """Find the machine by serial number; an unknown machine is registered as PENDING (not approved)."""
    sn = (sn or "").strip()[:100]
    dev = db.scalar(select(Device).where(Device.serial_number == sn, Device.brand.in_(("ZKTECO_PUSH", "ZKTECO"))))
    if dev is None:
        dev = Device(name=f"ZKTeco {sn}", brand="ZKTECO_PUSH", serial_number=sn, ip_address=ip, approved=False,
                     last_status="New machine - waiting for approval on the Machines page")
        db.add(dev)
    dev.last_seen = now_local()
    if ip and not dev.ip_address:
        dev.ip_address = ip
    db.commit()
    return dev


def zk_receive(db: Session, dev: Device, body: str) -> tuple[int, int]:
    punches, bad = parse_attlog(body)
    if not dev.approved or not dev.enabled:
        dev.last_status = f"Ignored {len(punches)} punches - machine not approved/enabled"
        db.commit()
        return len(punches), 0
    ins, _ = insert_punches(db, [(p, t, dev.name) for p, t in punches], "ZKTECO", device_id=dev.id,
                            direction=_dir(dev))
    dev.last_status = f"{now_local():%d/%m %H:%M} received {len(punches)}, new {ins}" + (f", bad lines {bad}" if bad else "")
    db.commit()
    if bad:
        log_error("MACHINE", f"{dev.name}: {bad} unreadable punch lines received")
    return len(punches), ins


# ------------------------------------------------------------ download (ZKTeco by IP, Hikvision)
def test_device(dev: Device) -> str:
    if dev.brand == "ZKTECO":
        return zk_tcp.device_info(dev.ip_address, dev.port or 4370, comm_key(dev))
    if dev.brand == "HIKVISION":
        return hikvision.device_info(dev.ip_address, dev.port or 80, dev.username or "admin", decrypt(dev.password_enc))
    when = f"{dev.last_seen:%d/%m/%Y %H:%M}" if dev.last_seen else "never"
    return f"this machine sends punches by itself. Last contact: {when}"


def pull_device(db: Session, dev: Device, days_back: int | None = None) -> dict:
    """Download punches from one machine. days_back=None: only what is new since the last download."""
    if not _lock(dev.id).acquire(blocking=False):
        return {"ok": False, "message": "a download from this machine is already running"}
    try:
        now = now_local()
        if days_back:
            start = now - timedelta(days=days_back)
        elif dev.cursor_time:
            start = dev.cursor_time - timedelta(minutes=10)    # overlap: duplicates are skipped
        else:
            start = None if dev.brand == "ZKTECO" else now - timedelta(days=31)
        t0 = _time.monotonic()
        try:
            if dev.brand == "ZKTECO":
                rows = [(u, t, dev.name) for u, t, _ in
                        zk_tcp.fetch(dev.ip_address, dev.port or 4370, comm_key(dev), since=start)]
            elif dev.brand == "HIKVISION":
                rows = [(p, t, dev.name) for p, t in
                        hikvision.fetch(dev.ip_address, dev.port or 80, dev.username or "admin",
                                        decrypt(dev.password_enc), start, now + timedelta(minutes=5))]
            else:
                return {"ok": False, "message": "this machine sends punches by itself (no download needed)"}
        except Exception as exc:  # noqa: BLE001
            dev.last_status = f"{now:%d/%m %H:%M} ERROR: {exc}"
            db.commit()
            log_error("MACHINE", f"{dev.name} ({dev.ip_address}): {exc}")
            return {"ok": False, "message": str(exc)}
        ins, _ = insert_punches(db, rows, dev.brand, device_id=dev.id, direction=_dir(dev))
        if rows:
            newest = max(t for _, t, _ in rows)
            dev.cursor_time = max(dev.cursor_time or newest, newest)
        dev.last_seen = now_local()
        dev.last_status = f"{now:%d/%m %H:%M} read {len(rows)}, new {ins} ({_time.monotonic() - t0:.0f}s)"
        db.commit()
        return {"ok": True, "read": len(rows), "inserted": ins}
    finally:
        _lock(dev.id).release()


def _pull_one(dev_id: int, days_back: int | None) -> str:
    db = SessionLocal()
    try:
        dev = db.get(Device, dev_id)
        r = pull_device(db, dev, days_back)
        return f"{dev.name}: " + (f"{r['read']} read, {r['inserted']} new" if r["ok"] else f"ERROR {r['message']}")
    except Exception as exc:  # noqa: BLE001
        log_error("MACHINE", f"machine {dev_id}: {exc}", exc=exc)
        return f"machine {dev_id}: ERROR {exc}"
    finally:
        db.close()


def pull_all(days_back: int | None = None, only_due: bool = False) -> list[str]:
    """Download from every approved, active ZKTeco-by-IP and Hikvision machine, 16 at a time."""
    db = SessionLocal()
    try:
        devs = db.scalars(select(Device).where(Device.brand.in_(PULL_BRANDS), Device.approved == True,  # noqa: E712
                                               Device.enabled == True)).all()  # noqa: E712
        targets = []
        now = _time.monotonic()
        for d in devs:
            if only_due and _next_due.get(d.id, 0) > now:
                continue
            interval = settings.zk_poll_seconds if d.brand == "ZKTECO" else settings.device_poll_seconds
            _next_due[d.id] = now + max(30, interval)
            targets.append(d.id)
    finally:
        db.close()
    if not targets:
        return []
    with ThreadPoolExecutor(max_workers=min(16, len(targets))) as pool:
        return list(pool.map(lambda i: _pull_one(i, days_back), targets))


def start_poller() -> None:
    def loop():
        _time.sleep(10)
        while True:
            try:
                if _poll_lock.acquire(blocking=False):
                    try:
                        pull_all(only_due=True)
                    finally:
                        _poll_lock.release()
            except Exception as exc:  # noqa: BLE001 - never let the thread die
                log_error("MACHINE", f"automatic download stopped once: {exc}", exc=exc)
            _time.sleep(15)
    threading.Thread(target=loop, name="machine-poller", daemon=True).start()
