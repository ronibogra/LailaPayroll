"""Employee photos: stored resized as JPEG in <data folder>/photos."""
import io

from config import DATA_DIR

PHOTO_DIR = DATA_DIR / "photos"
MAX_BYTES = 15 * 1024 * 1024


def save_photo(emp_id: int, content: bytes) -> str:
    from PIL import Image, ImageOps
    if not content:
        raise ValueError("empty photo file")
    if len(content) > MAX_BYTES:
        raise ValueError("photo is larger than 15 MB")
    try:
        img = Image.open(io.BytesIO(content))
        img = ImageOps.exif_transpose(img).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        raise ValueError("this file is not a photo (use JPG or PNG)") from exc
    img.thumbnail((600, 600))
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    name = f"emp_{emp_id}.jpg"
    img.save(PHOTO_DIR / name, "JPEG", quality=85)
    return name


def photo_path(name: str | None):
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    p = PHOTO_DIR / name
    return p if p.exists() else None


IMAGE_EXT = (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff")


def bulk_photos(db, files: list[tuple[str, bytes]], only_company_id: int | None = None) -> dict:
    """files: (filename, content). ZIP files are opened. Each photo is matched to an employee by its file name
    (employee code or card no., e.g. 12127.jpg or HO-0001.png)."""
    import zipfile
    from pathlib import PurePath
    from sqlalchemy import select
    from models import Employee

    images: list[tuple[str, bytes]] = []
    for name, content in files:
        if name.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(content)) as z:
                    for info in z.infolist():
                        if info.is_dir() or info.file_size > MAX_BYTES:
                            continue
                        if PurePath(info.filename).suffix.lower() in IMAGE_EXT and not PurePath(info.filename).name.startswith("."):
                            images.append((PurePath(info.filename).name, z.read(info)))
            except zipfile.BadZipFile:
                raise ValueError(f"{name} is not a valid ZIP file")
        elif PurePath(name).suffix.lower() in IMAGE_EXT:
            images.append((PurePath(name).name, content))
    if not images:
        raise ValueError("no photos found - upload JPG/PNG files or a ZIP of them")
    emps = list(db.scalars(select(Employee)))
    by_code = {e.employee_code.lower(): e for e in emps}
    by_card = {e.card_no.lower(): e for e in emps}
    done, unmatched, bad = [], [], []
    for name, content in images:
        stem = PurePath(name).stem.strip().lower()
        e = by_code.get(stem) or by_card.get(stem)
        if e is None or (only_company_id and e.company_id != only_company_id):
            unmatched.append(name)
            continue
        try:
            e.photo = save_photo(e.id, content)
            done.append(e.employee_code)
        except ValueError as exc:
            bad.append(f"{name}: {exc}")
    db.commit()
    return {"saved": len(done), "unmatched": unmatched, "bad": bad}
