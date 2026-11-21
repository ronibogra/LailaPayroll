"""Employee documents (certificates, NID copies, prescriptions...) stored in <data>/documents/emp_<id>/."""
import uuid
from pathlib import Path, PurePath

from config import DATA_DIR

DOC_DIR = DATA_DIR / "documents"
ALLOWED = {".pdf", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".doc", ".docx", ".xls", ".xlsx"}
MAX_BYTES = 20 * 1024 * 1024
DOC_TYPES = {"EDUCATION": "Educational certificate", "NID": "NID copy", "BIRTH_CERT": "Birth certificate",
             "PASSPORT": "Passport", "CV": "CV / resume", "APPOINTMENT": "Appointment letter",
             "MEDICAL": "Medical / prescription", "MATERNITY": "Maternity document", "OTHER": "Other"}
MEDIA = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
         ".gif": "image/gif", ".bmp": "image/bmp", ".webp": "image/webp", ".tif": "image/tiff", ".tiff": "image/tiff"}


def save_file(folder: str, original: str, content: bytes) -> tuple[str, int]:
    """Stores the file under <documents>/<folder>/ with a random name. Returns (stored path relative to DOC_DIR, size)."""
    ext = PurePath(original or "").suffix.lower()
    if ext not in ALLOWED:
        raise ValueError(f"{original}: file type not allowed (use PDF, JPG, PNG, Word or Excel)")
    if not content:
        raise ValueError(f"{original}: empty file")
    if len(content) > MAX_BYTES:
        raise ValueError(f"{original}: larger than 20 MB")
    d = DOC_DIR / folder
    d.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}{ext}"
    (d / name).write_bytes(content)
    return f"{folder}/{name}", len(content)


def path_of(stored: str) -> Path | None:
    if not stored or ".." in stored or stored.startswith(("/", "\\")):
        return None
    p = DOC_DIR / stored
    return p if p.exists() else None


def media_type(stored: str) -> str:
    return MEDIA.get(PurePath(stored).suffix.lower(), "application/octet-stream")


def delete_file(stored: str) -> None:
    p = path_of(stored)
    if p:
        p.unlink(missing_ok=True)
