#!/usr/bin/env python3
"""Run the real local-Agent half of an AgentLoop end-to-end acceptance.

The script intentionally does not create Dataset, Evaluator, or Experiment Plan
resources.  It configures an already-running launcher, validates an existing
OFFLINE Plan, connects the real reference Agent process, starts one cloud
Experiment Run, and waits until the launcher reports ``HANDED_OFF``.

Dry-run is the default.  A real run requires both ``--execute`` and the exact
``--confirm RUN_REAL_EXPERIMENT`` phrase.  Credentials and the one-time Agent
token are kept out of command lines, stdout, stderr, and the optional evidence
file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.client import HTTPResponse
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, quote_plus, unquote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CREDENTIALS_FILE = PROJECT_ROOT.parent / ".env"
REFERENCE_AGENT = PROJECT_ROOT / "examples" / "reference_calculator_agent.py"
BACKEND_ROOT = PROJECT_ROOT / "backend"
CONFIRMATION_PHRASE = "RUN_REAL_EXPERIMENT"
SUCCESS_RUNNER_STATE = "HANDED_OFF"
FAILURE_RUNNER_STATES = frozenset({"FAILED", "UNKNOWN"})
FAILURE_SUBMISSION_STATES = frozenset(
    {"DEFINITIVE_FAILURE", "SUBMISSION_UNKNOWN", "SKIPPED"}
)
ACCESS_ID_KEYS = (
    "access_id",
    "ACCESS_ID",
    "ALIBABA_CLOUD_ACCESS_KEY_ID",
)
ACCESS_KEY_KEYS = (
    "access_key",
    "ACCESS_KEY",
    "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
)


class AcceptanceError(RuntimeError):
    """A sanitized acceptance failure suitable for CLI output."""


@dataclass(frozen=True)
class Credentials:
    access_key_id: str = field(repr=False)
    access_key_secret: str = field(repr=False)


@dataclass(frozen=True)
class AcceptanceConfig:
    launcher_url: str
    region: str
    agent_space: str
    plan_id: str
    profile_display_name: str
    credentials_env_file: Path | None
    api_endpoint: str | None
    console_base_url: str
    agent_connection_name: str
    version_mode: str
    experiment_version: str | None
    experiment_parameters: dict[str, Any]
    record_name: str
    idempotency_key: str
    agent_python: Path
    request_timeout_seconds: float
    agent_connect_timeout_seconds: float
    launch_timeout_seconds: float
    poll_interval_seconds: float
    output: Path | None


class SecretRedactor:
    """Best-effort final guard against accidental secret output."""

    def __init__(self) -> None:
        self._values: set[str] = set()

    def register(self, *values: str) -> None:
        for value in values:
            if value:
                self._values.add(value)
                self._values.add(quote(value, safe=""))
                self._values.add(quote_plus(value, safe=""))

    def redact(self, value: object) -> str:
        text = str(value)
        for secret in sorted(self._values, key=len, reverse=True):
            if secret:
                text = text.replace(secret, "<redacted>")
        return text


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: HTTPResponse,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl


class LauncherClient:
    def __init__(self, base_url: str, timeout_seconds: float) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout_seconds = timeout_seconds
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        expected_status: frozenset[int] = frozenset({200}),
    ) -> dict[str, Any]:
        data = None
        request_headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if headers:
            request_headers.update(headers)
        request = Request(
            url=urljoin(self.base_url, path.lstrip("/")),
            data=data,
            headers=request_headers,
            method=method,
        )
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                status = response.status
                raw = response.read()
        except HTTPError as exc:
            raw = exc.read(64 * 1024)
            detail = _safe_http_error_detail(raw)
            raise AcceptanceError(
                f"{method} {path} failed with HTTP {exc.code}: {detail}"
            ) from None
        except (URLError, TimeoutError, OSError) as exc:
            raise AcceptanceError(
                f"{method} {path} could not reach launcher: {exc}"
            ) from None
        if status not in expected_status:
            raise AcceptanceError(
                f"{method} {path} returned unexpected HTTP {status}; "
                f"expected {sorted(expected_status)}"
            )
        if not raw:
            return {}
        try:
            body = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AcceptanceError(f"{method} {path} returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise AcceptanceError(
                f"{method} {path} returned a non-object JSON response"
            )
        return body


def _safe_http_error_detail(raw: bytes) -> str:
    try:
        body = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "launcher returned a non-JSON error"
    if not isinstance(body, dict):
        return "launcher returned an invalid error object"
    error = body.get("error")
    if not isinstance(error, dict):
        return "launcher returned an error without details"
    code = error.get("code")
    message = error.get("message")
    safe_code = code if isinstance(code, str) else "UNKNOWN"
    safe_message = message if isinstance(message, str) else "No message"
    return f"{safe_code}: {safe_message}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launcher-url", default="http://127.0.0.1:8090")
    parser.add_argument("--region", required=True)
    parser.add_argument("--agent-space", required=True)
    parser.add_argument("--plan-id", required=True)
    parser.add_argument("--profile-display-name", default="Real E2E Acceptance")
    parser.add_argument(
        "--credentials-env-file",
        type=Path,
        default=DEFAULT_CREDENTIALS_FILE,
        help=(
            "dotenv file containing access_id/access_key; values are never printed "
            f"(default: {DEFAULT_CREDENTIALS_FILE})"
        ),
    )
    parser.add_argument(
        "--reuse-profile-credentials",
        action="store_true",
        help="keep credentials already encrypted in the running launcher's profile",
    )
    parser.add_argument("--api-endpoint")
    parser.add_argument(
        "--console-base-url",
        default="https://agentloop.console.aliyun.com",
    )
    parser.add_argument("--agent-connection-name")
    parser.add_argument(
        "--version-mode",
        choices=("MANUAL", "AUTO"),
        default="MANUAL",
    )
    parser.add_argument("--experiment-version")
    parser.add_argument(
        "--experiment-parameters",
        type=_json_object,
        default={"acceptanceRun": True, "agent": "reference-calculator"},
    )
    parser.add_argument("--record-name")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--agent-python", type=Path)
    parser.add_argument(
        "--request-timeout-seconds", type=_positive_float, default=120.0
    )
    parser.add_argument(
        "--agent-connect-timeout-seconds", type=_positive_float, default=30.0
    )
    parser.add_argument("--launch-timeout-seconds", type=_positive_float, default=900.0)
    parser.add_argument("--poll-interval-seconds", type=_positive_float, default=1.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="perform local writes and start one real cloud Experiment Run",
    )
    parser.add_argument(
        "--confirm",
        metavar=CONFIRMATION_PHRASE,
        help=f"required with --execute; must equal {CONFIRMATION_PHRASE}",
    )
    return parser


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _json_object(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("must be a JSON object")
    return parsed


def build_config(
    args: argparse.Namespace, *, now: datetime | None = None
) -> AcceptanceConfig:
    _validate_loopback_launcher_url(args.launcher_url)
    timestamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    mode = args.version_mode
    version = args.experiment_version
    if mode == "AUTO" and version is not None:
        raise AcceptanceError(
            "--experiment-version must be omitted with --version-mode AUTO"
        )
    if mode == "MANUAL" and version is None:
        version = f"acceptance-{timestamp}"
    if version is not None and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", version
    ):
        raise AcceptanceError("experiment version has an unsupported format")
    credentials_file = (
        None if args.reuse_profile_credentials else args.credentials_env_file
    )
    agent_python = args.agent_python or _default_agent_python()
    return AcceptanceConfig(
        launcher_url=args.launcher_url.rstrip("/"),
        region=args.region,
        agent_space=args.agent_space,
        plan_id=args.plan_id,
        profile_display_name=args.profile_display_name,
        credentials_env_file=credentials_file,
        api_endpoint=args.api_endpoint,
        console_base_url=args.console_base_url,
        agent_connection_name=args.agent_connection_name
        or f"reference-calculator-e2e-{timestamp}",
        version_mode=mode,
        experiment_version=version,
        experiment_parameters=args.experiment_parameters,
        record_name=args.record_name or f"real-e2e-acceptance-{timestamp}",
        idempotency_key=args.idempotency_key or f"real-e2e-{uuid4().hex}",
        agent_python=agent_python,
        request_timeout_seconds=args.request_timeout_seconds,
        agent_connect_timeout_seconds=args.agent_connect_timeout_seconds,
        launch_timeout_seconds=args.launch_timeout_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
        output=args.output,
    )


def _validate_loopback_launcher_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AcceptanceError("--launcher-url must be an absolute HTTP(S) URL")
    try:
        loopback = ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname.casefold() == "localhost"
    if not loopback:
        raise AcceptanceError(
            "--launcher-url must resolve syntactically to localhost or a loopback IP; "
            "this prevents credentials from being sent to a remote endpoint"
        )
    if parsed.query or parsed.fragment or (parsed.path not in {"", "/"}):
        raise AcceptanceError(
            "--launcher-url must not contain a path, query, or fragment"
        )


def _validate_agent_connect_url(value: str, connection_id: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
        raise AcceptanceError("launcher returned an invalid Agent connectUrl")
    try:
        loopback = ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname.casefold() == "localhost"
    expected_path = f"/api/v1/agent-connections/{connection_id}/connect"
    if (
        not loopback
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or unquote(parsed.path) != expected_path
    ):
        raise AcceptanceError(
            "launcher returned an unsafe Agent connectUrl; refusing to send the token"
        )


def _default_agent_python() -> Path:
    candidate = BACKEND_ROOT / ".venv" / "bin" / "python"
    return candidate if candidate.is_file() else Path(sys.executable)


def render_preview(config: AcceptanceConfig) -> str:
    credential_source = (
        "existing encrypted launcher profile"
        if config.credentials_env_file is None
        else f"dotenv file {config.credentials_env_file} (values hidden)"
    )
    launch_payload = {
        "agentConnectionId": "<created at execution>",
        "versionMode": config.version_mode,
        "experimentParameters": config.experiment_parameters,
        "recordName": config.record_name,
        "contextSchemaVersion": 1,
    }
    if config.experiment_version is not None:
        launch_payload["experimentVersion"] = config.experiment_version
    lines = [
        "REAL AGENTLOOP ACCEPTANCE PREVIEW (no requests sent)",
        f"Launcher: {config.launcher_url} (must report gatewayMode=agentloop)",
        f"AgentLoop target: region={config.region}, agentSpace={config.agent_space}",
        f"Existing Plan: {config.plan_id} (must be runnable OFFLINE)",
        f"Credential source: {credential_source}",
        f"Agent connection: {config.agent_connection_name}",
        f"Reference Agent: {REFERENCE_AGENT}",
        f"Idempotency-Key: {config.idempotency_key}",
        "Launch payload: "
        + json.dumps(launch_payload, sort_keys=True, separators=(",", ":")),
        (
            f"Poll limits: agent={config.agent_connect_timeout_seconds:g}s, "
            f"launch={config.launch_timeout_seconds:g}s"
        ),
        "Persistent cloud resource creation: none (Dataset/Evaluator/Plan must already exist)",
        (
            "Cloud mutation on execute: exactly one Experiment Run for the selected Plan; "
            "its Dataset rows invoke the local Agent and bound evaluators"
        ),
        (
            "Local mutations on execute: update launcher Profile, create one Agent Connection, "
            "and retain one Launch History row"
        ),
        (
            "Success boundary: local runnerState=HANDED_OFF with a non-empty recordId. "
            "This does not claim the cloud evaluation passed."
        ),
    ]
    if config.output is not None:
        lines.append(
            f"Evidence output: {config.output} (created with mode 0600; no overwrite)"
        )
    return "\n".join(lines)


def _load_credentials(path: Path) -> Credentials:
    if not path.is_file():
        raise AcceptanceError(f"credentials env file does not exist: {path}")
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise AcceptanceError(
            f"unable to read credentials env file: {path}: {exc}"
        ) from None
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if key not in {*ACCESS_ID_KEYS, *ACCESS_KEY_KEYS}:
            continue
        try:
            parsed = shlex.split(raw_value, comments=True, posix=True)
        except ValueError as exc:
            raise AcceptanceError(
                f"invalid dotenv quoting for credential key {key} on line {line_number}"
            ) from exc
        values[key] = " ".join(parsed) if parsed else ""
    access_id = _first_non_empty(values, ACCESS_ID_KEYS)
    access_key = _first_non_empty(values, ACCESS_KEY_KEYS)
    if access_id is None or access_key is None:
        raise AcceptanceError(
            "credentials env file must define access_id/access_key or the documented "
            "Alibaba Cloud environment key aliases"
        )
    return Credentials(access_key_id=access_id, access_key_secret=access_key)


def _first_non_empty(values: dict[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = values.get(key)
        if value:
            return value
    return None


def build_agent_command(config: AcceptanceConfig, connect_url: str) -> list[str]:
    if not config.agent_python.is_file():
        raise AcceptanceError(
            f"Agent Python executable does not exist: {config.agent_python}"
        )
    if not REFERENCE_AGENT.is_file():
        raise AcceptanceError(f"reference Agent does not exist: {REFERENCE_AGENT}")
    return [
        str(config.agent_python),
        str(REFERENCE_AGENT),
        "--url",
        connect_url,
        "--no-reconnect",
    ]


def execute_acceptance(
    config: AcceptanceConfig,
    redactor: SecretRedactor,
) -> dict[str, Any]:
    client = LauncherClient(config.launcher_url, config.request_timeout_seconds)
    credentials = (
        _load_credentials(config.credentials_env_file)
        if config.credentials_env_file is not None
        else None
    )
    if credentials is not None:
        redactor.register(credentials.access_key_id, credentials.access_key_secret)

    health = client.request("GET", "/api/v1/health")
    if health.get("gatewayMode") != "agentloop":
        raise AcceptanceError(
            "launcher is not in real AgentLoop mode; "
            f"gatewayMode={health.get('gatewayMode')!r}"
        )

    profile_payload: dict[str, Any] = {
        "displayName": config.profile_display_name,
        "regionId": config.region,
        "agentSpace": config.agent_space,
        "apiEndpoint": config.api_endpoint,
        "consoleBaseUrl": config.console_base_url,
        "defaultTimezone": "UTC",
        "maxConcurrency": 1,
    }
    if credentials is not None:
        profile_payload.update(
            {
                "accessKeyId": credentials.access_key_id,
                "accessKeySecret": credentials.access_key_secret,
            }
        )
    client.request("PUT", "/api/v1/settings/agentloop", payload=profile_payload)
    check = client.request("POST", "/api/v1/settings/agentloop/test", payload={})
    if check.get("ok") is not True:
        raise AcceptanceError(
            "launcher rejected the AgentLoop Profile connection: "
            f"{check.get('code', 'UNKNOWN')}"
        )

    encoded_plan_id = quote(config.plan_id, safe="")
    plan = client.request("GET", f"/api/v1/plans/{encoded_plan_id}")
    _validate_plan(plan, config.plan_id)

    created = client.request(
        "POST",
        "/api/v1/agent-connections",
        payload={
            "name": config.agent_connection_name,
            "description": (
                "Created by scripts/real_e2e_acceptance.py for a real acceptance run"
            ),
        },
        expected_status=frozenset({201}),
    )
    connection_id = _required_string(created, "agentConnectionId")
    connect_url = _required_string(created, "connectUrl")
    _validate_agent_connect_url(connect_url, connection_id)
    token = _required_string(created, "token")
    redactor.register(token)
    created.pop("connectCommand", None)
    created.pop("token", None)

    command = build_agent_command(config, connect_url)
    agent_environment = os.environ.copy()
    agent_environment["AGENT_CONNECTION_TOKEN"] = token
    current_python_path = agent_environment.get("PYTHONPATH")
    agent_environment["PYTHONPATH"] = (
        str(BACKEND_ROOT)
        if not current_python_path
        else os.pathsep.join((str(BACKEND_ROOT), current_python_path))
    )
    agent_process: subprocess.Popen[bytes] | None = None
    try:
        try:
            agent_process = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                env=agent_environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise AcceptanceError(
                f"unable to start reference Agent process: {exc}"
            ) from None
        agent_environment.pop("AGENT_CONNECTION_TOKEN", None)
        token = ""
        _wait_for_agent_connection(client, connection_id, agent_process, config)

        launch_payload: dict[str, Any] = {
            "agentConnectionId": connection_id,
            "versionMode": config.version_mode,
            "experimentParameters": config.experiment_parameters,
            "recordName": config.record_name,
            "contextSchemaVersion": 1,
        }
        if config.experiment_version is not None:
            launch_payload["experimentVersion"] = config.experiment_version
        launch = client.request(
            "POST",
            f"/api/v1/plans/{encoded_plan_id}/launch",
            payload=launch_payload,
            headers={"Idempotency-Key": config.idempotency_key},
            expected_status=frozenset({202}),
        )
        launch_id = _required_string(launch, "launchId")
        launch = _wait_for_handoff(client, launch_id, agent_process, config, launch)
        return _build_summary(config, connection_id, launch)
    finally:
        agent_environment.pop("AGENT_CONNECTION_TOKEN", None)
        token = ""
        if agent_process is not None:
            _stop_agent(agent_process)


def _validate_plan(plan: dict[str, Any], expected_plan_id: str) -> None:
    if plan.get("planId") != expected_plan_id:
        raise AcceptanceError("launcher returned a different Plan than requested")
    if str(plan.get("experimentType", "")).upper() != "OFFLINE":
        raise AcceptanceError("selected Plan is not OFFLINE")
    if plan.get("canLaunchLocally") is not True:
        reason = plan.get("cannotLaunchReason")
        raise AcceptanceError(
            f"selected Plan cannot launch locally: {reason or 'unknown reason'}"
        )
    if not isinstance(plan.get("datasetId"), str) or not plan["datasetId"]:
        raise AcceptanceError("selected Plan has no Dataset")
    evaluator_count = plan.get("evaluatorCount")
    if not isinstance(evaluator_count, int) or evaluator_count < 1:
        raise AcceptanceError("selected Plan has no bound Evaluator")


def _required_string(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise AcceptanceError(f"launcher response is missing {key}")
    return item


def _wait_for_agent_connection(
    client: LauncherClient,
    connection_id: str,
    process: subprocess.Popen[bytes],
    config: AcceptanceConfig,
) -> None:
    deadline = time.monotonic() + config.agent_connect_timeout_seconds
    encoded_id = quote(connection_id, safe="")
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            raise AcceptanceError(
                f"reference Agent exited before connecting (exitCode={return_code})"
            )
        health = client.request("GET", f"/api/v1/agent-connections/{encoded_id}/health")
        if health.get("healthy") is True and health.get("status") == "CONNECTED":
            return
        time.sleep(config.poll_interval_seconds)
    raise AcceptanceError(
        f"reference Agent did not connect within {config.agent_connect_timeout_seconds:g}s"
    )


def _wait_for_handoff(
    client: LauncherClient,
    launch_id: str,
    process: subprocess.Popen[bytes],
    config: AcceptanceConfig,
    initial: dict[str, Any],
) -> dict[str, Any]:
    deadline = time.monotonic() + config.launch_timeout_seconds
    encoded_id = quote(launch_id, safe="")
    current = initial
    accepted_record_id: str | None = None
    while True:
        if current.get("launchId") != launch_id:
            raise AcceptanceError("launcher returned a different Launch than requested")
        record_id = current.get("recordId")
        if record_id is not None and (not isinstance(record_id, str) or not record_id):
            raise AcceptanceError("launcher returned an invalid recordId")
        if isinstance(record_id, str):
            if accepted_record_id is None:
                accepted_record_id = record_id
            elif accepted_record_id != record_id:
                raise AcceptanceError("recordId changed while polling the same Launch")
        submission_state = str(current.get("submissionState", ""))
        runner_state = str(current.get("runnerState", ""))
        if runner_state == SUCCESS_RUNNER_STATE:
            if submission_state != "ACCEPTED" or accepted_record_id is None:
                raise AcceptanceError(
                    "launcher reported HANDED_OFF without ACCEPTED and a recordId"
                )
            return current
        if (
            runner_state in FAILURE_RUNNER_STATES
            or submission_state in FAILURE_SUBMISSION_STATES
        ):
            error_code = current.get("errorCode") or "UNKNOWN"
            error_message = current.get("sanitizedError") or "No sanitized error"
            raise AcceptanceError(
                "real Launch failed: "
                f"submissionState={submission_state}, runnerState={runner_state}, "
                f"errorCode={error_code}, message={error_message}, launchId={launch_id}, "
                f"recordId={accepted_record_id or '<none>'}"
            )
        if process.poll() is not None:
            raise AcceptanceError(
                "reference Agent exited before local handoff completed "
                f"(exitCode={process.returncode}, launchId={launch_id})"
            )
        if time.monotonic() >= deadline:
            raise AcceptanceError(
                "timed out waiting for local handoff: "
                f"launchId={launch_id}, recordId={accepted_record_id or '<none>'}, "
                f"submissionState={submission_state}, runnerState={runner_state}"
            )
        time.sleep(config.poll_interval_seconds)
        current = client.request("GET", f"/api/v1/launches/{encoded_id}")


def _build_summary(
    config: AcceptanceConfig,
    connection_id: str,
    launch: dict[str, Any],
) -> dict[str, Any]:
    return {
        "acceptanceBoundary": "LOCAL_HANDOFF_ONLY",
        "agentSpace": config.agent_space,
        "region": config.region,
        "planId": config.plan_id,
        "agentConnectionId": connection_id,
        "launchId": _required_string(launch, "launchId"),
        "recordId": _required_string(launch, "recordId"),
        "experimentVersion": launch.get("experimentVersion"),
        "submissionState": launch.get("submissionState"),
        "runnerState": launch.get("runnerState"),
        "workerFinishedAt": launch.get("workerFinishedAt"),
        "agentLoopUrl": launch.get("agentLoopUrl"),
        "cloudEvaluationVerified": False,
    }


def _stop_agent(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _write_evidence(path: Path, summary: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    payload = (json.dumps(summary, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise AcceptanceError(
            f"evidence output already exists and was not overwritten: {path}"
        )
    except OSError as exc:
        raise AcceptanceError(
            f"unable to create evidence output {path}: {exc}"
        ) from None
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
    except OSError as exc:
        path.unlink(missing_ok=True)
        raise AcceptanceError(
            f"unable to write evidence output {path}: {exc}"
        ) from None


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    redactor = SecretRedactor()
    try:
        config = build_config(args)
        print(render_preview(config))
        if not args.execute:
            print("DRY RUN COMPLETE: no local or cloud request was sent.")
            return 0
        if args.confirm != CONFIRMATION_PHRASE:
            raise AcceptanceError(
                f"execution requires --confirm {CONFIRMATION_PHRASE} after reviewing the preview"
            )
        summary = execute_acceptance(config, redactor)
        if config.output is not None:
            _write_evidence(config.output, summary)
        print("REAL LOCAL HANDOFF SUCCEEDED")
        print(json.dumps(summary, indent=2, sort_keys=True))
        print(
            "Cloud evaluation is not yet verified; read back this recordId in AgentLoop "
            "before declaring full end-to-end acceptance."
        )
        return 0
    except (AcceptanceError, ValueError) as exc:
        print(f"ERROR: {redactor.redact(exc)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
