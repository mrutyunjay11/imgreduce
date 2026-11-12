"""Compression presets and configuration dataclasses."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final


@dataclass(frozen=True, slots=True)
class PresetConfig:
    """Tuning parameters for a named compression preset."""

    name: str
    jpeg_quality: int
    webp_quality: int
    heic_quality: int
    png_quantize: bool
    png_colors: int
    max_dim: int | None
    lossless: bool


PRESETS: Final[dict[str, PresetConfig]] = {
    "balanced": PresetConfig(
        name="balanced",
        jpeg_quality=86,
        webp_quality=85,
        heic_quality=80,
        png_quantize=False,
        png_colors=256,
        max_dim=None,
        lossless=False,
    ),
    "web": PresetConfig(
        name="web",
        jpeg_quality=80,
        webp_quality=80,
        heic_quality=75,
        png_quantize=True,
        png_colors=256,
        max_dim=2048,
        lossless=False,
    ),
    "max": PresetConfig(
        name="max",
        jpeg_quality=68,
        webp_quality=68,
        heic_quality=62,
        png_quantize=True,
        png_colors=128,
        max_dim=1600,
        lossless=False,
    ),
    "lossless": PresetConfig(
        name="lossless",
        jpeg_quality=95,
        webp_quality=100,
        heic_quality=95,
        png_quantize=False,
        png_colors=256,
        max_dim=None,
        lossless=True,
    ),
}


@dataclass(frozen=True, slots=True)
class ReduceOptions:
    """Resolved runtime options for image reduction."""

    preset: PresetConfig
    quality: int | None = None
    target_kb: int | None = None
    max_dim: int | None = None
    output_format: str = "keep"  # keep | jpeg | png | webp | heic
    keep_metadata: bool = False
    in_place: bool = False
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class ReduceResult:
    """Outcome summary for a single processed image."""

    source: Path
    destination: Path
    source_format: str
    target_format: str
    original_bytes: int
    reduced_bytes: int
    status: str  # compressed | kept-original | dry-run | error
    detail: str = ""
    quality_label: str = ""
    dimensions: str = ""

    @property
    def saved_bytes(self) -> int:
        return max(0, self.original_bytes - self.reduced_bytes)

    @property
    def saved_percent(self) -> float:
        if self.original_bytes <= 0:
            return 0.0
        return (self.original_bytes - self.reduced_bytes) / self.original_bytes * 100.0

