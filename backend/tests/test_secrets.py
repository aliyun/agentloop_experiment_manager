from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet

from app.secrets import EncryptedFileSecretStore, MemorySecretStore


def test_memory_secret_store_round_trip() -> None:
    store = MemorySecretStore()
    store.put("profile/default/sk", "very-secret")
    assert store.exists("profile/default/sk")
    assert store.get("profile/default/sk") == "very-secret"
    store.delete("profile/default/sk")
    assert not store.exists("profile/default/sk")


def test_encrypted_secret_store_never_writes_plaintext(tmp_path) -> None:
    path = tmp_path / "secrets.enc"
    store = EncryptedFileSecretStore(path, Fernet.generate_key().decode("ascii"))
    store.put("profile/default/sk", "plain-secret-value")

    assert store.get("profile/default/sk") == "plain-secret-value"
    assert b"plain-secret-value" not in path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600


def test_encrypted_secret_store_rejects_wrong_key(tmp_path) -> None:
    path = tmp_path / "secrets.enc"
    first = EncryptedFileSecretStore(path, Fernet.generate_key().decode("ascii"))
    first.put("profile/default/sk", "secret")

    second = EncryptedFileSecretStore(path, Fernet.generate_key().decode("ascii"))
    with pytest.raises(RuntimeError, match="cannot be decrypted"):
        second.get("profile/default/sk")


def test_encrypted_secret_file_is_not_json(tmp_path) -> None:
    path = tmp_path / "secrets.enc"
    store = EncryptedFileSecretStore(path, Fernet.generate_key().decode("ascii"))
    store.put("profile/default/sk", "secret")

    with pytest.raises((UnicodeDecodeError, json.JSONDecodeError)):
        json.loads(path.read_bytes())
