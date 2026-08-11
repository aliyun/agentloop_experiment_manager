#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"

exec uv run --project "$PROJECT_DIR/backend" --frozen --python 3.12 \
  python "$PROJECT_DIR/scripts/build_source_release.py" "$@"
