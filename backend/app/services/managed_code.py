"""Encrypted managed-code configuration and local worker supervision.

Managed code is a trusted, single-user capability.  The source is never
executed in the FastAPI process: a child process runs it and connects back
through the existing AgentConnector/WebSocket v1 protocol.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import re
import signal
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Protocol
from uuid import uuid4

from app.domain.errors import ConflictError, LauncherError, NotFoundError
from app.domain.experiment_context import (
    append_experiment_context,
    build_experiment_context_template,
    render_experiment_context,
)
from app.secrets import SecretStore
from app.storage import AgentConnectionRepository

from .agent_connections import (
    AgentConnectionHub,
    AgentConnectionUnavailableError,
    AgentExecutionError,
    AgentExecutionResult,
    AgentExecutionTimeoutError,
)

MAX_LOCAL_CODE_BYTES = 64 * 1024
MAX_TEST_MESSAGE_BYTES = 32 * 1024
MAX_TEST_PREVIEW_CHARS = 2_000
MANAGED_AGENT_CONNECT_TIMEOUT_SECONDS = 10.0

STARTER_SOURCE = '''from agentloop_sdk import SolutionOutput, Task


def build_request_body(task: Task) -> dict:
    """Translate one platform-provided Dataset Task for the customer Agent."""
    task_input = task.input if isinstance(task.input, dict) else {}

    if isinstance(task.input, dict):
        content = (
            task_input.get("question")
            or task_input.get("input")
            or task_input.get("query")
            or task_input.get("prompt")
            or ""
        )
    else:
        content = str(task.input or "")

    metadata = task.metadata or {}
    parameters = metadata.get("experiment_parameters") or {}

    return {
        # content 已经由外层 Runner 追加 experiment_context
        "content": content,
        "model": parameters.get("model") or "local-smoke",
    }


async def solution_http(task: Task, *args, **kwargs) -> SolutionOutput:
    """Execute exactly one Dataset Task; the outer platform owns the experiment."""
    request_body = build_request_body(task)

    # Replace this local echo with the customer's HTTP or SDK invocation.
    # The Web test runs only this function and never creates an Experiment Run.
    return SolutionOutput(
        success=True,
        output=request_body["content"],
        trajectory=[],
        meta={
            "agentName": "customer-solution-provider",
            "agentVersion": "1.0.0",
            "experiment_input": request_body,
        },
    )
'''

_ALLOWED_META_KEYS = ("traceId", "agentName", "agentVersion")
_TRACE_ID = re.compile(r"^[0-9a-fA-F]{32}$")
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(authorization|access[_-]?key(?:[_-]?(?:id|secret))?|secret|token|password)"
    r"(\s*[\"']?\s*[:=]\s*[\"']?\s*)([^\s,;\"'}]{4,})"
)
_ALIYUN_ACCESS_KEY = re.compile(r"\bLTAI[A-Za-z0-9]{12,}\b")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_rfc3339(value: str) -> datetime:
    parsed = value[:-1] + "+00:00" if value.endswith("Z") else value
    result = datetime.fromisoformat(parsed)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("managed-code timestamp must be timezone-aware")
    return result.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class LocalCodeConfiguration:
    language: str
    source: str
    configured: bool
    revision: int
    source_sha256: str | None
    runtime_state: str
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class SanitizedOutput:
    type: str
    preview: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class LocalCodeTestResult:
    test_id: str
    status: str
    duration_ms: int
    code_revision: int
    response_received: bool
    sanitized_output: SanitizedOutput | None
    sanitized_meta: dict[str, str]
    redaction_count: int
    tested_at: datetime
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class _StoredLocalCode:
    language: str
    source: str
    revision: int
    source_sha256: str
    updated_at: datetime
    auto_start: bool

    def to_json(self) -> str:
        return json.dumps(
            {
                "language": self.language,
                "source": self.source,
                "revision": self.revision,
                "sourceSha256": self.source_sha256,
                "updatedAt": _rfc3339(self.updated_at),
                "autoStart": self.auto_start,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @classmethod
    def from_json(cls, raw: str) -> "_StoredLocalCode":
        try:
            value = json.loads(raw)
            source = value["source"]
            revision = value["revision"]
            digest = value["sourceSha256"]
            language = value["language"]
            updated_at = _parse_rfc3339(value["updatedAt"])
            auto_start = value.get("autoStart", True)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("managed-code configuration is invalid") from exc
        if (
            language != "python"
            or not isinstance(source, str)
            or not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 1
            or not isinstance(digest, str)
            or digest != _source_digest(source)
            or not isinstance(auto_start, bool)
        ):
            raise RuntimeError("managed-code configuration is invalid")
        return cls(
            language=language,
            source=source,
            revision=revision,
            source_sha256=digest,
            updated_at=updated_at,
            auto_start=auto_start,
        )


class ManagedAgentProcessController(Protocol):
    def runtime_state(self, connection_id: str) -> str: ...

    def active_revision(self, connection_id: str) -> int | None: ...

    async def ensure_activation_allowed(self, connection_id: str) -> None: ...

    async def activate(
        self,
        connection_id: str,
        *,
        connect_url: str,
        token: str,
        source: str,
        revision: int,
    ) -> None: ...

    async def stop(self, connection_id: str) -> None: ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class _ManagedProcess:
    process: asyncio.subprocess.Process
    revision: int


class ManagedAgentProcessManager:
    """Own trusted managed-agent subprocesses without exposing source in argv/env."""

    def __init__(
        self,
        hub: AgentConnectionHub,
        *,
        connect_timeout_seconds: float = MANAGED_AGENT_CONNECT_TIMEOUT_SECONDS,
        command: tuple[str, ...] | None = None,
    ) -> None:
        self.hub = hub
        self.connect_timeout_seconds = connect_timeout_seconds
        self.command = command or _managed_agent_command()
        self._workers: dict[str, _ManagedProcess] = {}
        self._lock = asyncio.Lock()

    def runtime_state(self, connection_id: str) -> str:
        worker = self._workers.get(connection_id)
        if worker is None or worker.process.returncode is not None:
            return "STOPPED"
        return "RUNNING" if self.hub.is_connected(connection_id) else "STARTING"

    def active_revision(self, connection_id: str) -> int | None:
        worker = self._workers.get(connection_id)
        if worker is None or worker.process.returncode is not None:
            return None
        return worker.revision

    async def ensure_activation_allowed(self, connection_id: str) -> None:
        async with self._lock:
            worker = self._workers.get(connection_id)
            owned = worker is not None and worker.process.returncode is None
            if self.hub.is_connected(connection_id) and not owned:
                raise ConflictError(
                    "AGENT_CONNECTION_IN_USE",
                    "An external Agent is already connected; managed code was not activated",
                )

    async def activate(
        self,
        connection_id: str,
        *,
        connect_url: str,
        token: str,
        source: str,
        revision: int,
    ) -> None:
        async with self._lock:
            worker = self._workers.get(connection_id)
            owned = worker is not None and worker.process.returncode is None
            if self.hub.is_connected(connection_id) and not owned:
                raise ConflictError(
                    "AGENT_CONNECTION_IN_USE",
                    "An external Agent is already connected; managed code was not activated",
                )
            if owned:
                await self._stop_locked(connection_id, worker)

            process = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=_worker_cwd(),
                env=_minimal_worker_environment(),
                start_new_session=True,
            )
            managed = _ManagedProcess(process=process, revision=revision)
            self._workers[connection_id] = managed
            try:
                await _send_worker_envelope(
                    process,
                    {
                        "protocolVersion": 1,
                        "connectionId": connection_id,
                        "connectUrl": connect_url,
                        "token": token,
                        "language": "python",
                        "source": source,
                        "revision": revision,
                    },
                )
                await self._wait_connected(connection_id, managed)
            except BaseException:
                await self._stop_locked(connection_id, managed)
                raise

    async def stop(self, connection_id: str) -> None:
        async with self._lock:
            worker = self._workers.get(connection_id)
            if worker is not None:
                await self._stop_locked(connection_id, worker)

    async def close(self) -> None:
        async with self._lock:
            for connection_id, worker in list(self._workers.items()):
                await self._stop_locked(connection_id, worker)

    async def _wait_connected(
        self,
        connection_id: str,
        worker: _ManagedProcess,
    ) -> None:
        deadline = asyncio.get_running_loop().time() + self.connect_timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            if worker.process.returncode is not None:
                raise LauncherError(
                    code="MANAGED_CODE_START_FAILED",
                    message="Managed Agent could not start",
                    status_code=422,
                )
            if self.hub.is_connected(connection_id):
                return
            await asyncio.sleep(0.025)
        raise LauncherError(
            code="MANAGED_CODE_START_TIMEOUT",
            message="Managed Agent did not connect before the startup timeout",
            status_code=504,
            retryable=True,
        )

    async def _stop_locked(
        self,
        connection_id: str,
        worker: _ManagedProcess,
    ) -> None:
        if self._workers.get(connection_id) is worker:
            self._workers.pop(connection_id, None)
        process = worker.process
        was_alive = process.returncode is None
        if was_alive:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=1.0)
            except TimeoutError:
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        if was_alive and self.hub.is_connected(connection_id):
            await self.hub.disconnect(connection_id, reason="Managed Agent was stopped")


class ManagedCodeService:
    def __init__(
        self,
        repository: AgentConnectionRepository,
        secrets_store: SecretStore,
        hub: AgentConnectionHub,
        process_manager: ManagedAgentProcessController,
    ) -> None:
        self.repository = repository
        self.secrets = secrets_store
        self.hub = hub
        self.process_manager = process_manager
        self._locks: dict[str, asyncio.Lock] = {}

    def get(self, connection_id: str) -> LocalCodeConfiguration:
        self._connection(connection_id)
        stored = self._read_stored(connection_id)
        if stored is None:
            return LocalCodeConfiguration(
                language="python",
                source=STARTER_SOURCE,
                configured=False,
                revision=0,
                source_sha256=None,
                runtime_state="NOT_CONFIGURED",
                updated_at=None,
            )
        return self._configuration(connection_id, stored)

    async def put(
        self,
        connection_id: str,
        *,
        source: str,
        expected_revision: int,
        activate: bool,
        connect_url: str,
    ) -> LocalCodeConfiguration:
        connection = self._connection(connection_id)
        _validate_source(source)
        lock = self._locks.setdefault(connection_id, asyncio.Lock())
        async with lock:
            current = self._read_stored(connection_id)
            current_revision = current.revision if current is not None else 0
            if expected_revision != current_revision:
                raise ConflictError(
                    "LOCAL_CODE_REVISION_CONFLICT",
                    f"Expected local-code revision {expected_revision}, found {current_revision}",
                )
            if activate:
                await self.process_manager.ensure_activation_allowed(connection_id)

            stored = _StoredLocalCode(
                language="python",
                source=source,
                revision=current_revision + 1,
                source_sha256=_source_digest(source),
                updated_at=_utc_now(),
                auto_start=activate,
            )
            reference = self._code_reference(connection_id)
            self.secrets.put(reference, stored.to_json())
            try:
                if activate:
                    token = self.secrets.get(connection.token_secret_ref)
                    await self.process_manager.activate(
                        connection_id,
                        connect_url=connect_url,
                        token=token,
                        source=source,
                        revision=stored.revision,
                    )
                else:
                    await self.process_manager.stop(connection_id)
            except BaseException as exc:
                if current is None:
                    self.secrets.delete(reference)
                else:
                    self.secrets.put(reference, current.to_json())
                if isinstance(exc, (ConflictError, LauncherError, asyncio.CancelledError)):
                    raise
                else:
                    raise LauncherError(
                        code="MANAGED_CODE_START_FAILED",
                        message="Managed Agent could not start",
                        status_code=503,
                        retryable=True,
                    ) from None
            return self._configuration(connection_id, stored)

    async def resume_configured(
        self,
        connect_url_for: Callable[[str], str],
        *,
        startup_delay_seconds: float = 0.25,
    ) -> None:
        """Restore managed Providers that were previously saved as active.

        The short delay lets the ASGI server begin accepting the worker's
        WebSocket connection. Failures are isolated per Agent so one broken
        customer integration never prevents the scheduler from starting.
        """

        if startup_delay_seconds > 0:
            await asyncio.sleep(startup_delay_seconds)
        for connection in self.repository.list():
            stored = self._read_stored(connection.id)
            if stored is None or not stored.auto_start or self.hub.is_connected(connection.id):
                continue
            lock = self._locks.setdefault(connection.id, asyncio.Lock())
            async with lock:
                try:
                    token = self.secrets.get(connection.token_secret_ref)
                    await self.process_manager.activate(
                        connection.id,
                        connect_url=connect_url_for(connection.id),
                        token=token,
                        source=stored.source,
                        revision=stored.revision,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Runtime state remains STOPPED and is visible in the UI.
                    # A later explicit save-and-verify can surface the error.
                    continue

    async def test(
        self,
        connection_id: str,
        *,
        message: str,
        parameters: dict[str, Any],
        timeout_seconds: float,
    ) -> LocalCodeTestResult:
        connection = self._connection(connection_id)
        if len(message.encode("utf-8")) > MAX_TEST_MESSAGE_BYTES:
            raise LauncherError(
                code="LOCAL_CODE_TEST_INPUT_TOO_LARGE",
                message="Test message exceeds 32 KiB",
                status_code=422,
            )
        try:
            json.dumps(parameters, allow_nan=False)
        except (TypeError, ValueError):
            raise LauncherError(
                code="LOCAL_CODE_TEST_PARAMETERS_INVALID",
                message="Test parameters must be JSON compatible",
                status_code=422,
            ) from None

        lock = self._locks.setdefault(connection_id, asyncio.Lock())
        async with lock:
            stored = self._read_stored(connection_id)
            if stored is None:
                raise ConflictError(
                    "LOCAL_CODE_NOT_CONFIGURED",
                    "Save local Agent code before sending a test message",
                )
            if (
                not self.hub.is_connected(connection_id)
                or self.process_manager.runtime_state(connection_id) != "RUNNING"
                or self.process_manager.active_revision(connection_id) != stored.revision
            ):
                raise ConflictError(
                    "MANAGED_CODE_NOT_RUNNING",
                    "Managed Agent code is not currently connected",
                )
            test_id = str(uuid4())
            context = {
                "local_test": True,
                "schema_version": 1,
                "experiment_plan_id": "local-test-plan",
                "experiment_run_id": "local-test-run",
                "experiment_version": "local-test",
                "dataset_item_id": test_id,
                "local_launch_id": "local-test-launch",
            }
            context_xml = render_experiment_context(
                build_experiment_context_template(
                    experiment_plan_id=context["experiment_plan_id"],
                    experiment_version=context["experiment_version"],
                    local_launch_id=context["local_launch_id"],
                ),
                experiment_run_id=context["experiment_run_id"],
                dataset_item_id=context["dataset_item_id"],
            )
            experiment_input = append_experiment_context(message, context_xml)
            task_input = {
                "id": test_id,
                "question": experiment_input,
                "input": experiment_input,
            }
            task_metadata = {
                "experiment_id": context["experiment_run_id"],
                "experiment_plan_id": context["experiment_plan_id"],
                "experiment_version": context["experiment_version"],
                "dataset_item_id": context["dataset_item_id"],
                "local_launch_id": context["local_launch_id"],
                "experiment_context": context,
                "experiment_context_xml": context_xml,
                "experiment_input": experiment_input,
                "experiment_parameters": parameters,
                "original_input": {"id": test_id, "question": message, "input": message},
            }
            payload = {
                "type": "connection.test",
                "requestId": test_id,
                "input": experiment_input,
                "message": message,
                "originalInput": {
                    "id": test_id,
                    "question": message,
                    "input": message,
                },
                "sdkTask": {
                    "id": test_id,
                    "input": task_input,
                    "groundTruth": None,
                    "tags": None,
                    "metadata": task_metadata,
                },
                "context": context,
                "parameters": parameters,
            }
            started = time.monotonic()
            exact_secrets = [self.secrets.get(connection.token_secret_ref), stored.source]
            try:
                result = await self.hub.execute(
                    connection_id,
                    payload,
                    timeout_seconds=timeout_seconds,
                )
            except AgentExecutionTimeoutError:
                return self._failed_test(
                    test_id,
                    stored.revision,
                    started,
                    status="TIMED_OUT",
                    code="AGENT_TEST_TIMEOUT",
                    message="Managed Agent test timed out",
                    response_received=False,
                )
            except AgentExecutionError as exc:
                provider_error = exc.provider_error
                redaction_count = 0
                message_text = "Managed Agent test failed"
                if provider_error:
                    safe_error, redaction_count = _redact(
                        provider_error,
                        exact_secrets=exact_secrets,
                    )
                    message_text = f"Agent execution error: {safe_error}"
                return self._failed_test(
                    test_id,
                    stored.revision,
                    started,
                    status="FAILED",
                    code="AGENT_TEST_FAILED",
                    message=message_text,
                    response_received=True,
                    redaction_count=redaction_count,
                )
            except AgentConnectionUnavailableError:
                return self._failed_test(
                    test_id,
                    stored.revision,
                    started,
                    status="FAILED",
                    code="AGENT_CONNECTION_UNAVAILABLE",
                    message="Managed Agent connection became unavailable",
                    response_received=False,
                )

            try:
                sanitized_output, output_redactions = _sanitize_output(
                    result.output,
                    exact_secrets=exact_secrets,
                )
                sanitized_meta, meta_redactions = _sanitize_meta(
                    result,
                    exact_secrets=exact_secrets,
                )
            except (TypeError, ValueError):
                return self._failed_test(
                    test_id,
                    stored.revision,
                    started,
                    status="FAILED",
                    code="AGENT_TEST_OUTPUT_INVALID",
                    message="Managed Agent returned an invalid JSON output",
                    response_received=True,
                )
            if not result.success:
                return LocalCodeTestResult(
                    test_id=test_id,
                    status="FAILED",
                    duration_ms=_elapsed_ms(started),
                    code_revision=stored.revision,
                    response_received=True,
                    sanitized_output=sanitized_output,
                    sanitized_meta=sanitized_meta,
                    redaction_count=output_redactions + meta_redactions,
                    tested_at=_utc_now(),
                    error_code="AGENT_SOLUTION_FAILED",
                    error_message="Solution Provider returned success=false",
                )
            return LocalCodeTestResult(
                test_id=test_id,
                status="SUCCEEDED",
                duration_ms=_elapsed_ms(started),
                code_revision=stored.revision,
                response_received=True,
                sanitized_output=sanitized_output,
                sanitized_meta=sanitized_meta,
                redaction_count=output_redactions + meta_redactions,
                tested_at=_utc_now(),
            )

    async def delete(self, connection_id: str) -> None:
        lock = self._locks.setdefault(connection_id, asyncio.Lock())
        async with lock:
            await self.process_manager.stop(connection_id)
            self.secrets.delete(self._code_reference(connection_id))

    async def stop(self, connection_id: str) -> None:
        lock = self._locks.setdefault(connection_id, asyncio.Lock())
        async with lock:
            await self.process_manager.stop(connection_id)

    async def close(self) -> None:
        await self.process_manager.close()

    def _connection(self, connection_id: str):
        connection = self.repository.get(connection_id)
        if connection is None:
            raise NotFoundError(
                "AGENT_CONNECTION_NOT_FOUND",
                f"Agent connection not found: {connection_id}",
            )
        return connection

    def _read_stored(self, connection_id: str) -> _StoredLocalCode | None:
        reference = self._code_reference(connection_id)
        if not self.secrets.exists(reference):
            return None
        try:
            return _StoredLocalCode.from_json(self.secrets.get(reference))
        except (KeyError, RuntimeError):
            raise LauncherError(
                code="LOCAL_CODE_STORE_INVALID",
                message="Stored local Agent code cannot be read",
                status_code=500,
            ) from None

    def _configuration(
        self,
        connection_id: str,
        stored: _StoredLocalCode,
    ) -> LocalCodeConfiguration:
        return LocalCodeConfiguration(
            language=stored.language,
            source=stored.source,
            configured=True,
            revision=stored.revision,
            source_sha256=stored.source_sha256,
            runtime_state=self.process_manager.runtime_state(connection_id),
            updated_at=stored.updated_at,
        )

    @staticmethod
    def _code_reference(connection_id: str) -> str:
        return f"agent-connection/{connection_id}/local-code"

    @staticmethod
    def _failed_test(
        test_id: str,
        revision: int,
        started: float,
        *,
        status: str,
        code: str,
        message: str,
        response_received: bool,
        redaction_count: int = 0,
    ) -> LocalCodeTestResult:
        return LocalCodeTestResult(
            test_id=test_id,
            status=status,
            duration_ms=_elapsed_ms(started),
            code_revision=revision,
            response_received=response_received,
            sanitized_output=None,
            sanitized_meta={},
            redaction_count=redaction_count,
            tested_at=_utc_now(),
            error_code=code,
            error_message=message,
        )


def _validate_source(source: str) -> None:
    if not isinstance(source, str) or not source.strip():
        raise LauncherError(
            code="LOCAL_CODE_INVALID",
            message="Python source must not be empty",
            status_code=422,
        )
    if len(source.encode("utf-8")) > MAX_LOCAL_CODE_BYTES:
        raise LauncherError(
            code="LOCAL_CODE_TOO_LARGE",
            message="Python source exceeds 64 KiB",
            status_code=422,
        )
    try:
        tree = compile(
            source,
            "<managed-local-agent>",
            "exec",
            flags=ast.PyCF_ONLY_AST,
            dont_inherit=True,
        )
    except SyntaxError as exc:
        raise LauncherError(
            code="LOCAL_CODE_SYNTAX_ERROR",
            message="Python source contains a syntax error",
            status_code=422,
            line=exc.lineno,
            column=exc.offset,
        ) from None
    assert isinstance(tree, ast.Module)
    async_functions = {node.name for node in tree.body if isinstance(node, ast.AsyncFunctionDef)}
    legacy_handle = any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "handle"
        for node in tree.body
    )
    forbidden_names = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id in {"AgentLoopConfig", "run_experiment_parallel"}
    }
    has_main_block = any(
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
        for node in tree.body
    )
    if forbidden_names or has_main_block:
        raise LauncherError(
            code="LOCAL_CODE_OUTER_RUNNER_FORBIDDEN",
            message=(
                "Paste only solution_http(task); AgentLoopConfig, "
                "run_experiment_parallel, and main belong to the outer platform"
            ),
            status_code=422,
        )
    if not (legacy_handle or "solution_http" in async_functions):
        raise LauncherError(
            code="LOCAL_CODE_SOLUTION_REQUIRED",
            message="Python source must define async solution_http(task)",
            status_code=422,
        )


def _source_digest(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


async def _send_worker_envelope(
    process: asyncio.subprocess.Process,
    value: dict[str, Any],
) -> None:
    if process.stdin is None:
        raise RuntimeError("managed Agent stdin is unavailable")
    encoded = bytearray(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    try:
        process.stdin.write(encoded)
        await process.stdin.drain()
        process.stdin.close()
        await process.stdin.wait_closed()
    finally:
        encoded[:] = b"\0" * len(encoded)
        value.clear()


def _managed_agent_command() -> tuple[str, ...]:
    if getattr(sys, "frozen", False):
        return (sys.executable, "managed-agent")
    return (sys.executable, "-m", "app.cli", "managed-agent")


def _minimal_worker_environment() -> dict[str, str]:
    environment = {
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if not getattr(sys, "frozen", False):
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    for name in ("LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def _worker_cwd() -> str:
    return str(Path("/") if os.name == "posix" else Path.cwd().anchor)


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))


def _output_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "array"
    if isinstance(value, dict):
        return "object"
    # json.dumps has already rejected non-JSON values before this helper is used.
    return "string"


def _sanitize_output(
    value: Any,
    *,
    exact_secrets: list[str],
) -> tuple[SanitizedOutput, int]:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    preview = value if isinstance(value, str) else serialized
    preview, redactions = _redact(str(preview), exact_secrets=exact_secrets)
    truncated = len(preview) > MAX_TEST_PREVIEW_CHARS
    if truncated:
        preview = preview[:MAX_TEST_PREVIEW_CHARS]
    return (
        SanitizedOutput(type=_output_type(value), preview=preview, truncated=truncated),
        redactions,
    )


def _sanitize_meta(
    result: AgentExecutionResult,
    *,
    exact_secrets: list[str],
) -> tuple[dict[str, str], int]:
    sanitized: dict[str, str] = {}
    redactions = 0
    for key in _ALLOWED_META_KEYS:
        raw = result.meta.get(key)
        if raw is None:
            continue
        value = str(raw)
        if key == "traceId" and (
            not _TRACE_ID.fullmatch(value) or not any(character != "0" for character in value)
        ):
            continue
        value, count = _redact(value[:256], exact_secrets=exact_secrets)
        sanitized[key] = value
        redactions += count
    return sanitized, redactions


def _redact(value: str, *, exact_secrets: list[str]) -> tuple[str, int]:
    result = value
    count = 0
    for secret in exact_secrets:
        if secret and secret in result:
            occurrences = result.count(secret)
            result = result.replace(secret, "[REDACTED]")
            count += occurrences
    result, access_key_count = _ALIYUN_ACCESS_KEY.subn("[REDACTED]", result)
    count += access_key_count

    def replace_assignment(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"{match.group(1)}{match.group(2)}[REDACTED]"

    result = _SENSITIVE_ASSIGNMENT.sub(replace_assignment, result)
    return result, count
