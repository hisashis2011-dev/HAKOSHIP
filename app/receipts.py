"""領収書ファイルの検証・保存"""
import hashlib
import io
import uuid
from datetime import date
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .config import UPLOAD_DIR, MAX_UPLOAD_MB

MAX_PX = 2400


class ReceiptError(Exception):
    pass


def save_receipt(data: bytes) -> tuple[str, str, str]:
    """検証して保存し (相対パス, mime, sha256) を返す。

    画像: 向きを補正し長辺2400pxのJPEGに正規化(スマホ写真の容量削減・EXIF位置情報の除去)
    PDF : そのまま保存
    """
    if not data:
        raise ReceiptError("ファイルが空です")
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise ReceiptError(f"ファイルが大きすぎます(上限 {MAX_UPLOAD_MB}MB)")

    if data[:5] == b"%PDF-":
        out, mime, ext = data, "application/pdf", "pdf"
    else:
        try:
            img = Image.open(io.BytesIO(data))
            img.load()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            raise ReceiptError("画像(JPEG/PNG)またはPDFを選択してください")
        img = ImageOps.exif_transpose(img)
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((MAX_PX, MAX_PX))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85, optimize=True)
        out, mime, ext = buf.getvalue(), "image/jpeg", "jpg"

    sub = Path(str(date.today().year)) / f"{date.today().month:02d}"
    (UPLOAD_DIR / sub).mkdir(parents=True, exist_ok=True)
    rel = sub / f"{uuid.uuid4().hex}.{ext}"
    (UPLOAD_DIR / rel).write_bytes(out)
    return str(rel), mime, hashlib.sha256(out).hexdigest()


def delete_receipt(rel: str | None):
    if rel:
        p = (UPLOAD_DIR / rel).resolve()
        if UPLOAD_DIR.resolve() in p.parents and p.exists():
            p.unlink()


def receipt_file(rel: str) -> Path:
    p = (UPLOAD_DIR / rel).resolve()
    if UPLOAD_DIR.resolve() not in p.parents:
        raise ReceiptError("invalid path")
    return p
