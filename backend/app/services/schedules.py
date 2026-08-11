from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from app.agentloop import (
    AgentLoopConnection,
    AgentLoopGateway,
    AgentLoopGatewayError,
    PlanNotFoundError,
)
from app.domain.clock import Clock
from app.domain.errors import ConflictError, NotFoundError, ServiceUnavailableError
from app.scheduler import ScheduleTiming, next_fire
from app.storage import (
    AgentConnectionRepository,
    ConcurrentUpdateError,
    ProfileRepository,
    ScheduleRepository,
    ValidationStatus,
)

from .launches import LaunchService


class ScheduleService:
    def __init__(
        self,
        *,
        profiles: ProfileRepository,
        schedules: ScheduleRepository,
        gateway: AgentLoopGateway,
        launches: LaunchService,
        clock: Clock,
        connection_provider: Callable[[], AgentLoopConnection],
        agent_connections: AgentConnectionRepository | None = None,
        require_agent_connection: bool = False,
    ) -> None:
        self.profiles = profiles
        self.schedules = schedules
        self.gateway = gateway
        self.launches = launches
        self.clock = clock
        self.connection_provider = connection_provider
        self.agent_connections = agent_connections
        self.require_agent_connection = require_agent_connection

    async def create(
        self,
        *,
        name: str,
        plan_id: str,
        cadence_type: str,
        timezone: str,
        interval_hours: int | None,
        interval_minutes: int | None,
        daily_time: str | None,
        anchor_at: datetime | None,
        misfire_policy: str,
        misfire_grace_seconds: int,
        enabled: bool,
        agent_connection_id: str | None = None,
        version_mode: str = "AUTO",
        experiment_version: str | None = None,
        experiment_parameters: dict[str, Any] | None = None,
        record_name: str | None = None,
        context_schema_version: int = 1,
    ):
        profile = self.profiles.get_configured()
        if profile is None:
            raise ServiceUnavailableError(
                "CONNECTION_NOT_CONFIGURED",
                "AgentLoop connection is not configured",
                retryable=False,
            )
        if profile.validation_status != ValidationStatus.VALID.value:
            raise ServiceUnavailableError(
                "CONNECTION_NOT_VALIDATED",
                "Validate the saved AgentLoop connection before creating schedules",
                retryable=False,
            )
        try:
            plan = await self.gateway.get_plan(plan_id, connection=self.connection_provider())
        except PlanNotFoundError as exc:
            raise NotFoundError("PLAN_NOT_FOUND", str(exc)) from exc
        except AgentLoopGatewayError as exc:
            raise ServiceUnavailableError(exc.code, str(exc), retryable=exc.retryable) from exc
        if not plan.can_launch_locally:
            raise ConflictError(
                "PLAN_NOT_RUNNABLE", plan.cannot_launch_reason or "Plan is not runnable"
            )
        if self.require_agent_connection and agent_connection_id is None:
            raise ConflictError(
                "AGENT_CONNECTION_REQUIRED",
                "Select a local Agent before creating the schedule",
            )
        agent_connection_name = None
        if agent_connection_id is not None and self.agent_connections is not None:
            agent_connection = self.agent_connections.get(agent_connection_id)
            if agent_connection is None:
                raise NotFoundError(
                    "AGENT_CONNECTION_NOT_FOUND",
                    f"Agent connection not found: {agent_connection_id}",
                )
            agent_connection_name = agent_connection.name
        timing = ScheduleTiming(
            cadence_type=cadence_type,
            timezone=timezone,
            interval_hours=interval_hours,
            interval_minutes=interval_minutes,
            daily_time=daily_time,
            anchor_at=anchor_at,
        )
        next_at = next_fire(timing, after_utc=self.clock.now(), inclusive=True) if enabled else None
        return self.schedules.create(
            profile_id=profile.id,
            name=name,
            plan_id=plan.plan_id,
            plan_name_snapshot=plan.plan_name,
            agent_connection_id=agent_connection_id,
            agent_connection_name_snapshot=agent_connection_name,
            version_mode=version_mode,
            manual_experiment_version=experiment_version,
            experiment_parameters_json=experiment_parameters,
            record_name=record_name,
            context_schema_version=context_schema_version,
            enabled=enabled,
            cadence_type=cadence_type,
            timezone=timezone,
            interval_hours=interval_hours,
            interval_minutes=interval_minutes,
            daily_time=daily_time,
            anchor_at=anchor_at,
            misfire_policy=misfire_policy,
            misfire_grace_seconds=misfire_grace_seconds,
            next_fire_at_utc=next_at,
        )

    def get(self, schedule_id: str):
        schedule = self.schedules.get(schedule_id)
        if schedule is None:
            raise NotFoundError("SCHEDULE_NOT_FOUND", f"Schedule not found: {schedule_id}")
        return schedule

    def list(self):
        return self.schedules.list()

    def update(self, schedule_id: str, changes: dict, *, expected_revision: int):
        current = self.get(schedule_id)
        update_values = dict(changes)
        if "agent_connection_id" in update_values:
            connection_id = update_values["agent_connection_id"]
            if self.require_agent_connection and connection_id is None:
                raise ConflictError(
                    "AGENT_CONNECTION_REQUIRED",
                    "A production schedule must remain bound to a local Agent",
                )
            connection_name = None
            if connection_id is not None and self.agent_connections is not None:
                agent_connection = self.agent_connections.get(connection_id)
                if agent_connection is None:
                    raise NotFoundError(
                        "AGENT_CONNECTION_NOT_FOUND",
                        f"Agent connection not found: {connection_id}",
                    )
                connection_name = agent_connection.name
            update_values["agent_connection_name_snapshot"] = connection_name
        merged = {
            "cadence_type": update_values.get("cadence_type", current.cadence_type),
            "timezone": update_values.get("timezone", current.timezone),
            "interval_hours": update_values.get("interval_hours", current.interval_hours),
            "interval_minutes": update_values.get(
                "interval_minutes", current.interval_minutes
            ),
            "daily_time": update_values.get("daily_time", current.daily_time),
            "anchor_at": update_values.get("anchor_at", current.anchor_at),
        }
        timing = ScheduleTiming(**merged)
        timing.validate()
        try:
            updated = self.schedules.update_config(
                schedule_id, expected_revision=expected_revision, **update_values
            )
            next_at = (
                next_fire(timing, after_utc=self.clock.now(), inclusive=True)
                if bool(updated.enabled)
                else None
            )
            return self.schedules.set_next_fire(
                schedule_id, next_at, expected_revision=updated.revision
            )
        except ConcurrentUpdateError as exc:
            raise ConflictError("SCHEDULE_REVISION_CONFLICT", str(exc)) from exc

    async def run_now(self, schedule_id: str, request_id: str):
        return await self.launches.schedule_run_now(self.get(schedule_id), request_id)

    def set_enabled(self, schedule_id: str, *, enabled: bool, expected_revision: int):
        self.get(schedule_id)
        try:
            updated = self.schedules.set_enabled(
                schedule_id, enabled=enabled, expected_revision=expected_revision
            )
            next_at = None
            if enabled:
                next_at = next_fire(
                    ScheduleTiming(
                        cadence_type=updated.cadence_type,
                        timezone=updated.timezone,
                        interval_hours=updated.interval_hours,
                        interval_minutes=updated.interval_minutes,
                        daily_time=updated.daily_time,
                        anchor_at=updated.anchor_at,
                    ),
                    after_utc=self.clock.now(),
                    inclusive=True,
                )
            return self.schedules.set_next_fire(
                schedule_id, next_at, expected_revision=updated.revision
            )
        except ConcurrentUpdateError as exc:
            raise ConflictError("SCHEDULE_REVISION_CONFLICT", str(exc)) from exc

    def delete(self, schedule_id: str, *, expected_revision: int):
        self.get(schedule_id)
        try:
            return self.schedules.soft_delete(schedule_id, expected_revision=expected_revision)
        except ConcurrentUpdateError as exc:
            raise ConflictError("SCHEDULE_REVISION_CONFLICT", str(exc)) from exc
