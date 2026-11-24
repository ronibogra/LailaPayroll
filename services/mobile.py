"""Mobile attendance: employee logs in with employee code + PIN and punches from the phone.
The punch is only accepted inside the factory geofence and is stored with its GPS fix."""
import math
import threading
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from config import settings
from models import AttendanceLog, Employee
from security import verify_password
from services.policy import geofence
from timeutil import now_local

MIN_GAP_MINUTES = 5
MAX_FAILS, LOCK_MINUTES = 5, 15
_fails: dict[str, list[datetime]] = {}
_lock = threading.Lock()


class MobileError(Exception):
    pass


def distance_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


MODES = ("ANYWHERE", "RECORD", "STRICT")


def geo_mode(db: Session) -> str:
    from services.policy import get_setting
    m = (get_setting(db, "geofence_mode", default="ANYWHERE") or "ANYWHERE").upper()
    return m if m in MODES else "ANYWHERE"


def check_location(db: Session, lat: float, lon: float, accuracy: float) -> tuple[float | None, bool]:
    """Location is mandatory and comes only from the phone's GPS (the page has no field to type it).
    ANYWHERE (default): punch from any place, the GPS location is saved - nothing to set up.
    RECORD: anywhere, marked with the distance from the office. STRICT: only inside the office radius."""
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        raise MobileError("Location is required for mobile punch - allow location for this site and try again")
    if accuracy is None or accuracy <= 0 or accuracy > settings.max_gps_accuracy_m:
        raise MobileError(f"GPS signal too weak (±{int(accuracy or 0)} m). Move to open sky and try again")
    mode = geo_mode(db)
    g_lat, g_lon, radius = geofence(db)
    if not g_lat and not g_lon:
        if mode == "STRICT":
            raise MobileError("Office location is not set up yet - contact HR/IT")
        return None, True
    d = distance_m(lat, lon, g_lat, g_lon)
    if mode == "ANYWHERE":
        return d, True
    inside = d <= radius + min(accuracy, settings.max_gps_accuracy_m)
    if not inside and mode == "STRICT":
        raise MobileError(f"You are {int(d)} m from the office. Mobile punch only works inside the premises")
    return d, inside


def login(db: Session, code: str, pin: str) -> Employee:
    code = code.strip()
    now = now_local()
    with _lock:
        recent = [t for t in _fails.get(code, []) if now - t < timedelta(minutes=LOCK_MINUTES)]
        _fails[code] = recent
        if len(recent) >= MAX_FAILS:
            raise MobileError(f"Too many wrong PINs. Try again in {LOCK_MINUTES} minutes")
    emp = db.scalar(select(Employee).where(Employee.employee_code == code))
    # a PIN lets the employee log in (leave requests); punching also needs "mobile punch allowed"
    ok = (emp is not None and emp.mobile_pin_hash
          and emp.employment_status == "ACTIVE" and verify_password(pin, emp.mobile_pin_hash))
    if not ok:
        with _lock:
            _fails.setdefault(code, []).append(now)
        raise MobileError("Wrong employee ID or PIN, or mobile attendance not enabled for you")
    with _lock:
        _fails.pop(code, None)
    return emp


def _photo_dir():
    from config import DATA_DIR
    return DATA_DIR / "punch_photos"


def photo_required(db: Session) -> bool:
    from services.policy import get_setting
    return (get_setting(db, "mobile_photo_required", default="1") or "1") == "1"


def check_photo(content: bytes | None) -> bytes:
    """The picture taken by the phone camera at the moment of punching -> small JPEG."""
    from PIL import Image, ImageOps
    import io as _io
    if not content:
        raise MobileError("A photo from the camera is required - allow the camera for this site and try again")
    if len(content) > 4 * 1024 * 1024:
        raise MobileError("Photo too large")
    try:
        im = ImageOps.exif_transpose(Image.open(_io.BytesIO(content))).convert("RGB")
    except Exception:  # noqa: BLE001
        raise MobileError("The camera photo could not be read - try again")
    if im.width < 120 or im.height < 120:
        raise MobileError("The camera photo is too small - try again")
    im.thumbnail((640, 640))
    buf = _io.BytesIO()
    im.save(buf, "JPEG", quality=78)
    return buf.getvalue()


def save_photo(log: AttendanceLog, jpeg: bytes) -> str:
    rel = f"{log.punch_time:%Y-%m}/{log.id}_{log.card_no}.jpg".replace("/", "/")
    d = _photo_dir() / f"{log.punch_time:%Y-%m}"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{log.id}_{log.card_no}.jpg").write_bytes(jpeg)
    return rel


def punch(db: Session, emp: Employee, lat: float, lon: float, accuracy: float,
          client_ip: str | None = None, photo: bytes | None = None) -> AttendanceLog:
    if not settings.mobile_enabled:
        raise MobileError("Mobile attendance is switched off")
    if not emp.allow_mobile or emp.employment_status != "ACTIVE":
        raise MobileError("Mobile attendance is not enabled for you")
    dist, inside = check_location(db, lat, lon, accuracy)
    jpeg = check_photo(photo) if (photo or photo_required(db)) else None
    now = now_local()
    last = db.scalar(select(AttendanceLog).where(AttendanceLog.card_no == emp.card_no)
                     .order_by(AttendanceLog.punch_time.desc()).limit(1))
    if last and now - last.punch_time < timedelta(minutes=MIN_GAP_MINUTES) and last.punch_time <= now:
        raise MobileError(f"Already punched at {last.punch_time:%H:%M}. Wait {MIN_GAP_MINUTES} minutes")
    log = AttendanceLog(card_no=emp.card_no, punch_time=now, device="MOBILE", source="MOBILE",
                        latitude=round(lat, 6), longitude=round(lon, 6), accuracy_m=int(accuracy),
                        note=("Mobile GPS" if dist is None else f"{int(dist)} m from office")
                        + ("" if inside else " - OUTSIDE office area"),
                        client_ip=(client_ip or "")[:45] or None, created_by=emp.employee_code)
    db.add(log)
    db.flush()
    if jpeg:
        log.photo = save_photo(log, jpeg)
    db.commit()
    return log


def today_punches(db: Session, emp: Employee) -> list[AttendanceLog]:
    start = now_local().replace(hour=0, minute=0, second=0)
    return list(db.scalars(select(AttendanceLog).where(AttendanceLog.card_no == emp.card_no,
                                                       AttendanceLog.punch_time >= start)
                           .order_by(AttendanceLog.punch_time)))
