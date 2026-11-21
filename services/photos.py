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


def clean_signature(content: bytes, max_size=(700, 240)):
    """Signature photo / scan -> PNG with a transparent background (paper removed), trimmed and resized."""
    from PIL import Image, ImageOps
    if not content:
        raise ValueError("empty signature file")
    if len(content) > MAX_BYTES:
        raise ValueError("signature file is larger than 15 MB")
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(content)))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("this file is not an image (use JPG or PNG)") from exc
    gray = img.convert("L")
    if img.mode in ("RGBA", "LA") and img.getchannel("A").getextrema()[0] < 250:
        alpha = img.getchannel("A")
    else:      # ink = darker than the paper
        lo, hi = gray.getextrema()
        cut = lo + (hi - lo) * 0.55
        alpha = gray.point(lambda v: 255 if v < cut else 0)
    box = alpha.getbbox()
    if not box:
        raise ValueError("no signature found in the image - use a clear photo or scan")
    ink = Image.new("RGBA", img.size, (16, 24, 64, 0))
    ink.putalpha(alpha)
    ink = ink.crop(box)
    ink.thumbnail(max_size)
    buf = io.BytesIO()
    ink.save(buf, "PNG")
    return buf.getvalue()


def save_signature(emp_id: int, content: bytes) -> str:
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    name = f"sig_{emp_id}.png"
    (PHOTO_DIR / name).write_bytes(clean_signature(content))
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
        is_sig = False
        for suffix in ("_signature", "-signature", " signature", "_sign", "-sign", " sign", "_sig", "-sig"):
            if stem.endswith(suffix):        # 12127_sign.png = signature of 12127
                stem, is_sig = stem[: -len(suffix)].strip(), True
                break
        e = by_code.get(stem) or by_card.get(stem)
        if e is None or (only_company_id and e.company_id != only_company_id):
            unmatched.append(name)
            continue
        try:
            if is_sig:
                e.signature = save_signature(e.id, content)
            else:
                e.photo = save_photo(e.id, content)
            done.append(e.employee_code)
        except ValueError as exc:
            bad.append(f"{name}: {exc}")
    db.commit()
    return {"saved": len(done), "unmatched": unmatched, "bad": bad}
