from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.domain.instance_lock import InstanceAlreadyRunningError, InstanceFileLock
from app.main import _is_loopback_origin


def test_database_url_environment_override_is_not_a_supported_setting(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AGENTLOOP_LAUNCHER_DATABASE_URL", "sqlite:////tmp/unsafe.db")
    settings = Settings(data_dir=tmp_path)
    assert not hasattr(settings, "database_url")


def test_trace_lookup_timeout_defaults_to_180_and_supports_environment_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
    assert Settings(data_dir=tmp_path).trace_lookup_timeout_seconds == 180

    monkeypatch.setenv("AGENTLOOP_LAUNCHER_TRACE_LOOKUP_TIMEOUT_SECONDS", "321")
    assert Settings(data_dir=tmp_path).trace_lookup_timeout_seconds == 321


def test_remote_access_defaults_off_and_supports_environment_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
    assert Settings(data_dir=tmp_path).allow_remote_access is False

    monkeypatch.setenv("AGENTLOOP_LAUNCHER_ALLOW_REMOTE_ACCESS", "true")
    assert Settings(data_dir=tmp_path).allow_remote_access is True


@pytest.mark.parametrize("timeout", [60, 1801])
def test_trace_lookup_timeout_rejects_out_of_range_values(
    tmp_path: Path,
    timeout: int,
) -> None:
    with pytest.raises(ValueError):
        Settings(data_dir=tmp_path, trace_lookup_timeout_seconds=timeout)


def test_prepare_directories_uses_private_mode(tmp_path: Path) -> None:
    data_dir = tmp_path / "private"
    settings = Settings(data_dir=data_dir)
    settings.prepare_directories()
    assert data_dir.stat().st_mode & 0o777 == 0o700


def test_prepare_directories_rejects_shared_existing_directory_without_mutating_it(
    tmp_path: Path,
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)

    with pytest.raises(RuntimeError, match="must not be group/world accessible"):
        Settings(data_dir=shared).prepare_directories()

    assert shared.stat().st_mode & 0o777 == 0o755


def test_prepare_directories_rejects_broad_or_symlink_directories(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="dedicated application data directory"):
        Settings(data_dir=Path.home()).prepare_directories()

    dedicated = tmp_path / "dedicated"
    dedicated.mkdir(mode=0o700)
    link = tmp_path / "data-link"
    link.symlink_to(dedicated, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symbolic link"):
        Settings(data_dir=link).prepare_directories()


def test_instance_file_lock_rejects_a_second_process_contract(tmp_path: Path) -> None:
    first = InstanceFileLock(tmp_path / "app.lock")
    second = InstanceFileLock(tmp_path / "app.lock")
    first.acquire()
    try:
        with pytest.raises(InstanceAlreadyRunningError):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


def test_development_origin_guard_only_accepts_loopback() -> None:
    assert _is_loopback_origin("http://127.0.0.1:4173") is True
    assert _is_loopback_origin("http://[::1]:8090") is True
    assert _is_loopback_origin("https://example.com") is False
    assert _is_loopback_origin("null") is False
