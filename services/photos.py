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
