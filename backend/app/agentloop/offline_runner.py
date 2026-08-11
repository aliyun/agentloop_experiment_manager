"""Runner-owned AgentLoop offline experiment execution.

The runner creates exactly one authoritative AgentLoop Experiment Run through
``agentloop-sdk`` and then dispatches every Dataset row to a connected local
Agent.  The control-plane adapter must never create a second Run first.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from agentloop_sdk import (
    AgentLoopBenchmark,
    AgentLoopConfig,
    AgentLoopEvaluatorStorage,
    AgentLoopGeneralEvaluator,
    SolutionOutput,
    Task,
)

from app.domain.agent_error import (
    SAFE_AGENT_ERROR_CATEGORIES,
    safe_agent_error_category,
)
from app.domain.experiment_context import (
    append_experiment_context,
    render_experiment_context,
)

from .errors import (
    AgentLoopGatewayError,
    ExperimentPreparationError,
    SubmissionUnknownError,
)
from .gateway import ExperimentHandle, RecordCreatedCallback
from .models import AgentLoopConnection, LocalHandoffOutcome
from .trace_correlation import (
    AgentSpaceTrajectoryLogStoreClient,
    TraceCorrelationContext,
    TraceCorrelationProgress,
    TraceCorrelationResult,
    TraceCorrelationService,
    TraceCorrelationStatus,
    TracePollingPolicy,
    validate_trace_id,
)

_DEFAULT_USER_AGENT = "agentloop-local-experiment-launcher/0.1.0"


class _RunnerConfig(AgentLoopConfig):
    """AgentLoopConfig variant that identifies this launcher to OpenAPI."""

    _runner_user_agent = _DEFAULT_USER_AGENT

    def _create_agentloop_client(self) -> Any:
        from agentloop_sdk._vendor.alibabacloud_agentloop20260520.client import Client
        from alibabacloud_tea_openapi import utils_models as open_api_models

        return Client(
            open_api_models.Config(
                access_key_id=self.access_key_id,
                access_key_secret=self.access_key_secret,
                region_id=self.region_id,
                endpoint=self.agentloop_endpoint,
                user_agent=self._runner_user_agent,
            )
        )

    def resolve_experiment_plan(self) -> None:
        """Resolve the current Plan inputs at the execution boundary.

        ``agentloop-sdk==1.0.3`` resolves the Dataset and evaluator bindings but
        does not retain every Plan field.  The cloud-returned Plan status is
        informational and must not be used as an execution gate; the
        CreateExperimentRun response is authoritative for run acceptance.
        """

        if getattr(self, "_plan_resolved", False):
            return

        self.validate_credentials()

        from agentloop_sdk._vendor.alibabacloud_agentloop20260520 import (
            models as api_models,
        )

        try:
            response = self._create_agentloop_client().get_experiment_plan(
                self.agent_space,
                self.experiment_plan_id,
                api_models.GetExperimentPlanRequest(),
            )
        except Exception:
            raise ValueError("Experiment Plan read failed") from None

        body = response.body
        if not body:
            raise ValueError("Experiment Plan response was empty")

        experiment_type = str(body.experiment_type or "")
        if experiment_type.casefold() != "offline":
            raise ValueError("Only OFFLINE Experiment Plans can run locally")

        if not body.dataset_id:
            raise ValueError("The Experiment Plan has no Dataset binding")

        self.dataset = body.dataset_id
        self.plan_name = body.plan_name or self.experiment_plan_id
        self.plan_evaluators = body.evaluators or []
        self.selected_item_ids = body.selected_item_ids or []
        self.query_sql = body.query_sql or ""
        self._plan_resolved = True

    def resolve_sls_project(self) -> None:
        """Resolve the AgentSpace SLS project before the Run creation boundary."""

        if self.project:
            return
        from agentloop_sdk._vendor.alibabacloud_agentloop20260520 import (
            models as api_models,
        )

        try:
            response = self._create_agentloop_client().get_agent_space(
                self.agent_space,
                api_models.GetAgentSpaceRequest(),
            )
        except Exception:
            raise ValueError("AgentSpace SLS project lookup failed") from None
        body = response.body
        if body is None or not body.sls_project:
            raise ValueError("AgentSpace has no SLS project configured")
        self.project = body.sls_project


class _RunnerBenchmark(AgentLoopBenchmark):
    def _get_client(self) -> Any:
        return self.config._create_agentloop_client()

    def _execute_query_request(
        self,
        client: Any,
        agentloop_models: Any,
        agent_space: str,
        query: str,
    ) -> list[dict[str, Any]]:
        """Execute one complete Dataset query without logging rows/errors."""

        try:
            response = client.execute_query(
                agent_space,
                self.config.dataset,
                agentloop_models.ExecuteQueryRequest(type="SQL", query=query),
            )
        except Exception:
            raise RuntimeError("AgentLoop Dataset query failed") from None

        body = response.body
        if body is None:
            raise RuntimeError("AgentLoop Dataset query returned an empty response")
        meta = getattr(body, "meta", None)
        if meta is None or str(getattr(meta, "progress", "")).casefold() != "complete":
            raise RuntimeError("AgentLoop Dataset query did not complete")
        truncation = getattr(meta, "truncation", None)
        if truncation is not None and bool(getattr(truncation, "truncated", False)):
            raise RuntimeError("AgentLoop Dataset query response was truncated")

        columns = list(getattr(body, "columns", None) or [])
        rows = list(getattr(body, "rows", None) or [])
        raw_column_types = getattr(body, "column_types", None)
        column_types = list(raw_column_types or [])
        if not columns or any(not isinstance(column, str) or not column for column in columns):
            raise RuntimeError("AgentLoop Dataset query response has invalid columns")
        if len(columns) != len(set(columns)):
            raise RuntimeError("AgentLoop Dataset query response has duplicate columns")
        if (
            raw_column_types is None
            or len(column_types) != len(columns)
            or any(
                not isinstance(column_type, str) or not column_type for column_type in column_types
            )
        ):
            raise RuntimeError("AgentLoop Dataset query column metadata is inconsistent")
        if any(not isinstance(row, (list, tuple)) or len(row) != len(columns) for row in rows):
            raise RuntimeError("AgentLoop Dataset query row shape is inconsistent")
        returned_count = getattr(meta, "count", None)
        if (
            isinstance(returned_count, bool)
            or not isinstance(returned_count, int)
            or returned_count < 0
            or returned_count != len(rows)
        ):
            raise RuntimeError("AgentLoop Dataset query row count is inconsistent")
        return [dict(zip(columns, row, strict=True)) for row in rows]

    def _load_data(self) -> list[dict[str, Any]]:
        """Load the exact selected Dataset rows and fail closed on partial data."""

        from agentloop_sdk._vendor.alibabacloud_agentloop20260520 import (
            models as api_models,
        )

        client = self._get_client()
        agent_space = self.config.agent_space
        escaped_dataset = self.config.dataset.replace("`", "``")

        if self.config.selected_item_ids:
            selected_ids = [str(value) for value in self.config.selected_item_ids]
            if len(selected_ids) != len(set(selected_ids)):
                raise RuntimeError("Experiment Plan contains duplicate selected Dataset IDs")
            if len(selected_ids) > self.config.max_rows:
                raise RuntimeError("Experiment Plan selection exceeds the local row limit")
            escaped_ids = [value.replace("'", "''") for value in selected_ids]
            in_clause = ",".join(f"'{value}'" for value in escaped_ids)
            records = self._execute_query_request(
                client,
                api_models,
                agent_space,
                (f"* | select * from `{escaped_dataset}` WHERE id IN ({in_clause}) ORDER BY id"),
            )
            self._validate_loaded_records(records)
            if {str(item["id"]) for item in records} != set(selected_ids):
                raise RuntimeError("Experiment Plan selected Dataset rows are incomplete")
            return records

        if self.config.query_sql:
            records = self._execute_query_request(
                client,
                api_models,
                agent_space,
                self.config.query_sql,
            )
            if len(records) > self.config.max_rows:
                raise RuntimeError("Experiment Plan query exceeds the local row limit")
            self._validate_loaded_records(records)
            return records

        records: list[dict[str, Any]] = []
        probe_limit = self.config.max_rows + 1
        while len(records) < probe_limit:
            page_size = min(self._PAGE_SIZE, probe_limit - len(records))
            page = self._execute_query_request(
                client,
                api_models,
                agent_space,
                (
                    f"* | select * from `{escaped_dataset}` ORDER BY id "
                    f"LIMIT {len(records)}, {page_size}"
                ),
            )
            if not page:
                break
            records.extend(page)

        if len(records) > self.config.max_rows:
            raise RuntimeError("Experiment Plan Dataset exceeds the local row limit")
        self._validate_loaded_records(records)
        return records

    @staticmethod
    def _validate_loaded_records(records: list[dict[str, Any]]) -> None:
        ids = [str(item.get("id") or "") for item in records]
        if any(not item_id for item_id in ids):
            raise RuntimeError("AgentLoop Dataset rows must contain a non-empty id")
        if len(ids) != len(set(ids)):
            raise RuntimeError("AgentLoop Dataset query returned duplicate rows")


@dataclass(frozen=True, slots=True)
class OfflineExperimentSpec:
    """Immutable customer-side execution configuration for one launch."""

    launch_id: str
    plan_id: str
    agent_connection_id: str
    experiment_version: str
    context_xml_template: str
    parameters: dict[str, Any] = field(default_factory=dict)
    context_schema_version: int = 1
    record_name: str | None = None
    max_rows: int = 1_000


@dataclass(frozen=True, slots=True)
class AgentTaskResult:
    success: bool
    output: Any
    trace_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    trajectory: list[Any] = field(default_factory=list)


class AgentTaskDispatcher(Protocol):
    """Dispatch a task to an Agent that initiated a local platform connection."""

    async def execute(
        self,
        agent_connection_id: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> AgentTaskResult | dict[str, Any]: ...


class _RunnerHandle(ExperimentHandle):
    def __init__(self, record_id: str, task: asyncio.Task[LocalHandoffOutcome]) -> None:
        self._record_id = record_id
        self._task = task

    @property
    def record_id(self) -> str:
        return self._record_id

    async def wait_local_handoff(self) -> LocalHandoffOutcome:
        return await self._task


class _AuditedStorage(AgentLoopEvaluatorStorage):
    """Cloud-only result storage with audited upload and final Run status.

    The upstream storage inherits a file backend that persists Dataset rows,
    Agent inputs, and outputs under ``save_dir``.  This launcher intentionally
    keeps only Launch metadata in SQLite, so all task-level file writes are
    redirected to the operating system null device while the normal AgentLoop
    SLS upload path remains active.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.trace_report_callback = kwargs.pop("trace_report_callback", None)
        self.upload_failures: list[str] = []
        self.progress_update_failures = 0
        self.aggregation_saved = False
        self._expected_task_keys: set[tuple[str, str]] = set()
        self._verified_trace_ids: dict[tuple[str, str], str] = {}
        self._trace_owners: dict[str, tuple[str, str]] = {}
        self._reported_task_keys: set[tuple[str, str]] = set()
        self._agent_failure_categories: set[str] = set()
        self._solution_stats_memory: dict[tuple[str, str], dict[str, Any]] = {}
        self._evaluation_results_memory: dict[tuple[str, str, str], Any] = {}
        super().__init__(*args, **kwargs)

    def configure_expected_task_keys(self, task_keys: set[tuple[str, str]]) -> None:
        """Freeze the exact detail keys that must be reported for this Run."""

        normalized = {(str(task_id), str(repeat_id)) for task_id, repeat_id in task_keys}
        if not normalized:
            raise RuntimeError("Experiment Run has no expected detail keys")
        if self._expected_task_keys and self._expected_task_keys != normalized:
            raise RuntimeError("Experiment detail key set cannot be changed after configuration")
        self._expected_task_keys = normalized

    def register_verified_trace(
        self,
        task_id: str,
        repeat_id: str,
        trace_id: str,
    ) -> None:
        """Bind one Resolver-verified Trace to one expected detail key."""

        task_key = (str(task_id), str(repeat_id))
        normalized_trace_id = validate_trace_id(trace_id)
        if task_key not in self._expected_task_keys:
            raise RuntimeError("Trace proof does not belong to an expected experiment detail")
        if normalized_trace_id is None:
            raise RuntimeError("Trace proof contains an invalid Trace ID")
        existing = self._verified_trace_ids.get(task_key)
        if existing is not None and existing != normalized_trace_id:
            raise RuntimeError("Verified Trace ID cannot change for an experiment detail")
        owner = self._trace_owners.get(normalized_trace_id)
        if owner is not None and owner != task_key:
            raise RuntimeError("One Trace ID cannot be reused by multiple experiment details")
        self._verified_trace_ids[task_key] = normalized_trace_id
        self._trace_owners[normalized_trace_id] = task_key

    def save_solution_result(
        self,
        task_id: str,
        repeat_id: str,
        output: SolutionOutput,
        **kwargs: Any,
    ) -> None:
        """Write one detail only after the Trace ID is resolved and validated."""

        task_key = (str(task_id), str(repeat_id))
        if task_key in self._reported_task_keys:
            # The evaluator currently calls this once, but keep the local
            # boundary idempotent if an SDK retry invokes it again after the
            # first SLS write was already acknowledged.
            return
        output_meta = output.meta if isinstance(output.meta, dict) else {}
        trace_id = validate_trace_id(output_meta.get("traceId"))
        if trace_id is None:
            raise RuntimeError("A verified Trace ID is required before result upload")
        if task_key not in self._expected_task_keys:
            raise RuntimeError("Unexpected experiment detail cannot be uploaded")
        if self._verified_trace_ids.get(task_key) != trace_id:
            raise RuntimeError("Experiment detail Trace ID was not verified by the resolver")
        output_meta["traceId"] = trace_id
        output.meta = output_meta
        failure_count = len(self.upload_failures)
        super().save_solution_result(task_id, repeat_id, output, **kwargs)
        if len(self.upload_failures) != failure_count:
            return
        self._reported_task_keys.add(task_key)
        if self.trace_report_callback is not None:
            self.trace_report_callback(task_id, trace_id)

    @property
    def reported_detail_count(self) -> int:
        return len(self._reported_task_keys)

    def register_agent_failure_category(self, category: str) -> None:
        """Retain only an allow-listed category for the local Launch summary."""

        self._agent_failure_categories.add(
            category if category in SAFE_AGENT_ERROR_CATEGORIES else "ProviderError"
        )

    def completion_failure_message(self) -> str | None:
        """Return a payload-free reason why this Run cannot become completed."""

        if self.upload_failures:
            return f"{len(self.upload_failures)} Dataset result(s) failed to upload to SLS"
        expected = self._expected_task_keys
        reported = self._reported_task_keys
        if not expected or len(expected) != self._total_tasks:
            return "Experiment detail key set is incomplete"
        if set(self._verified_trace_ids) != expected:
            return (
                f"Only {len(self._verified_trace_ids)} of {len(expected)} experiment Trace(s) "
                "were verified"
            )
        if reported != expected:
            return f"Only {len(reported)} of {len(expected)} experiment detail(s) were reported"
        if self._failed_tasks:
            return _agent_task_failure_message(
                self._failed_tasks,
                self._total_tasks,
                getattr(self, "_agent_failure_categories", set()),
            )
        return None

    def _put_log(self, contents: list[tuple[str, str]]) -> None:
        try:
            super()._put_log(contents)
        except Exception:
            self.upload_failures.append("SLS_UPLOAD_FAILED")
            raise RuntimeError("AgentLoop SLS upload failed") from None

    def _get_save_path(
        self,
        task_id: str,
        repeat_id: str | None,
        *args: str,
    ) -> str:
        del task_id, repeat_id, args
        return os.devnull

    def save_evaluation_meta(self, meta_info: dict[str, Any]) -> None:
        del meta_info

    def save_solution_stats(
        self,
        task_id: str,
        repeat_id: str,
        stats: dict[str, Any],
    ) -> None:
        self._solution_stats_memory[(task_id, repeat_id)] = stats

    def get_solution_stats(self, task_id: str, repeat_id: str) -> dict[str, Any]:
        return self._solution_stats_memory.get((task_id, repeat_id), {})

    def save_evaluation_result(
        self,
        task_id: str,
        repeat_id: str,
        evaluation: Any,
        **kwargs: Any,
    ) -> None:
        del kwargs
        self._evaluation_results_memory[(task_id, repeat_id, str(evaluation.name))] = evaluation

    def get_evaluation_result(
        self,
        task_id: str,
        repeat_id: str,
        metric_name: str,
    ) -> Any:
        return self._evaluation_results_memory[(task_id, repeat_id, metric_name)]

    def evaluation_result_exists(
        self,
        task_id: str,
        repeat_id: str,
        metric_name: str,
    ) -> bool:
        return (task_id, repeat_id, metric_name) in self._evaluation_results_memory

    def save_aggregation_result(
        self,
        aggregation_result: dict[str, Any],
        **kwargs: Any,
    ) -> None:
        del aggregation_result, kwargs
        self._complete_experiment()

    def aggregation_result_exists(self, **kwargs: Any) -> bool:
        del kwargs
        return self.aggregation_saved

    def _get_agentloop_client(self) -> Any:
        if self._agentloop_client is None:
            self._agentloop_client = self.config._create_agentloop_client()
        return self._agentloop_client

    def _create_experiment_run(self) -> str:
        """Create the unique Run and preserve the acceptance uncertainty boundary."""

        from agentloop_sdk._vendor.alibabacloud_agentloop20260520 import (
            models as api_models,
        )

        try:
            client = self._get_agentloop_client()
            request = api_models.CreateExperimentRunRequest(
                experiment_plan_id=self.config.experiment_plan_id,
                offline_experiments=[
                    api_models.OfflineExperimentConfig(
                        name=self._base_experiment_name,
                        label=self._base_experiment_name,
                    )
                ],
            )
            request.validate()
        except Exception:
            raise ExperimentPreparationError(
                "Customer-side experiment preparation failed"
            ) from None

        try:
            response = client.create_experiment_run(self.config.agent_space, request)
        except Exception as exc:
            raw_status_code = getattr(exc, "status_code", None)
            try:
                status_code = None if isinstance(raw_status_code, bool) else int(raw_status_code)
            except (TypeError, ValueError):
                status_code = None
            if isinstance(status_code, int) and 400 <= status_code < 500:
                raise ExperimentPreparationError(
                    "AgentLoop rejected Experiment Run creation"
                ) from None
            raise SubmissionUnknownError() from None

        record_id = response.body.record_id if response.body else None
        if not record_id:
            raise SubmissionUnknownError()
        return record_id

    def _get_sls_client(self) -> Any:
        client = super()._get_sls_client()
        client.set_user_agent(getattr(self.config, "_runner_user_agent", _DEFAULT_USER_AGENT))
        return client

    def start_experiment(self, total_tasks: int) -> None:
        """Mark the Run active without logging raw SDK exception payloads."""

        from agentloop_sdk._vendor.alibabacloud_agentloop20260520 import (
            models as api_models,
        )

        self._total_tasks = total_tasks
        try:
            self._get_agentloop_client().update_experiment_run(
                self.config.agent_space,
                self.experiment_id,
                api_models.UpdateExperimentRunRequest(
                    status="running",
                    total_tasks=total_tasks,
                    executed_at=self.experiment_start_time,
                    record_name=self.experiment_name,
                ),
            )
        except Exception:
            raise RuntimeError("AgentLoop Run start update failed") from None

    def _report_progress(self) -> None:
        """Best-effort progress update with a metadata-only failure counter."""

        from agentloop_sdk._vendor.alibabacloud_agentloop20260520 import (
            models as api_models,
        )

        try:
            self._get_agentloop_client().update_experiment_run(
                self.config.agent_space,
                self.experiment_id,
                api_models.UpdateExperimentRunRequest(
                    completed_tasks=self._completed_tasks,
                    failed_tasks=self._failed_tasks,
                    total_tasks=self._total_tasks,
                ),
            )
        except Exception:
            self.progress_update_failures += 1

    def _complete_experiment(self) -> None:
        """Delay the SDK's automatic completion until uploads are audited."""

        self.aggregation_saved = True

    def finalize_experiment(self, status: str) -> None:
        """Write the single final status after every task and upload is checked."""

        if status not in {"completed", "failed"}:
            raise ValueError(f"unsupported final experiment status: {status}")
        if not self.aggregation_saved:
            raise RuntimeError("experiment aggregation was not saved before finalization")
        if status == "completed":
            failure = self.completion_failure_message()
            if failure is not None:
                raise RuntimeError(f"Experiment completion barrier rejected: {failure}")

        from agentloop_sdk._vendor.alibabacloud_agentloop20260520 import (
            models as api_models,
        )

        self._get_agentloop_client().update_experiment_run(
            self.config.agent_space,
            self.experiment_id,
            api_models.UpdateExperimentRunRequest(
                status=status,
                completed_tasks=self._completed_tasks,
                failed_tasks=self._failed_tasks,
                total_tasks=self._total_tasks,
                completed_at=int(time.time() * 1000),
            ),
        )


class AgentLoopOfflineRunner:
    """Execute a cloud Dataset against one connected customer-side Agent."""

    def __init__(
        self,
        dispatcher: AgentTaskDispatcher,
        *,
        result_dir: Path,
        task_timeout_seconds: float = 300.0,
        trace_lookup_timeout_seconds: float = 180.0,
        user_agent: str = _DEFAULT_USER_AGENT,
        trace_resolver_factory: Callable[[AgentLoopConnection], TraceCorrelationService]
        | None = None,
        trace_observer: Callable[[TraceCorrelationProgress], None] | None = None,
    ) -> None:
        self.dispatcher = dispatcher
        self.result_dir = result_dir
        self.task_timeout_seconds = task_timeout_seconds
        if not 60 < trace_lookup_timeout_seconds <= 1800:
            raise ValueError(
                "trace_lookup_timeout_seconds must be greater than 60 and at most 1800"
            )
        self.trace_polling_policy = TracePollingPolicy(timeout_seconds=trace_lookup_timeout_seconds)
        self.user_agent = user_agent
        self.trace_resolver_factory = trace_resolver_factory
        self.trace_observer = trace_observer

    async def start(
        self,
        spec: OfflineExperimentSpec,
        on_record_created: RecordCreatedCallback,
        *,
        connection: AgentLoopConnection,
    ) -> ExperimentHandle:
        record_ready: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(
            self._run(
                spec,
                on_record_created,
                record_ready,
                connection=connection,
            ),
            name=f"agentloop-offline-run:{spec.launch_id}",
        )
        try:
            record_id = await record_ready
        except BaseException as exc:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if isinstance(exc, (asyncio.CancelledError, AgentLoopGatewayError)):
                raise
            raise ExperimentPreparationError(
                "Customer-side experiment preparation failed"
            ) from None
        return _RunnerHandle(record_id, task)

    async def _run(
        self,
        spec: OfflineExperimentSpec,
        on_record_created: RecordCreatedCallback,
        record_ready: asyncio.Future[str],
        *,
        connection: AgentLoopConnection,
    ) -> LocalHandoffOutcome:
        storage: _AuditedStorage | None = None
        trace_contexts: dict[str, TraceCorrelationContext] = {}
        trace_results: dict[str, TraceCorrelationResult] = {}
        try:
            config = _RunnerConfig(
                agent_space=connection.agent_space,
                experiment_plan_id=spec.plan_id,
                region_id=connection.region_id,
                max_rows=spec.max_rows,
                experiment_name=spec.record_name
                or f"local-{spec.experiment_version}-{spec.launch_id[:8]}",
                custom_agentloop_endpoint=connection.api_endpoint or "",
                access_key_id=connection.access_key_id,
                access_key_secret=connection.access_key_secret,
            )
            config._runner_user_agent = self.user_agent

            # All SDK constructors below perform blocking cloud I/O.  Keep it
            # off FastAPI's event loop, but preserve the SDK's runner-owned Run.
            await asyncio.to_thread(config.resolve_experiment_plan)
            self._validate_plan(config)
            benchmark = await asyncio.to_thread(
                _RunnerBenchmark,
                config=config,
                description=f"Customer-side launch {spec.launch_id}",
            )
            if len(benchmark) == 0:
                raise RuntimeError("The Experiment Plan Dataset contains no selected rows")
            expected_task_keys = {(str(task.id), "0") for task in benchmark}
            if len(expected_task_keys) != len(benchmark):
                raise RuntimeError("Experiment Dataset produced duplicate task IDs")

            await asyncio.to_thread(config.resolve_sls_project)

            trace_resolver = (
                self.trace_resolver_factory(connection)
                if self.trace_resolver_factory is not None
                else TraceCorrelationService(
                    AgentSpaceTrajectoryLogStoreClient(
                        connection,
                        user_agent=self.user_agent,
                        sls_project=config.project,
                    ),
                    policy=self.trace_polling_policy,
                )
            )
            if trace_resolver is None:
                raise RuntimeError("Trace resolver is required for every local experiment")

            def on_trace_reported(task_id: str, trace_id: str) -> None:
                context = trace_contexts.get(task_id)
                result = trace_results.get(task_id)
                if context is None or result is None or result.trace_id != trace_id:
                    return
                self._observe_trace(
                    TraceCorrelationProgress(
                        context=context,
                        status=TraceCorrelationStatus.REPORTED,
                        attempts=result.attempts,
                        occurred_at_ms=int(time.time() * 1000),
                        trace_id=trace_id,
                        source=result.source,
                    )
                )

            launch_result_dir = self.result_dir / spec.launch_id
            try:
                storage = await asyncio.to_thread(
                    _AuditedStorage,
                    save_dir=str(launch_result_dir),
                    config=config,
                    experiment_type="agent",
                    experiment_metadata={
                        "run_env": "customer_launcher",
                        "local_launch_id": spec.launch_id,
                        "agent_connection_id": spec.agent_connection_id,
                        "experiment_version": spec.experiment_version,
                        "experiment_parameters": spec.parameters,
                        "context_schema_version": spec.context_schema_version,
                    },
                    trace_report_callback=on_trace_reported,
                )
            except asyncio.CancelledError:
                raise
            except AgentLoopGatewayError:
                raise
            except (TypeError, ValueError):
                raise ExperimentPreparationError(
                    "Customer-side experiment preparation failed"
                ) from None
            except Exception:
                # The storage constructor owns CreateExperimentRun.  A
                # transport exception may happen after the service accepted
                # the Run but before its recordId reached this process.
                raise SubmissionUnknownError() from None

            storage.configure_expected_task_keys(expected_task_keys)

            # Persist the authoritative record ID before the first customer
            # Agent task is dispatched.
            try:
                await on_record_created(storage.experiment_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                try:
                    await asyncio.to_thread(
                        self._mark_run_failed,
                        storage.config,
                        storage.experiment_id,
                        storage._completed_tasks,
                        max(1, storage._failed_tasks),
                        storage._total_tasks,
                    )
                except Exception:
                    pass
                raise SubmissionUnknownError() from None
            if not record_ready.done():
                record_ready.set_result(storage.experiment_id)

            await asyncio.to_thread(storage.start_experiment, len(benchmark))
            for task in benchmark:
                if task.metadata is None:
                    task.metadata = {}
                task.metadata["experiment_id"] = storage.experiment_id

            evaluator = AgentLoopGeneralEvaluator(
                name="AgentLoop Customer-side Evaluation",
                benchmark=benchmark,
                n_repeat=1,
                storage=storage,
                n_workers=1,
            )

            async def solution(task: Task, pre_hook: Callable[..., Any]) -> SolutionOutput:
                del pre_hook
                output = await self._execute_task(
                    spec,
                    storage.experiment_id,
                    task,
                    trace_resolver=trace_resolver,
                    trace_contexts=trace_contexts,
                    trace_results=trace_results,
                )
                if not output.success and isinstance(output.meta, dict):
                    storage.register_agent_failure_category(
                        str(output.meta.get("agent_error_category") or "ProviderError")
                    )
                correlation = trace_results.get(task.id)
                if correlation is None:
                    raise RuntimeError("Trace resolver did not produce a verification proof")
                storage.register_verified_trace(task.id, "0", correlation.trace_id)
                return output

            await evaluator.run(solution)
            return await self._finalize_handoff(storage)
        except asyncio.CancelledError:
            if storage is not None:
                await self._fail_storage_after_cancellation(storage)
            if not record_ready.done():
                record_ready.cancel()
            raise
        except Exception as exc:
            if not record_ready.done():
                record_ready.set_exception(exc)
            elif storage is not None:
                try:
                    await asyncio.to_thread(
                        self._mark_run_failed,
                        storage.config,
                        storage.experiment_id,
                        storage._completed_tasks,
                        max(1, storage._failed_tasks),
                        storage._total_tasks,
                    )
                except Exception:
                    pass
            return LocalHandoffOutcome.definitive_failure("Local Runner failed during execution")

    @classmethod
    async def _fail_storage_after_cancellation(cls, storage: _AuditedStorage) -> None:
        """Best-effort cloud convergence before propagating process cancellation."""

        try:
            await asyncio.to_thread(
                cls._mark_run_failed,
                storage.config,
                storage.experiment_id,
                storage._completed_tasks,
                max(1, storage._failed_tasks),
                storage._total_tasks,
            )
        except Exception:
            # The local Launch is independently moved to UNKNOWN by its waiter.
            # Never disguise a failed convergence attempt as graceful success.
            pass

    @staticmethod
    async def _finalize_handoff(storage: _AuditedStorage) -> LocalHandoffOutcome:
        failure = _storage_failure_message(storage)
        await asyncio.to_thread(
            storage.finalize_experiment,
            "failed" if failure is not None else "completed",
        )
        if failure is not None:
            return LocalHandoffOutcome.definitive_failure(failure)
        return LocalHandoffOutcome.succeeded(
            f"Uploaded {storage._completed_tasks + storage._failed_tasks} result(s)"
        )

    async def _execute_task(
        self,
        spec: OfflineExperimentSpec,
        record_id: str,
        task: Task,
        *,
        trace_resolver: TraceCorrelationService | None = None,
        trace_contexts: dict[str, TraceCorrelationContext] | None = None,
        trace_results: dict[str, TraceCorrelationResult] | None = None,
    ) -> SolutionOutput:
        row = task.input if isinstance(task.input, dict) else {"input": task.input}
        dataset_item_id = str(row.get("id") or task.id)
        question = _question_from_row(row)
        context = {
            "schema_version": spec.context_schema_version,
            "experiment_plan_id": spec.plan_id,
            "experiment_run_id": record_id,
            "experiment_version": spec.experiment_version,
            "dataset_item_id": dataset_item_id,
            "local_launch_id": spec.launch_id,
        }
        rendered_context = render_experiment_context(
            spec.context_xml_template,
            experiment_run_id=record_id,
            dataset_item_id=dataset_item_id,
        )
        experiment_input = append_experiment_context(question, rendered_context)
        provider_input = dict(row)
        provider_input_field = next(
            (name for name in ("question", "input", "query", "prompt") if name in provider_input),
            "question",
        )
        provider_input[provider_input_field] = experiment_input
        provider_metadata = dict(task.metadata or {})
        provider_metadata.update(
            {
                "experiment_id": record_id,
                "experiment_plan_id": spec.plan_id,
                "experiment_version": spec.experiment_version,
                "dataset_item_id": dataset_item_id,
                "local_launch_id": spec.launch_id,
                "experiment_context": context,
                "experiment_context_xml": rendered_context,
                "experiment_input": experiment_input,
                "experiment_parameters": spec.parameters,
                "original_input": row,
            }
        )
        payload = {
            "type": "experiment.task",
            "requestId": task.id,
            "input": experiment_input,
            "originalInput": row,
            "sdkTask": {
                "id": task.id,
                "input": provider_input,
                "groundTruth": task.ground_truth,
                "tags": task.tags,
                "metadata": provider_metadata,
            },
            "context": context,
            "parameters": spec.parameters,
        }
        agent_started_at_ms = int(time.time() * 1000)
        agent_error_category: str | None = None
        try:
            result = await self.dispatcher.execute(
                spec.agent_connection_id,
                payload,
                timeout_seconds=self.task_timeout_seconds,
            )
            normalized = _normalize_agent_result(result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            agent_error_category = safe_agent_error_category(
                provider_error=getattr(exc, "provider_error", None),
                execution_type=type(exc).__name__,
            )
            normalized = AgentTaskResult(
                success=False,
                output="",
                metadata={
                    "errorType": agent_error_category,
                },
            )
        agent_completed_at_ms = int(time.time() * 1000)
        agent_metadata = normalized.metadata
        solution_output = normalized.output
        if not normalized.success:
            if agent_error_category is None:
                agent_error_category = safe_agent_error_category(
                    reported_type=agent_metadata.get("errorType"),
                )
        metadata = {
            "experiment_input": experiment_input,
            "experiment_context": context,
            "agent_connection_id": spec.agent_connection_id,
            "agent_metadata": agent_metadata,
        }
        trace_id = _validated_trace_id(normalized.trace_id)
        correlation_result: TraceCorrelationResult | None = None
        if trace_resolver is not None:
            correlation_context = TraceCorrelationContext(
                launch_id=spec.launch_id,
                experiment_plan_id=spec.plan_id,
                experiment_run_id=record_id,
                dataset_item_id=dataset_item_id,
                agent_started_at_ms=agent_started_at_ms,
                agent_completed_at_ms=agent_completed_at_ms,
            )
            if trace_contexts is not None:
                trace_contexts[task.id] = correlation_context
            correlation_result = await trace_resolver.resolve(
                correlation_context,
                hinted_trace_id=trace_id,
                on_progress=self._observe_trace,
            )
            trace_id = correlation_result.trace_id
            if trace_results is not None:
                trace_results[task.id] = correlation_result
        if trace_id:
            metadata["traceId"] = trace_id
        if correlation_result is not None:
            metadata["trace_correlation"] = {
                "source": correlation_result.source,
                "attempts": correlation_result.attempts,
                "resolved_at_ms": correlation_result.resolved_at_ms,
            }
        if not normalized.success:
            metadata["agent_error_category"] = agent_error_category
            metadata["error_type"] = agent_error_category
            metadata["error_message"] = "Customer Agent execution failed"
        return SolutionOutput(
            success=normalized.success,
            output=solution_output,
            trajectory=normalized.trajectory,
            meta=metadata,
        )

    def _observe_trace(self, progress: TraceCorrelationProgress) -> None:
        """Persist safe correlation metadata without weakening cloud correctness."""

        if self.trace_observer is None:
            return
        try:
            self.trace_observer(progress)
        except Exception:
            # A local audit write must never cause a second experiment_detail
            # upload. Cloud correctness remains guarded by the Trace barrier.
            return

    @staticmethod
    def _validate_plan(config: AgentLoopConfig) -> None:
        if not config.plan_evaluators:
            raise ValueError("The Experiment Plan must bind at least one evaluator")
        for evaluator in config.plan_evaluators:
            variables = (evaluator.config or {}).get("variables", [])
            required = {
                item.get("name")
                for item in variables
                if isinstance(item, dict) and item.get("name")
            }
            missing = required - set((evaluator.variable_mapping or {}).keys())
            if missing:
                name = evaluator.evaluator_ref or evaluator.name or "unnamed evaluator"
                raise ValueError(
                    f"Evaluator {name} is missing variable mappings: {sorted(missing)}"
                )

    @staticmethod
    def _mark_run_failed(
        config: AgentLoopConfig,
        record_id: str,
        completed: int,
        failed: int,
        total: int,
    ) -> None:
        from agentloop_sdk._vendor.alibabacloud_agentloop20260520 import (
            models as api_models,
        )

        config._create_agentloop_client().update_experiment_run(
            config.agent_space,
            record_id,
            api_models.UpdateExperimentRunRequest(
                status="failed",
                completed_tasks=completed,
                failed_tasks=failed,
                total_tasks=total,
                completed_at=int(time.time() * 1000),
            ),
        )


def _question_from_row(row: dict[str, Any]) -> str:
    for name in ("question", "input", "query", "prompt"):
        value = row.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return json.dumps(row, ensure_ascii=False, default=str)


def _storage_failure_message(storage: _AuditedStorage) -> str | None:
    completion_failure = getattr(storage, "completion_failure_message", None)
    if callable(completion_failure):
        return completion_failure()
    if storage.upload_failures:
        return f"{len(storage.upload_failures)} Dataset result(s) failed to upload to SLS"
    expected = storage._completed_tasks + storage._failed_tasks
    reported = getattr(storage, "reported_detail_count", expected)
    if reported != expected or reported != storage._total_tasks:
        return f"Only {reported} of {storage._total_tasks} experiment detail(s) were reported"
    if storage._failed_tasks:
        return _agent_task_failure_message(
            storage._failed_tasks,
            storage._total_tasks,
            getattr(storage, "_agent_failure_categories", set()),
        )
    return None


def _agent_task_failure_message(
    failed_tasks: int,
    total_tasks: int,
    categories: set[str],
) -> str:
    message = f"{failed_tasks} of {total_tasks} customer Agent task(s) failed"
    safe_categories = categories & SAFE_AGENT_ERROR_CATEGORIES
    if not safe_categories:
        return message
    category = next(iter(safe_categories)) if len(safe_categories) == 1 else "ProviderError"
    return f"{message} ({category})"


def _validated_trace_id(value: Any) -> str | None:
    return validate_trace_id(value)


def _normalize_agent_result(value: AgentTaskResult | dict[str, Any]) -> AgentTaskResult:
    if isinstance(value, AgentTaskResult):
        return value
    if hasattr(value, "output") and hasattr(value, "meta"):
        metadata = value.meta if isinstance(value.meta, dict) else {}
        return AgentTaskResult(
            success=bool(getattr(value, "success", True)),
            output=value.output,
            trace_id=metadata.get("traceId") or metadata.get("trace_id"),
            metadata=metadata,
            trajectory=list(getattr(value, "trajectory", []) or []),
        )
    if not isinstance(value, dict):
        raise TypeError("Agent dispatcher result must be AgentTaskResult or a JSON object")
    if "success" not in value or "output" not in value:
        raise ValueError("Agent result must contain success and output")
    metadata = value.get("metadata", value.get("meta"))
    return AgentTaskResult(
        success=bool(value["success"]),
        output=value["output"],
        trace_id=value.get("traceId") or value.get("trace_id"),
        metadata=metadata if isinstance(metadata, dict) else {},
        trajectory=list(value.get("trajectory") or []),
    )
