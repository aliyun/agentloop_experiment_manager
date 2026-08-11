from __future__ import annotations

import os
import runpy
import subprocess
import tarfile
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.cli import _url_host, build_parser, load_or_create_master_key, main, runtime_settings
from app.config import Settings
from app.main import create_app
from app.runtime import build_container
from app.secrets import MemorySecretStore
from app.storage import in_memory_database


def test_master_key_is_created_privately_and_reused(tmp_path: Path) -> None:
    first = load_or_create_master_key(tmp_path, None)
    second = load_or_create_master_key(tmp_path, None)

    assert first == second
    Fernet(first.encode("ascii"))
    assert (tmp_path / "master.key").stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o777 == 0o700


def test_missing_key_never_overwrites_existing_encrypted_credentials(tmp_path: Path) -> None:
    (tmp_path / "secrets.enc").write_bytes(b"encrypted")
    (tmp_path / "secrets.enc").chmod(0o600)

    with pytest.raises(RuntimeError, match="master.key is missing"):
        load_or_create_master_key(tmp_path, None)

    assert not (tmp_path / "master.key").exists()


def test_master_key_bootstrap_rejects_shared_directory_without_chmod(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)

    with pytest.raises(RuntimeError, match="must not be group/world accessible"):
        load_or_create_master_key(shared, None)

    assert shared.stat().st_mode & 0o777 == 0o755
    assert not (shared / "master.key").exists()


def test_master_key_bootstrap_rejects_symlinked_key(tmp_path: Path) -> None:
    target = tmp_path / "outside-key"
    target.write_bytes(Fernet.generate_key())
    target.chmod(0o600)
    key_link = tmp_path / "master.key"
    key_link.symlink_to(target)

    with pytest.raises(RuntimeError, match="master.key must not be a symbolic link"):
        load_or_create_master_key(tmp_path, None)


@pytest.mark.parametrize("configured_key", [None, Fernet.generate_key().decode("ascii")])
def test_master_key_bootstrap_always_rejects_symlinked_secret_store(
    tmp_path: Path,
    configured_key: str | None,
) -> None:
    if configured_key is None:
        (tmp_path / "master.key").write_bytes(Fernet.generate_key() + b"\n")
        (tmp_path / "master.key").chmod(0o600)
    outside = tmp_path / "outside-secret"
    outside.write_bytes(b"not-a-secret-store")
    outside.chmod(0o600)
    (tmp_path / "secrets.enc").symlink_to(outside)

    with pytest.raises(RuntimeError, match="secrets.enc must not be a symbolic link"):
        load_or_create_master_key(tmp_path, configured_key)


def test_runtime_defaults_to_real_gateway_and_scheduler(tmp_path: Path, monkeypatch) -> None:
    for name in tuple(os.environ):
        if name.startswith("AGENTLOOP_LAUNCHER_"):
            monkeypatch.delenv(name)
    args = type(
        "Args",
        (),
        {
            "data_dir": tmp_path,
            "environment": None,
            "host": None,
            "port": None,
            "no_scheduler": False,
        },
    )()

    settings = runtime_settings(args)

    assert settings.gateway_mode == "agentloop"
    assert settings.scheduler_enabled is True
    assert settings.host == "127.0.0.1"
    assert settings.port == 8090
    assert settings.allow_remote_access is False
    assert settings.master_key


def test_runtime_rejects_symlink_data_dir_before_writing(tmp_path: Path, monkeypatch) -> None:
    for name in tuple(os.environ):
        if name.startswith("AGENTLOOP_LAUNCHER_"):
            monkeypatch.delenv(name)
    dedicated = tmp_path / "dedicated"
    dedicated.mkdir(mode=0o700)
    link = tmp_path / "data-link"
    link.symlink_to(dedicated, target_is_directory=True)
    args = type(
        "Args",
        (),
        {
            "data_dir": link,
            "environment": None,
            "host": None,
            "port": None,
            "no_scheduler": False,
        },
    )()

    with pytest.raises(RuntimeError, match="symbolic link"):
        runtime_settings(args)

    assert not (dedicated / "master.key").exists()


@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.10", "::"])
def test_runtime_rejects_non_loopback_without_explicit_opt_in(
    tmp_path: Path,
    monkeypatch,
    host: str,
) -> None:
    for name in tuple(os.environ):
        if name.startswith("AGENTLOOP_LAUNCHER_"):
            monkeypatch.delenv(name)
    args = type(
        "Args",
        (),
        {
            "data_dir": tmp_path / "dedicated",
            "environment": None,
            "host": host,
            "port": None,
            "no_scheduler": False,
            "allow_remote_access": None,
        },
    )()

    with pytest.raises(RuntimeError, match="requires --allow-remote-access"):
        runtime_settings(args)

    assert not (tmp_path / "dedicated" / "master.key").exists()


def test_runtime_remote_opt_in_requires_production_mode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    for name in tuple(os.environ):
        if name.startswith("AGENTLOOP_LAUNCHER_"):
            monkeypatch.delenv(name)
    args = type(
        "Args",
        (),
        {
            "data_dir": tmp_path / "dedicated",
            "environment": None,
            "host": "0.0.0.0",
            "port": None,
            "no_scheduler": False,
            "allow_remote_access": True,
        },
    )()

    with pytest.raises(RuntimeError, match="requires --environment production"):
        runtime_settings(args)

    assert not (tmp_path / "dedicated" / "master.key").exists()


def test_runtime_production_requires_external_master_key(
    tmp_path: Path,
    monkeypatch,
) -> None:
    for name in tuple(os.environ):
        if name.startswith("AGENTLOOP_LAUNCHER_"):
            monkeypatch.delenv(name)
    args = type(
        "Args",
        (),
        {
            "data_dir": tmp_path / "dedicated",
            "environment": "production",
            "host": "0.0.0.0",
            "port": None,
            "no_scheduler": False,
            "allow_remote_access": True,
        },
    )()

    with pytest.raises(RuntimeError, match="requires AGENTLOOP_LAUNCHER_MASTER_KEY"):
        runtime_settings(args)

    assert not (tmp_path / "dedicated" / "master.key").exists()


def test_runtime_production_local_master_key_mode_creates_and_reuses_key(
    tmp_path: Path,
    monkeypatch,
) -> None:
    for name in tuple(os.environ):
        if name.startswith("AGENTLOOP_LAUNCHER_"):
            monkeypatch.delenv(name)
    data_dir = tmp_path / "dedicated"
    args = type(
        "Args",
        (),
        {
            "data_dir": data_dir,
            "environment": "production",
            "host": "0.0.0.0",
            "port": None,
            "no_scheduler": False,
            "allow_remote_access": True,
            "use_local_master_key": True,
        },
    )()

    first = runtime_settings(args)
    second = runtime_settings(args)

    assert first.master_key == second.master_key
    assert first.host == "0.0.0.0"
    assert first.allow_remote_access is True
    assert (data_dir / "master.key").stat().st_mode & 0o777 == 0o600


def test_local_master_key_mode_ignores_inherited_external_value(
    tmp_path: Path,
    monkeypatch,
) -> None:
    for name in tuple(os.environ):
        if name.startswith("AGENTLOOP_LAUNCHER_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("AGENTLOOP_LAUNCHER_MASTER_KEY", "stale-placeholder")
    data_dir = tmp_path / "dedicated"
    args = type(
        "Args",
        (),
        {
            "data_dir": data_dir,
            "environment": "production",
            "host": "0.0.0.0",
            "port": None,
            "no_scheduler": False,
            "allow_remote_access": True,
            "use_local_master_key": True,
        },
    )()

    settings = runtime_settings(args)

    Fernet(settings.master_key.encode("ascii"))
    assert (data_dir / "master.key").is_file()
    assert settings.master_key == (data_dir / "master.key").read_text(encoding="ascii").strip()


def test_runtime_local_master_key_mode_never_replaces_missing_encryption_key(
    tmp_path: Path,
    monkeypatch,
) -> None:
    for name in tuple(os.environ):
        if name.startswith("AGENTLOOP_LAUNCHER_"):
            monkeypatch.delenv(name)
    data_dir = tmp_path / "dedicated"
    data_dir.mkdir(mode=0o700)
    (data_dir / "secrets.enc").write_bytes(b"encrypted")
    (data_dir / "secrets.enc").chmod(0o600)
    args = type(
        "Args",
        (),
        {
            "data_dir": data_dir,
            "environment": "production",
            "host": "0.0.0.0",
            "port": None,
            "no_scheduler": False,
            "allow_remote_access": True,
            "use_local_master_key": True,
        },
    )()

    with pytest.raises(RuntimeError, match="master.key is missing"):
        runtime_settings(args)

    assert not (data_dir / "master.key").exists()


@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.10", "::"])
def test_runtime_accepts_explicit_production_remote_access(
    tmp_path: Path,
    monkeypatch,
    host: str,
) -> None:
    for name in tuple(os.environ):
        if name.startswith("AGENTLOOP_LAUNCHER_"):
            monkeypatch.delenv(name)
    master_key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("AGENTLOOP_LAUNCHER_MASTER_KEY", master_key)
    args = type(
        "Args",
        (),
        {
            "data_dir": tmp_path / "dedicated",
            "environment": "production",
            "host": host,
            "port": None,
            "no_scheduler": False,
            "allow_remote_access": True,
        },
    )()

    settings = runtime_settings(args)

    assert settings.environment == "production"
    assert settings.host == host
    assert settings.allow_remote_access is True
    assert settings.master_key == master_key
    assert not (tmp_path / "dedicated" / "master.key").exists()


def test_remote_access_environment_can_be_overridden_by_cli(
    tmp_path: Path,
    monkeypatch,
) -> None:
    for name in tuple(os.environ):
        if name.startswith("AGENTLOOP_LAUNCHER_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("AGENTLOOP_LAUNCHER_ALLOW_REMOTE_ACCESS", "true")
    monkeypatch.setenv("AGENTLOOP_LAUNCHER_MASTER_KEY", Fernet.generate_key().decode("ascii"))
    args = type(
        "Args",
        (),
        {
            "data_dir": tmp_path / "dedicated",
            "environment": "production",
            "host": "0.0.0.0",
            "port": None,
            "no_scheduler": False,
            "allow_remote_access": False,
        },
    )()

    with pytest.raises(RuntimeError, match="requires --allow-remote-access"):
        runtime_settings(args)


def test_remote_access_parser_and_ipv6_url_formatting() -> None:
    assert build_parser().parse_args(["--allow-remote-access"]).allow_remote_access is True
    assert build_parser().parse_args(["--no-allow-remote-access"]).allow_remote_access is False
    assert build_parser().parse_args(["--use-local-master-key"]).use_local_master_key is True
    assert _url_host("127.0.0.1") == "127.0.0.1"
    assert _url_host("::1") == "[::1]"
    assert _url_host("launcher.internal") == "launcher.internal"


def test_remote_start_script_is_executable_and_uses_safe_local_key_mode() -> None:
    root = Path(__file__).resolve().parents[2]
    script = root / "start-agentloop-remote"

    assert script.stat().st_mode & 0o111
    content = script.read_text(encoding="utf-8")
    assert "--environment production" in content
    assert "--allow-remote-access" in content
    assert "--use-local-master-key" in content
    assert "unset AGENTLOOP_LAUNCHER_MASTER_KEY" in content
    assert '"$@"' in content
    subprocess.run(["bash", "-n", str(script)], check=True)


def test_built_frontend_and_spa_routes_share_the_backend_origin(tmp_path: Path) -> None:
    web_dir = tmp_path / "web"
    (web_dir / "assets").mkdir(parents=True)
    (web_dir / "index.html").write_text("<main>packaged UI</main>", encoding="utf-8")
    (web_dir / "assets" / "app.js").write_text("window.packaged=true", encoding="utf-8")
    settings = Settings(environment="test", gateway_mode="fake", data_dir=tmp_path / "data")
    container = build_container(
        settings,
        database=in_memory_database(),
        secret_store=MemorySecretStore(),
    )

    with TestClient(create_app(settings=settings, container=container, web_dir=web_dir)) as client:
        assert client.get("/").text == "<main>packaged UI</main>"
        for route in (
            "/plans",
            "/schedules",
            "/experiments",
            "/settings",
            "/agent-connections",
        ):
            assert client.get(route).text == "<main>packaged UI</main>"
        assert client.get("/assets/app.js").text == "window.packaged=true"
        assert client.get("/assets/missing.js").status_code == 404
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/does-not-exist").status_code == 404


def test_doctor_is_read_only_and_imports_packaged_runtime(tmp_path: Path, capsys) -> None:
    data_dir = tmp_path / "must-not-be-created"

    assert main(["doctor", "--data-dir", str(data_dir)]) == 0

    assert not data_dir.exists()
    assert "Runtime check: OK" in capsys.readouterr().out


def test_pyinstaller_bundles_the_versioned_dashboard_template_at_its_package_path() -> None:
    root = Path(__file__).resolve().parents[2]
    namespace = runpy.run_path(str(root / "scripts" / "build_native_release.py"))
    options = namespace["pyinstaller_add_data_options"](os.pathsep)

    assert options == [
        "--add-data",
        f"{root / 'backend' / 'app' / 'static'}{os.pathsep}web",
        "--add-data",
        (
            f"{root / 'backend' / 'app' / 'dashboards' / 'templates'}"
            f"{os.pathsep}app/dashboards/templates"
        ),
    ]


def test_release_staging_deduplicates_identical_large_files(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    namespace = runpy.run_path(str(root / "scripts" / "build_native_release.py"))
    stage = tmp_path / "stage"
    stage.mkdir()
    first = stage / "first.so"
    second = stage / "second.so"
    unique = stage / "unique.so"
    first.write_bytes(b"a" * (70 * 1024))
    second.write_bytes(first.read_bytes())
    unique.write_bytes(b"b" * (70 * 1024))

    linked_files, saved_bytes = namespace["deduplicate_identical_files"](stage)

    assert linked_files == 1
    assert saved_bytes == first.stat().st_size
    assert first.stat().st_ino == second.stat().st_ino
    assert first.stat().st_ino != unique.stat().st_ino


@pytest.mark.parametrize("archive_format", ["tar.gz", "tar.xz"])
def test_release_tar_preserves_deduplicated_files_as_hard_links(
    tmp_path: Path,
    archive_format: str,
) -> None:
    root = Path(__file__).resolve().parents[2]
    namespace = runpy.run_path(str(root / "scripts" / "build_native_release.py"))
    stage = tmp_path / "bundle"
    stage.mkdir()
    first = stage / "first.so"
    second = stage / "second.so"
    first.write_bytes(b"same" * (20 * 1024))
    os.link(first, second)
    archive = tmp_path / f"bundle.{archive_format}"
    work_root = tmp_path / "work"
    work_root.mkdir()

    namespace["create_tar_archive"](
        stage,
        bundle_name="bundle",
        archive=archive,
        archive_format=archive_format,
        work_root=work_root,
    )

    with tarfile.open(archive, "r:*") as packaged:
        first_member = packaged.getmember("bundle/first.so")
        second_member = packaged.getmember("bundle/second.so")
    assert not first_member.islnk()
    assert second_member.islnk()
    assert second_member.linkname == "bundle/first.so"
