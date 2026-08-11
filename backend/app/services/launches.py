from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from app.agentloop import (
    AgentLoopConnection,
    AgentLoopGateway,
    AgentLoopGatewayError,
    LocalHandoffState,
    PlanNotFoundError,
    SubmissionUnknownError,
)
from app.domain.agent_error import SAFE_AGENT_ERROR_CATEGORIES
from app.domain.clock import Clock
from app.domain.errors import ConflictError, LauncherError, NotFoundError, ServiceUnavailableError
from app.domain.experiment_context import (
    CONTEXT_SCHEMA_VERSION,
    build_experiment_context_template,
)
from app.domain.launch_config import (
    LaunchExecutionConfig,
    VersionMode,
    canonical_launch_config_hash,
    canonical_parameters_json,
    resolve_experiment_version,
)
from app.scheduler import schedule_slot_idempotency_key
from app.storage import (
    AgentConnectionRepository,
    IdempotencyConflictError,
    LaunchIntentRepository,
    ProfileRepository,
    RunnerState,
    SkipReason,
    SubmissionState,
    TriggerSource,
    ValidationStatus,
)

from .tasks import BackgroundTaskSupervisor

if TYPE_CHECKING:
    from .plan_dashboards import PlanDashboardService

_SAFE_RUNNER_FAILURE_PATTERNS = (
    re.compile(r"\d+ Dataset result\(s\) failed to upload to SLS"),
    re.compile(r"Local Runner failed during execution"),
)
_SAFE_AGENT_FAILURE_PATTERN = re.compile(
    r"\d+ of \d+ customer Agent task\(s\) failed(?: \(([A-Za-z][A-Za-z0-9_]*)\))?"
)


class LaunchService:
    def __init__(
        self,
        *,
        profiles: ProfileRepository,
        launches: LaunchIntentRepository,
        gateway: AgentLoopGateway,
        clock: Clock,
        tasks: BackgroundTaskSupervisor,
        connection_provider: Callable[[], AgentLoopConnection],
        agent_connections: AgentConnectionRepository | None = None,
        agent_connection_online: Callable[[str], bool] | None = None,
        require_agent_connection: bool = False,
        plan_dashboards: PlanDashboardService | None = None,
    ) -> None:
        self.profiles = profiles
        self.launches = launches
        self.gateway = gateway
        self.clock = clock
        self.tasks = tasks
        self.connection_provider = connection_provider
        self.agent_connections = agent_connections
        self.agent_connection_online = agent_connection_online
        self.require_agent_connection = require_agent_connection
        self.plan_dashboards = plan_dashboards
        self._locks: dict[str, asyncio.Lock] = {}
        self._capacity_lock = asyncio.Lock()

    async def manual(
        self,
        plan_id: str,
        request_id: str,
        *,
        agent_connection_id: str | None = None,
        version_mode: str = VersionMode.AUTO.value,
        experiment_version: str | None = None,
        experiment_parameters: dict[str, Any] | None = None,
        record_name: str | None = None,
        context_schema_version: int = CONTEXT_SCHEMA_VERSION,
    ):
        return await self._launch(
            plan_id=plan_id,
            idempotency_key=f"manual:{request_id}",
            trigger_source=TriggerSource.MANUAL.value,
            agent_connection_id=agent_connection_id,
            version_mode=version_mode,
            requested_experiment_version=experiment_version,
            experiment_parameters=experiment_parameters,
            record_name=record_name,
            context_schema_version=context_schema_version,
        )

    async def schedule_run_now(self, schedule, request_id: str):
        return await self._launch(
            plan_id=schedule.plan_id,
            idempotency_key=f"schedule-run-now:{schedule.id}:{request_id}",
            trigger_source=TriggerSource.SCHEDULE_RUN_NOW.value,
            schedule_id=schedule.id,
            schedule_revision=schedule.revision,
            agent_connection_id=schedule.agent_connection_id,
            agent_connection_name_snapshot=schedule.agent_connection_name_snapshot,
            version_mode=schedule.version_mode,
            requested_experiment_version=schedule.manual_experiment_version,
            experiment_parameters=schedule.experiment_parameters_json,
            record_name=schedule.record_name,
            context_schema_version=schedule.context_schema_version,
        )

    async def scheduled(self, schedule, scheduled_for_utc: datetime):
        key = schedule_slot_idempotency_key(schedule.id, schedule.revision, scheduled_for_utc)
        if (
            schedule.agent_connection_id is not None
            and self.agent_connection_online is not None
            and not self.agent_connection_online(schedule.agent_connection_id)
        ):
            return self.skip_schedule(
                schedule,
                scheduled_for_utc,
                SkipReason.AGENT_OFFLINE.value,
            )
        return await self._launch(
            plan_id=schedule.plan_id,
            idempotency_key=key,
            trigger_source=TriggerSource.SCHEDULE.value,
            schedule_id=schedule.id,
            schedule_revision=schedule.revision,
            scheduled_for_utc=scheduled_for_utc,
            agent_connection_id=schedule.agent_connection_id,
            agent_connection_name_snapshot=schedule.agent_connection_name_snapshot,
            version_mode=schedule.version_mode,
            requested_experiment_version=schedule.manual_experiment_version,
            experiment_parameters=schedule.experiment_parameters_json,
            record_name=schedule.record_name,
            context_schema_version=schedule.context_schema_version,
        )

    def skip_schedule(self, schedule, scheduled_for_utc: datetime, reason: str):
        profile = self._profile(require_valid=False)
        key = schedule_slot_idempotency_key(schedule.id, schedule.revision, scheduled_for_utc)
        launch_id = str(uuid4())
        requested_at = self.clock.now()
        mode, manual_version, final_version = resolve_experiment_version(
            version_mode=schedule.version_mode,
            requested_version=schedule.manual_experiment_version,
            now=requested_at,
            launch_id=launch_id,
        )
        template = build_experiment_context_template(
            experiment_plan_id=schedule.plan_id,
            experiment_version=final_version,
            local_launch_id=launch_id,
            schema_version=schedule.context_schema_version,
        )
        intent, _ = self.launches.create_or_get(
            launch_id=launch_id,
            profile_id=profile.id,
            plan_id=schedule.plan_id,
            plan_name_snapshot=schedule.plan_name_snapshot,
            agent_connection_id=schedule.agent_connection_id,
            agent_connection_name_snapshot=schedule.agent_connection_name_snapshot,
            version_mode=mode,
            manual_experiment_version=manual_version,
            experiment_version=final_version,
            experiment_parameters_json=schedule.experiment_parameters_json,
            record_name=schedule.record_name,
            context_schema_version=schedule.context_schema_version,
            context_xml_template=template,
            launch_config_hash=schedule.launch_config_hash,
            trigger_source=TriggerSource.SCHEDULE.value,
            schedule_id=schedule.id,
            schedule_revision=schedule.revision,
            scheduled_for_utc=scheduled_for_utc,
            idempotency_key=key,
            submission_state=SubmissionState.SKIPPED.value,
            runner_state=RunnerState.NOT_STARTED.value,
            skip_reason=reason,
            requested_at=requested_at,
        )
        return intent

    async def _launch(
        self,
        *,
        plan_id: str,
        idempotency_key: str,
        trigger_source: str,
        agent_connection_id: str | None = None,
        agent_connection_name_snapshot: str | None = None,
        version_mode: str = VersionMode.AUTO.value,
        requested_experiment_version: str | None = None,
        experiment_parameters: dict[str, Any] | str | None = None,
        record_name: str | None = None,
        context_schema_version: int = CONTEXT_SCHEMA_VERSION,
        schedule_id: str | None = None,
        schedule_revision: int | None = None,
        scheduled_for_utc: datetime | None = None,
    ):
        parameters_json = canonical_parameters_json(experiment_parameters)
        request_config_hash = canonical_launch_config_hash(
            agent_connection_id=agent_connection_id,
            version_mode=version_mode,
            requested_version=requested_experiment_version,
            experiment_parameters_json=parameters_json,
            record_name=record_name,
            context_schema_version=context_schema_version,
        )
        lock = self._locks.setdefault(idempotency_key, asyncio.Lock())
        async with lock:
            existing = self.launches.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                if (
                    existing.plan_id != plan_id
                    or existing.launch_config_hash != request_config_hash
                ):
                    raise ConflictError(
                        "IDEMPOTENCY_KEY_CONFLICT",
                        "The idempotency key is already bound to another launch configuration",
                    )
                if existing.submission_state == SubmissionState.DEFINITIVE_FAILURE.value:
                    code = existing.error_code or "LAUNCH_DEFINITIVE_FAILURE"
                    raise LauncherError(
                        code=code,
                        message=existing.sanitized_error
                        or "The previous launch definitively failed",
                        status_code=(503 if code.startswith("AGENTLOOP") else 409),
                        retryable=False,
                        launch_id=existing.id,
                    )
                if existing.submission_state in {
                    SubmissionState.ACCEPTED.value,
                    SubmissionState.SKIPPED.value,
                    SubmissionState.SUBMITTING.value,
                    SubmissionState.SUBMISSION_UNKNOWN.value,
                }:
                    return existing

            profile = self._profile()
            connection = self.connection_provider()
            try:
                plan = await self.gateway.get_plan(plan_id, connection=connection)
            except PlanNotFoundError as exc:
                raise NotFoundError("PLAN_NOT_FOUND", str(exc)) from exc
            except AgentLoopGatewayError as exc:
                raise ServiceUnavailableError(exc.code, str(exc), retryable=exc.retryable) from exc

            if not plan.can_launch_locally:
                raise LauncherError(
                    code="PLAN_NOT_RUNNABLE",
                    message=plan.cannot_launch_reason or "The plan cannot be launched locally",
                    status_code=409,
                )

            if self.require_agent_connection and agent_connection_id is None:
                raise ConflictError(
                    "AGENT_CONNECTION_REQUIRED",
                    "Select a connected local Agent before launching the experiment",
                )

            if agent_connection_id is not None and self.agent_connections is not None:
                agent_connection = self.agent_connections.get(agent_connection_id)
                if agent_connection is None:
                    raise NotFoundError(
                        "AGENT_CONNECTION_NOT_FOUND",
                        f"Agent connection not found: {agent_connection_id}",
                    )
                agent_connection_name_snapshot = agent_connection.name
            if (
                agent_connection_id is not None
                and self.agent_connection_online is not None
                and not self.agent_connection_online(agent_connection_id)
            ):
                raise ConflictError(
                    "AGENT_CONNECTION_OFFLINE",
                    "The selected local Agent is not connected",
                )

            launch_id = str(uuid4())
            requested_at = self.clock.now()
            mode, manual_version, final_version = resolve_experiment_version(
                version_mode=version_mode,
                requested_version=requested_experiment_version,
                now=requested_at,
                launch_id=launch_id,
            )
            context_template = build_experiment_context_template(
                experiment_plan_id=plan.plan_id,
                experiment_version=final_version,
                local_launch_id=launch_id,
                schema_version=context_schema_version,
            )

            async with self._capacity_lock:
                if schedule_id is not None and self.launches.has_local_overlap(
                    schedule_id,
                    exclude_launch_id=(existing.id if existing is not None else None),
                ):
                    if trigger_source == TriggerSource.SCHEDULE.value:
                        skipped, _ = self.launches.create_or_get(
                            launch_id=launch_id,
                            profile_id=profile.id,
                            plan_id=plan.plan_id,
                            plan_name_snapshot=plan.plan_name,
                            plan_updated_at_snapshot=plan.updated_at,
                            agent_connection_id=agent_connection_id,
                            agent_connection_name_snapshot=agent_connection_name_snapshot,
                            version_mode=mode,
                            manual_experiment_version=manual_version,
                            experiment_version=final_version,
                            experiment_parameters_json=parameters_json,
                            record_name=record_name,
                            context_schema_version=context_schema_version,
                            context_xml_template=context_template,
                            launch_config_hash=request_config_hash,
                            trigger_source=trigger_source,
                            schedule_id=schedule_id,
                            schedule_revision=schedule_revision,
                            scheduled_for_utc=scheduled_for_utc,
                            idempotency_key=idempotency_key,
                            submission_state=SubmissionState.SKIPPED.value,
                            runner_state=RunnerState.NOT_STARTED.value,
                            skip_reason=SkipReason.OVERLAP.value,
                            requested_at=requested_at,
                        )
                        return skipped
                    raise ConflictError(
                        "LOCAL_SCHEDULE_OVERLAP",
                        "Another customer-side run for this schedule may still be active",
                    )
                active = self.launches.active_runner_count(
                    exclude_launch_id=(existing.id if existing is not None else None)
                )
                if active >= profile.max_concurrency:
                    raise ConflictError(
                        "LOCAL_CONCURRENCY_LIMIT",
                        "The configured customer-side experiment concurrency limit is reached",
                    )
                try:
                    intent, created = self.launches.create_or_get(
                        launch_id=launch_id,
                        profile_id=profile.id,
                        plan_id=plan.plan_id,
                        plan_name_snapshot=plan.plan_name,
                        plan_updated_at_snapshot=plan.updated_at,
                        agent_connection_id=agent_connection_id,
                        agent_connection_name_snapshot=agent_connection_name_snapshot,
                        version_mode=mode,
                        manual_experiment_version=manual_version,
                        experiment_version=final_version,
                        experiment_parameters_json=parameters_json,
                        record_name=record_name,
                        context_schema_version=context_schema_version,
                        context_xml_template=context_template,
                        launch_config_hash=request_config_hash,
                        trigger_source=trigger_source,
                        schedule_id=schedule_id,
                        schedule_revision=schedule_revision,
                        scheduled_for_utc=scheduled_for_utc,
                        idempotency_key=idempotency_key,
                        requested_at=requested_at,
                    )
                except IdempotencyConflictError as exc:
                    raise ConflictError("IDEMPOTENCY_KEY_CONFLICT", str(exc)) from exc

                if created or intent.submission_state == SubmissionState.CREATED.value:
                    intent = self.launches.transition_submission(
                        intent.id, SubmissionState.SUBMITTING.value, at=self.clock.now()
                    )
                    intent = self.launches.transition_runner(
                        intent.id, RunnerState.RUNNING.value, at=self.clock.now()
                    )

            if self.plan_dashboards is not None:
                try:
                    await self.plan_dashboards.ensure_dashboard(
                        profile_id=profile.id,
                        plan=plan,
                        connection=connection,
                    )
                except Exception:
                    # A dashboard is an analysis aid, not part of the customer
                    # Agent execution contract.  The service records controlled
                    # publication failures; this final guard also ensures a
                    # local template defect cannot block an Experiment Run.
                    pass

            async def on_record_created(record_id: str) -> None:
                self.launches.transition_submission(
                    intent.id,
                    SubmissionState.ACCEPTED.value,
                    at=self.clock.now(),
                    record_id=record_id,
                )

            try:
                execution_config = LaunchExecutionConfig(
                    launch_id=intent.id,
                    agent_connection_id=intent.agent_connection_id,
                    experiment_version=intent.experiment_version or final_version,
                    version_mode=intent.version_mode,
                    experiment_parameters_json=intent.experiment_parameters_json,
                    context_schema_version=intent.context_schema_version,
                    context_xml_template=intent.context_xml_template or context_template,
                    record_name=intent.record_name,
                    launch_config_hash=intent.launch_config_hash,
                )
                handle = await self.gateway.start_offline_experiment(
                    plan.plan_id,
                    on_record_created,
                    connection=connection,
                    execution_config=execution_config,
                )
            except SubmissionUnknownError as exc:
                self._mark_unknown(intent.id, exc.code, str(exc))
                return self.launches.get(intent.id)
            except AgentLoopGatewayError as exc:
                remote_unknown = bool(getattr(exc, "remote_acceptance_unknown", exc.retryable))
                if remote_unknown:
                    self._mark_unknown(intent.id, exc.code, str(exc))
                else:
                    self._mark_definitive_failure(intent.id, exc.code, str(exc))
                if remote_unknown:
                    return self.launches.get(intent.id)
                raise ServiceUnavailableError(exc.code, str(exc), retryable=exc.retryable) from exc
            except Exception as exc:
                self._mark_unknown(intent.id, "SUBMISSION_UNKNOWN", "Unexpected gateway failure")
                raise ServiceUnavailableError(
                    "SUBMISSION_UNKNOWN", "Unable to determine whether AgentLoop accepted the run"
                ) from exc

            accepted = self.launches.get(intent.id)
            if accepted is None:
                raise RuntimeError("launch intent disappeared after AgentLoop acceptance")
            if accepted.submission_state != SubmissionState.ACCEPTED.value:
                await on_record_created(handle.record_id)
                accepted = self.launches.get(intent.id)
            self.tasks.start(self._wait_handoff(intent.id, handle.wait_local_handoff))
            return accepted

    async def _wait_handoff(self, launch_id: str, wait: Callable[[], Awaitable]) -> None:
        try:
            outcome = await wait()
        except BaseException as exc:
            self._safe_runner_transition(
                launch_id,
                RunnerState.UNKNOWN.value,
                error_code="RUNNER_OUTCOME_UNKNOWN",
                sanitized_error=f"{type(exc).__name__}: local Runner outcome is unknown",
            )
            return
        mapping = {
            LocalHandoffState.SUCCEEDED: RunnerState.HANDED_OFF.value,
            LocalHandoffState.DEFINITIVE_FAILURE: RunnerState.FAILED.value,
            LocalHandoffState.UNKNOWN: RunnerState.UNKNOWN.value,
        }
        target = mapping[outcome.state]
        self._safe_runner_transition(
            launch_id,
            target,
            error_code=(
                "RUNNER_DEFINITIVE_FAILURE"
                if target == RunnerState.FAILED.value
                else "RUNNER_OUTCOME_UNKNOWN"
                if target == RunnerState.UNKNOWN.value
                else None
            ),
            sanitized_error=_safe_runner_outcome_message(target, outcome.message),
        )

    def _profile(self, *, require_valid: bool = True):
        profile = self.profiles.get_configured()
        if profile is None:
            raise ServiceUnavailableError(
                "CONNECTION_NOT_CONFIGURED",
                "AgentLoop connection is not configured",
                retryable=False,
            )
        if require_valid and profile.validation_status != ValidationStatus.VALID.value:
            raise ServiceUnavailableError(
                "CONNECTION_NOT_VALIDATED",
                "Validate the saved AgentLoop connection before launching experiments",
                retryable=False,
            )
        return profile

    def _mark_unknown(self, launch_id: str, code: str, message: str) -> None:
        current = self.launches.get(launch_id)
        if current and current.submission_state == SubmissionState.SUBMITTING.value:
            self.launches.transition_submission(
                launch_id,
                SubmissionState.SUBMISSION_UNKNOWN.value,
                at=self.clock.now(),
                error_code=code,
                sanitized_error=message,
            )
        self._safe_runner_transition(launch_id, RunnerState.UNKNOWN.value)

    def _mark_definitive_failure(self, launch_id: str, code: str, message: str) -> None:
        current = self.launches.get(launch_id)
        if current and current.submission_state in {
            SubmissionState.CREATED.value,
            SubmissionState.SUBMITTING.value,
            SubmissionState.SUBMISSION_UNKNOWN.value,
        }:
            self.launches.transition_submission(
                launch_id,
                SubmissionState.DEFINITIVE_FAILURE.value,
                at=self.clock.now(),
                error_code=code,
                sanitized_error=message,
            )
        self._safe_runner_transition(launch_id, RunnerState.FAILED.value)

    def _safe_runner_transition(
        self,
        launch_id: str,
        state: str,
        *,
        error_code: str | None = None,
        sanitized_error: str | None = None,
    ) -> None:
        current = self.launches.get(launch_id)
        if current is None or current.runner_state == state:
            return
        try:
            self.launches.transition_runner(
                launch_id,
                state,
                at=self.clock.now(),
                error_code=error_code,
                sanitized_error=sanitized_error,
            )
        except Exception:
            # Recovery can observe an already-final runner state. Submission
            # state remains authoritative for whether a remote record exists.
            return


def _safe_runner_outcome_message(target: str, message: str | None) -> str | None:
    if target == RunnerState.HANDED_OFF.value:
        return None
    if target == RunnerState.UNKNOWN.value:
        return "Local Runner outcome is unknown"
    if target != RunnerState.FAILED.value:
        return None
    candidate = (message or "").strip()
    if any(pattern.fullmatch(candidate) for pattern in _SAFE_RUNNER_FAILURE_PATTERNS):
        return candidate
    agent_failure = _SAFE_AGENT_FAILURE_PATTERN.fullmatch(candidate)
    if agent_failure is not None:
        category = agent_failure.group(1)
        if category is None or category in SAFE_AGENT_ERROR_CATEGORIES:
            return candidate
    return "Local Runner failed; inspect customer-side service logs"
