#!/usr/bin/env python3
"""Build a secret-free source handoff archive for the offline experiment launcher."""

from __future__ import annotations

import argparse
import hashlib
import math
import re
import shutil
import tarfile
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "release"
APP_NAME = "agentloop-local-experiment-launcher"

SOURCE_ENTRIES = (
    ".gitignore",
    "README.md",
    "SPEC.md",
    "SDK_CONTRACT.md",
    "agentloop-launcher",
    "start-agentloop-remote",
    "backend",
    "docs",
    "examples",
    "frontend",
    "scripts",
    "sdk",
    "third_party",
)

EXCLUDED_DIR_NAMES = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "dist",
    "htmlcov",
    "node_modules",
    "release",
    "var",
}
EXCLUDED_FILE_NAMES = {
    ".DS_Store",
    ".coverage",
    ".env",
    "master.key",
    "secrets.enc",
    "vite.config.d.ts",
    "vite.config.js",
}
EXCLUDED_SUFFIXES = {
    ".db",
    ".db-shm",
    ".db-wal",
    ".log",
    ".map",
    ".pyc",
    ".pyo",
    ".tsbuildinfo",
}

HIGH_CONFIDENCE_PATTERNS = {
    "Alibaba Cloud AccessKey ID": re.compile(rb"LTAI[A-Za-z0-9]{12,}"),
    "private key": re.compile(
        rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
    ),
}
ASSIGNED_CREDENTIAL = re.compile(
    r"(?ix)"
    r"(?:access[_-]?key[_-]?(?:id|secret)|access[_-]?id|access[_-]?secret|"
    r"aliyun[_-]?(?:ak|sk)|agentloop[_-]?(?:ak|sk)|\bak\b|\bsk\b)"
    r"[\"']?\s*[:=]\s*[\"']([^\"'\r\n]{12,})[\"']"
)
SAFE_VALUE_MARKERS = (
    "${",
    "<",
    "dummy",
    "example",
    "must-not-",
    "placeholder",
    "redacted",
    "replace",
    "test-",
    "your-",
)


def project_version() -> str:
    return str(
        tomllib.loads((ROOT / "backend" / "pyproject.toml").read_text(encoding="utf-8"))[
            "project"
        ]["version"]
    )


def is_excluded(relative_path: Path) -> bool:
    if any(part in EXCLUDED_DIR_NAMES for part in relative_path.parts[:-1]):
        return True
    if relative_path.name in EXCLUDED_FILE_NAMES:
        return True
    return any(relative_path.name.endswith(suffix) for suffix in EXCLUDED_SUFFIXES)


def source_files() -> list[tuple[Path, Path]]:
    files: list[tuple[Path, Path]] = []
    for entry_name in SOURCE_ENTRIES:
        entry = ROOT / entry_name
        if not entry.exists():
            raise RuntimeError(f"Required source entry is missing: {entry_name}")
        candidates = (entry,) if entry.is_file() else entry.rglob("*")
        for source in candidates:
            relative = source.relative_to(ROOT)
            if is_excluded(relative) or source.is_dir():
                continue
            if source.is_symlink():
                raise RuntimeError(f"Source package refuses symbolic links: {relative}")
            if not source.is_file():
                raise RuntimeError(f"Unsupported source entry: {relative}")
            files.append((source, relative))
    return sorted(files, key=lambda item: item[1].as_posix())


def shannon_entropy(value: str) -> float:
    counts = Counter(value)
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def scan_file(path: Path) -> list[str]:
    payload = path.read_bytes()
    violations = [name for name, pattern in HIGH_CONFIDENCE_PATTERNS.items() if pattern.search(payload)]
    if b"\x00" in payload:
        return violations
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return violations
    for match in ASSIGNED_CREDENTIAL.finditer(text):
        value = match.group(1).strip()
        lowered = value.casefold()
        if any(marker in lowered for marker in SAFE_VALUE_MARKERS):
            continue
        if len(value) >= 16 and shannon_entropy(value) >= 3.3:
            violations.append("high-entropy credential assignment")
            break
    return violations


def assert_release_hygiene(stage: Path) -> None:
    violations: list[str] = []
    for path in stage.rglob("*"):
        if path.is_dir():
            continue
        relative = path.relative_to(stage)
        if is_excluded(relative):
            violations.append(f"forbidden path: {relative}")
        for reason in scan_file(path):
            violations.append(f"{reason}: {relative}")
    if violations:
        rendered = "; ".join(violations[:10])
        raise RuntimeError(f"Source release hygiene check failed: {rendered}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(output_dir: Path = DEFAULT_OUTPUT_DIR) -> Path:
    version = project_version()
    bundle_name = f"{APP_NAME}-{version}-source"
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"{bundle_name}.tar.gz"
    checksum_path = output_dir / f"{bundle_name}.tar.gz.sha256"

    with tempfile.TemporaryDirectory(prefix=".source-release-", dir=output_dir) as temp_dir:
        stage = Path(temp_dir) / bundle_name
        stage.mkdir()
        files = source_files()
        for source, relative in files:
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

        manifest = stage / "SOURCE_PACKAGE_MANIFEST.txt"
        manifest.write_text(
            "\n".join(
                (
                    f"name={bundle_name}",
                    f"version={version}",
                    f"built_at={datetime.now(UTC).isoformat()}",
                    f"source_files={len(files)}",
                    "credentials_included=false",
                    "runtime_data_included=false",
                    "",
                )
            ),
            encoding="utf-8",
        )
        assert_release_hygiene(stage)

        archive.unlink(missing_ok=True)
        checksum_path.unlink(missing_ok=True)
        with tarfile.open(archive, "w:gz") as package:
            package.add(stage, arcname=bundle_name)

    checksum = sha256(archive)
    checksum_path.write_text(f"{checksum}  {archive.name}\n", encoding="ascii")
    print(archive)
    print(f"sha256={checksum}")
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a secret-free source release")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    build(args.output_dir.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
