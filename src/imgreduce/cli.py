"""One-command CLI entrypoint for imgreduce."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from imgreduce import __version__
from imgreduce.parallel import default_worker_count, discover_inputs, run_batch
from imgreduce.presets import PRESETS, ReduceOptions, ReduceResult


def _format_bytes(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / (1024 * 1024):.2f} MB"


def _resolve_default_out_root(inputs: list[Path], explicit_out: Path | None) -> Path:
    if explicit_out is not None:
        return explicit_out.expanduser().resolve(strict=False)
    first = inputs[0].expanduser().resolve(strict=False)
    base_dir = first if first.is_dir() else first.parent
    return base_dir / "reduced"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="imgreduce",
        description="100% offline, Apple Silicon-optimized, security-hardened image size reducer.",
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="Image file(s) or directory(ies) to reduce.",
    )
    parser.add_argument(
        "--preset",
        choices=tuple(PRESETS.keys()),
        default="balanced",
        help="Compression preset (default: balanced).",
    )
    parser.add_argument(
        "-q",
        "--quality",
        type=int,
        default=None,
        help="Override quality (1-100).",
    )
    parser.add_argument(
        "--target-kb",
        type=int,
        default=None,
        help="Target maximum file size in KB (binary searches quality/scale).",
    )
    parser.add_argument(
        "--max-dim",
        type=int,
        default=None,
        help="Resize longest dimension to at most N pixels (preserves aspect ratio).",
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=("keep", "jpeg", "png", "webp", "heic"),
        default="keep",
        help="Output image format (default: keep).",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output directory (default: ./reduced/ inside target folder).",
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Overwrite original files atomically (disabled by default for safety).",
    )
    parser.add_argument(
        "--keep-metadata",
        action="store_true",
        help="Preserve EXIF/GPS metadata (stripped by default for privacy).",
    )
    parser.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="Recursively process subdirectories.",
    )
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=default_worker_count(),
        help=f"Parallel worker threads (default: {default_worker_count()}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview files that would be processed without writing output.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output structured JSON report.",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def _print_report(results: list[ReduceResult], elapsed: float, out_root: Path, in_place: bool) -> None:
    total_orig = sum(r.original_bytes for r in results if r.status != "error")
    total_new = sum(r.reduced_bytes for r in results if r.status != "error")
    errors = [r for r in results if r.status == "error"]
    png_photo_hint = False

    for r in results:
        if r.status == "error":
            print(f"  [ERROR] {r.source}: {r.detail}", file=sys.stderr)
            continue
        if r.status == "dry-run":
            print(f"  [DRY]   {r.source.name} -> {r.destination} ({_format_bytes(r.original_bytes)})")
            continue

        meta_parts = [p for p in (r.dimensions, r.quality_label) if p]
        meta_suffix = f" | {' | '.join(meta_parts)}" if meta_parts else ""
        if r.status == "kept-original":
            print(f"  [KEEP]  {r.source.name}: {_format_bytes(r.original_bytes)}{meta_suffix} (already optimal)")
            if r.source_format == "png" and r.target_format == "png":
                png_photo_hint = True
            continue
        if r.source_format == "png" and r.target_format == "png" and r.saved_percent < 15.0:
            png_photo_hint = True
        print(
            f"  [OK]    {r.source.name}: {_format_bytes(r.original_bytes)} -> "
            f"{_format_bytes(r.reduced_bytes)} (-{r.saved_percent:.1f}%){meta_suffix}"
        )

    saved = max(0, total_orig - total_new)
    pct = (saved / total_orig * 100.0) if total_orig > 0 else 0.0
    dest_label = "in-place" if in_place else str(out_root)
    print(
        f"\nProcessed {len(results)} file(s) in {elapsed:.2f}s | "
        f"{_format_bytes(total_orig)} -> {_format_bytes(total_new)} "
        f"(saved {_format_bytes(saved)}, {pct:.1f}%) | Output: {dest_label}"
    )
    if png_photo_hint:
        print(
            "Tip: If a PNG is a camera/portrait photo, add '--format jpeg' or '--format webp' "
            "for much smaller file size and cleaner photo quality."
        )
    if errors:
        print(f"Completed with {len(errors)} error(s).", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.quality is not None and not (1 <= args.quality <= 100):
        parser.error("--quality must be between 1 and 100.")
    if args.target_kb is not None and args.target_kb <= 0:
        parser.error("--target-kb must be greater than 0.")
    if args.max_dim is not None and args.max_dim <= 0:
        parser.error("--max-dim must be greater than 0.")

    out_root = _resolve_default_out_root(args.inputs, args.output)
    options = ReduceOptions(
        preset=PRESETS[args.preset],
        quality=args.quality,
        target_kb=args.target_kb,
        max_dim=args.max_dim,
        output_format=args.format,
        keep_metadata=args.keep_metadata,
        in_place=args.in_place,
        dry_run=args.dry_run,
    )

    items = discover_inputs(
        args.inputs,
        recursive=args.recursive,
        out_root=None if args.in_place else out_root,
    )
    if not items:
        print("No supported images found.", file=sys.stderr)
        return 1

    started = time.perf_counter()
    results = run_batch(items, out_root=out_root, options=options, workers=args.workers)
    elapsed = time.perf_counter() - started

    if args.json:
        payload = {
            "elapsed_seconds": round(elapsed, 4),
            "output_root": "in-place" if args.in_place else str(out_root),
            "results": [
                {
                    "source": str(r.source),
                    "destination": str(r.destination),
                    "source_format": r.source_format,
                    "target_format": r.target_format,
                    "original_bytes": r.original_bytes,
                    "reduced_bytes": r.reduced_bytes,
                    "saved_percent": round(r.saved_percent, 2),
                    "quality": r.quality_label,
                    "dimensions": r.dimensions,
                    "status": r.status,
                    "detail": r.detail,
                }
                for r in results
            ],
        }
        print(json.dumps(payload, indent=2))
    else:
        _print_report(results, elapsed, out_root, args.in_place)

    has_errors = any(r.status == "error" for r in results)
    return 1 if has_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

