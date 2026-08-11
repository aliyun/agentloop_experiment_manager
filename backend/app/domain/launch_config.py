"""Canonical launch configuration and auditable experiment version helpers."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Mapping


class VersionMode(StrEnum):
    MANUAL = "MANUAL"
    AUTO = "AUTO"


@dataclass(frozen=True, slots=True)
class LaunchExecutionConfig:
    """Immutable snapshot handed from the control plane to the offline Runner."""

    launch_id: str
    agent_connection_id: str | None
    experiment_version: str
    version_mode: str
    experiment_parameters_json: str
    context_schema_version: int
    context_xml_template: str
    record_name: str | None
    launch_config_hash: str

    @property
    def experiment_parameters(self) -> dict[str, Any]:
        return json.loads(self.experiment_parameters_json)


_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def normalize_version_request(
    version_mode: str | VersionMode,
    experiment_version: str | None,
) -> tuple[str, str | None]:
    """Validate a version request without resolving an automatic version."""

    try:
        mode = VersionMode(str(version_mode)).value
    except ValueError as exc:
        raise ValueError(f"unsupported version_mode: {version_mode}") from exc

    if mode == VersionMode.AUTO.value:
        if experiment_version is not None:
            raise ValueError("experiment_version must be omitted when version_mode is AUTO")
        return mode, None

    version = (experiment_version or "").strip()
    if not version:
        raise ValueError("experiment_version is required when version_mode is MANUAL")
    if not _VERSION_PATTERN.fullmatch(version):
        raise ValueError(
            "experiment_version must be 1..128 characters using letters, numbers, '.', '_' or '-'"
        )
    return mode, version


def generate_auto_version(now: datetime, launch_id: str) -> str:
    """Generate one human-auditable version for a launch.

    The UTC wall-clock portion makes the creation time visible while the launch
    suffix makes concurrent launches distinguishable.  Callers persist the
    result and must never regenerate it for the same launch.
    """

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    normalized = now.astimezone(UTC)
    compact_id = re.sub(r"[^A-Za-z0-9]", "", launch_id)[:12] or "unknown"
    timestamp = normalized.strftime("%Y%m%dT%H%M%S%fZ")
    return f"auto-{timestamp}-{compact_id}"


def resolve_experiment_version(
    *,
    version_mode: str | VersionMode,
    requested_version: str | None,
    now: datetime,
    launch_id: str,
) -> tuple[str, str | None, str]:
    """Return normalized mode, requested manual version, and final version."""

    mode, manual = normalize_version_request(version_mode, requested_version)
    final = manual if mode == VersionMode.MANUAL.value else generate_auto_version(now, launch_id)
    assert final is not None
    return mode, manual, final


def canonical_parameters_json(parameters: Mapping[str, Any] | str | None) -> str:
    """Return a stable JSON object representation suitable for persistence/hash."""

    if parameters is None:
        value: Any = {}
    elif isinstance(parameters, str):
        try:
            value = json.loads(parameters)
        except json.JSONDecodeError as exc:
            raise ValueError("experiment_parameters must be valid JSON") from exc
    else:
        value = dict(parameters)
    if not isinstance(value, dict):
        raise ValueError("experiment_parameters must be a JSON object")
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("experiment_parameters must contain JSON-compatible values") from exc


def canonical_launch_config_hash(
    *,
    agent_connection_id: str | None,
    version_mode: str | VersionMode,
    requested_version: str | None,
    experiment_parameters_json: str | Mapping[str, Any] | None,
    record_name: str | None,
    context_schema_version: int,
) -> str:
    """Hash the caller-controlled launch configuration for idempotency checks.

    The generated AUTO version is intentionally not included: it is execution
    metadata generated once after idempotency lookup.  The ``AUTO`` declaration
    itself is included, so a retry remains stable while a MANUAL/AUTO change
    conflicts.
    """

    mode, manual = normalize_version_request(version_mode, requested_version)
    if context_schema_version < 1:
        raise ValueError("context_schema_version must be positive")
    parameters_json = canonical_parameters_json(experiment_parameters_json)
    normalized_connection_id = agent_connection_id.strip() if agent_connection_id else None
    normalized_record_name = record_name.strip() if record_name else None
    payload = {
        "agentConnectionId": normalized_connection_id,
        "contextSchemaVersion": context_schema_version,
        "experimentParameters": json.loads(parameters_json),
        "recordName": normalized_record_name,
        "requestedExperimentVersion": manual,
        "versionMode": mode,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
