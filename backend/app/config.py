from __future__ import annotations

import os
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENTLOOP_LAUNCHER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["development", "test", "production"] = "development"
    host: str = "127.0.0.1"
    port: int = 8090
    allow_remote_access: bool = False
    data_dir: Path = Field(default=Path("./var"))
    gateway_mode: Literal["fake", "disabled", "agentloop"] = "disabled"
    master_key: str | None = None
    console_base_url: str | None = None
    scheduler_poll_seconds: float = 5.0
    scheduler_enabled: bool = False
    agent_task_timeout_seconds: float = Field(default=300.0, gt=0, le=3600)
    trace_lookup_timeout_seconds: float = Field(default=180.0, gt=60, le=1800)
    api_user_agent: str = "agentloop-local-experiment-launcher/0.1.0"

    @property
    def secret_file(self) -> Path:
        return self.data_dir / "secrets.enc"

    def prepare_directories(self) -> None:
        ensure_private_data_dir(self.data_dir)


def ensure_private_data_dir(path: Path) -> Path:
    """Create a dedicated private directory without mutating an existing directory."""

    expanded = path.expanduser()
    if expanded.is_symlink():
        raise RuntimeError(f"Launcher data directory must not be a symbolic link: {path}")
    resolved = expanded.resolve()
    unsafe_directories = {
        Path("/").resolve(),
        Path.home().resolve(),
        Path(tempfile.gettempdir()).resolve(),
    }
    if resolved in unsafe_directories:
        raise RuntimeError(f"Launcher requires a dedicated application data directory: {path}")

    if resolved.exists():
        if not resolved.is_dir():
            raise RuntimeError(f"Launcher data path is not a directory: {path}")
        stat = resolved.stat()
        if hasattr(os, "getuid") and stat.st_uid != os.getuid():
            raise RuntimeError(f"Launcher data directory is owned by another user: {path}")
        if stat.st_mode & 0o077:
            raise RuntimeError(
                f"Launcher data directory must not be group/world accessible: {path}"
            )
        return resolved
    resolved.mkdir(mode=0o700, parents=True)
    resolved.chmod(0o700)
    return resolved


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
