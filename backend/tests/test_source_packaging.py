from __future__ import annotations

import runpy
from pathlib import Path


def _namespace() -> dict:
    root = Path(__file__).resolve().parents[2]
    return runpy.run_path(str(root / "scripts" / "build_source_release.py"))


def test_source_release_includes_handoff_docs_and_excludes_runtime_data() -> None:
    namespace = _namespace()
    root = namespace["ROOT"]
    relative_paths = {relative.as_posix() for _, relative in namespace["source_files"]()}

    assert "docs/DEPLOYMENT.md" in relative_paths
    assert "docs/SECONDARY_DEVELOPMENT.md" in relative_paths
    assert "backend/.env.example" in relative_paths
    assert "start-agentloop-remote" in relative_paths
    assert "backend/.venv" not in relative_paths
    assert not any("/.git/" in f"/{path}/" for path in relative_paths)
    assert not any("node_modules" in path for path in relative_paths)
    assert not any("__pycache__" in path for path in relative_paths)
    assert not any(path.endswith((".db", ".pyc", ".tsbuildinfo")) for path in relative_paths)
    assert all((root / path).is_file() for path in relative_paths)


def test_source_release_secret_scanner_rejects_access_key_and_private_key(
    tmp_path: Path,
) -> None:
    namespace = _namespace()
    scan_file = namespace["scan_file"]

    access_key = tmp_path / "credential.txt"
    access_key.write_text(
        "access_key_id = '" + "LTAI" + ("0" * 16) + "'", encoding="utf-8"
    )
    private_key = tmp_path / "private.pem"
    private_key.write_text(
        "-----BEGIN " + "PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----\n",
        encoding="utf-8",
    )

    assert "Alibaba Cloud AccessKey ID" in scan_file(access_key)
    assert "private key" in scan_file(private_key)


def test_source_release_secret_scanner_allows_documented_placeholders(tmp_path: Path) -> None:
    namespace = _namespace()
    placeholder = tmp_path / "example.env"
    placeholder.write_text(
        "access_key_id='<customer-access-key-id>'\n"
        "access_key_secret='<injected-at-runtime>'\n",
        encoding="utf-8",
    )

    assert namespace["scan_file"](placeholder) == []
