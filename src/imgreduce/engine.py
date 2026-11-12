"""Core hybrid Pillow + macOS native sips compression engine."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Final

from PIL import Image

from imgreduce.presets import ReduceOptions, ReduceResult
from imgreduce.security import (
    UnsafeImageError,
    detect_image_format,
    extract_safe_save_kwargs,
    safe_open_pillow,
    validate_input_path,
    validate_output_path,
)

SIPS_BIN: Final[str] = "/usr/bin/sips"

FORMAT_EXTENSIONS: Final[dict[str, str]] = {
    "jpeg": ".jpg",
    "png": ".png",
    "webp": ".webp",
    "heic": ".heic",
}

TARGET_FORMATS: Final[frozenset[str]] = frozenset(FORMAT_EXTENSIONS)


def resolve_target_format(src_format: str, requested_format: str) -> str:
    """Resolve output image format from source format and CLI option."""
    if requested_format != "keep":
        if requested_format not in TARGET_FORMATS:
            raise ValueError(f"Unsupported output format: {requested_format}")
        return requested_format

    if src_format in TARGET_FORMATS:
        return src_format
    # Uncompressed legacy raster formats (tiff, bmp) shrink best as lossless PNG.
    return "png"


def build_destination_path(
    src: Path,
    src_root: Path | None,
    out_root: Path,
    target_format: str,
    in_place: bool,
) -> Path:
    """Compute and validate destination file path inside out_root."""
    ext = FORMAT_EXTENSIONS[target_format]
    if in_place:
        candidate = src.with_suffix(ext)
        return validate_output_path(candidate, src.parent)

    if src_root is not None:
        rel = src.relative_to(src_root)
        candidate = (out_root / rel).with_suffix(ext)
    else:
        candidate = out_root / f"{src.stem}{ext}"

    return validate_output_path(candidate, out_root)


def _resize_if_needed(img: Image.Image, max_dim: int | None) -> Image.Image:
    """Downscale image preserving aspect ratio when larger than max_dim."""
    if max_dim is None or max_dim <= 0:
        return img
    width, height = img.size
    longest = max(width, height)
    if longest <= max_dim:
        return img

    scale = max_dim / float(longest)
    new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return img.resize(new_size, Image.Resampling.LANCZOS)


def _prepare_jpeg_image(img: Image.Image) -> Image.Image:
    """Flatten alpha channels onto white and normalize mode for JPEG encoding."""
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    if img.mode not in ("RGB", "L"):
        return img.convert("RGB")
    return img


def _quantize_png(img: Image.Image, colors: int) -> Image.Image:
    """Quantize PNG cleanly (MEDIANCUT for RGB, FASTOCTREE for RGBA alpha)."""
    if img.mode == "P":
        return img
    if img.mode in ("RGBA", "LA"):
        rgba = img.convert("RGBA")
        return rgba.quantize(colors=colors, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.FLOYDSTEINBERG)
    rgb = img.convert("RGB") if img.mode != "RGB" else img
    return rgb.quantize(colors=colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.FLOYDSTEINBERG)


def _has_sips() -> bool:
    """Check whether macOS native sips is available."""
    return os.path.isfile(SIPS_BIN) and os.access(SIPS_BIN, os.X_OK)


def _load_image(src: Path, src_format: str) -> Image.Image:
    """Load any supported image (via Pillow or macOS native sips for HEIC)."""
    if src_format != "heic":
        return safe_open_pillow(src)

    if not _has_sips():
        raise UnsafeImageError("HEIC decoding requires macOS /usr/bin/sips.")

    with tempfile.TemporaryDirectory(prefix="imgreduce_heic_") as tmp:
        tmp_png = Path(tmp) / "decoded.png"
        proc = subprocess.run(
            [SIPS_BIN, "-s", "format", "png", str(src), "--out", str(tmp_png)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0 or not tmp_png.exists():
            raise UnsafeImageError(f"Failed to decode HEIC {src.name}: {proc.stderr.strip()}")
        return safe_open_pillow(tmp_png)


def _encode_bytes(
    img: Image.Image,
    target_format: str,
    quality: int,
    options: ReduceOptions,
    save_kwargs: dict[str, bytes],
    force_png_quantize: bool = False,
) -> bytes:
    """Encode a PIL Image into compressed bytes for the target format."""
    if target_format == "heic":
        if not _has_sips():
            raise UnsafeImageError("HEIC encoding requires macOS /usr/bin/sips.")
        with tempfile.TemporaryDirectory(prefix="imgreduce_enc_") as tmp:
            tmp_in = Path(tmp) / "source.png"
            tmp_out = Path(tmp) / "output.heic"
            img.save(tmp_in, format="PNG", compress_level=1, **save_kwargs)
            proc = subprocess.run(
                [
                    SIPS_BIN,
                    "-s",
                    "format",
                    "heic",
                    "-s",
                    "formatOptions",
                    str(max(1, min(100, quality))),
                    str(tmp_in),
                    "--out",
                    str(tmp_out),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0 or not tmp_out.exists():
                raise UnsafeImageError(f"HEIC encoding failed: {proc.stderr.strip()}")
            return tmp_out.read_bytes()

    buf = io.BytesIO()
    if target_format == "jpeg":
        jpeg_img = _prepare_jpeg_image(img)
        if quality >= 88:
            subsampling = 0
        elif quality >= 74:
            subsampling = 1
        else:
            subsampling = 2
        jpeg_img.save(
            buf,
            format="JPEG",
            quality=max(1, min(95, quality)),
            optimize=True,
            progressive=True,
            subsampling=subsampling,
            **save_kwargs,
        )
    elif target_format == "png":
        use_quantize = force_png_quantize or (
            not options.preset.lossless and options.preset.png_quantize
        )
        png_img = _quantize_png(img, options.preset.png_colors) if use_quantize else img
        png_img.save(buf, format="PNG", optimize=True, compress_level=9, **save_kwargs)
    elif target_format == "webp":
        webp_img = img if img.mode in ("RGB", "RGBA", "L") else img.convert("RGBA")
        webp_img.save(
            buf,
            format="WEBP",
            quality=max(1, min(100, quality)),
            lossless=options.preset.lossless and options.quality is None and options.target_kb is None,
            method=6,
            sharp_yuv=True,
            **save_kwargs,
        )
    else:
        raise ValueError(f"Unsupported target format: {target_format}")

    return buf.getvalue()


def _initial_quality(target_format: str, options: ReduceOptions) -> int:
    """Resolve starting quality from CLI override or preset defaults."""
    if options.quality is not None:
        return max(1, min(100, options.quality))
    if target_format == "jpeg":
        return options.preset.jpeg_quality
    if target_format == "webp":
        return options.preset.webp_quality
    if target_format == "heic":
        return options.preset.heic_quality
    return 85


def _downscale_until_budget(
    img: Image.Image,
    attempt_fn,
    max_steps: int = 12,
    min_dim: int = 128,
) -> tuple[bytes, str, tuple[int, int]]:
    """Iteratively downscale image gently by 0.88x until attempt_fn meets byte budget."""
    encoded, label, met = attempt_fn(img)
    working_img = img
    for _ in range(max_steps):
        if met:
            break
        longest = max(working_img.size)
        if longest <= min_dim:
            break
        next_dim = max(min_dim, int(longest * 0.88))
        if next_dim >= longest:
            break
        working_img = _resize_if_needed(working_img, next_dim)
        encoded, label, met = attempt_fn(working_img)
    return encoded, label, working_img.size


def _encode_to_target_kb(
    img: Image.Image,
    target_format: str,
    options: ReduceOptions,
    save_kwargs: dict[str, bytes],
) -> tuple[bytes, str, tuple[int, int]]:
    """Binary search quality (and downscale if required) to meet target_kb."""
    assert options.target_kb is not None
    budget_bytes = options.target_kb * 1024

    if target_format == "png":
        lossless_bytes = _encode_bytes(img, "png", 95, options, save_kwargs, force_png_quantize=False)
        if len(lossless_bytes) <= budget_bytes:
            return lossless_bytes, "lossless", img.size

        def attempt_png(working_img: Image.Image) -> tuple[bytes, str, bool]:
            data = _encode_bytes(working_img, "png", 80, options, save_kwargs, force_png_quantize=True)
            return data, f"{options.preset.png_colors}c", len(data) <= budget_bytes

        return _downscale_until_budget(img, attempt_png)

    def search_quality(working_img: Image.Image) -> tuple[bytes, str, bool]:
        low, high = 45, 95
        best_under: tuple[bytes, int] | None = None
        smallest: tuple[bytes, int] | None = None
        for _ in range(7):
            mid = (low + high) // 2
            data = _encode_bytes(working_img, target_format, mid, options, save_kwargs)
            if smallest is None or len(data) < len(smallest[0]):
                smallest = (data, mid)
            if len(data) <= budget_bytes:
                best_under = (data, mid)
                low = mid + 1
            else:
                high = mid - 1
            if low > high:
                break
        if best_under is not None:
            return best_under[0], f"q={best_under[1]}", True
        assert smallest is not None
        return smallest[0], f"q={smallest[1]}", False

    return _downscale_until_budget(img, search_quality)


def _atomic_write_bytes(dst: Path, data: bytes) -> None:
    """Write output atomically so interrupted runs never leave partial files."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".imgreduce_", dir=str(dst.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_path, dst)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _atomic_copy(src: Path, dst: Path) -> None:
    """Copy original file atomically when it is already smaller than re-encoding."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".imgreduce_", dir=str(dst.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        shutil.copy2(src, tmp_path)
        os.replace(tmp_path, dst)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def compress_image(
    src: Path,
    out_root: Path,
    options: ReduceOptions,
    src_root: Path | None = None,
) -> ReduceResult:
    """Validate, optimize, and safely write a single image."""
    try:
        safe_src = validate_input_path(src, allowed_root=src_root)
        src_format = detect_image_format(safe_src)
        target_format = resolve_target_format(src_format, options.output_format)
        dst = build_destination_path(safe_src, src_root, out_root, target_format, options.in_place)
        original_bytes = safe_src.stat().st_size

        effective_max_dim = options.max_dim if options.max_dim is not None else options.preset.max_dim

        if options.dry_run:
            return ReduceResult(
                source=safe_src,
                destination=dst,
                source_format=src_format,
                target_format=target_format,
                original_bytes=original_bytes,
                reduced_bytes=original_bytes,
                status="dry-run",
                detail="preview only",
            )

        img = _load_image(safe_src, src_format)
        orig_size = img.size
        save_kwargs = extract_safe_save_kwargs(img, keep_metadata=options.keep_metadata)
        resized = _resize_if_needed(img, effective_max_dim)
        was_resized = resized.size != orig_size

        if options.target_kb is not None:
            encoded, quality_label, out_size = _encode_to_target_kb(resized, target_format, options, save_kwargs)
        else:
            quality = _initial_quality(target_format, options)
            encoded = _encode_bytes(resized, target_format, quality, options, save_kwargs)
            out_size = resized.size
            if target_format == "png":
                use_quant = not options.preset.lossless and options.preset.png_quantize
                quality_label = f"{options.preset.png_colors}c" if use_quant else "lossless"
            elif target_format == "webp" and options.preset.lossless and options.quality is None:
                quality_label = "lossless"
            else:
                quality_label = f"q={quality}"

        dim_str = (
            f"{orig_size[0]}x{orig_size[1]}"
            if out_size == orig_size
            else f"{orig_size[0]}x{orig_size[1]}->{out_size[0]}x{out_size[1]}"
        )

        same_format = src_format == target_format
        if same_format and not was_resized and options.target_kb is None and len(encoded) >= original_bytes:
            if not options.in_place:
                _atomic_copy(safe_src, dst)
            return ReduceResult(
                source=safe_src,
                destination=dst,
                source_format=src_format,
                target_format=target_format,
                original_bytes=original_bytes,
                reduced_bytes=original_bytes,
                status="kept-original",
                detail="original already optimal",
                quality_label=quality_label,
                dimensions=f"{orig_size[0]}x{orig_size[1]}",
            )

        _atomic_write_bytes(dst, encoded)
        if options.in_place and safe_src != dst and dst.exists():
            safe_src.unlink()

        return ReduceResult(
            source=safe_src,
            destination=dst,
            source_format=src_format,
            target_format=target_format,
            original_bytes=original_bytes,
            reduced_bytes=len(encoded),
            status="compressed",
            quality_label=quality_label,
            dimensions=dim_str,
        )
    except Exception as exc:
        return ReduceResult(
            source=src,
            destination=src,
            source_format="unknown",
            target_format="unknown",
            original_bytes=src.stat().st_size if src.exists() and src.is_file() else 0,
            reduced_bytes=0,
            status="error",
            detail=str(exc),
        )
