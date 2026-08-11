from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from threading import RLock

from cryptography.fernet import Fernet, InvalidToken


class EncryptedFileSecretStore:
    """Small encrypted secret store whose master key is injected outside SQLite.

    The master key must be a Fernet key supplied by deployment configuration. The
    encrypted file is intentionally separate from the launcher database and is
    written with mode 0600.
    """

    def __init__(self, path: Path, master_key: str) -> None:
        if not master_key:
            raise ValueError("master_key is required")
        self._path = path
        validate_encrypted_secret_file(path)
        self._fernet = Fernet(master_key.encode("ascii"))
        self._lock = RLock()

    def put(self, reference: str, value: str) -> None:
        if not reference or not value:
            raise ValueError("reference and value are required")
        with self._lock:
            values = self._read()
            values[reference] = value
            self._write(values)

    def get(self, reference: str) -> str:
        with self._lock:
            return self._read()[reference]

    def delete(self, reference: str) -> None:
        with self._lock:
            values = self._read()
            values.pop(reference, None)
            self._write(values)

    def exists(self, reference: str) -> bool:
        with self._lock:
            return reference in self._read()

    def _read(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        token = _read_private_file(self._path)
        try:
            payload = self._fernet.decrypt(token)
        except InvalidToken as exc:
            raise RuntimeError("secret store cannot be decrypted with the configured key") from exc
        data = json.loads(payload.decode("utf-8"))
        if not isinstance(data, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in data.items()
        ):
            raise RuntimeError("secret store payload is invalid")
        return data

    def _write(self, values: dict[str, str]) -> None:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._path.parent.chmod(0o700)
        token = self._fernet.encrypt(
            json.dumps(values, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        validate_encrypted_secret_file(self._path)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self._path.parent,
            prefix=".secrets-",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(token)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
            self._path.chmod(0o600)
        finally:
            if temporary.exists():
                temporary.unlink()


def validate_encrypted_secret_file(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError("secrets.enc must not be a symbolic link")
    if not path.exists():
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("secrets.enc must be a regular file")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise RuntimeError("secrets.enc is owned by another user")
        if metadata.st_mode & 0o077:
            raise RuntimeError("secrets.enc must not be group/world accessible")
    finally:
        os.close(descriptor)


def _read_private_file(path: Path) -> bytes:
    validate_encrypted_secret_file(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        return stream.read()
