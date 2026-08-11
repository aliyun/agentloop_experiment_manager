from __future__ import annotations

from contextlib import asynccontextmanager
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.api.routes import router
from app.config import Settings, get_settings
from app.domain.errors import LauncherError
from app.runtime import AppContainer, build_container
from app.web import SPAStaticFiles, resolve_web_dir


def create_app(
    *,
    settings: Settings | None = None,
    container: AppContainer | None = None,
    web_dir: Path | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        active_container = container or build_container(resolved_settings)
        app.state.container = active_container
        await active_container.start()
        try:
            yield
        finally:
            await active_container.close()

    app = FastAPI(
        title="AgentLoop Local Experiment Launcher",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.container = container
    if resolved_settings.environment != "production":
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[
                "http://127.0.0.1:4173",
                "http://localhost:4173",
                "http://127.0.0.1:5173",
                "http://localhost:5173",
            ],
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Content-Type", "Idempotency-Key"],
        )

    if resolved_settings.environment == "development":

        @app.middleware("http")
        async def enforce_local_development_boundary(request: Request, call_next):
            client_host = request.client.host if request.client else None
            if not _is_loopback_host(client_host):
                return JSONResponse(
                    status_code=403,
                    content={"error": {"code": "LOCAL_ACCESS_ONLY", "message": "Forbidden"}},
                )
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                origin = request.headers.get("origin")
                if origin and not _is_loopback_origin(origin):
                    return JSONResponse(
                        status_code=403,
                        content={
                            "error": {
                                "code": "UNTRUSTED_ORIGIN",
                                "message": "Forbidden",
                            }
                        },
                    )
            return await call_next(request)

    @app.exception_handler(LauncherError)
    async def launcher_error_handler(request: Request, exc: LauncherError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "launchId": exc.launch_id,
                    "retryable": exc.retryable,
                    "requestId": exc.request_id,
                    "line": exc.line,
                    "column": exc.column,
                }
            },
        )

    app.include_router(router)
    resolved_web_dir = web_dir if web_dir is not None else resolve_web_dir()
    if resolved_web_dir is not None:
        app.mount(
            "/",
            SPAStaticFiles(directory=resolved_web_dir, html=True),
            name="frontend",
        )
    return app


def _is_loopback_host(value: str | None) -> bool:
    if value is None:
        return False
    if value.casefold() == "localhost":
        return True
    try:
        return ip_address(value).is_loopback
    except ValueError:
        return False


def _is_loopback_origin(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme in {"http", "https"} and _is_loopback_host(parsed.hostname)


app = create_app()
