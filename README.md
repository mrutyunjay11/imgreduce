# imgreduce

100% offline, security-hardened, Apple Silicon-optimized image size reducer for macOS.

## One-Command Usage (No Manual Repo Install Required)

### Option A: Run directly from GitHub (`uvx`)
Run immediately without cloning or manually installing the repository:

```bash
uvx --from git+https://github.com/mrutyunjay11/imgreduce imgreduce ./photos
```

Once cached on your Mac, run completely offline anytime:

```bash
uvx --offline --from git+https://github.com/mrutyunjay11/imgreduce imgreduce ./photos
```

### Option B: Run locally from this folder (`./reduce`)
All dependencies live strictly inside `.venv`. Run with one command:

```bash
./reduce ./photos
```

## Common Examples

```bash
# Default balanced preset -> writes to ./photos/reduced/
./reduce ./photos

# Aggressive web optimization (max 2048px, quantized PNG, smaller JPEG/WebP)
./reduce ./photos --preset web

# Hit a strict file-size budget (e.g. <= 200 KB per image)
./reduce ./hero.png --target-kb 200

# Convert folder of PNG/HEIC photos to WebP recursively
./reduce ./pictures -r --format webp

# Preview without writing files
./reduce ./photos --dry-run
```

## Offline Portability
On this Mac, `.venv` contains all downloaded packages so `imgreduce` runs **100% offline** with zero network calls.

To copy this repo to another air-gapped Mac without internet:
```bash
./scripts/bundle_wheels.sh
```
This downloads all macOS `arm64` wheels into `vendor/wheels/`, which `./reduce` automatically uses offline when creating `.venv`.

## Security & Privacy Guarantees
- **100% Offline Runtime:** Zero outbound network calls, update checks, or telemetry.
- **Privacy by Default:** Strips EXIF/GPS/camera metadata automatically (opt out with `--keep-metadata`) while preserving upright orientation.
- **Non-Destructive:** Saves outputs to `./reduced/` by default (requires `--in-place` to overwrite originals) using atomic file writes.
- **Hardened Parsing:** Validates magic bytes (never trusts extensions blindly), rejects escaping symlinks/path traversal, enforces `MAX_IMAGE_PIXELS` decompression-bomb caps, and uses Apple's signed `/usr/bin/sips` for HEIC.

