"""Head Office (e.g. LAILA GROUP) that oversees the companies: name, address, contact and LOGO.
The logo is shown on every page, the login and mobile pages, payslips and every Excel/PDF report."""
import io
from pathlib import Path

from config import DATA_DIR, settings

BRAND_DIR = DATA_DIR / "branding"
LOGO_FILE = BRAND_DIR / "logo.png"
DEFAULT_LOGO = Path(__file__).resolve().parent.parent / "static" / "img" / "lg-logo.png"
KEYS = ("ho_name", "ho_address", "ho_phone", "ho_email")

# one shared dict, used as a template global and by the reports; refreshed after every change
BRAND: dict = {"name": settings.group_name, "address": "", "phone": "", "email": "", "logo_version": 0}


def logo_path() -> Path:
    return LOGO_FILE if LOGO_FILE.exists() else DEFAULT_LOGO


def refresh(db) -> dict:
    from services.policy import get_setting
    BRAND["name"] = get_setting(db, "ho_name", default=settings.group_name) or settings.group_name
    BRAND["address"] = get_setting(db, "ho_address", default="") or ""
    BRAND["phone"] = get_setting(db, "ho_phone", default="") or ""
    BRAND["email"] = get_setting(db, "ho_email", default="") or ""
    BRAND["logo_version"] = int(LOGO_FILE.stat().st_mtime) if LOGO_FILE.exists() else 0
    return BRAND


def save_logo(content: bytes) -> None:
    from PIL import Image, ImageOps
    if not content:
        raise ValueError("empty logo file")
    if len(content) > 10 * 1024 * 1024:
        raise ValueError("logo is larger than 10 MB")
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(content))).convert("RGBA")
    except Exception as exc:  # noqa: BLE001
        raise ValueError("this file is not an image (use PNG or JPG)") from exc
    bbox = img.getbbox()                     # trim transparent border
    if bbox:
        img = img.crop(bbox)
    img.thumbnail((512, 512))
    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    img.save(LOGO_FILE, "PNG")
    BRAND["logo_version"] = int(LOGO_FILE.stat().st_mtime)


def reset_logo() -> None:
    LOGO_FILE.unlink(missing_ok=True)
    BRAND["logo_version"] = 0
