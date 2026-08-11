#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[1]
RELEASE_ROOT = ROOT / "release"
APP_NAME = "agentloop-launcher"


@contextmanager
def timed_step(name: str, timings: dict[str, float]) -> Iterator[None]:
    started = time.perf_counter()
    try:
        yield
    finally:
        timings[name] = time.perf_counter() - started


def run(command: list[str], *, cwd: Path = ROOT) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def platform_tag() -> str:
    operating_system = platform.system().casefold()
    machine = platform.machine().casefold()
    supported = {
        ("darwin", "arm64"),
        ("darwin", "x86_64"),
        ("linux", "aarch64"),
        ("linux", "x86_64"),
    }
    if (operating_system, machine) not in supported:
        raise RuntimeError(
            f"Unsupported release target: {operating_system}-{machine}; "
            "build on macOS or Linux."
        )
    return f"{operating_system}-{machine}"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def deduplicate_identical_files(
    root: Path,
    *,
    minimum_size: int = 64 * 1024,
) -> tuple[int, int]:
    """Replace identical staged files with hard links.

    PyInstaller can copy the same native dependency into both ``_internal``
    and a package-specific ``*.libs`` directory.  Both paths are required at
    runtime, but a tar archive only needs to store the bytes once.  Files are
    linked only when their size, mode and SHA-256 digest match.
    """

    candidates: dict[tuple[int, int], list[Path]] = defaultdict(list)
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        metadata = path.stat()
        if metadata.st_size < minimum_size:
            continue
        candidates[(metadata.st_size, metadata.st_mode & 0o7777)].append(path)

    linked_files = 0
    deduplicated_bytes = 0
    for (size, _mode), paths in candidates.items():
        if len(paths) < 2:
            continue
        canonical_by_digest: dict[str, Path] = {}
        for path in paths:
            digest = sha256(path)
            canonical = canonical_by_digest.get(digest)
            if canonical is None:
                canonical_by_digest[digest] = path
                continue
            if canonical.stat().st_ino == path.stat().st_ino:
                continue
            path.unlink()
            try:
                os.link(canonical, path)
            except OSError:
                shutil.copy2(canonical, path)
                continue
            linked_files += 1
            deduplicated_bytes += size
    return linked_files, deduplicated_bytes


def default_archive_format() -> str:
    return "tar.xz" if platform.system().casefold() == "linux" else "tar.gz"


def create_tar_archive(
    stage: Path,
    *,
    bundle_name: str,
    archive: Path,
    archive_format: str,
    work_root: Path,
) -> None:
    if archive_format == "tar.gz":
        with tarfile.open(archive, "w:gz", compresslevel=9) as tar:
            tar.add(stage, arcname=bundle_name)
        return
    if archive_format != "tar.xz":
        raise ValueError(f"Unsupported archive format: {archive_format}")

    xz = shutil.which("xz")
    if not xz:
        with tarfile.open(archive, "w:xz", preset=6) as tar:
            tar.add(stage, arcname=bundle_name)
        return

    raw_archive = work_root / f"{bundle_name}.tar"
    raw_archive.unlink(missing_ok=True)
    try:
        with tarfile.open(raw_archive, "w") as tar:
            tar.add(stage, arcname=bundle_name)
        with archive.open("wb") as output:
            subprocess.run(
                [xz, "-T0", "-6", "-c", str(raw_archive)],
                check=True,
                stdout=output,
            )
    finally:
        raw_archive.unlink(missing_ok=True)


def assert_release_hygiene(stage: Path) -> None:
    allowed_top_level = {
        APP_NAME,
        "start-agentloop-remote",
        "_internal",
        "START_HERE.md",
        "DEPLOYMENT.md",
        "LOCAL_AGENT_INTEGRATION.md",
        "SECONDARY_DEVELOPMENT.md",
        "TRACE_CORRELATION.md",
    }
    unexpected = {path.name for path in stage.iterdir()} - allowed_top_level
    if unexpected:
        raise RuntimeError(
            f"Release contains non-allowlisted top-level entries: {sorted(unexpected)}"
        )
    forbidden_names = {".env", "master.key", "secrets.enc", "launcher.db"}
    forbidden_suffixes = {".map", ".pyc"}
    violations = [
        path.relative_to(stage)
        for path in stage.rglob("*")
        if path.name in forbidden_names or path.suffix in forbidden_suffixes
    ]
    if violations:
        rendered = ", ".join(str(path) for path in violations[:10])
        raise RuntimeError(
            f"Release contains forbidden runtime or source-map files: {rendered}"
        )


def project_version() -> str:
    backend_version = tomllib.loads((ROOT / "backend" / "pyproject.toml").read_text())[
        "project"
    ]["version"]
    frontend_version = json.loads((ROOT / "frontend" / "package.json").read_text())[
        "version"
    ]
    match = re.search(
        r'^__version__\s*=\s*["\']([^"\']+)["\']',
        (ROOT / "backend" / "app" / "__init__.py").read_text(),
        flags=re.MULTILINE,
    )
    app_version = match.group(1) if match else None
    versions = {backend_version, frontend_version, app_version}
    if None in versions or len(versions) != 1:
        raise RuntimeError(
            "Version mismatch between backend pyproject, frontend package, and app.__version__: "
            f"{sorted(str(value) for value in versions)}"
        )
    return str(backend_version)


def pyinstaller_add_data_options(separator: str) -> list[str]:
    """Return the runtime data files that must retain their package paths."""

    return [
        "--add-data",
        f"{ROOT / 'backend' / 'app' / 'static'}{separator}web",
        "--add-data",
        (
            f"{ROOT / 'backend' / 'app' / 'dashboards' / 'templates'}"
            f"{separator}app/dashboards/templates"
        ),
    ]


def build(
    version: str | None = None,
    *,
    codesign_identity: str | None = None,
    notary_profile: str | None = None,
    archive_format: str | None = None,
    incremental: bool = False,
) -> Path:
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError(
            f"Release builder requires Python 3.12, got {sys.version.split()[0]}"
        )
    authoritative_version = project_version()
    if version is not None and version != authoritative_version:
        raise RuntimeError(
            f"Requested release version {version} does not match project {authoritative_version}"
        )
    version = authoritative_version
    tag = platform_tag()
    if notary_profile and platform.system().casefold() != "darwin":
        raise RuntimeError("Apple notarization is only available on macOS")
    if notary_profile and not codesign_identity:
        raise RuntimeError("Apple notarization requires --codesign-identity")
    archive_format = archive_format or default_archive_format()
    if archive_format not in {"tar.gz", "tar.xz"}:
        raise RuntimeError(f"Unsupported archive format: {archive_format}")
    if notary_profile and archive_format != default_archive_format():
        raise RuntimeError("--archive-format cannot be combined with --notary-profile")
    bundle_name = f"{APP_NAME}-{version}-{tag}"
    work_root = RELEASE_ROOT / "work"
    pyinstaller_dist = work_root / "dist"
    stage = RELEASE_ROOT / bundle_name
    archive = RELEASE_ROOT / (
        f"{bundle_name}.zip" if notary_profile else f"{bundle_name}.{archive_format}"
    )
    timings: dict[str, float] = {}

    with timed_step("frontend", timings):
        run(["npm", "ci"], cwd=ROOT / "frontend")
        run(["npm", "run", "build"], cwd=ROOT / "frontend")

    if not incremental and work_root.exists():
        shutil.rmtree(work_root)
    if stage.exists():
        shutil.rmtree(stage)
    archive.unlink(missing_ok=True)
    work_root.mkdir(parents=True, exist_ok=True)

    separator = ";" if os.name == "nt" else ":"
    pyinstaller_command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--onedir",
        "--name",
        APP_NAME,
        "--distpath",
        str(pyinstaller_dist),
        "--workpath",
        str(work_root / "build"),
        "--specpath",
        str(work_root / "spec"),
        "--paths",
        str(ROOT / "backend"),
        *pyinstaller_add_data_options(separator),
        "--collect-all",
        "agentloop_sdk",
        "--collect-all",
        "httpx",
        "--copy-metadata",
        "agentloop-sdk",
        str(ROOT / "backend" / "app" / "cli.py"),
    ]
    if not incremental:
        pyinstaller_command.insert(4, "--clean")
    if codesign_identity:
        pyinstaller_command[-1:-1] = ["--codesign-identity", codesign_identity]
    with timed_step("pyinstaller", timings):
        run(pyinstaller_command)

    generated = pyinstaller_dist / APP_NAME
    generated_entries = {path.name for path in generated.iterdir()}
    expected_generated = {APP_NAME, "_internal"}
    if generated_entries != expected_generated:
        raise RuntimeError(
            f"Unexpected PyInstaller output entries: {sorted(generated_entries)}"
        )
    stage.mkdir(parents=True)
    shutil.copy2(generated / APP_NAME, stage / APP_NAME)
    shutil.copytree(generated / "_internal", stage / "_internal")
    shutil.copy2(ROOT / "start-agentloop-remote", stage / "start-agentloop-remote")
    (stage / "start-agentloop-remote").chmod(0o755)
    shutil.copy2(ROOT / "docs" / "PACKAGED_START.md", stage / "START_HERE.md")
    shutil.copy2(ROOT / "docs" / "DEPLOYMENT.md", stage / "DEPLOYMENT.md")
    shutil.copy2(
        ROOT / "docs" / "LOCAL_AGENT_INTEGRATION.md",
        stage / "LOCAL_AGENT_INTEGRATION.md",
    )
    shutil.copy2(
        ROOT / "docs" / "SECONDARY_DEVELOPMENT.md",
        stage / "SECONDARY_DEVELOPMENT.md",
    )
    shutil.copy2(ROOT / "docs" / "TRACE_CORRELATION.md", stage / "TRACE_CORRELATION.md")
    linked_files, deduplicated_bytes = deduplicate_identical_files(stage)
    print(
        "staged deduplication: "
        f"{linked_files} hard links, {deduplicated_bytes / (1024 * 1024):.2f} MiB"
    )
    assert_release_hygiene(stage)

    with timed_step("archive", timings):
        if notary_profile:
            run(
                [
                    "ditto",
                    "-c",
                    "-k",
                    "--sequesterRsrc",
                    "--keepParent",
                    str(stage),
                    str(archive),
                ]
            )
            run(
                [
                    "xcrun",
                    "notarytool",
                    "submit",
                    str(archive),
                    "--keychain-profile",
                    notary_profile,
                    "--wait",
                ]
            )
        else:
            create_tar_archive(
                stage,
                bundle_name=bundle_name,
                archive=archive,
                archive_format=archive_format,
                work_root=work_root,
            )
    checksum = sha256(archive)
    archive.with_suffix(archive.suffix + ".sha256").write_text(
        f"{checksum}  {archive.name}\n",
        encoding="ascii",
    )
    print(archive)
    print(f"sha256={checksum}")
    print(
        "timings="
        + ", ".join(f"{name}:{duration:.2f}s" for name, duration in timings.items())
    )
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a native AgentLoop Launcher release"
    )
    parser.add_argument("--version", default=None)
    parser.add_argument("--codesign-identity")
    parser.add_argument("--notary-profile")
    parser.add_argument("--archive-format", choices=("tar.gz", "tar.xz"))
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="Reuse PyInstaller work files; formal releases should use the clean default.",
    )
    args = parser.parse_args()
    build(
        args.version,
        codesign_identity=args.codesign_identity,
        notary_profile=args.notary_profile,
        archive_format=args.archive_format,
        incremental=args.incremental,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
