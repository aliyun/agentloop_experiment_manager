from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from xml.etree import ElementTree

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from app import agent_connector
from app.api.schemas import LocalCodeTestRequest
from app.cli import run_managed_agent
from app.config import Settings
from app.domain.errors import ConflictError, LauncherError
from app.main import create_app
from app.runtime import AppContainer, build_container
from app.secrets import EncryptedFileSecretStore
from app.services import (
    AgentExecutionError,
    AgentExecutionResult,
    AgentExecutionTimeoutError,
    ManagedAgentProcessManager,
)
from app.services.managed_code import STARTER_SOURCE
from app.storage import in_memory_database


class FakeManagedProcessManager:
    def __init__(self) -> None:
        self.revisions: dict[str, int] = {}
        self.activations: list[tuple[str, str, int]] = []
        self.stop_calls: list[str] = []
        self.external_online: set[str] = set()
        self.activation_error: BaseException | None = None
        self.closed = False
        self.execution_output: Any = "ok"
        self.execution_meta: dict[str, Any] = {}
        self.execution_success = True
        self.execution_error: BaseException | None = None
        self.executions: list[tuple[str, dict[str, Any], float]] = []

    def is_connected(self, connection_id: str) -> bool:
        return connection_id in self.revisions or connection_id in self.external_online

    def runtime_state(self, connection_id: str) -> str:
        return "RUNNING" if connection_id in self.revisions else "STOPPED"

    def active_revision(self, connection_id: str) -> int | None:
        return self.revisions.get(connection_id)

    async def ensure_activation_allowed(self, connection_id: str) -> None:
        if connection_id in self.external_online and connection_id not in self.revisions:
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
        del source
        if self.activation_error is not None:
            raise self.activation_error
        assert token
        self.activations.append((connection_id, connect_url, revision))
        self.revisions[connection_id] = revision

    async def stop(self, connection_id: str) -> None:
        self.stop_calls.append(connection_id)
        self.revisions.pop(connection_id, None)

    async def close(self) -> None:
        self.closed = True
        self.revisions.clear()

    async def execute(
        self,
        connection_id: str,
        payload: dict[str, Any],
        timeout_seconds: float = 300,
    ) -> AgentExecutionResult:
        self.executions.append((connection_id, payload, timeout_seconds))
        if self.execution_error is not None:
            raise self.execution_error
        return AgentExecutionResult(
            task_id="managed-test-task",
            output=self.execution_output,
            meta=self.execution_meta,
            success=self.execution_success,
        )


def _make_client(
    tmp_path: Path,
) -> tuple[TestClient, AppContainer, FakeManagedProcessManager, Path]:
    settings = Settings(
        environment="test",
        data_dir=tmp_path,
        gateway_mode="fake",
    )
    secrets_path = tmp_path / "secrets.enc"
    secret_store = EncryptedFileSecretStore(
        secrets_path,
        Fernet.generate_key().decode("ascii"),
    )
    process_manager = FakeManagedProcessManager()
    container = build_container(
        settings,
        database=in_memory_database(),
        secret_store=secret_store,
        managed_process_manager=process_manager,
    )
    container.agent_connection_hub.is_connected = process_manager.is_connected  # type: ignore[method-assign]
    container.agent_connection_hub.execute = process_manager.execute  # type: ignore[method-assign]
    client = TestClient(create_app(settings=settings, container=container))
    return client, container, process_manager, secrets_path


def _create_connection(client: TestClient) -> tuple[str, str]:
    response = client.post("/api/v1/agent-connections", json={"name": "Managed Agent"})
    assert response.status_code == 201, response.text
    body = response.json()
    return body["agentConnectionId"], body["token"]


def _sqlite_dump(container: AppContainer) -> str:
    values: list[str] = []
    with container.database.engine.connect() as connection:
        for table in inspect(container.database.engine).get_table_names():
            rows = connection.exec_driver_sql(f'SELECT * FROM "{table}"').fetchall()
            values.extend(repr(row) for row in rows)
    return "\n".join(values)


def test_starter_source_builds_the_default_customer_agent_request() -> None:
    namespace: dict[str, Any] = {}
    exec(STARTER_SOURCE, namespace)
    build_request_body = namespace["build_request_body"]

    task = SimpleNamespace(
        input={
            "question": "question-with-experiment-context",
            "input": "lower-priority-input",
            "query": "lower-priority-query",
            "prompt": "lower-priority-prompt",
        },
        metadata={"experiment_parameters": {"model": "customer-model-v2"}},
    )
    assert build_request_body(task) == {
        "content": "question-with-experiment-context",
        "model": "customer-model-v2",
    }

    assert build_request_body(
        SimpleNamespace(input={"query": "query-fallback"}, metadata={}),
    ) == {
        "content": "query-fallback",
        "model": "local-smoke",
    }
    assert build_request_body(
        SimpleNamespace(input="plain-string-input", metadata=None),
    ) == {
        "content": "plain-string-input",
        "model": "local-smoke",
    }


def test_local_code_api_starter_activation_test_and_encrypted_storage(tmp_path: Path) -> None:
    client, container, process_manager, secrets_path = _make_client(tmp_path)
    with client:
        connection_id, token = _create_connection(client)
        base = f"/api/v1/agent-connections/{connection_id}"

        starter = client.get(f"{base}/local-code")
        assert starter.status_code == 200, starter.text
        assert starter.headers["cache-control"] == "no-store"
        assert starter.json()["configured"] is False
        assert starter.json()["revision"] == 0
        assert "def build_request_body(task: Task)" in starter.json()["source"]
        assert "async def solution_http(task: Task" in starter.json()["source"]
        assert "run_experiment_parallel" not in starter.json()["source"]

        source = """async def handle(payload):
    return {"output": payload["input"], "meta": {"agentName": "source-canary"}}
"""
        saved = client.put(
            f"{base}/local-code",
            json={"source": source, "expectedRevision": 0},
        )
        assert saved.status_code == 200, saved.text
        assert saved.headers["cache-control"] == "no-store"
        assert saved.json()["configured"] is True
        assert saved.json()["revision"] == 1
        assert saved.json()["runtimeState"] == "RUNNING"
        assert len(saved.json()["sourceSha256"]) == 64
        assert source.encode() not in secrets_path.read_bytes()
        assert "source-canary" not in _sqlite_dump(container)

        process_manager.execution_output = f"{source}|{token}|password=visible-secret"
        process_manager.execution_meta = {
            "traceId": "1" * 32,
            "agentName": token,
            "agentVersion": "1.2.3",
            "debug": "must-not-leak",
        }
        tested = client.post(
            f"{base}/test",
            json={
                "message": "test-message-canary",
                "parameters": {"temperature": 0},
                "timeoutSeconds": 120,
            },
        )
        assert tested.status_code == 200, tested.text
        assert tested.headers["cache-control"] == "no-store"
        result = tested.json()
        assert result["status"] == "SUCCEEDED"
        assert result["responseReceived"] is True
        assert result["codeRevision"] == 1
        assert result["sanitizedOutput"]["type"] == "string"
        assert result["sanitizedOutput"]["preview"].count("[REDACTED]") >= 3
        assert result["redactionCount"] >= 4
        assert result["sanitizedMeta"] == {
            "traceId": "1" * 32,
            "agentName": "[REDACTED]",
            "agentVersion": "1.2.3",
        }
        assert token not in tested.text
        assert "source-canary" not in tested.text
        assert "must-not-leak" not in tested.text

        _, payload, timeout = process_manager.executions[-1]
        assert timeout == 120
        assert payload["message"] == "test-message-canary"
        assert payload["parameters"] == {"temperature": 0}
        assert payload["context"]["local_test"] is True
        experiment_input = payload["input"]
        question, xml = experiment_input.split("\n", 1)
        assert question == "test-message-canary"
        context_root = ElementTree.fromstring(xml)
        assert context_root.tag == "experiment_context"
        assert context_root.attrib == {"schema_version": "1"}
        context_values = {child.tag: child.text for child in context_root}
        assert context_values == {
            "experiment_plan_id": "local-test-plan",
            "experiment_run_id": "local-test-run",
            "experiment_version": "local-test",
            "dataset_item_id": result["testId"],
            "local_launch_id": "local-test-launch",
        }

        sdk_task = payload["sdkTask"]
        assert sdk_task["id"] == result["testId"]
        assert sdk_task["input"] == {
            "id": result["testId"],
            "question": experiment_input,
            "input": experiment_input,
        }
        assert sdk_task["groundTruth"] is None
        assert sdk_task["tags"] is None
        assert sdk_task["metadata"] == {
            "experiment_id": "local-test-run",
            "experiment_plan_id": "local-test-plan",
            "experiment_version": "local-test",
            "dataset_item_id": result["testId"],
            "local_launch_id": "local-test-launch",
            "experiment_context": payload["context"],
            "experiment_context_xml": xml,
            "experiment_input": experiment_input,
            "experiment_parameters": {"temperature": 0},
            "original_input": {
                "id": result["testId"],
                "question": "test-message-canary",
                "input": "test-message-canary",
            },
        }
        assert "test-message-canary" not in _sqlite_dump(container)

        process_manager.execution_output = ("first", "second")
        tuple_result = client.post(f"{base}/test", json={"message": "tuple result"})
        assert tuple_result.status_code == 200, tuple_result.text
        assert tuple_result.json()["sanitizedOutput"] == {
            "type": "array",
            "preview": '["first","second"]',
            "truncated": False,
        }

    assert process_manager.closed is True


def test_local_code_test_preserves_failed_agent_output_and_safe_error_details(
    tmp_path: Path,
) -> None:
    client, _, process_manager, _ = _make_client(tmp_path)
    with client:
        connection_id, token = _create_connection(client)
        base = f"/api/v1/agent-connections/{connection_id}"
        source = "async def handle(payload):\n    return payload['input']\n"
        saved = client.put(
            f"{base}/local-code",
            json={"source": source, "expectedRevision": 0, "activate": True},
        )
        assert saved.status_code == 200, saved.text

        process_manager.execution_success = False
        process_manager.execution_output = {
            "answer": "wrong",
            "diagnostic": "password=provider-secret",
        }
        process_manager.execution_meta = {"traceId": "2" * 32}
        failed_solution = client.post(f"{base}/test", json={"message": "test failure"})
        assert failed_solution.status_code == 200, failed_solution.text
        failed_body = failed_solution.json()
        assert failed_body["status"] == "FAILED"
        assert failed_body["responseReceived"] is True
        assert failed_body["errorCode"] == "AGENT_SOLUTION_FAILED"
        assert failed_body["sanitizedOutput"]["type"] == "object"
        assert "wrong" in failed_body["sanitizedOutput"]["preview"]
        assert "provider-secret" not in failed_solution.text
        assert "[REDACTED]" in failed_body["sanitizedOutput"]["preview"]
        assert failed_body["sanitizedMeta"] == {"traceId": "2" * 32}

        process_manager.execution_error = AgentExecutionError(
            "Customer Agent reported task failure",
            provider_error=(
                f"NameError: name 'httpx' is not defined; token={token}; password=visible-secret"
            ),
        )
        failed_execution = client.post(f"{base}/test", json={"message": "test error"})
        assert failed_execution.status_code == 200, failed_execution.text
        error_body = failed_execution.json()
        assert error_body["status"] == "FAILED"
        assert error_body["responseReceived"] is True
        assert error_body["sanitizedOutput"] is None
        assert error_body["errorCode"] == "AGENT_TEST_FAILED"
        assert "NameError" in error_body["errorMessage"]
        assert token not in failed_execution.text
        assert "visible-secret" not in failed_execution.text
        assert error_body["redactionCount"] >= 2

        process_manager.execution_error = AgentExecutionTimeoutError("timed out")
        timed_out = client.post(f"{base}/test", json={"message": "test timeout"})
        assert timed_out.status_code == 200, timed_out.text
        timeout_body = timed_out.json()
        assert timeout_body["status"] == "TIMED_OUT"
        assert timeout_body["responseReceived"] is False
        assert timeout_body["sanitizedOutput"] is None


def test_save_only_stops_old_revision_and_external_agent_cannot_be_tested(
    tmp_path: Path,
) -> None:
    client, container, process_manager, _ = _make_client(tmp_path)
    with client:
        connection_id, _ = _create_connection(client)
        base = f"/api/v1/agent-connections/{connection_id}"
        first_source = "async def handle(payload):\n    return payload['input']\n"
        second_source = "async def handle(payload):\n    return 'saved-only'\n"

        first = client.put(
            f"{base}/local-code",
            json={"source": first_source, "expectedRevision": 0, "activate": True},
        )
        assert first.status_code == 200, first.text
        assert process_manager.active_revision(connection_id) == 1

        second = client.put(
            f"{base}/local-code",
            json={"source": second_source, "expectedRevision": 1, "activate": False},
        )
        assert second.status_code == 200, second.text
        assert second.json()["runtimeState"] == "STOPPED"
        assert process_manager.active_revision(connection_id) is None
        assert connection_id in process_manager.stop_calls

        process_manager.external_online.add(connection_id)
        blocked = client.post(f"{base}/test", json={"message": "do not dispatch"})
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["error"]["code"] == "MANAGED_CODE_NOT_RUNNING"
        assert process_manager.executions == []

        external_write = client.put(
            f"{base}/local-code",
            json={"source": first_source, "expectedRevision": 2, "activate": True},
        )
        assert external_write.status_code == 409, external_write.text
        assert external_write.json()["error"]["code"] == "AGENT_CONNECTION_IN_USE"
        unchanged = client.get(f"{base}/local-code").json()
        assert unchanged["revision"] == 2
        assert unchanged["source"] == second_source
        assert connection_id in process_manager.external_online
        assert "do not dispatch" not in _sqlite_dump(container)


def test_failed_activation_rolls_back_revision_and_cas_and_syntax_are_safe(
    tmp_path: Path,
) -> None:
    client, _, process_manager, _ = _make_client(tmp_path)
    with client:
        connection_id, _ = _create_connection(client)
        path = f"/api/v1/agent-connections/{connection_id}/local-code"
        old_source = "async def handle(payload):\n    return 'old'\n"
        new_source = "async def handle(payload):\n    return 'new'\n"
        assert (
            client.put(
                path,
                json={"source": old_source, "expectedRevision": 0, "activate": False},
            ).status_code
            == 200
        )

        process_manager.activation_error = LauncherError(
            code="MANAGED_CODE_START_TIMEOUT",
            message="Managed Agent did not connect before the startup timeout",
            status_code=504,
            retryable=True,
        )
        failed = client.put(
            path,
            json={"source": new_source, "expectedRevision": 1, "activate": True},
        )
        assert failed.status_code == 504, failed.text
        assert failed.json()["error"]["code"] == "MANAGED_CODE_START_TIMEOUT"
        restored = client.get(path).json()
        assert restored["revision"] == 1
        assert restored["source"] == old_source

        stale = client.put(
            path,
            json={"source": new_source, "expectedRevision": 0, "activate": False},
        )
        assert stale.status_code == 409, stale.text
        assert stale.json()["error"]["code"] == "LOCAL_CODE_REVISION_CONFLICT"

        syntax = client.put(
            path,
            json={"source": "async def handle(payload)\n    pass\n", "expectedRevision": 1},
        )
        assert syntax.status_code == 422, syntax.text
        assert syntax.json()["error"]["code"] == "LOCAL_CODE_SYNTAX_ERROR"
        assert syntax.json()["error"]["line"] == 1
        assert syntax.json()["error"]["column"] is not None

        full_experiment_script = """from agentloop_sdk import (
    AgentLoopConfig,
    SolutionOutput,
    Task,
    run_experiment_parallel,
)


async def solution_http(task: Task) -> SolutionOutput:
    return SolutionOutput(success=True, output=task.input, trajectory=[])


if __name__ == "__main__":
    raise RuntimeError("outer experiment must not run here")
"""
        wrong_boundary = client.put(
            path,
            json={
                "source": full_experiment_script,
                "expectedRevision": 1,
                "activate": False,
            },
        )
        assert wrong_boundary.status_code == 422, wrong_boundary.text
        assert wrong_boundary.json()["error"]["code"] == "LOCAL_CODE_OUTER_RUNNER_FORBIDDEN"

        too_large = "async def handle(payload):\n    return payload\n#" + ("x" * (64 * 1024))
        rejected = client.put(
            path,
            json={"source": too_large, "expectedRevision": 1, "activate": False},
        )
        assert rejected.status_code == 422, rejected.text
        assert rejected.json()["error"]["code"] == "LOCAL_CODE_TOO_LARGE"
        assert too_large not in rejected.text


def test_rotate_stops_worker_and_delete_removes_code(tmp_path: Path) -> None:
    client, container, process_manager, _ = _make_client(tmp_path)
    with client:
        connection_id, _ = _create_connection(client)
        base = f"/api/v1/agent-connections/{connection_id}"
        source = "async def handle(payload):\n    return payload['input']\n"
        assert (
            client.put(
                f"{base}/local-code",
                json={"source": source, "expectedRevision": 0},
            ).status_code
            == 200
        )

        rotated = client.post(f"{base}/rotate-token")
        assert rotated.status_code == 200, rotated.text
        assert process_manager.active_revision(connection_id) is None
        retained = client.get(f"{base}/local-code")
        assert retained.status_code == 200
        assert retained.json()["revision"] == 1
        assert retained.json()["runtimeState"] == "STOPPED"

        deleted = client.delete(base)
        assert deleted.status_code == 204, deleted.text
        assert not container.secret_store.exists(f"agent-connection/{connection_id}/local-code")
        assert connection_id in process_manager.stop_calls


def test_active_managed_code_resumes_after_platform_restart(tmp_path: Path) -> None:
    client, container, process_manager, _ = _make_client(tmp_path)
    with client:
        connection_id, _ = _create_connection(client)
        source = "async def handle(payload):\n    return payload['input']\n"
        saved = client.put(
            f"/api/v1/agent-connections/{connection_id}/local-code",
            json={"source": source, "expectedRevision": 0, "activate": True},
        )
        assert saved.status_code == 200, saved.text
        assert process_manager.active_revision(connection_id) == 1

        process_manager.revisions.clear()
        process_manager.activations.clear()
        client.portal.call(
            lambda: container.managed_code_service.resume_configured(
                lambda candidate: f"ws://127.0.0.1:8090/connect/{candidate}",
                startup_delay_seconds=0,
            )
        )
        assert process_manager.activations == [
            (
                connection_id,
                f"ws://127.0.0.1:8090/connect/{connection_id}",
                1,
            )
        ]


def test_save_without_activation_remains_stopped_after_restart(tmp_path: Path) -> None:
    client, container, process_manager, _ = _make_client(tmp_path)
    with client:
        connection_id, _ = _create_connection(client)
        source = "async def handle(payload):\n    return payload['input']\n"
        saved = client.put(
            f"/api/v1/agent-connections/{connection_id}/local-code",
            json={"source": source, "expectedRevision": 0, "activate": False},
        )
        assert saved.status_code == 200, saved.text

        process_manager.activations.clear()
        client.portal.call(
            lambda: container.managed_code_service.resume_configured(
                lambda candidate: f"ws://127.0.0.1:8090/connect/{candidate}",
                startup_delay_seconds=0,
            )
        )
        assert process_manager.activations == []


def test_test_timeout_contract_and_tuple_output_type() -> None:
    assert LocalCodeTestRequest(message="ok", timeoutSeconds=1).timeout_seconds == 1
    assert LocalCodeTestRequest(message="ok", timeoutSeconds=120).timeout_seconds == 120
    with pytest.raises(ValueError):
        LocalCodeTestRequest(message="", timeoutSeconds=30)
    with pytest.raises(ValueError):
        LocalCodeTestRequest(message="ok", timeoutSeconds=121)


def test_managed_agent_cli_invokes_solution_http_with_sdk_task_and_preserves_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = """from agentloop_sdk import SolutionOutput, Task


async def solution_http(task: Task, *args, **kwargs) -> SolutionOutput:
    return SolutionOutput(
        success=False,
        output={
            "id": task.id,
            "input": task.input,
            "ground_truth": task.ground_truth,
            "tags": task.tags,
        },
        trajectory=[{"type": "text", "text": "provider-trajectory"}],
        meta={
            "traceId": "b" * 32,
            "providerMetadata": task.metadata,
        },
    )


if __name__ == "__main__":
    raise RuntimeError("the platform-managed outer runner must not execute user main")
"""
    envelope = (
        json.dumps(
            {
                "protocolVersion": 1,
                "connectionId": "solution-provider-1",
                "connectUrl": "ws://127.0.0.1:8090/api/v1/agent-connections/x/connect",
                "token": "solution-provider-token",
                "language": "python",
                "source": source,
                "revision": 3,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    sdk_task = {
        "id": "sdk-task-1",
        "input": {
            "id": "dataset-item-1",
            "question": (
                'Where am I?\n<experiment_context schema_version="1"></experiment_context>'
            ),
            "scenario": "game-location",
        },
        "groundTruth": {"answer": "spawn-point"},
        "tags": {"scenario": "game", "difficulty": "smoke"},
        "metadata": {
            "experiment_id": "experiment-run-1",
            "experiment_version": "1.2.3",
            "dataset_item_id": "dataset-item-1",
        },
    }
    observed: dict[str, Any] = {}

    class FakeConnector:
        def __init__(self, **kwargs: Any) -> None:
            observed.update(kwargs)

        async def run(self) -> None:
            observed["result"] = await observed["handler"](
                {
                    "type": "experiment.task",
                    "requestId": "wire-request-1",
                    "sdkTask": sdk_task,
                }
            )

    monkeypatch.setattr(agent_connector, "AgentConnector", FakeConnector)
    stdin = io.TextIOWrapper(io.BytesIO(envelope), encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", stdin)

    assert run_managed_agent() == 0
    result = observed["result"]
    assert isinstance(result, agent_connector.LocalAgentResponse)
    assert result.success is False
    assert result.output == {
        "id": "sdk-task-1",
        "input": sdk_task["input"],
        "ground_truth": {"answer": "spawn-point"},
        "tags": {"scenario": "game", "difficulty": "smoke"},
    }
    assert result.trajectory == [{"type": "text", "text": "provider-trajectory"}]
    assert result.meta == {
        "traceId": "b" * 32,
        "providerMetadata": sdk_task["metadata"],
    }
    assert observed["reconnect"] is False


def test_managed_agent_cli_keeps_legacy_handle_and_accepts_64_kib_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = "async def handle(payload):\n    return payload['input']\n#"
    source = prefix + ("\\" * (64 * 1024 - len(prefix.encode("utf-8"))))
    assert len(source.encode("utf-8")) == 64 * 1024
    envelope = (
        json.dumps(
            {
                "protocolVersion": 1,
                "connectionId": "agent-boundary",
                "connectUrl": "ws://127.0.0.1:8090/api/v1/agent-connections/x/connect",
                "token": "boundary-token",
                "language": "python",
                "source": source,
                "revision": 1,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    assert 128 * 1024 < len(envelope) < 256 * 1024
    observed: dict[str, Any] = {}

    class FakeConnector:
        def __init__(self, **kwargs: Any) -> None:
            observed.update(kwargs)

        async def run(self) -> None:
            observed["result"] = await observed["handler"]({"input": "boundary-ok"})

    monkeypatch.setattr(agent_connector, "AgentConnector", FakeConnector)
    stdin = io.TextIOWrapper(io.BytesIO(envelope), encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", stdin)

    assert run_managed_agent() == 0
    assert observed["connect_url"].startswith("ws://127.0.0.1")
    assert observed["token"] == "boundary-token"
    assert observed["reconnect"] is False
    assert observed["result"] == "boundary-ok"


def test_process_manager_keeps_source_and_token_out_of_argv_and_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeHub:
        connected = False

        def is_connected(self, connection_id: str) -> bool:
            del connection_id
            return self.connected

    hub = FakeHub()
    captured: dict[str, Any] = {}

    class FakeStdin:
        written = bytearray()

        def write(self, value: bytes | bytearray) -> None:
            self.written.extend(value)

        async def drain(self) -> None:
            hub.connected = True

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    class FakeProcess:
        stdin = FakeStdin()
        returncode = None
        pid = 999_999

    async def fake_subprocess(*args: str, **kwargs: Any) -> FakeProcess:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    manager = ManagedAgentProcessManager(  # type: ignore[arg-type]
        hub,
        command=("managed-agent-test",),
    )
    source = "async def handle(payload):\n    return 'sensitive-source'\n"
    token = "sensitive-token"

    asyncio.run(
        manager.activate(
            "agent-1",
            connect_url="ws://127.0.0.1/connect",
            token=token,
            source=source,
            revision=7,
        )
    )

    assert captured["args"] == ("managed-agent-test",)
    assert token not in repr(captured["args"])
    assert source not in repr(captured["args"])
    assert token not in repr(captured["kwargs"]["env"])
    assert source not in repr(captured["kwargs"]["env"])
    assert captured["kwargs"]["stdout"] is asyncio.subprocess.DEVNULL
    assert captured["kwargs"]["stderr"] is asyncio.subprocess.DEVNULL
    worker_envelope = json.loads(FakeProcess.stdin.written)
    assert worker_envelope["token"] == token
    assert worker_envelope["source"] == source
    assert manager.runtime_state("agent-1") == "RUNNING"
    assert manager.active_revision("agent-1") == 7
