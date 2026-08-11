from __future__ import annotations

import os
import sys
from pathlib import Path

from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope


class SPAStaticFiles(StaticFiles):
    """Serve built assets and fall back to index.html for browser routes only."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404 or _must_remain_not_found(path):
                raise
            return await super().get_response("index.html", scope)

        if response.status_code == 404 and not _must_remain_not_found(path):
            return await super().get_response("index.html", scope)
        return response


def resolve_web_dir() -> Path | None:
    """Locate the frontend in an override, native bundle, wheel, or source checkout."""

    override = os.environ.get("AGENTLOOP_LAUNCHER_WEB_DIR")
    if override:
        path = Path(override).expanduser().resolve()
        _validate_web_dir(path)
        return path

    candidates: list[Path] = []
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        candidates.append(Path(bundle_root) / "web")
    candidates.extend((Path(__file__).resolve().parent / "static",))
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate.resolve()
    return None


def require_web_dir() -> Path:
    path = resolve_web_dir()
    if path is None:
        raise RuntimeError(
            "Frontend assets are missing. Run `npm ci && npm run build` in frontend, "
            "or use a packaged release."
        )
    return path


def _validate_web_dir(path: Path) -> None:
    if not path.is_dir() or not (path / "index.html").is_file():
        raise RuntimeError(f"Frontend directory does not contain index.html: {path}")


def _must_remain_not_found(path: str) -> bool:
    normalized = path.lstrip("/")
    return (
        normalized == "api"
        or normalized.startswith("api/")
        or normalized == "assets"
        or normalized.startswith("assets/")
    )
