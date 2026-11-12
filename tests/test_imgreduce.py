"""Unit and integration tests for imgreduce."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from imgreduce import security
from imgreduce.cli import main as cli_main
from imgreduce.engine import compress_image
from imgreduce.presets import PRESETS, ReduceOptions


def _make_noisy_image(path: Path, fmt: str, size: tuple[int, int] = (640, 480)) -> Path:
    """Create a deterministic textured image that compresses meaningfully."""
    img = Image.new("RGB", size)
    pixels = img.load()
    assert pixels is not None
    for y in range(size[1]):
        for x in range(size[0]):
            pixels[x, y] = ((x * 7 + y * 3) % 256, (x * 5 + y * 11) % 256, (x * 13 + y * 2) % 256)
    if fmt == "JPEG":
        img.save(path, format=fmt, quality=98)
    elif fmt == "PNG":
        img.save(path, format=fmt, compress_level=1)
    else:
        img.save(path, format=fmt)
    return path


def _make_image_with_exif(path: Path) -> Path:
    """Create a JPEG containing dummy EXIF metadata."""
    img = Image.new("RGB", (400, 300), color=(30, 120, 200))
    exif = Image.Exif()
    exif[0x010F] = "TestCameraMaker"  # Make
    exif[0x0110] = "SecretModel9000"  # Model
    img.save(path, format="JPEG", quality=95, exif=exif.tobytes())
    return path


def test_jpeg_and_png_reduction_to_reduced_folder(tmp_path: Path) -> None:
    src_dir = tmp_path / "photos"
    src_dir.mkdir()
    jpg = _make_noisy_image(src_dir / "sample.jpg", "JPEG")
    # Smooth gradient PNG with low initial compression shrinks via both lossless & quantize
    grad = Image.new("RGB", (640, 480))
    gpix = grad.load()
    assert gpix is not None
    for y in range(480):
        for x in range(640):
            gpix[x, y] = (x % 256, y % 256, (x + y) % 256)
    png = src_dir / "screen.png"
    grad.save(png, format="PNG", compress_level=0)

    exit_code = cli_main([str(src_dir), "--preset", "web"])
    assert exit_code == 0

    out_dir = src_dir / "reduced"
    out_jpg = out_dir / "sample.jpg"
    out_png = out_dir / "screen.png"
    assert out_jpg.exists()
    assert out_png.exists()
    assert out_jpg.stat().st_size < jpg.stat().st_size
    assert out_png.stat().st_size < png.stat().st_size


def test_target_kb_budget_and_max_dim(tmp_path: Path) -> None:
    src = _make_noisy_image(tmp_path / "big.jpg", "JPEG", size=(1200, 800))

    # Max-dim preserves aspect ratio cleanly without target-kb downscaling
    maxdim_dir = tmp_path / "maxdim"
    assert cli_main([str(src), "--max-dim", "600", "-o", str(maxdim_dir)]) == 0
    with Image.open(maxdim_dir / "big.jpg") as im:
        assert im.size == (600, 400)

    # Target-kb strictly meets tight byte budget
    budget_dir = tmp_path / "budget"
    assert cli_main([str(src), "--target-kb", "25", "-o", str(budget_dir)]) == 0
    out_file = budget_dir / "big.jpg"
    assert out_file.exists()
    assert out_file.stat().st_size <= 25 * 1024


def test_webp_conversion(tmp_path: Path) -> None:
    src = _make_noisy_image(tmp_path / "photo.jpg", "JPEG", size=(320, 240))
    out_dir = tmp_path / "webp_out"
    assert cli_main([str(src), "--format", "webp", "-o", str(out_dir)]) == 0
    out_webp = out_dir / "photo.webp"
    assert out_webp.exists()
    assert security.detect_image_format(out_webp) == "webp"


def test_exif_stripped_by_default_and_kept_with_flag(tmp_path: Path) -> None:
    src = _make_image_with_exif(tmp_path / "gps_photo.jpg")

    stripped_dir = tmp_path / "stripped"
    assert cli_main([str(src), "-q", "60", "-o", str(stripped_dir)]) == 0
    with Image.open(stripped_dir / "gps_photo.jpg") as im_stripped:
        assert len(im_stripped.getexif()) == 0

    kept_dir = tmp_path / "kept"
    assert cli_main([str(src), "-q", "60", "--keep-metadata", "-o", str(kept_dir)]) == 0
    with Image.open(kept_dir / "gps_photo.jpg") as im_kept:
        exif = im_kept.getexif()
        assert exif.get(0x010F) == "TestCameraMaker"


def test_rejects_spoofed_extension_magic_bytes(tmp_path: Path) -> None:
    fake = tmp_path / "malicious.jpg"
    fake.write_text("#!/bin/sh\necho not an image\n", encoding="utf-8")

    with pytest.raises(security.UnsafeImageError):
        security.detect_image_format(fake)

    res = compress_image(fake, tmp_path / "reduced", ReduceOptions(preset=PRESETS["balanced"]))
    assert res.status == "error"


def test_rejects_escaping_symlink_and_path_traversal(tmp_path: Path) -> None:
    outside = _make_noisy_image(tmp_path / "secret.jpg", "JPEG", size=(64, 64))
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    link = sandbox / "escape.jpg"
    os.symlink(outside, link)

    with pytest.raises(security.SecurityError):
        security.validate_input_path(link, allowed_root=sandbox)

    with pytest.raises(security.SecurityError):
        security.validate_output_path(sandbox / ".." / "outside.jpg", sandbox)


def test_decompression_bomb_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = _make_noisy_image(tmp_path / "normal.png", "PNG", size=(200, 200))
    monkeypatch.setattr(security, "MAX_IMAGE_PIXELS", 10_000)

    with pytest.raises(security.UnsafeImageError):
        security.safe_open_pillow(src)


def test_never_grow_keeps_original_when_already_optimal(tmp_path: Path) -> None:
    # Already tiny solid-color PNG compressed minimally; re-encoding at q=95 JPEG or lossless shouldn't inflate.
    src = tmp_path / "tiny.jpg"
    Image.new("RGB", (64, 64), (10, 20, 30)).save(src, format="JPEG", quality=20, optimize=True)
    out_dir = tmp_path / "out"

    res = compress_image(src, out_dir, ReduceOptions(preset=PRESETS["lossless"]))
    assert res.status == "kept-original"
    assert res.reduced_bytes == res.original_bytes
    assert (out_dir / "tiny.jpg").read_bytes() == src.read_bytes()


def test_dry_run_and_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = _make_noisy_image(tmp_path / "preview.png", "PNG", size=(120, 120))
    out_dir = tmp_path / "reduced"

    code = cli_main([str(src), "--dry-run", "--json", "-o", str(out_dir)])
    assert code == 0
    assert not out_dir.exists()

    captured = capsys.readouterr().out
    data = json.loads(captured)
    assert data["results"][0]["status"] == "dry-run"
    assert data["results"][0]["source_format"] == "png"


def test_json_includes_quality_and_dimensions(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = _make_noisy_image(tmp_path / "portrait.jpg", "JPEG", size=(800, 600))
    out_dir = tmp_path / "reduced"

    code = cli_main([str(src), "--max-dim", "400", "-q", "75", "--json", "-o", str(out_dir)])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    entry = data["results"][0]
    assert entry["status"] == "compressed"
    assert entry["quality"] == "q=75"
    assert entry["dimensions"] == "800x600->400x300"


@pytest.mark.skipif(not os.path.isfile("/usr/bin/sips"), reason="macOS sips required")
def test_macos_heic_roundtrip_via_sips(tmp_path: Path) -> None:
    src_png = _make_noisy_image(tmp_path / "source.png", "PNG", size=(256, 256))
    src_heic = tmp_path / "photo.heic"
    proc = subprocess.run(
        ["/usr/bin/sips", "-s", "format", "heic", str(src_png), "--out", str(src_heic)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not src_heic.exists():
        pytest.skip("HEIC encoding unavailable in sandboxed sips environment")

    out_dir = tmp_path / "reduced"
    code = cli_main([str(src_heic), "--preset", "web", "-o", str(out_dir)])
    assert code == 0
    out_heic = out_dir / "photo.heic"
    assert out_heic.exists()
    assert security.detect_image_format(out_heic) == "heic"
