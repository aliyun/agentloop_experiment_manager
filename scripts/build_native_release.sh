#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"
if [ "$(uname -s)" = "Darwin" ]; then
  DEFAULT_BUILD_CACHE_DIR="$HOME/Library/Caches/agentloop-launcher-build"
else
  DEFAULT_BUILD_CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/agentloop-launcher-build"
fi
BUILD_CACHE_DIR="${AGENTLOOP_BUILD_CACHE_DIR:-$DEFAULT_BUILD_CACHE_DIR}"
UV_PROJECT_DIR="$BUILD_CACHE_DIR/uv-project"
PYPI_MIRROR_BASE="${AGENTLOOP_PYPI_MIRROR_BASE:-}"

if grep -q 'registry\.anpm\.alibaba-inc\.com' "$PROJECT_DIR/frontend/package-lock.json"; then
  echo "frontend/package-lock.json contains an internal-only npm registry" >&2
  exit 2
fi

mkdir -p "$UV_PROJECT_DIR"
cp "$PROJECT_DIR/backend/pyproject.toml" "$UV_PROJECT_DIR/pyproject.toml"
cp "$PROJECT_DIR/backend/uv.lock" "$UV_PROJECT_DIR/uv.lock"
cp "$PROJECT_DIR/backend/README.md" "$UV_PROJECT_DIR/README.md"

if [ -n "$PYPI_MIRROR_BASE" ]; then
  python3 - "$UV_PROJECT_DIR/uv.lock" "$PYPI_MIRROR_BASE" <<'PY'
from pathlib import Path
import sys
from urllib.parse import urlsplit

lock_path = Path(sys.argv[1])
mirror_base = sys.argv[2].rstrip("/")
parsed = urlsplit(mirror_base)
if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
    raise SystemExit("AGENTLOOP_PYPI_MIRROR_BASE must be an HTTPS base URL")

content = lock_path.read_text(encoding="utf-8")
content = content.replace("https://pypi.org/simple", f"{mirror_base}/simple")
content = content.replace("https://files.pythonhosted.org/", f"{mirror_base}/")
lock_path.write_text(content, encoding="utf-8")
print(f"Python dependency mirror: {mirror_base}")
PY
  export UV_DEFAULT_INDEX="$PYPI_MIRROR_BASE/simple"
fi

export UV_CACHE_DIR="${UV_CACHE_DIR:-$BUILD_CACHE_DIR/uv-cache}"
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$BUILD_CACHE_DIR/python}"

uv sync --project "$UV_PROJECT_DIR" --no-default-groups --group package --frozen \
  --python 3.12 --no-install-project

exec "$UV_PROJECT_DIR/.venv/bin/python" \
  "$PROJECT_DIR/scripts/build_native_release.py" "$@"
