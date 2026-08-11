from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest
from agentloop_sdk import (
    AgentLoopEvaluatorStorage,
    AgentLoopGeneralEvaluator,
    SolutionOutput,
    Task,
)
from agentscope.evaluate import BenchmarkBase

from app.agentloop import (
    AgentLoopCloudControl,
    AgentLoopConnection,
    ExperimentPreparationError,
    FakeAgentLoopGateway,
    PlanDetail,
    SubmissionUnknownError,
)
from app.agentloop.cloud_control import _gateway_error
from app.agentloop.offline_runner import (
    AgentLoopOfflineRunner,
    AgentTaskResult,
    OfflineExperimentSpec,
    _AuditedStorage,
    _RunnerBenchmark,
    _RunnerConfig,
    _storage_failure_message,
    _validated_trace_id,
)
from app.agentloop.trace_correlation import (
    TraceCandidate,
    TraceCorrelationError,
    TraceCorrelationResult,
    TraceCorrelationService,
    TraceCorrelationStatus,
    TracePollingPolicy,
)
from app.domain.agent_error import safe_agent_error_category
from app.domain.clock import FixedClock
from app.domain.experiment_context import build_experiment_context_template
from app.services import (
    AgentExecutionError,
    AgentExecutionResult,
    BackgroundTaskSupervisor,
    LaunchService,
)
from app.storage import (
    AgentConnectionRepository,
    LaunchIntentRepository,
    ProfileRepository,
    RunnerState,
    ScheduleRepository,
    SkipReason,
    SubmissionState,
    in_memory_database,
)


class _CapturingDispatcher:
    def __init__(self) -> None:
        self.connection_id: str | None = None
        self.payload: dict | None = None
        self.timeout: float | None = None

    async def execute(self, connection_id, payload, timeout_seconds=300):
        self.connection_id = connection_id
        self.payload = payload
        self.timeout = timeout_seconds
        return AgentExecutionResult(
            task_id="wire-task-1",
            output="5",
            meta={"traceId": "a" * 32, "agentVersion": "1.0.0"},
            success=True,
            trajectory=[{"type": "text", "text": "provider-trajectory"}],
        )


class _FailingDispatcher:
    async def execute(self, connection_id, payload, timeout_seconds=300):
        del connection_id, payload, timeout_seconds
        raise AgentExecutionError(
            "Customer Agent reported task failure",
            provider_error=(
                "ReadTimeout: request to https://agent.example/?token=secret-canary-123 timed out"
            ),
        )


def test_provider_error_category_is_allow_listed_and_payload_free() -> None:
    assert (
        safe_agent_error_category(
            provider_error="ReadTimeout: https://agent.example/?token=secret-canary-123"
        )
        == "ReadTimeout"
    )
    assert (
        safe_agent_error_category(
            provider_error="SecretBearingError: password=secret-canary-123"
        )
        == "ProviderError"
    )
    assert (
        safe_agent_error_category(
            provider_error="ReadTimeoutWithSecret: secret-canary-123"
        )
        == "ProviderError"
    )


def test_runner_builds_a_bounded_trace_polling_policy(tmp_path) -> None:
    runner = AgentLoopOfflineRunner(
        _CapturingDispatcher(),
        result_dir=tmp_path,
        trace_lookup_timeout_seconds=321,
    )
    assert runner.trace_polling_policy.timeout_seconds == 321
    assert TracePollingPolicy().timeout_seconds == 180

    for invalid_timeout in (60, 1801):
        with pytest.raises(ValueError, match="greater than 60 and at most 1800"):
            AgentLoopOfflineRunner(
                _CapturingDispatcher(),
                result_dir=tmp_path,
                trace_lookup_timeout_seconds=invalid_timeout,
            )


class _AcceptingTraceResolver:
    """Test-only resolver that models an already verified Trace Store result."""

    def __init__(self, default_trace_id: str = "d" * 32) -> None:
        self.default_trace_id = default_trace_id

    async def resolve(self, context, *, hinted_trace_id=None, on_progress=None):
        del on_progress
        trace_id = hinted_trace_id or self.default_trace_id
        return TraceCorrelationResult(
            trace_id=trace_id,
            source="test_verified_trace",
            attempts=1,
            started_at_ms=context.agent_completed_at_ms,
            resolved_at_ms=context.agent_completed_at_ms,
        )


def _query_response(
    rows: list[list[str]],
    *,
    progress: str = "Complete",
    truncated: bool = False,
    count: int | None = None,
    columns: list[str] | None = None,
):
    response_columns = columns or ["id", "question"]
    return SimpleNamespace(
        body=SimpleNamespace(
            columns=response_columns,
            column_types=["text"] * len(response_columns),
            rows=rows,
            meta=SimpleNamespace(
                progress=progress,
                truncation=SimpleNamespace(truncated=truncated),
                count=len(rows) if count is None else count,
            ),
        )
    )


def _benchmark_for_client(client, *, max_rows: int = 1_000) -> _RunnerBenchmark:
    benchmark = object.__new__(_RunnerBenchmark)
    benchmark.config = SimpleNamespace(
        agent_space="space-1",
        dataset="dataset-1",
        selected_item_ids=[],
        query_sql="",
        max_rows=max_rows,
    )
    benchmark._get_client = lambda: client
    return benchmark


def test_stopped_cloud_plan_status_is_informational() -> None:
    evaluator = SimpleNamespace(
        config={"variables": []},
        variable_mapping={},
        evaluator_ref="eval-1",
        name="eval-1",
    )
    body = SimpleNamespace(
        plan_id="plan-stopped",
        plan_name="Stopped Plan",
        experiment_type="OFFLINE",
        status="stopped",
        description=None,
        dataset_id="dataset-1",
        experiments=[],
        evaluators=[evaluator],
        created_at=None,
        updated_at=None,
    )
    control = AgentLoopCloudControl()
    control._client = lambda connection: SimpleNamespace(  # type: ignore[method-assign]
        get_experiment_plan=lambda agent_space, plan_id, request: SimpleNamespace(body=body)
    )

    detail = control._get_plan(
        "plan-stopped",
        AgentLoopConnection(
            region_id="cn-hangzhou",
            agent_space="space-1",
            access_key_id="test-ak",
            access_key_secret="test-sk",
        ),
    )

    assert detail.status == "stopped"
    assert detail.can_launch_locally is True
    assert detail.cannot_launch_reason is None


def test_benchmark_fails_closed_on_later_page_error_without_logging(caplog) -> None:
    class Client:
        calls = 0

        def execute_query(self, agent_space, dataset, request):
            del agent_space, dataset, request
            self.calls += 1
            if self.calls == 1:
                return _query_response([[f"item-{index}", "question"] for index in range(100)])
            raise RuntimeError("dataset-query-secret-canary")

    caplog.set_level("DEBUG")
    client = Client()
    benchmark = _benchmark_for_client(client)

    with pytest.raises(RuntimeError, match="AgentLoop Dataset query failed") as caught:
        benchmark._load_data()

    assert client.calls == 2
    assert "dataset-query-secret-canary" not in repr(caught.value)
    assert "dataset-query-secret-canary" not in caplog.text
    assert "item-0" not in caplog.text


@pytest.mark.parametrize(
    ("progress", "truncated", "message"),
    [
        ("Running", False, "did not complete"),
        ("Complete", True, "response was truncated"),
    ],
)
def test_benchmark_rejects_incomplete_query_metadata(
    progress,
    truncated,
    message,
) -> None:
    client = SimpleNamespace(
        execute_query=lambda agent_space, dataset, request: _query_response(
            [["item-1", "question"]],
            progress=progress,
            truncated=truncated,
        )
    )
    benchmark = _benchmark_for_client(client)

    with pytest.raises(RuntimeError, match=message):
        benchmark._load_data()


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (_query_response([["item-1", "question"]], count=2), "row count"),
        (
            _query_response(
                [["item-1", "question"]],
                columns=["id", "id"],
            ),
            "duplicate columns",
        ),
    ],
)
def test_benchmark_rejects_inconsistent_query_shape(response, message) -> None:
    client = SimpleNamespace(execute_query=lambda agent_space, dataset, request: response)

    with pytest.raises(RuntimeError, match=message):
        _benchmark_for_client(client)._load_data()

    missing_types = _query_response([["item-1", "question"]])
    missing_types.body.column_types = None
    missing_type_client = SimpleNamespace(
        execute_query=lambda agent_space, dataset, request: missing_types
    )
    with pytest.raises(RuntimeError, match="column metadata"):
        _benchmark_for_client(missing_type_client)._load_data()


def test_benchmark_rejects_duplicates_and_row_limit_without_logging_ids(caplog) -> None:
    class DuplicateClient:
        calls = 0

        def execute_query(self, agent_space, dataset, request):
            del agent_space, dataset, request
            self.calls += 1
            if self.calls == 1:
                return _query_response(
                    [
                        ["duplicate-secret-canary", "one"],
                        ["duplicate-secret-canary", "two"],
                    ]
                )
            return _query_response([])

    caplog.set_level("DEBUG")
    with pytest.raises(RuntimeError, match="duplicate rows") as duplicate_error:
        _benchmark_for_client(DuplicateClient(), max_rows=5)._load_data()
    assert "duplicate-secret-canary" not in repr(duplicate_error.value)
    assert "duplicate-secret-canary" not in caplog.text

    oversized_client = SimpleNamespace(
        execute_query=lambda agent_space, dataset, request: _query_response(
            [
                ["item-1", "one"],
                ["item-2", "two"],
                ["item-3", "three"],
            ]
        )
    )
    with pytest.raises(RuntimeError, match="exceeds the local row limit"):
        _benchmark_for_client(oversized_client, max_rows=2)._load_data()


def test_run_creation_transport_failure_is_submission_unknown(monkeypatch, tmp_path) -> None:
    class OneRowBenchmark:
        def __init__(self, **kwargs):
            del kwargs

        def __len__(self):
            return 1

        def __iter__(self):
            return iter([SimpleNamespace(id="task-1")])

    def fail_storage(**kwargs):
        del kwargs
        raise RuntimeError("create-run-secret-canary")

    monkeypatch.setattr(_RunnerConfig, "resolve_experiment_plan", lambda self: None)
    monkeypatch.setattr(_RunnerConfig, "resolve_sls_project", lambda self: None)
    monkeypatch.setattr(
        AgentLoopOfflineRunner,
        "_validate_plan",
        staticmethod(lambda config: None),
    )
    monkeypatch.setattr(
        "app.agentloop.offline_runner._RunnerBenchmark",
        OneRowBenchmark,
    )
    monkeypatch.setattr("app.agentloop.offline_runner._AuditedStorage", fail_storage)
    runner = AgentLoopOfflineRunner(_CapturingDispatcher(), result_dir=Path(tmp_path))

    async def scenario() -> None:
        with pytest.raises(SubmissionUnknownError) as caught:
            await runner.start(
                OfflineExperimentSpec(
                    launch_id="launch-1",
                    plan_id="plan-1",
                    agent_connection_id="agent-1",
                    experiment_version="1.2.3",
                    context_xml_template=build_experiment_context_template(
                        experiment_plan_id="plan-1",
                        experiment_version="1.2.3",
                        local_launch_id="launch-1",
                    ),
                ),
                lambda record_id: asyncio.sleep(0),
                connection=AgentLoopConnection(
                    region_id="cn-hangzhou",
                    agent_space="space-1",
                    access_key_id="test-ak",
                    access_key_secret="test-sk",
                ),
            )
        assert "create-run-secret-canary" not in repr(caught.value)

    asyncio.run(scenario())


def test_record_persistence_failure_is_submission_unknown(monkeypatch, tmp_path) -> None:
    class OneRowBenchmark:
        def __init__(self, **kwargs):
            del kwargs

        def __len__(self):
            return 1

        def __iter__(self):
            return iter([SimpleNamespace(id="task-1")])

    class CreatedStorage:
        experiment_id = "record-1"
        config = SimpleNamespace()
        _completed_tasks = 0
        _failed_tasks = 0
        _total_tasks = 0

        def __init__(self, **kwargs):
            del kwargs

        def configure_expected_task_keys(self, task_keys):
            assert task_keys == {("task-1", "0")}

    monkeypatch.setattr(_RunnerConfig, "resolve_experiment_plan", lambda self: None)
    monkeypatch.setattr(_RunnerConfig, "resolve_sls_project", lambda self: None)
    monkeypatch.setattr(
        AgentLoopOfflineRunner,
        "_validate_plan",
        staticmethod(lambda config: None),
    )
    monkeypatch.setattr(
        AgentLoopOfflineRunner,
        "_mark_run_failed",
        staticmethod(lambda *args: None),
    )
    monkeypatch.setattr(
        "app.agentloop.offline_runner._RunnerBenchmark",
        OneRowBenchmark,
    )
    monkeypatch.setattr("app.agentloop.offline_runner._AuditedStorage", CreatedStorage)
    runner = AgentLoopOfflineRunner(_CapturingDispatcher(), result_dir=Path(tmp_path))

    async def fail_record_persistence(record_id: str) -> None:
        del record_id
        raise RuntimeError("sqlite-secret-canary")

    async def scenario() -> None:
        with pytest.raises(SubmissionUnknownError) as caught:
            await runner.start(
                OfflineExperimentSpec(
                    launch_id="launch-1",
                    plan_id="plan-1",
                    agent_connection_id="agent-1",
                    experiment_version="1.2.3",
                    context_xml_template=build_experiment_context_template(
                        experiment_plan_id="plan-1",
                        experiment_version="1.2.3",
                        local_launch_id="launch-1",
                    ),
                ),
                fail_record_persistence,
                connection=AgentLoopConnection(
                    region_id="cn-hangzhou",
                    agent_space="space-1",
                    access_key_id="test-ak",
                    access_key_secret="test-sk",
                ),
            )
        assert "sqlite-secret-canary" not in repr(caught.value)

    asyncio.run(scenario())


def test_sls_project_lookup_failure_is_definitive_before_run_creation(
    monkeypatch,
    tmp_path,
) -> None:
    class OneRowBenchmark:
        def __init__(self, **kwargs):
            del kwargs

        def __len__(self):
            return 1

    storage_calls = 0

    def forbidden_storage(**kwargs):
        nonlocal storage_calls
        storage_calls += 1
        del kwargs
        raise AssertionError("Run creation boundary must not be reached")

    monkeypatch.setattr(_RunnerConfig, "resolve_experiment_plan", lambda self: None)
    monkeypatch.setattr(
        _RunnerConfig,
        "resolve_sls_project",
        lambda self: (_ for _ in ()).throw(RuntimeError("project-secret-canary")),
    )
    monkeypatch.setattr(
        AgentLoopOfflineRunner,
        "_validate_plan",
        staticmethod(lambda config: None),
    )
    monkeypatch.setattr(
        "app.agentloop.offline_runner._RunnerBenchmark",
        OneRowBenchmark,
    )
    monkeypatch.setattr("app.agentloop.offline_runner._AuditedStorage", forbidden_storage)
    runner = AgentLoopOfflineRunner(_CapturingDispatcher(), result_dir=Path(tmp_path))

    async def scenario() -> None:
        with pytest.raises(ExperimentPreparationError) as caught:
            await runner.start(
                OfflineExperimentSpec(
                    launch_id="launch-1",
                    plan_id="plan-1",
                    agent_connection_id="agent-1",
                    experiment_version="1.2.3",
                    context_xml_template=build_experiment_context_template(
                        experiment_plan_id="plan-1",
                        experiment_version="1.2.3",
                        local_launch_id="launch-1",
                    ),
                ),
                lambda record_id: asyncio.sleep(0),
                connection=AgentLoopConnection(
                    region_id="cn-hangzhou",
                    agent_space="space-1",
                    access_key_id="test-ak",
                    access_key_secret="test-sk",
                ),
            )
        assert "project-secret-canary" not in repr(caught.value)

    asyncio.run(scenario())
    assert storage_calls == 0


def test_create_run_rejection_and_unknown_transport_are_classified_separately() -> None:
    class RejectedError(RuntimeError):
        status_code = "403"

    class FailingClient:
        def __init__(self, failure):
            self.failure = failure

        def create_experiment_run(self, agent_space, request):
            del agent_space, request
            raise self.failure

    storage = object.__new__(_AuditedStorage)
    storage.config = SimpleNamespace(
        agent_space="space-1",
        experiment_plan_id="plan-1",
    )
    storage._base_experiment_name = "Run 1"

    storage._agentloop_client = FailingClient(RejectedError("rejected-secret-canary"))
    with pytest.raises(ExperimentPreparationError) as rejected:
        storage._create_experiment_run()
    assert "rejected-secret-canary" not in repr(rejected.value)

    storage._agentloop_client = FailingClient(RuntimeError("transport-secret-canary"))
    with pytest.raises(SubmissionUnknownError) as unknown:
        storage._create_experiment_run()
    assert "transport-secret-canary" not in repr(unknown.value)

    storage._agentloop_client = FailingClient(
        json.JSONDecodeError("response-secret-canary", "{", 1)
    )
    with pytest.raises(SubmissionUnknownError) as malformed_response:
        storage._create_experiment_run()
    assert "response-secret-canary" not in repr(malformed_response.value)


def test_runner_dispatches_dataset_row_with_exact_persisted_xml_context(tmp_path) -> None:
    async def scenario() -> None:
        dispatcher = _CapturingDispatcher()
        runner = AgentLoopOfflineRunner(
            dispatcher,
            result_dir=Path(tmp_path),
            task_timeout_seconds=17,
        )
        template = build_experiment_context_template(
            experiment_plan_id="plan<&",
            experiment_version="1.2.3<&",
            local_launch_id="launch-1",
        )
        spec = OfflineExperimentSpec(
            launch_id="launch-1",
            plan_id="plan<&",
            agent_connection_id="agent-1",
            experiment_version="1.2.3<&",
            context_xml_template=template,
            parameters={"temperature": 0},
        )
        task = Task(
            id="sdk-task-1",
            input={
                "id": "dataset-item-1",
                "question": "Compute 2 + 3. Return only the integer.",
                "expected_output": "5",
            },
            ground_truth="5",
            metrics=[],
            tags={"scenario": "game", "difficulty": "smoke"},
            metadata={"dataset_source": "regression"},
        )

        output = await runner._execute_task(
            spec,
            "run<&",
            task,
            trace_resolver=_AcceptingTraceResolver(),
        )

        assert output.success is True
        assert output.output == "5"
        assert output.trajectory == [{"type": "text", "text": "provider-trajectory"}]
        assert output.meta is not None
        assert output.meta["traceId"] == "a" * 32
        assert output.meta["agent_metadata"] == {
            "traceId": "a" * 32,
            "agentVersion": "1.0.0",
        }
        assert dispatcher.connection_id == "agent-1"
        assert dispatcher.timeout == 17
        assert dispatcher.payload is not None
        assert dispatcher.payload["parameters"] == {"temperature": 0}
        assert dispatcher.payload["originalInput"] == task.input
        rendered_input = dispatcher.payload["input"]
        question, xml = rendered_input.split("\n", 1)
        assert question == "Compute 2 + 3. Return only the integer."
        root = ElementTree.fromstring(xml)
        values = {child.tag: child.text for child in root}
        assert root.attrib == {"schema_version": "1"}
        assert values == {
            "experiment_plan_id": "plan<&",
            "experiment_run_id": "run<&",
            "experiment_version": "1.2.3<&",
            "dataset_item_id": "dataset-item-1",
            "local_launch_id": "launch-1",
        }
        sdk_task = dispatcher.payload["sdkTask"]
        assert sdk_task["id"] == "sdk-task-1"
        assert sdk_task["input"] == {
            "id": "dataset-item-1",
            "question": rendered_input,
            "expected_output": "5",
        }
        assert sdk_task["groundTruth"] == "5"
        assert sdk_task["tags"] == {"scenario": "game", "difficulty": "smoke"}
        assert sdk_task["metadata"] == {
            "dataset_source": "regression",
            "experiment_id": "run<&",
            "experiment_plan_id": "plan<&",
            "experiment_version": "1.2.3<&",
            "dataset_item_id": "dataset-item-1",
            "local_launch_id": "launch-1",
            "experiment_context": dispatcher.payload["context"],
            "experiment_context_xml": xml,
            "experiment_input": rendered_input,
            "experiment_parameters": {"temperature": 0},
            "original_input": task.input,
        }

    asyncio.run(scenario())


def test_runner_records_one_agent_error_without_aborting_the_batch(tmp_path) -> None:
    async def scenario() -> None:
        runner = AgentLoopOfflineRunner(
            _FailingDispatcher(),
            result_dir=Path(tmp_path),
        )
        template = build_experiment_context_template(
            experiment_plan_id="plan-1",
            experiment_version="1.2.3",
            local_launch_id="launch-1",
        )
        task = Task(
            id="sdk-task-1",
            input={"id": "item-1", "question": "Compute 2 + 3."},
            ground_truth="5",
            metrics=[],
            metadata={},
        )

        output = await runner._execute_task(
            OfflineExperimentSpec(
                launch_id="launch-1",
                plan_id="plan-1",
                agent_connection_id="agent-1",
                experiment_version="1.2.3",
                context_xml_template=template,
            ),
            "run-1",
            task,
            trace_resolver=_AcceptingTraceResolver(),
        )

        assert output.success is False
        assert output.output == ""
        assert output.meta is not None
        assert output.meta["agent_error_category"] == "ReadTimeout"
        assert output.meta["error_type"] == "ReadTimeout"
        assert output.meta["error_message"] == "Customer Agent execution failed"
        assert "secret-canary-123" not in repr(output)
        assert "<experiment_run_id>run-1</experiment_run_id>" in output.meta["experiment_input"]

    asyncio.run(scenario())


def test_runner_preserves_failed_solution_evidence_with_platform_error_fields(tmp_path) -> None:
    class FailedSolutionDispatcher:
        async def execute(self, connection_id, payload, timeout_seconds=300):
            del connection_id, payload, timeout_seconds
            return AgentTaskResult(
                success=False,
                output={"partial": "customer Agent stopped after one tool call"},
                trace_id="c" * 32,
                metadata={
                    "errorType": "ProviderError",
                    "retryable": False,
                },
                trajectory=[{"type": "text", "text": "partial provider trajectory"}],
            )

    async def scenario() -> None:
        runner = AgentLoopOfflineRunner(
            FailedSolutionDispatcher(),
            result_dir=Path(tmp_path),
        )
        template = build_experiment_context_template(
            experiment_plan_id="plan-1",
            experiment_version="1.2.3",
            local_launch_id="launch-1",
        )
        task = Task(
            id="sdk-task-1",
            input={"id": "item-1", "question": "Compute 2 + 3."},
            ground_truth="5",
            metrics=[],
            metadata={},
        )

        output = await runner._execute_task(
            OfflineExperimentSpec(
                launch_id="launch-1",
                plan_id="plan-1",
                agent_connection_id="agent-1",
                experiment_version="1.2.3",
                context_xml_template=template,
            ),
            "run-1",
            task,
            trace_resolver=_AcceptingTraceResolver(),
        )

        assert output.success is False
        assert output.output == {"partial": "customer Agent stopped after one tool call"}
        assert output.trajectory == [{"type": "text", "text": "partial provider trajectory"}]
        assert output.meta is not None
        assert output.meta["agent_metadata"] == {
            "errorType": "ProviderError",
            "retryable": False,
        }
        assert output.meta["agent_error_category"] == "ProviderError"
        assert output.meta["error_type"] == "ProviderError"
        assert output.meta["error_message"] == "Customer Agent execution failed"
        assert output.meta["traceId"] == "c" * 32

    asyncio.run(scenario())


def test_runner_waits_for_delayed_trace_then_reports_detail_once_before_completion(
    monkeypatch,
    tmp_path,
) -> None:
    events: list[str] = []
    trace_id = "f" * 32

    class Dispatcher:
        async def execute(self, connection_id, payload, timeout_seconds=300):
            del connection_id, payload, timeout_seconds
            events.append("agent_returned")
            return AgentTaskResult(success=True, output="answer")

    class SearchClient:
        attempts = 0

        def search(self, request):
            del request
            self.attempts += 1
            return [] if self.attempts == 1 else [TraceCandidate(trace_id)]

    class OneRowBenchmark:
        def __init__(self, **kwargs):
            del kwargs
            self.task = Task(
                id="item-1",
                input={"id": "item-1", "question": "question"},
                ground_truth="answer",
                metrics=[],
                metadata={},
            )

        def __len__(self):
            return 1

        def __iter__(self):
            return iter([self.task])

    class Storage:
        def __init__(self, **kwargs):
            self.config = kwargs["config"]
            self.trace_report_callback = kwargs["trace_report_callback"]
            self.experiment_id = "record-1"
            self.upload_failures = []
            self._completed_tasks = 0
            self._failed_tasks = 0
            self._total_tasks = 0
            self.reported_detail_count = 0
            self.aggregation_saved = False
            self.expected_task_keys = set()
            self.verified_trace_ids = {}

        def configure_expected_task_keys(self, task_keys):
            self.expected_task_keys = set(task_keys)

        def register_verified_trace(self, task_id, repeat_id, verified_trace_id):
            key = (task_id, repeat_id)
            assert key in self.expected_task_keys
            self.verified_trace_ids[key] = verified_trace_id

        def start_experiment(self, total_tasks):
            self._total_tasks = total_tasks
            events.append("run_started")

        def save_solution_result(self, task_id, output):
            assert output.meta["traceId"] == trace_id
            assert self.verified_trace_ids[(task_id, "0")] == trace_id
            events.append("detail_written")
            self._completed_tasks += 1
            self.reported_detail_count += 1
            self.trace_report_callback(task_id, trace_id)

        def finalize_experiment(self, status):
            assert self.reported_detail_count == self._total_tasks
            events.append(f"run_{status}")

    class Evaluator:
        def __init__(self, *, benchmark, storage, **kwargs):
            del kwargs
            self.benchmark = benchmark
            self.storage = storage

        async def run(self, solution):
            for task in self.benchmark:
                output = await solution(task, lambda: None)
                events.append("solution_returned")
                self.storage.save_solution_result(task.id, output)
            self.storage.aggregation_saved = True

    monkeypatch.setattr(_RunnerConfig, "resolve_experiment_plan", lambda self: None)
    monkeypatch.setattr(_RunnerConfig, "resolve_sls_project", lambda self: None)
    monkeypatch.setattr(
        AgentLoopOfflineRunner,
        "_validate_plan",
        staticmethod(lambda config: None),
    )
    monkeypatch.setattr("app.agentloop.offline_runner._RunnerBenchmark", OneRowBenchmark)
    monkeypatch.setattr("app.agentloop.offline_runner._AuditedStorage", Storage)
    monkeypatch.setattr("app.agentloop.offline_runner.AgentLoopGeneralEvaluator", Evaluator)

    search = SearchClient()
    resolver = TraceCorrelationService(
        search,
        policy=TracePollingPolicy(
            timeout_seconds=1,
            initial_interval_seconds=0,
            max_interval_seconds=0,
            jitter_ratio=0,
        ),
    )
    runner = AgentLoopOfflineRunner(
        Dispatcher(),
        result_dir=Path(tmp_path),
        trace_resolver_factory=lambda connection: resolver,
        trace_observer=lambda progress: events.append(progress.status.value),
    )
    template = build_experiment_context_template(
        experiment_plan_id="plan-1",
        experiment_version="1.0.0",
        local_launch_id="launch-1",
    )

    async def scenario():
        handle = await runner.start(
            OfflineExperimentSpec(
                launch_id="launch-1",
                plan_id="plan-1",
                agent_connection_id="agent-1",
                experiment_version="1.0.0",
                context_xml_template=template,
            ),
            lambda record_id: asyncio.sleep(0),
            connection=_connection_for_runner_test(),
        )
        return await handle.wait_local_handoff()

    outcome = asyncio.run(scenario())

    assert outcome.state.value == "succeeded"
    assert search.attempts == 2
    assert events.count("detail_written") == 1
    assert events.index(TraceCorrelationStatus.RESOLVED.value) < events.index("solution_returned")
    assert events.index("detail_written") < events.index(TraceCorrelationStatus.REPORTED.value)
    assert events.index(TraceCorrelationStatus.REPORTED.value) < events.index("run_completed")


def _connection_for_runner_test() -> AgentLoopConnection:
    return AgentLoopConnection(
        region_id="cn-hangzhou",
        agent_space="space-1",
        access_key_id="test-ak",
        access_key_secret="test-sk",
    )


def test_runner_does_not_return_solution_when_trace_lookup_is_ambiguous(tmp_path) -> None:
    class Dispatcher:
        async def execute(self, connection_id, payload, timeout_seconds=300):
            del connection_id, payload, timeout_seconds
            return AgentTaskResult(success=True, output="answer")

    class SearchClient:
        def search(self, request):
            del request
            return [TraceCandidate("a" * 32), TraceCandidate("b" * 32)]

    events = []
    runner = AgentLoopOfflineRunner(
        Dispatcher(),
        result_dir=Path(tmp_path),
        trace_observer=events.append,
    )
    resolver = TraceCorrelationService(
        SearchClient(),
        policy=TracePollingPolicy(timeout_seconds=1, jitter_ratio=0),
    )
    template = build_experiment_context_template(
        experiment_plan_id="plan-1",
        experiment_version="1.0.0",
        local_launch_id="launch-1",
    )
    task = Task(
        id="item-1",
        input={"id": "item-1", "question": "question"},
        ground_truth="answer",
        metrics=[],
        metadata={},
    )

    with pytest.raises(TraceCorrelationError) as caught:
        asyncio.run(
            runner._execute_task(
                OfflineExperimentSpec(
                    launch_id="launch-1",
                    plan_id="plan-1",
                    agent_connection_id="agent-1",
                    experiment_version="1.0.0",
                    context_xml_template=template,
                ),
                "record-1",
                task,
                trace_resolver=resolver,
            )
        )

    assert getattr(caught.value, "code", None) == "TRACE_AMBIGUOUS"
    assert events[-1].status is TraceCorrelationStatus.AMBIGUOUS


def test_audited_storage_applies_session_user_agent_and_propagates_completion() -> None:
    class SlsClient:
        user_agent = None

        def set_user_agent(self, value):
            self.user_agent = value

    class ControlClient:
        request = None

        def update_experiment_run(self, agent_space, record_id, request):
            self.request = (agent_space, record_id, request)

    storage = object.__new__(_AuditedStorage)
    storage.config = SimpleNamespace(
        _runner_user_agent="acceptance-session/123",
        agent_space="space-1",
    )
    storage._sls_client = SlsClient()
    storage._agentloop_client = ControlClient()
    storage.experiment_id = "run-1"
    storage._completed_tasks = 3
    storage._failed_tasks = 0
    storage._total_tasks = 3
    storage.upload_failures = []
    storage._expected_task_keys = {("task-1", "0"), ("task-2", "0"), ("task-3", "0")}
    storage._verified_trace_ids = {
        ("task-1", "0"): "a" * 32,
        ("task-2", "0"): "b" * 32,
        ("task-3", "0"): "c" * 32,
    }
    storage._reported_task_keys = set(storage._expected_task_keys)

    assert storage._get_sls_client().user_agent == "acceptance-session/123"
    storage._complete_experiment()
    assert storage.aggregation_saved is True
    assert storage._agentloop_client.request is None
    storage.finalize_experiment("completed")

    agent_space, record_id, request = storage._agentloop_client.request
    assert (agent_space, record_id) == ("space-1", "run-1")
    assert type(request).__module__.startswith("agentloop_sdk._vendor.")
    assert request.status == "completed"
    assert request.completed_tasks == 3
    assert request.failed_tasks == 0
    assert request.total_tasks == 3


def test_audited_storage_uploads_without_persisting_dataset_or_agent_content(
    tmp_path,
) -> None:
    uploaded: list[list[tuple[str, str]]] = []
    storage = object.__new__(_AuditedStorage)
    storage.save_dir = str(tmp_path)
    storage.config = SimpleNamespace(project="project-1", dataset="dataset-1")
    storage.experiment_id = "run-1"
    storage.experiment_name = "Run 1"
    storage._base_experiment_name = "Run 1"
    storage.experiment_start_time = 1
    storage.experiment_type = "agent"
    storage.experiment_metadata = {}
    storage._task_meta_cache = {}
    storage._completed_tasks = 0
    storage._failed_tasks = 0
    storage._total_tasks = 0
    storage._next_milestone_idx = 0
    storage._progress_milestones = [1.0]
    storage.aggregation_saved = False
    storage._solution_stats_memory = {}
    storage._evaluation_results_memory = {}
    storage.upload_failures = []
    storage._expected_task_keys = {("task-1", "0")}
    storage._verified_trace_ids = {("task-1", "0"): "d" * 32}
    storage._trace_owners = {"d" * 32: ("task-1", "0")}
    storage._reported_task_keys = set()
    storage._agent_failure_categories = set()
    storage.trace_report_callback = None
    storage._put_log = lambda contents: uploaded.append(contents)

    storage.save_evaluation_meta({"secret": "dataset-secret-canary"})
    storage.save_task_meta(
        "task-1",
        {"input": {"id": "item-1", "question": "dataset-secret-canary"}},
    )
    storage.save_solution_result(
        "task-1",
        "0",
        SolutionOutput(
            success=True,
            output="agent-output-secret-canary",
            trajectory=[],
            meta={
                "experiment_input": "dataset-secret-canary",
                "traceId": "d" * 32,
            },
        ),
    )
    storage.save_solution_stats("task-1", "0", {"agent": 1})
    storage.save_aggregation_result({"answer": "agent-output-secret-canary"})

    assert uploaded
    assert storage.get_solution_stats("task-1", "0") == {"agent": 1}
    assert storage.aggregation_saved is True
    assert list(tmp_path.rglob("*")) == []

    storage._task_meta_cache = {}
    storage._completed_tasks = 0
    storage._failed_tasks = 0
    storage._total_tasks = 1
    storage._next_milestone_idx = 0
    storage._report_progress = lambda: None
    storage._expected_task_keys = {("task-failed", "0")}
    storage._verified_trace_ids = {("task-failed", "0"): "f" * 32}
    storage._trace_owners = {"f" * 32: ("task-failed", "0")}
    storage._reported_task_keys = set()
    storage.save_task_meta(
        "task-failed",
        {"input": {"id": "item-failed", "question": "question-secret-canary"}},
    )
    storage.save_solution_result(
        "task-failed",
        "0",
        SolutionOutput(
            success=False,
            output="",
            trajectory=[],
            meta={
                "experiment_input": "question-secret-canary",
                "traceId": "f" * 32,
                "error_type": "ReadTimeout",
                "error_message": "Customer Agent execution failed",
            },
        ),
    )
    failed_detail = dict(uploaded[-1])
    assert failed_detail["experiment_output"] == (
        "ERROR: ReadTimeout - Customer Agent execution failed"
    )
    assert "secret-canary" not in failed_detail["experiment_output"]

    class OneTaskBenchmark(BenchmarkBase):
        def __init__(self) -> None:
            super().__init__("cloud-only", "cloud-only storage regression")
            self.tasks = [
                Task(
                    id="task-2",
                    input={"id": "item-2", "question": "dataset-secret-canary"},
                    ground_truth="agent-output-secret-canary",
                    metrics=[],
                    metadata={},
                )
            ]

        def __iter__(self):
            return iter(self.tasks)

        def __len__(self) -> int:
            return len(self.tasks)

        def __getitem__(self, index: int) -> Task:
            return self.tasks[index]

    async def run_evaluator() -> None:
        storage.aggregation_saved = False
        storage._completed_tasks = 0
        storage._failed_tasks = 0
        storage._total_tasks = 1
        storage._next_milestone_idx = 0
        storage._report_progress = lambda: None
        evaluator = AgentLoopGeneralEvaluator(
            name="cloud-only",
            benchmark=OneTaskBenchmark(),
            n_repeat=1,
            storage=storage,
            n_workers=1,
        )

        async def solution(task, pre_hook):
            del task, pre_hook
            return SolutionOutput(
                success=True,
                output="agent-output-secret-canary",
                trajectory=[],
                meta={
                    "experiment_input": "dataset-secret-canary",
                    "traceId": "e" * 32,
                },
            )

        storage._expected_task_keys = {("task-2", "0")}
        storage._verified_trace_ids = {("task-2", "0"): "e" * 32}
        storage._trace_owners = {"e" * 32: ("task-2", "0")}
        storage._reported_task_keys = set()
        await evaluator.run(solution)

    asyncio.run(run_evaluator())
    assert storage._completed_tasks == 1
    assert storage.aggregation_saved is True
    assert list(tmp_path.rglob("*")) == []


def test_audited_storage_never_writes_detail_before_verified_trace() -> None:
    storage = object.__new__(_AuditedStorage)
    storage.trace_report_callback = None
    storage._expected_task_keys = {("task-1", "0")}
    storage._verified_trace_ids = {}
    storage._reported_task_keys = set()

    with pytest.raises(RuntimeError, match="not verified by the resolver"):
        storage.save_solution_result(
            "task-1",
            "0",
            SolutionOutput(
                success=True,
                output="answer",
                trajectory=[],
                meta={"traceId": "d" * 32},
            ),
        )


def test_audited_storage_does_not_write_a_reported_detail_twice(monkeypatch) -> None:
    storage = object.__new__(_AuditedStorage)
    storage._expected_task_keys = {("task-1", "0")}
    storage._verified_trace_ids = {("task-1", "0"): "f" * 32}
    storage._reported_task_keys = {("task-1", "0")}
    storage.trace_report_callback = None
    writes = 0

    def unexpected_write(*args, **kwargs):
        del args, kwargs
        nonlocal writes
        writes += 1

    monkeypatch.setattr(AgentLoopEvaluatorStorage, "save_solution_result", unexpected_write)

    storage.save_solution_result(
        "task-1",
        "0",
        SolutionOutput(
            success=True,
            output="answer",
            trajectory=[],
            meta={"traceId": "f" * 32},
        ),
    )

    assert writes == 0
    assert storage.reported_detail_count == 1


def test_audited_storage_rejects_trace_reuse_across_dataset_items() -> None:
    storage = object.__new__(_AuditedStorage)
    storage._expected_task_keys = {("task-1", "0"), ("task-2", "0")}
    storage._verified_trace_ids = {}
    storage._trace_owners = {}

    storage.register_verified_trace("task-1", "0", "a" * 32)
    with pytest.raises(RuntimeError, match="cannot be reused"):
        storage.register_verified_trace("task-2", "0", "a" * 32)


def test_completed_status_rechecks_exact_detail_barrier_before_cloud_update() -> None:
    class ControlClient:
        calls = 0

        def update_experiment_run(self, agent_space, record_id, request):
            del agent_space, record_id, request
            self.calls += 1

    storage = object.__new__(_AuditedStorage)
    storage.config = SimpleNamespace(agent_space="space-1")
    storage.experiment_id = "run-1"
    storage.aggregation_saved = True
    storage.upload_failures = []
    storage._expected_task_keys = {("task-1", "0")}
    storage._verified_trace_ids = {("task-1", "0"): "a" * 32}
    storage._reported_task_keys = {("unexpected-task", "0")}
    storage._completed_tasks = 1
    storage._failed_tasks = 0
    storage._total_tasks = 1
    storage._agentloop_client = ControlClient()

    with pytest.raises(RuntimeError, match="completion barrier rejected"):
        storage.finalize_experiment("completed")

    assert storage._agentloop_client.calls == 0


def test_sls_upload_exception_is_sanitized_before_upstream_logging(monkeypatch) -> None:
    storage = object.__new__(_AuditedStorage)
    storage.upload_failures = []

    def leak_secret(storage_self, contents):
        del storage_self, contents
        raise RuntimeError("sls-secret-canary")

    monkeypatch.setattr(AgentLoopEvaluatorStorage, "_put_log", leak_secret)

    with pytest.raises(RuntimeError, match="AgentLoop SLS upload failed") as caught:
        storage._put_log([("key", "value")])

    assert storage.upload_failures == ["SLS_UPLOAD_FAILED"]
    assert "sls-secret-canary" not in repr(caught.value)


def test_run_status_update_failures_never_log_sdk_error_payload(caplog) -> None:
    class FailingControlClient:
        def update_experiment_run(self, agent_space, record_id, request):
            del agent_space, record_id, request
            raise RuntimeError("control-plane-secret-canary")

    storage = object.__new__(_AuditedStorage)
    storage.config = SimpleNamespace(agent_space="space-1")
    storage.experiment_id = "run-1"
    storage.experiment_start_time = 1
    storage.experiment_name = "Run 1"
    storage._completed_tasks = 1
    storage._failed_tasks = 0
    storage._total_tasks = 1
    storage.progress_update_failures = 0
    storage._agentloop_client = FailingControlClient()

    with pytest.raises(RuntimeError, match="AgentLoop Run start update failed") as caught:
        storage.start_experiment(1)
    storage._report_progress()

    assert storage.progress_update_failures == 1
    assert "control-plane-secret-canary" not in repr(caught.value)
    assert "control-plane-secret-canary" not in caplog.text


def test_storage_failure_message_rejects_failed_agent_tasks_and_uploads() -> None:
    storage = SimpleNamespace(
        upload_failures=[],
        _completed_tasks=2,
        _failed_tasks=1,
        _total_tasks=3,
    )
    assert _storage_failure_message(storage) == "1 of 3 customer Agent task(s) failed"

    storage._agent_failure_categories = {"ReadTimeout"}
    assert _storage_failure_message(storage) == (
        "1 of 3 customer Agent task(s) failed (ReadTimeout)"
    )

    storage.upload_failures = ["sensitive raw SDK failure"]
    assert _storage_failure_message(storage) == "1 Dataset result(s) failed to upload to SLS"


def test_failed_agent_task_finalizes_cloud_run_as_failed_not_handed_off() -> None:
    class Storage:
        upload_failures = []
        _completed_tasks = 2
        _failed_tasks = 1
        _total_tasks = 3
        final_status = None

        def finalize_experiment(self, status):
            self.final_status = status

    storage = Storage()
    outcome = asyncio.run(AgentLoopOfflineRunner._finalize_handoff(storage))

    assert storage.final_status == "failed"
    assert outcome.state.value == "definitive_failure"
    assert outcome.message == "1 of 3 customer Agent task(s) failed"


def test_runner_config_uses_agentloop_sdk_vendored_client_and_request_models() -> None:
    config = _RunnerConfig(
        agent_space="space-1",
        experiment_plan_id="plan-1",
        region_id="cn-hangzhou",
        access_key_id="test-ak",
        access_key_secret="test-sk",
    )

    client = config._create_agentloop_client()

    assert type(client).__module__.startswith("agentloop_sdk._vendor.")


def test_runner_accepts_stopped_plan_status_at_execution_boundary(monkeypatch) -> None:
    evaluator = SimpleNamespace(
        config={"variables": []},
        variable_mapping={},
        evaluator_ref="eval-1",
        name="eval-1",
    )
    body = SimpleNamespace(
        experiment_type="offline",
        status="stopped",
        dataset_id="dataset-1",
        plan_name="Stopped Plan",
        evaluators=[evaluator],
        selected_item_ids=[],
        query_sql="",
    )
    client = SimpleNamespace(
        get_experiment_plan=lambda agent_space, plan_id, request: SimpleNamespace(body=body)
    )
    monkeypatch.setattr(_RunnerConfig, "_create_agentloop_client", lambda self: client)
    config = _RunnerConfig(
        agent_space="space-1",
        experiment_plan_id="plan-1",
        region_id="cn-hangzhou",
        access_key_id="test-ak",
        access_key_secret="test-sk",
    )

    config.resolve_experiment_plan()
    AgentLoopOfflineRunner._validate_plan(config)

    assert getattr(config, "_plan_resolved", False) is True
    assert config.dataset == "dataset-1"
    assert config.plan_evaluators == [evaluator]


def test_gateway_error_does_not_expose_sdk_message_or_unsafe_code() -> None:
    sdk_error = RuntimeError("secret-canary-456")
    sdk_error.message = "credential secret-canary-456 was rejected"  # type: ignore[attr-defined]
    sdk_error.code = "NoPermission"  # type: ignore[attr-defined]
    sdk_error.request_id = "request-1"  # type: ignore[attr-defined]

    rendered = _gateway_error("GetExperimentPlan", sdk_error)

    assert str(rendered) == "GetExperimentPlan failed [NoPermission]"
    assert rendered.request_id == "request-1"
    assert "secret-canary-456" not in repr(rendered)

    sdk_error.code = "unsafe-secret-canary-456!"  # type: ignore[attr-defined]
    sdk_error.request_id = "unsafe-request-secret-canary!"  # type: ignore[attr-defined]
    unsafe = _gateway_error("GetExperimentPlan", sdk_error)
    assert str(unsafe) == "GetExperimentPlan failed"
    assert unsafe.request_id is None


def test_only_valid_otel_trace_ids_are_forwarded() -> None:
    assert _validated_trace_id("A" * 32) == "a" * 32
    assert _validated_trace_id("0" * 32) is None
    assert _validated_trace_id("trace-secret-canary") is None


def test_failed_run_update_reuses_runner_client_and_vendored_request() -> None:
    class ControlClient:
        request = None

        def update_experiment_run(self, agent_space, record_id, request):
            self.request = (agent_space, record_id, request)

    client = ControlClient()
    config = SimpleNamespace(
        agent_space="space-1",
        _create_agentloop_client=lambda: client,
    )

    AgentLoopOfflineRunner._mark_run_failed(config, "run-1", 2, 1, 3)

    agent_space, record_id, request = client.request
    assert (agent_space, record_id) == ("space-1", "run-1")
    assert type(request).__module__.startswith("agentloop_sdk._vendor.")
    assert request.status == "failed"
    assert request.completed_tasks == 2
    assert request.failed_tasks == 1
    assert request.total_tasks == 3


def test_cancelled_runner_best_effort_marks_existing_cloud_run_failed(monkeypatch) -> None:
    captured = []

    def mark_failed(config, record_id, completed, failed, total):
        captured.append((config, record_id, completed, failed, total))

    monkeypatch.setattr(
        AgentLoopOfflineRunner,
        "_mark_run_failed",
        staticmethod(mark_failed),
    )
    storage = SimpleNamespace(
        config="config",
        experiment_id="run-1",
        _completed_tasks=2,
        _failed_tasks=0,
        _total_tasks=3,
    )

    asyncio.run(AgentLoopOfflineRunner._fail_storage_after_cancellation(storage))

    assert captured == [("config", "run-1", 2, 1, 3)]


def test_scheduled_offline_agent_is_recorded_as_skipped_without_cloud_run() -> None:
    async def scenario() -> None:
        database = in_memory_database()
        profiles = ProfileRepository(database)
        profile = profiles.create(
            display_name="test",
            region_id="cn-hangzhou",
            agent_space="space-a",
            secret_ref="secret/profile",
            validation_status="VALID",
        )
        agents = AgentConnectionRepository(database)
        agent = agents.create(
            connection_id="agent-offline",
            name="offline",
            token_secret_ref="secret/agent",
            token_hint="****line",
        )
        gateway = FakeAgentLoopGateway(
            plans=(
                PlanDetail(
                    plan_id="plan-1",
                    plan_name="smoke",
                    experiment_type="OFFLINE",
                    status="active",
                    dataset_id="dataset-1",
                    evaluator_count=1,
                    can_launch_locally=True,
                ),
            )
        )
        tasks = BackgroundTaskSupervisor()
        service = LaunchService(
            profiles=profiles,
            launches=LaunchIntentRepository(database),
            gateway=gateway,
            clock=FixedClock(datetime(2026, 8, 8, 4, 0, tzinfo=UTC)),
            tasks=tasks,
            connection_provider=lambda: AgentLoopConnection(
                region_id=profile.region_id,
                agent_space=profile.agent_space,
                access_key_id="test-ak",
                access_key_secret="test-sk",
            ),
            agent_connections=agents,
            agent_connection_online=lambda _: False,
            require_agent_connection=True,
        )
        scheduled_for = datetime(2026, 8, 8, 4, 0, tzinfo=UTC)
        schedule = ScheduleRepository(database).create(
            schedule_id="schedule-1",
            profile_id=profile.id,
            name="hourly smoke",
            plan_id="plan-1",
            plan_name_snapshot="smoke",
            agent_connection_id=agent.id,
            agent_connection_name_snapshot=agent.name,
            cadence_type="INTERVAL_HOURS",
            timezone="UTC",
            interval_hours=1,
            anchor_at=scheduled_for,
            next_fire_at_utc=scheduled_for,
            version_mode="AUTO",
            experiment_parameters_json="{}",
            context_schema_version=1,
        )
        launch = await service.scheduled(schedule, scheduled_for)
        assert launch.submission_state == SubmissionState.SKIPPED.value
        assert launch.runner_state == RunnerState.NOT_STARTED.value
        assert launch.skip_reason == SkipReason.AGENT_OFFLINE.value
        assert gateway.created_record_count == 0
        await tasks.close()
        database.dispose()

    asyncio.run(scenario())
