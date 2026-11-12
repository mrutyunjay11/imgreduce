"""Parallel Apple Silicon batch discovery and execution."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from imgreduce.engine import compress_image
from imgreduce.presets import ReduceOptions, ReduceResult
from imgreduce.security import SUPPORTED_EXTENSIONS


def default_worker_count() -> int:
    """Use up to 6 workers on Apple M2 (8 cores) to keep macOS responsive."""
    cores = os.cpu_count() or 4
    return max(1, min(6, cores))


def discover_inputs(
    inputs: list[Path],
    recursive: bool,
    out_root: Path | None = None,
) -> list[tuple[Path, Path | None]]:
    """Collect image files alongside their scan root (for relative tree preservation)."""
    discovered: list[tuple[Path, Path | None]] = []
    seen: set[Path] = set()
    excluded_resolved = out_root.resolve(strict=False) if out_root is not None else None

    for raw_path in inputs:
        path = raw_path.expanduser()
        if not path.exists():
            discovered.append((path, None))
            continue

        if path.is_file():
            resolved = path.resolve(strict=False)
            if resolved not in seen:
                seen.add(resolved)
                discovered.append((path, None))
            continue

        if path.is_dir():
            dir_resolved = path.resolve(strict=True)
            iterator = dir_resolved.rglob("*") if recursive else dir_resolved.iterdir()
            for item in sorted(iterator):
                if item.is_symlink():
                    # Symlink target safety is enforced in validate_input_path against dir_resolved.
                    pass
                if not item.is_file():
                    continue
                if item.suffix.lower() not in SUPPORTED_EXTENSIONS:
                    continue
                item_resolved = item.resolve(strict=False)
                if excluded_resolved is not None and item_resolved.is_relative_to(excluded_resolved):
                    continue
                if item_resolved not in seen:
                    seen.add(item_resolved)
                    discovered.append((item, dir_resolved))

    return discovered


def run_batch(
    items: list[tuple[Path, Path | None]],
    out_root: Path,
    options: ReduceOptions,
    workers: int | None = None,
) -> list[ReduceResult]:
    """Process discovered image items concurrently and preserve input ordering."""
    if not items:
        return []

    worker_count = max(1, workers if workers is not None else default_worker_count())
    if worker_count == 1 or len(items) == 1:
        return [compress_image(src, out_root, options, src_root=root) for src, root in items]

    results: list[ReduceResult | None] = [None] * len(items)
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        future_map = {
            pool.submit(compress_image, src, out_root, options, root): idx
            for idx, (src, root) in enumerate(items)
        }
        for future in as_completed(future_map):
            idx = future_map[future]
            results[idx] = future.result()

    return [res for res in results if res is not None]

