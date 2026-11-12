#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." >/dev/null 2>&1 && pwd)"
WHEEL_DIR="$REPO_DIR/vendor/wheels"

mkdir -p "$WHEEL_DIR"

if ! command -v uv >/dev/null 2>&1; then
  echo "Error: 'uv' is required to build the offline wheel bundle." >&2
  exit 1
fi

TMP_REQ="$(mktemp)"
trap 'rm -f "$TMP_REQ"' EXIT

uv export --project "$REPO_DIR" --no-dev --no-hashes --format requirements-txt > "$TMP_REQ"
uv build --project "$REPO_DIR" --wheel --out-dir "$WHEEL_DIR" --quiet

python3 -m pip download \
  --dest "$WHEEL_DIR" \
  --only-binary=:all: \
  -r "$TMP_REQ"

echo "Offline wheels saved to: $WHEEL_DIR"
