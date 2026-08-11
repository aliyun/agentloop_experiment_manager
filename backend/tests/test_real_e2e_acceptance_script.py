from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "real_e2e_acceptance.py"
SPEC = importlib.util.spec_from_file_location("real_e2e_acceptance", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _args(*extra: str):
    return MODULE.build_parser().parse_args(
        [
            "--region",
            "cn-hangzhou",
            "--agent-space",
            "demo-space",
            "--plan-id",
            "plan-real-1",
            *extra,
        ]
    )


def test_preview_is_exact_and_contains_no_secret_values(tmp_path, capsys) -> None:
    secret_id = "id-that-must-not-be-read"
    secret_key = "key-that-must-not-be-read"
    credentials = tmp_path / ".env"
    credentials.write_text(
        f"access_id={secret_id}\naccess_key={secret_key}\n",
        encoding="utf-8",
    )

    exit_code = MODULE.main(
        [
            "--region",
            "cn-hangzhou",
            "--agent-space",
            "demo-space",
            "--plan-id",
            "plan-real-1",
            "--credentials-env-file",
            str(credentials),
            "--experiment-version",
            "acceptance-fixed",
            "--idempotency-key",
            "acceptance-fixed",
        ]
    )

    output = capsys.readouterr()
    assert exit_code == 0
    assert "no local or cloud request was sent" in output.out
    assert '"experimentVersion":"acceptance-fixed"' in output.out
    assert secret_id not in output.out + output.err
    assert secret_key not in output.out + output.err


def test_execute_requires_the_exact_confirmation_before_loading_credentials(
    tmp_path, capsys
) -> None:
    missing_file = tmp_path / "not-present.env"
    exit_code = MODULE.main(
        [
            "--region",
            "cn-hangzhou",
            "--agent-space",
            "demo-space",
            "--plan-id",
            "plan-real-1",
            "--credentials-env-file",
            str(missing_file),
            "--execute",
        ]
    )

    output = capsys.readouterr()
    assert exit_code == 2
    assert f"--confirm {MODULE.CONFIRMATION_PHRASE}" in output.err
    assert "does not exist" not in output.err


def test_config_rejects_remote_launcher_before_any_execution() -> None:
    with pytest.raises(MODULE.AcceptanceError, match="loopback"):
        MODULE.build_config(
            _args("--launcher-url", "https://customer.example.com"),
            now=datetime(2026, 8, 8, tzinfo=UTC),
        )


def test_agent_command_never_contains_the_connection_token() -> None:
    config = MODULE.build_config(
        _args("--reuse-profile-credentials"),
        now=datetime(2026, 8, 8, tzinfo=UTC),
    )
    token = "one-time-agent-token"
    command = MODULE.build_agent_command(
        config,
        "ws://127.0.0.1:8090/api/v1/agent-connections/connection-1/connect",
    )

    assert token not in " ".join(command)
    assert "--url" in command
    assert "--no-reconnect" in command


def test_agent_token_can_only_be_sent_to_the_expected_loopback_websocket() -> None:
    MODULE._validate_agent_connect_url(
        "ws://127.0.0.1:8090/api/v1/agent-connections/connection-1/connect",
        "connection-1",
    )

    with pytest.raises(MODULE.AcceptanceError, match="unsafe"):
        MODULE._validate_agent_connect_url(
            "wss://attacker.example/api/v1/agent-connections/connection-1/connect",
            "connection-1",
        )


def test_secret_redactor_covers_plain_and_url_encoded_values() -> None:
    redactor = MODULE.SecretRedactor()
    redactor.register("a b+c")

    assert redactor.redact("a b+c") == "<redacted>"
    assert redactor.redact("a%20b%2Bc") == "<redacted>"
    assert redactor.redact("a+b%2Bc") == "<redacted>"
