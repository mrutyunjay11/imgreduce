"""Security guards, magic-byte inspection, and safe image loading."""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Final

from PIL import Image, ImageOps

MAX_IMAGE_PIXELS: Final[int] = 178_956_970
MAX_FILE_BYTES: Final[int] = 256 * 1024 * 1024  # 256 MiB
MAX_ICC_BYTES: Final[int] = 64 * 1024

HEIC_BRANDS: Final[frozenset[bytes]] = frozenset(
    {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}
)

SUPPORTED_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".tif", ".tiff", ".bmp"}
)


class SecurityError(ValueError):
    """Raised when an input or output violates safety constraints."""


class UnsafeImageError(SecurityError):
    """Raised when an image fails validation or exceeds decompression limits."""


def detect_image_format(path: Path) -> str:
    """Identify image format from file magic bytes rather than extension."""
    try:
        with path.open("rb") as fh:
            header = fh.read(16)
    except OSError as exc:
        raise UnsafeImageError(f"Cannot read header for {path.name}: {exc}") from exc

    if header.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "webp"
    if len(header) >= 12 and header[4:8] == b"ftyp" and header[8:12] in HEIC_BRANDS:
        return "heic"
    if header[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if header[:2] == b"BM":
        return "bmp"

    raise UnsafeImageError(f"Unsupported or unrecognized image signature: {path.name}")


def validate_input_path(path: Path, allowed_root: Path | None = None) -> Path:
    """Resolve and verify an input file path against traversal, symlinks, and size limits."""
    if not path.exists():
        raise SecurityError(f"Input path does not exist: {path}")

    resolved = path.resolve(strict=True)
    if allowed_root is not None:
        root_resolved = allowed_root.resolve(strict=True)
        if not resolved.is_relative_to(root_resolved):
            raise SecurityError(f"Path escapes allowed root: {path}")

    if not resolved.is_file():
        raise SecurityError(f"Input is not a regular file: {path}")

    size = resolved.stat().st_size
    if size <= 0:
        raise UnsafeImageError(f"Empty file: {resolved.name}")
    if size > MAX_FILE_BYTES:
        raise UnsafeImageError(
            f"File exceeds {MAX_FILE_BYTES // (1024 * 1024)} MiB safety cap: {resolved.name}"
        )

    return resolved


def validate_output_path(out_path: Path, out_root: Path) -> Path:
    """Ensure the resolved output path stays strictly within out_root."""
    root_resolved = out_root.resolve(strict=False)
    candidate_resolved = out_path.resolve(strict=False)
    if not candidate_resolved.is_relative_to(root_resolved):
        raise SecurityError(f"Output path escapes target directory: {out_path}")
    return candidate_resolved


def safe_open_pillow(path: Path) -> Image.Image:
    """Open and pixel-decode an image with decompression-bomb protection and EXIF orientation."""
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            with Image.open(path) as img:
                img.load()
                oriented = ImageOps.exif_transpose(img)
                return oriented.copy()
        except Image.DecompressionBombWarning as exc:
            raise UnsafeImageError(f"Decompression bomb detected in {path.name}") from exc
        except Image.DecompressionBombError as exc:
            raise UnsafeImageError(f"Image exceeds max pixel limit ({MAX_IMAGE_PIXELS}): {path.name}") from exc
        except Exception as exc:
            raise UnsafeImageError(f"Corrupted or unreadable image {path.name}: {exc}") from exc


def extract_safe_save_kwargs(img: Image.Image, keep_metadata: bool) -> dict[str, bytes]:
    """Return safe metadata kwargs; strips EXIF/GPS by default while preserving bounded ICC."""
    kwargs: dict[str, bytes] = {}
    icc = img.info.get("icc_profile")
    if isinstance(icc, (bytes, bytearray)) and len(icc) <= MAX_ICC_BYTES:
        kwargs["icc_profile"] = bytes(icc)

    if keep_metadata:
        exif = img.info.get("exif")
        if isinstance(exif, (bytes, bytearray)):
            kwargs["exif"] = bytes(exif)

    return kwargs

