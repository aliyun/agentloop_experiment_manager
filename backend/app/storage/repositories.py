"""Transactional repositories for profiles, schedules, and launch intents."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import Select, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.launch_config import (
    VersionMode as LaunchVersionMode,
)
from app.domain.launch_config import (
    canonical_launch_config_hash,
    canonical_parameters_json,
    normalize_version_request,
)

from .database import Database
from .models import (
    AgentConnection,
    AgentLoopProfile,
    CadenceType,
    LaunchIntent,
    MisfirePolicy,
    OverlapPolicy,
    PlanDashboard,
    PlanDashboardSyncState,
    RunnerState,
    Schedule,
    SubmissionState,
    TraceCorrelation,
    TraceCorrelationState,
    TriggerSource,
    ValidationStatus,
    as_utc,
    utc_now,
)


class StorageError(RuntimeError):
    pass


class NotFoundError(StorageError):
    pass


class ProfileAlreadyConfiguredError(StorageError):
    pass


class ConcurrentUpdateError(StorageError):
    pass


class IdempotencyConflictError(StorageError):
    pass


class InvalidTransitionError(StorageError):
    pass


_DAILY_TIME = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _non_empty(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    return normalized


def _validate_timezone(name: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"invalid IANA timezone: {name}") from exc
    return name


def _enum_value(value: str | Any, enum_type: type) -> str:
    raw = value.value if hasattr(value, "value") else str(value)
    try:
        enum_type(raw)
    except ValueError as exc:
        raise ValueError(f"unsupported {enum_type.__name__}: {raw}") from exc
    return raw


def _validate_schedule_values(values: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(values)
    cadence = _enum_value(normalized["cadence_type"], CadenceType)
    normalized["cadence_type"] = cadence
    normalized["timezone"] = _validate_timezone(normalized["timezone"])
    normalized["misfire_policy"] = _enum_value(
        normalized.get("misfire_policy", MisfirePolicy.FIRE_ONCE), MisfirePolicy
    )
    normalized["overlap_policy"] = _enum_value(
        normalized.get("overlap_policy", OverlapPolicy.FORBID_LOCAL_OVERLAP), OverlapPolicy
    )

    grace = int(normalized.get("misfire_grace_seconds", 3600))
    if grace < 0:
        raise ValueError("misfire_grace_seconds must be non-negative")
    normalized["misfire_grace_seconds"] = grace

    if cadence == CadenceType.INTERVAL_MINUTES.value:
        interval = normalized.get("interval_minutes")
        if not isinstance(interval, int) or isinstance(interval, bool) or interval < 5:
            raise ValueError(
                "interval_minutes must be an integer greater than or equal to 5"
            )
        if normalized.get("interval_hours") is not None:
            raise ValueError("interval_hours must be null for INTERVAL_MINUTES")
        if normalized.get("daily_time") is not None:
            raise ValueError("daily_time must be null for INTERVAL_MINUTES")
        anchor = normalized.get("anchor_at")
        if anchor is None:
            raise ValueError("anchor_at is required for INTERVAL_MINUTES")
        normalized["anchor_at"] = as_utc(anchor)
    elif cadence == CadenceType.INTERVAL_HOURS.value:
        interval = normalized.get("interval_hours")
        if not isinstance(interval, int) or isinstance(interval, bool) or interval <= 0:
            raise ValueError("interval_hours must be a positive integer")
        if normalized.get("daily_time") is not None:
            raise ValueError("daily_time must be null for INTERVAL_HOURS")
        if normalized.get("interval_minutes") is not None:
            raise ValueError("interval_minutes must be null for INTERVAL_HOURS")
        anchor = normalized.get("anchor_at")
        if anchor is None:
            raise ValueError("anchor_at is required for INTERVAL_HOURS")
        normalized["anchor_at"] = as_utc(anchor)
    else:
        daily_time = normalized.get("daily_time")
        if not isinstance(daily_time, str) or not _DAILY_TIME.fullmatch(daily_time):
            raise ValueError("daily_time must use HH:mm (00:00-23:59)")
        if normalized.get("interval_hours") is not None:
            raise ValueError("interval_hours must be null for DAILY")
        if normalized.get("interval_minutes") is not None:
            raise ValueError("interval_minutes must be null for DAILY")
        if normalized.get("anchor_at") is not None:
            normalized["anchor_at"] = as_utc(normalized["anchor_at"])

    normalized["enabled"] = 1 if bool(normalized.get("enabled", True)) else 0
    return normalized


def _normalize_launch_config_values(
    *,
    agent_connection_id: str | None,
    agent_connection_name_snapshot: str | None,
    version_mode: str,
    manual_experiment_version: str | None,
    experiment_parameters_json: str | dict[str, Any] | None,
    record_name: str | None,
    context_schema_version: int,
) -> dict[str, Any]:
    mode, manual = normalize_version_request(version_mode, manual_experiment_version)
    parameters_json = canonical_parameters_json(experiment_parameters_json)
    connection_id = agent_connection_id.strip() if agent_connection_id else None
    if agent_connection_id is not None and not connection_id:
        raise ValueError("agent_connection_id must not be empty")
    connection_name = (
        agent_connection_name_snapshot.strip() if agent_connection_name_snapshot else None
    )
    normalized_record_name = record_name.strip() if record_name else None
    if not isinstance(context_schema_version, int) or isinstance(context_schema_version, bool):
        raise ValueError("context_schema_version must be an integer")
    digest = canonical_launch_config_hash(
        agent_connection_id=connection_id,
        version_mode=mode,
        requested_version=manual,
        experiment_parameters_json=parameters_json,
        record_name=normalized_record_name,
        context_schema_version=context_schema_version,
    )
    return {
        "agent_connection_id": connection_id,
        "agent_connection_name_snapshot": connection_name,
        "version_mode": mode,
        "manual_experiment_version": manual,
        "experiment_parameters_json": parameters_json,
        "record_name": normalized_record_name,
        "context_schema_version": context_schema_version,
        "launch_config_hash": digest,
    }


class ProfileRepository:
    """Store the one configured AgentLoop profile without credential material."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        *,
        display_name: str,
        region_id: str,
        agent_space: str,
        secret_ref: str,
        profile_id: str | None = None,
        api_endpoint: str | None = None,
        console_base_url: str | None = None,
        access_key_id_hint: str | None = None,
        default_timezone: str = "UTC",
        max_concurrency: int = 1,
        validation_status: str = ValidationStatus.UNVALIDATED.value,
    ) -> AgentLoopProfile:
        profile_id = profile_id or str(uuid4())
        if (
            not isinstance(max_concurrency, int)
            or isinstance(max_concurrency, bool)
            or not 1 <= max_concurrency <= 32
        ):
            raise ValueError("max_concurrency must be between 1 and 32")
        with self.database.session() as session:
            configured = session.scalar(select(func.count()).select_from(AgentLoopProfile))
            if configured:
                raise ProfileAlreadyConfiguredError("MVP permits one AgentLoop profile")
            profile = AgentLoopProfile(
                id=profile_id,
                display_name=_non_empty(display_name, "display_name"),
                region_id=_non_empty(region_id, "region_id"),
                agent_space=_non_empty(agent_space, "agent_space"),
                api_endpoint=api_endpoint,
                console_base_url=console_base_url,
                access_key_id_hint=access_key_id_hint,
                secret_ref=_non_empty(secret_ref, "secret_ref"),
                default_timezone=_validate_timezone(default_timezone),
                max_concurrency=max_concurrency,
                validation_status=_enum_value(validation_status, ValidationStatus),
            )
            session.add(profile)
            session.flush()
            return profile

    def get(self, profile_id: str) -> AgentLoopProfile | None:
        with self.database.session() as session:
            return session.get(AgentLoopProfile, profile_id)

    def get_configured(self) -> AgentLoopProfile | None:
        with self.database.session() as session:
            return session.scalar(
                select(AgentLoopProfile).order_by(AgentLoopProfile.created_at).limit(1)
            )

    def update(self, profile_id: str, **changes: Any) -> AgentLoopProfile:
        allowed = {
            "display_name",
            "region_id",
            "agent_space",
            "api_endpoint",
            "console_base_url",
            "access_key_id_hint",
            "secret_ref",
            "default_timezone",
            "max_concurrency",
            "validation_status",
            "validated_at",
            "last_error_code",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported profile fields: {sorted(unknown)}")
        # Deliberately no access_key_id/access_key_secret fields are accepted.
        with self.database.session() as session:
            profile = session.get(AgentLoopProfile, profile_id)
            if profile is None:
                raise NotFoundError(f"profile not found: {profile_id}")
            for field, value in changes.items():
                if field in {"display_name", "region_id", "agent_space", "secret_ref"}:
                    value = _non_empty(value, field)
                elif field == "validation_status":
                    value = _enum_value(value, ValidationStatus)
                elif field == "default_timezone":
                    value = _validate_timezone(value)
                elif field == "max_concurrency":
                    if (
                        not isinstance(value, int)
                        or isinstance(value, bool)
                        or not 1 <= value <= 32
                    ):
                        raise ValueError("max_concurrency must be between 1 and 32")
                elif field == "validated_at" and value is not None:
                    value = as_utc(value)
                setattr(profile, field, value)
            profile.updated_at = utc_now()
            session.flush()
            return profile


class AgentConnectionRepository:
    """Persist Agent registrations while keeping bearer tokens out of SQLite."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        *,
        name: str,
        token_secret_ref: str,
        token_hint: str,
        description: str | None = None,
        connection_id: str | None = None,
    ) -> AgentConnection:
        connection = AgentConnection(
            id=connection_id or str(uuid4()),
            name=_non_empty(name, "name"),
            description=description.strip() if description else None,
            token_secret_ref=_non_empty(token_secret_ref, "token_secret_ref"),
            token_hint=_non_empty(token_hint, "token_hint"),
        )
        with self.database.session() as session:
            session.add(connection)
            session.flush()
            return connection


    def get(self, connection_id: str, *, include_deleted: bool = False) -> AgentConnection | None:
        with self.database.session() as session:
            connection = session.get(AgentConnection, connection_id)
            if connection is not None and connection.deleted_at is not None and not include_deleted:
                return None
            return connection

    def list(self, *, include_deleted: bool = False) -> list[AgentConnection]:
        statement: Select[tuple[AgentConnection]] = select(AgentConnection)
        if not include_deleted:
            statement = statement.where(AgentConnection.deleted_at.is_(None))
        statement = statement.order_by(AgentConnection.created_at, AgentConnection.id)
        with self.database.session() as session:
            return list(session.scalars(statement))

    def rotate_token(
        self, connection_id: str, *, token_secret_ref: str, token_hint: str
    ) -> AgentConnection:
        with self.database.session() as session:
            connection = session.get(AgentConnection, connection_id)
            if connection is None or connection.deleted_at is not None:
                raise NotFoundError(f"agent connection not found: {connection_id}")
            connection.token_secret_ref = _non_empty(token_secret_ref, "token_secret_ref")
            connection.token_hint = _non_empty(token_hint, "token_hint")
            connection.updated_at = utc_now()
            session.flush()
            return connection

    def record_connected(self, connection_id: str, at: datetime | None = None) -> AgentConnection:
        changed_at = as_utc(at) if at else utc_now()
        with self.database.session() as session:
            connection = session.get(AgentConnection, connection_id)
            if connection is None or connection.deleted_at is not None:
                raise NotFoundError(f"agent connection not found: {connection_id}")
            connection.last_connected_at = changed_at
            connection.last_seen_at = changed_at
            connection.updated_at = changed_at
            session.flush()
            return connection

    def record_seen(self, connection_id: str, at: datetime | None = None) -> AgentConnection:
        changed_at = as_utc(at) if at else utc_now()
        with self.database.session() as session:
            connection = session.get(AgentConnection, connection_id)
            if connection is None or connection.deleted_at is not None:
                raise NotFoundError(f"agent connection not found: {connection_id}")
            connection.last_seen_at = changed_at
            connection.updated_at = changed_at
            session.flush()
            return connection

    def record_disconnected(
        self, connection_id: str, at: datetime | None = None
    ) -> AgentConnection:
        changed_at = as_utc(at) if at else utc_now()
        with self.database.session() as session:
            connection = session.get(AgentConnection, connection_id)
            if connection is None:
                raise NotFoundError(f"agent connection not found: {connection_id}")
            connection.last_disconnected_at = changed_at
            connection.updated_at = changed_at
            session.flush()
            return connection

    def soft_delete(self, connection_id: str) -> AgentConnection:
        now = utc_now()
        with self.database.session() as session:
            connection = session.get(AgentConnection, connection_id)
            if connection is None:
                raise NotFoundError(f"agent connection not found: {connection_id}")
            if connection.deleted_at is None:
                connection.deleted_at = now
                connection.updated_at = now
                session.flush()
            return connection


class PlanDashboardRepository:
    """Persist only reconciliation metadata for one dashboard per Plan."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def get(self, profile_id: str, plan_id: str) -> PlanDashboard | None:
        with self.database.session() as session:
            return session.get(PlanDashboard, (profile_id, plan_id))

    def begin_sync(
        self,
        *,
        profile_id: str,
        plan_id: str,
        plan_name_snapshot: str | None,
        agent_space_snapshot: str,
        dashboard_name: str,
        display_name: str,
        template_version: str,
        template_sha256: str,
        attempted_at: datetime,
    ) -> PlanDashboard:
        attempted_at = as_utc(attempted_at)
        with self.database.session() as session:
            dashboard = session.get(PlanDashboard, (profile_id, plan_id))
            if dashboard is None:
                dashboard = PlanDashboard(
                    profile_id=_non_empty(profile_id, "profile_id"),
                    plan_id=_non_empty(plan_id, "plan_id"),
                    plan_name_snapshot=plan_name_snapshot,
                    agent_space_snapshot=_non_empty(
                        agent_space_snapshot, "agent_space_snapshot"
                    ),
                    dashboard_name=_non_empty(dashboard_name, "dashboard_name"),
                    display_name=_non_empty(display_name, "display_name"),
                    template_version=_non_empty(template_version, "template_version"),
                    template_sha256=_non_empty(template_sha256, "template_sha256"),
                    sync_state=PlanDashboardSyncState.PENDING.value,
                    last_attempt_at=attempted_at,
                )
                session.add(dashboard)
            else:
                dashboard.plan_name_snapshot = plan_name_snapshot
                dashboard.agent_space_snapshot = _non_empty(
                    agent_space_snapshot, "agent_space_snapshot"
                )
                dashboard.dashboard_name = _non_empty(dashboard_name, "dashboard_name")
                dashboard.display_name = _non_empty(display_name, "display_name")
                dashboard.template_version = _non_empty(
                    template_version, "template_version"
                )
                dashboard.template_sha256 = _non_empty(
                    template_sha256, "template_sha256"
                )
                dashboard.sync_state = PlanDashboardSyncState.PENDING.value
                dashboard.last_error_code = None
                dashboard.last_attempt_at = attempted_at
                dashboard.updated_at = attempted_at
            session.flush()
            return dashboard

    def mark_ready(
        self,
        *,
        profile_id: str,
        plan_id: str,
        sls_project: str,
        dashboard_name: str,
        display_name: str,
        template_version: str,
        template_sha256: str,
        synced_at: datetime,
    ) -> PlanDashboard:
        synced_at = as_utc(synced_at)
        with self.database.session() as session:
            dashboard = session.get(PlanDashboard, (profile_id, plan_id))
            if dashboard is None:
                raise NotFoundError(f"plan dashboard not found: {profile_id}/{plan_id}")
            dashboard.sls_project = _non_empty(sls_project, "sls_project")
            dashboard.dashboard_name = _non_empty(dashboard_name, "dashboard_name")
            dashboard.display_name = _non_empty(display_name, "display_name")
            dashboard.template_version = _non_empty(template_version, "template_version")
            dashboard.template_sha256 = _non_empty(template_sha256, "template_sha256")
            dashboard.sync_state = PlanDashboardSyncState.READY.value
            dashboard.last_error_code = None
            dashboard.last_synced_at = synced_at
            dashboard.updated_at = synced_at
            session.flush()
            return dashboard

    def mark_failed(
        self,
        *,
        profile_id: str,
        plan_id: str,
        error_code: str,
        failed_at: datetime,
    ) -> PlanDashboard:
        failed_at = as_utc(failed_at)
        with self.database.session() as session:
            dashboard = session.get(PlanDashboard, (profile_id, plan_id))
            if dashboard is None:
                raise NotFoundError(f"plan dashboard not found: {profile_id}/{plan_id}")
            dashboard.sync_state = PlanDashboardSyncState.FAILED.value
            dashboard.last_error_code = _non_empty(error_code, "error_code")
            dashboard.updated_at = failed_at
            session.flush()
            return dashboard


class ScheduleRepository:
    """Persist schedules with optimistic revision control and soft deletion."""

    _CONFIG_FIELDS = {
        "name",
        "plan_id",
        "agent_connection_id",
        "agent_connection_name_snapshot",
        "version_mode",
        "manual_experiment_version",
        "experiment_parameters_json",
        "record_name",
        "context_schema_version",
        "enabled",
        "cadence_type",
        "interval_hours",
        "interval_minutes",
        "daily_time",
        "timezone",
        "anchor_at",
        "misfire_policy",
        "misfire_grace_seconds",
        "overlap_policy",
    }

    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        *,
        profile_id: str,
        name: str,
        plan_id: str,
        cadence_type: str,
        timezone: str,
        schedule_id: str | None = None,
        plan_name_snapshot: str | None = None,
        agent_connection_id: str | None = None,
        agent_connection_name_snapshot: str | None = None,
        version_mode: str = LaunchVersionMode.AUTO.value,
        manual_experiment_version: str | None = None,
        experiment_parameters_json: str | dict[str, Any] | None = None,
        record_name: str | None = None,
        context_schema_version: int = 1,
        enabled: bool = True,
        interval_hours: int | None = None,
        interval_minutes: int | None = None,
        daily_time: str | None = None,
        anchor_at: datetime | None = None,
        misfire_policy: str = MisfirePolicy.FIRE_ONCE.value,
        misfire_grace_seconds: int = 3600,
        overlap_policy: str = OverlapPolicy.FORBID_LOCAL_OVERLAP.value,
        next_fire_at_utc: datetime | None = None,
    ) -> Schedule:
        values = _validate_schedule_values(
            {
                "cadence_type": cadence_type,
                "timezone": timezone,
                "enabled": enabled,
                "interval_hours": interval_hours,
                "interval_minutes": interval_minutes,
                "daily_time": daily_time,
                "anchor_at": anchor_at,
                "misfire_policy": misfire_policy,
                "misfire_grace_seconds": misfire_grace_seconds,
                "overlap_policy": overlap_policy,
            }
        )
        launch_values = _normalize_launch_config_values(
            agent_connection_id=agent_connection_id,
            agent_connection_name_snapshot=agent_connection_name_snapshot,
            version_mode=version_mode,
            manual_experiment_version=manual_experiment_version,
            experiment_parameters_json=experiment_parameters_json,
            record_name=record_name,
            context_schema_version=context_schema_version,
        )
        schedule = Schedule(
            id=schedule_id or str(uuid4()),
            profile_id=profile_id,
            name=_non_empty(name, "name"),
            plan_id=_non_empty(plan_id, "plan_id"),
            plan_name_snapshot=plan_name_snapshot,
            next_fire_at_utc=as_utc(next_fire_at_utc) if next_fire_at_utc else None,
            **values,
            **launch_values,
        )
        with self.database.session() as session:
            session.add(schedule)
            session.flush()
            return schedule

    def get(self, schedule_id: str, *, include_deleted: bool = False) -> Schedule | None:
        with self.database.session() as session:
            schedule = session.get(Schedule, schedule_id)
            if schedule is not None and schedule.deleted_at is not None and not include_deleted:
                return None
            return schedule

    def list(self, *, include_deleted: bool = False, enabled_only: bool = False) -> list[Schedule]:
        statement: Select[tuple[Schedule]] = select(Schedule)
        if not include_deleted:
            statement = statement.where(Schedule.deleted_at.is_(None))
        if enabled_only:
            statement = statement.where(Schedule.enabled == 1)
        statement = statement.order_by(Schedule.created_at, Schedule.id)
        with self.database.session() as session:
            return list(session.scalars(statement))

    def due(self, now: datetime) -> list[Schedule]:
        now = as_utc(now)
        statement = (
            select(Schedule)
            .where(
                Schedule.enabled == 1,
                Schedule.deleted_at.is_(None),
                Schedule.next_fire_at_utc.is_not(None),
                Schedule.next_fire_at_utc <= now,
            )
            .order_by(Schedule.next_fire_at_utc, Schedule.id)
        )
        with self.database.session() as session:
            return list(session.scalars(statement))

    def update_config(
        self, schedule_id: str, *, expected_revision: int, **changes: Any
    ) -> Schedule:
        unknown = set(changes) - self._CONFIG_FIELDS
        if unknown:
            raise ValueError(f"unsupported schedule fields: {sorted(unknown)}")

        with self.database.session() as session:
            current = session.get(Schedule, schedule_id)
            if current is None or current.deleted_at is not None:
                raise NotFoundError(f"schedule not found: {schedule_id}")
            if current.revision != expected_revision:
                raise ConcurrentUpdateError(
                    f"expected revision {expected_revision}, found {current.revision}"
                )

            merged = {
                field: getattr(current, field)
                for field in self._CONFIG_FIELDS
                if field
                not in {
                    "name",
                    "plan_id",
                    "agent_connection_id",
                    "agent_connection_name_snapshot",
                    "version_mode",
                    "manual_experiment_version",
                    "experiment_parameters_json",
                    "record_name",
                    "context_schema_version",
                }
            }
            merged.update(changes)
            normalized = _validate_schedule_values(merged)
            normalized.update(
                _normalize_launch_config_values(
                    agent_connection_id=changes.get(
                        "agent_connection_id", current.agent_connection_id
                    ),
                    agent_connection_name_snapshot=changes.get(
                        "agent_connection_name_snapshot",
                        current.agent_connection_name_snapshot,
                    ),
                    version_mode=changes.get("version_mode", current.version_mode),
                    manual_experiment_version=changes.get(
                        "manual_experiment_version", current.manual_experiment_version
                    ),
                    experiment_parameters_json=changes.get(
                        "experiment_parameters_json", current.experiment_parameters_json
                    ),
                    record_name=changes.get("record_name", current.record_name),
                    context_schema_version=changes.get(
                        "context_schema_version", current.context_schema_version
                    ),
                )
            )
            candidate: dict[str, Any] = {}
            for field in self._CONFIG_FIELDS:
                if field in {"name", "plan_id"}:
                    value = changes.get(field, getattr(current, field))
                    value = _non_empty(value, field)
                else:
                    value = normalized[field]
                if getattr(current, field) != value:
                    candidate[field] = value

            if not candidate:
                return current

            if any(
                field
                in {
                    "agent_connection_id",
                    "agent_connection_name_snapshot",
                    "version_mode",
                    "manual_experiment_version",
                    "experiment_parameters_json",
                    "record_name",
                    "context_schema_version",
                }
                for field in candidate
            ):
                candidate["launch_config_hash"] = normalized["launch_config_hash"]

            candidate["revision"] = expected_revision + 1
            candidate["updated_at"] = utc_now()
            result = session.execute(
                update(Schedule)
                .where(
                    Schedule.id == schedule_id,
                    Schedule.revision == expected_revision,
                    Schedule.deleted_at.is_(None),
                )
                .values(**candidate)
            )
            if result.rowcount != 1:
                raise ConcurrentUpdateError("schedule changed concurrently")
            session.expire(current)
            session.refresh(current)
            return current

    def set_enabled(self, schedule_id: str, *, enabled: bool, expected_revision: int) -> Schedule:
        return self.update_config(schedule_id, expected_revision=expected_revision, enabled=enabled)

    def soft_delete(self, schedule_id: str, *, expected_revision: int) -> Schedule:
        now = utc_now()
        with self.database.session() as session:
            current = session.get(Schedule, schedule_id)
            if current is None:
                raise NotFoundError(f"schedule not found: {schedule_id}")
            if current.deleted_at is not None:
                return current
            result = session.execute(
                update(Schedule)
                .where(
                    Schedule.id == schedule_id,
                    Schedule.revision == expected_revision,
                    Schedule.deleted_at.is_(None),
                )
                .values(
                    enabled=0,
                    deleted_at=now,
                    revision=expected_revision + 1,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                raise ConcurrentUpdateError("schedule changed concurrently")
            session.expire(current)
            session.refresh(current)
            return current

    def record_fire(
        self,
        schedule_id: str,
        *,
        fired_at_utc: datetime,
        next_fire_at_utc: datetime | None,
        expected_revision: int | None = None,
    ) -> Schedule:
        with self.database.session() as session:
            schedule = session.get(Schedule, schedule_id)
            if schedule is None or schedule.deleted_at is not None:
                raise NotFoundError(f"schedule not found: {schedule_id}")
            if expected_revision is not None and schedule.revision != expected_revision:
                raise ConcurrentUpdateError(
                    f"expected revision {expected_revision}, found {schedule.revision}"
                )
            schedule.last_fire_at_utc = as_utc(fired_at_utc)
            schedule.next_fire_at_utc = (
                as_utc(next_fire_at_utc) if next_fire_at_utc is not None else None
            )
            schedule.last_error_code = None
            schedule.last_error_message = None
            schedule.updated_at = utc_now()
            session.flush()
            return schedule

    def set_next_fire(
        self,
        schedule_id: str,
        next_fire_at_utc: datetime | None,
        *,
        expected_revision: int | None = None,
    ) -> Schedule:
        """Update the derived next slot without claiming that a fire occurred.

        Configuration changes and re-enabling a schedule use this path after
        recalculation.  Supplying ``expected_revision`` prevents a stale
        calculation from overwriting the result of a newer edit.
        """

        with self.database.session() as session:
            schedule = session.get(Schedule, schedule_id)
            if schedule is None or schedule.deleted_at is not None:
                raise NotFoundError(f"schedule not found: {schedule_id}")
            if expected_revision is not None and schedule.revision != expected_revision:
                raise ConcurrentUpdateError(
                    f"expected revision {expected_revision}, found {schedule.revision}"
                )
            schedule.next_fire_at_utc = (
                as_utc(next_fire_at_utc) if next_fire_at_utc is not None else None
            )
            schedule.updated_at = utc_now()
            session.flush()
            return schedule

    def record_error(self, schedule_id: str, *, code: str, message: str) -> Schedule:
        with self.database.session() as session:
            schedule = session.get(Schedule, schedule_id)
            if schedule is None:
                raise NotFoundError(f"schedule not found: {schedule_id}")
            schedule.last_error_code = code
            schedule.last_error_message = message
            schedule.updated_at = utc_now()
            session.flush()
            return schedule


_SUBMISSION_TRANSITIONS: dict[str, set[str]] = {
    SubmissionState.CREATED.value: {
        SubmissionState.SUBMITTING.value,
        SubmissionState.DEFINITIVE_FAILURE.value,
    },
    SubmissionState.SUBMITTING.value: {
        SubmissionState.ACCEPTED.value,
        SubmissionState.DEFINITIVE_FAILURE.value,
        SubmissionState.SUBMISSION_UNKNOWN.value,
    },
    SubmissionState.SUBMISSION_UNKNOWN.value: {
        SubmissionState.ACCEPTED.value,
        SubmissionState.DEFINITIVE_FAILURE.value,
    },
}

_RUNNER_TRANSITIONS: dict[str, set[str]] = {
    RunnerState.NOT_STARTED.value: {RunnerState.RUNNING.value},
    RunnerState.RUNNING.value: {
        RunnerState.HANDED_OFF.value,
        RunnerState.FAILED.value,
        RunnerState.UNKNOWN.value,
    },
    RunnerState.UNKNOWN.value: set(),
}


class LaunchIntentRepository:
    """Maintain the local launch ledger and its two independent state machines."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _same_intent(existing: LaunchIntent, expected: dict[str, Any]) -> bool:
        fields = (
            "profile_id",
            "plan_id",
            "trigger_source",
            "schedule_id",
            "schedule_revision",
            "scheduled_for_utc",
            "launch_config_hash",
        )
        return all(getattr(existing, field) == expected.get(field) for field in fields)

    @staticmethod
    def _find_existing(session: Session, values: dict[str, Any]) -> LaunchIntent | None:
        by_key = session.scalar(
            select(LaunchIntent).where(LaunchIntent.idempotency_key == values["idempotency_key"])
        )
        if by_key is not None:
            return by_key
        if values.get("schedule_id") is not None and values.get("scheduled_for_utc") is not None:
            return session.scalar(
                select(LaunchIntent).where(
                    LaunchIntent.schedule_id == values["schedule_id"],
                    LaunchIntent.schedule_revision == values["schedule_revision"],
                    LaunchIntent.scheduled_for_utc == values["scheduled_for_utc"],
                )
            )
        return None

    def create_or_get(
        self,
        *,
        profile_id: str,
        plan_id: str,
        trigger_source: str,
        idempotency_key: str,
        launch_id: str | None = None,
        plan_name_snapshot: str | None = None,
        plan_updated_at_snapshot: datetime | None = None,
        agent_connection_id: str | None = None,
        agent_connection_name_snapshot: str | None = None,
        version_mode: str = LaunchVersionMode.AUTO.value,
        manual_experiment_version: str | None = None,
        experiment_version: str | None = None,
        experiment_parameters_json: str | dict[str, Any] | None = None,
        record_name: str | None = None,
        context_schema_version: int = 1,
        context_xml_template: str | None = None,
        launch_config_hash: str | None = None,
        schedule_id: str | None = None,
        schedule_revision: int | None = None,
        scheduled_for_utc: datetime | None = None,
        submission_state: str = SubmissionState.CREATED.value,
        runner_state: str = RunnerState.NOT_STARTED.value,
        skip_reason: str | None = None,
        requested_at: datetime | None = None,
    ) -> tuple[LaunchIntent, bool]:
        source = _enum_value(trigger_source, TriggerSource)
        submission = _enum_value(submission_state, SubmissionState)
        runner = _enum_value(runner_state, RunnerState)
        if source == TriggerSource.SCHEDULE.value:
            if schedule_id is None or schedule_revision is None or scheduled_for_utc is None:
                raise ValueError("scheduled launches require schedule id, revision, and UTC slot")
        if submission == SubmissionState.SKIPPED.value and not skip_reason:
            raise ValueError("SKIPPED launch intents require skip_reason")
        if submission != SubmissionState.SKIPPED.value and skip_reason is not None:
            raise ValueError("skip_reason is only valid for a SKIPPED launch intent")

        launch_values = _normalize_launch_config_values(
            agent_connection_id=agent_connection_id,
            agent_connection_name_snapshot=agent_connection_name_snapshot,
            version_mode=version_mode,
            manual_experiment_version=manual_experiment_version,
            experiment_parameters_json=experiment_parameters_json,
            record_name=record_name,
            context_schema_version=context_schema_version,
        )
        if (
            launch_config_hash is not None
            and launch_config_hash != launch_values["launch_config_hash"]
        ):
            raise ValueError("launch_config_hash does not match the canonical launch configuration")
        final_version = experiment_version.strip() if experiment_version else None
        if launch_values["version_mode"] == LaunchVersionMode.MANUAL.value:
            if final_version is None:
                final_version = launch_values["manual_experiment_version"]
            if final_version != launch_values["manual_experiment_version"]:
                raise ValueError("final experiment_version must equal the requested MANUAL version")
        elif final_version is not None:
            # Reuse MANUAL syntax validation for generated/custom AUTO labels.
            normalize_version_request(LaunchVersionMode.MANUAL.value, final_version)

        values: dict[str, Any] = {
            "id": launch_id or str(uuid4()),
            "profile_id": _non_empty(profile_id, "profile_id"),
            "plan_id": _non_empty(plan_id, "plan_id"),
            "plan_name_snapshot": plan_name_snapshot,
            "plan_updated_at_snapshot": (
                as_utc(plan_updated_at_snapshot) if plan_updated_at_snapshot else None
            ),
            **launch_values,
            "experiment_version": final_version,
            "context_xml_template": context_xml_template,
            "trigger_source": source,
            "schedule_id": schedule_id,
            "schedule_revision": schedule_revision,
            "scheduled_for_utc": as_utc(scheduled_for_utc) if scheduled_for_utc else None,
            "idempotency_key": _non_empty(idempotency_key, "idempotency_key"),
            "submission_state": submission,
            "runner_state": runner,
            "skip_reason": skip_reason,
            "requested_at": as_utc(requested_at) if requested_at else utc_now(),
        }

        try:
            with self.database.session() as session:
                existing = self._find_existing(session, values)
                if existing is not None:
                    if not self._same_intent(existing, values):
                        raise IdempotencyConflictError(
                            "idempotency key or schedule slot belongs to another launch payload"
                        )
                    return existing, False
                intent = LaunchIntent(**values)
                session.add(intent)
                session.flush()
                return intent, True
        except IntegrityError:
            # A concurrent writer won the unique key/slot race.  Read back the
            # winner and apply the same semantic-payload check.
            with self.database.session() as session:
                existing = self._find_existing(session, values)
                if existing is None or not self._same_intent(existing, values):
                    raise IdempotencyConflictError(
                        "idempotency key or schedule slot conflicts with an existing launch"
                    )
                return existing, False

    def get(self, launch_id: str) -> LaunchIntent | None:
        with self.database.session() as session:
            return session.get(LaunchIntent, launch_id)

    def get_by_idempotency_key(self, key: str) -> LaunchIntent | None:
        with self.database.session() as session:
            return session.scalar(select(LaunchIntent).where(LaunchIntent.idempotency_key == key))

    def get_by_record_id(self, record_id: str) -> LaunchIntent | None:
        with self.database.session() as session:
            return session.scalar(select(LaunchIntent).where(LaunchIntent.record_id == record_id))

    def list(
        self,
        *,
        plan_id: str | None = None,
        schedule_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[LaunchIntent]:
        if limit < 1 or limit > 1000 or offset < 0:
            raise ValueError("limit must be 1..1000 and offset must be non-negative")
        statement = select(LaunchIntent)
        if plan_id is not None:
            statement = statement.where(LaunchIntent.plan_id == plan_id)
        if schedule_id is not None:
            statement = statement.where(LaunchIntent.schedule_id == schedule_id)
        statement = (
            statement.order_by(LaunchIntent.requested_at.desc(), LaunchIntent.id)
            .limit(limit)
            .offset(offset)
        )
        with self.database.session() as session:
            return list(session.scalars(statement))

    def count(
        self,
        *,
        plan_id: str | None = None,
        schedule_id: str | None = None,
        with_record_only: bool = False,
    ) -> int:
        statement = select(func.count()).select_from(LaunchIntent)
        if plan_id is not None:
            statement = statement.where(LaunchIntent.plan_id == plan_id)
        if schedule_id is not None:
            statement = statement.where(LaunchIntent.schedule_id == schedule_id)
        if with_record_only:
            statement = statement.where(LaunchIntent.record_id.is_not(None))
        with self.database.session() as session:
            return int(session.scalar(statement) or 0)

    def transition_submission(
        self,
        launch_id: str,
        new_state: str,
        *,
        at: datetime | None = None,
        record_id: str | None = None,
        request_id: str | None = None,
        error_code: str | None = None,
        sanitized_error: str | None = None,
    ) -> LaunchIntent:
        target = _enum_value(new_state, SubmissionState)
        changed_at = as_utc(at) if at else utc_now()
        with self.database.session() as session:
            intent = session.get(LaunchIntent, launch_id)
            if intent is None:
                raise NotFoundError(f"launch intent not found: {launch_id}")
            if target == intent.submission_state:
                if target == SubmissionState.ACCEPTED.value and record_id != intent.record_id:
                    raise IdempotencyConflictError("accepted record_id cannot be changed")
                return intent
            if target not in _SUBMISSION_TRANSITIONS.get(intent.submission_state, set()):
                raise InvalidTransitionError(
                    f"invalid submission transition {intent.submission_state} -> {target}"
                )
            if target == SubmissionState.ACCEPTED.value and not record_id:
                raise ValueError("ACCEPTED requires the authoritative AgentLoop record_id")

            intent.submission_state = target
            intent.request_id = request_id or intent.request_id
            intent.error_code = error_code
            intent.sanitized_error = sanitized_error
            if target == SubmissionState.SUBMITTING.value:
                intent.submit_started_at = changed_at
                intent.attempt_count += 1
            elif target == SubmissionState.ACCEPTED.value:
                intent.record_id = record_id
                intent.accepted_at = changed_at
            intent.updated_at = changed_at
            session.flush()
            return intent

    def transition_runner(
        self,
        launch_id: str,
        new_state: str,
        *,
        at: datetime | None = None,
        error_code: str | None = None,
        sanitized_error: str | None = None,
    ) -> LaunchIntent:
        target = _enum_value(new_state, RunnerState)
        changed_at = as_utc(at) if at else utc_now()
        with self.database.session() as session:
            intent = session.get(LaunchIntent, launch_id)
            if intent is None:
                raise NotFoundError(f"launch intent not found: {launch_id}")
            if target == intent.runner_state:
                return intent
            if target not in _RUNNER_TRANSITIONS.get(intent.runner_state, set()):
                raise InvalidTransitionError(
                    f"invalid runner transition {intent.runner_state} -> {target}"
                )
            intent.runner_state = target
            if target in {
                RunnerState.HANDED_OFF.value,
                RunnerState.FAILED.value,
                RunnerState.UNKNOWN.value,
            }:
                intent.worker_finished_at = changed_at
            elif target == RunnerState.RUNNING.value:
                intent.worker_finished_at = None
            if target in {RunnerState.FAILED.value, RunnerState.UNKNOWN.value}:
                intent.error_code = error_code or intent.error_code
                intent.sanitized_error = sanitized_error or intent.sanitized_error
            elif error_code is not None or sanitized_error is not None:
                raise ValueError("runner errors are only valid for FAILED or UNKNOWN")
            intent.updated_at = changed_at
            session.flush()
            return intent

    def has_local_overlap(self, schedule_id: str, *, exclude_launch_id: str | None = None) -> bool:
        statement = (
            select(func.count())
            .select_from(LaunchIntent)
            .where(
                LaunchIntent.schedule_id == schedule_id,
                LaunchIntent.runner_state == RunnerState.RUNNING.value,
            )
        )
        if exclude_launch_id is not None:
            statement = statement.where(LaunchIntent.id != exclude_launch_id)
        with self.database.session() as session:
            count = session.scalar(statement)
            return bool(count)

    def active_runner_count(self, *, exclude_launch_id: str | None = None) -> int:
        statement = (
            select(func.count())
            .select_from(LaunchIntent)
            .where(LaunchIntent.runner_state == RunnerState.RUNNING.value)
        )
        if exclude_launch_id is not None:
            statement = statement.where(LaunchIntent.id != exclude_launch_id)
        with self.database.session() as session:
            return int(session.scalar(statement) or 0)

    def record_ids(self, *, plan_id: str | None = None) -> list[str]:
        statement = select(LaunchIntent.record_id).where(LaunchIntent.record_id.is_not(None))
        if plan_id is not None:
            statement = statement.where(LaunchIntent.plan_id == plan_id)
        statement = statement.order_by(LaunchIntent.accepted_at.desc(), LaunchIntent.id)
        with self.database.session() as session:
            return [record_id for record_id in session.scalars(statement) if record_id]

    def record_ids_page(
        self, *, plan_id: str | None = None, offset: int = 0, limit: int = 50
    ) -> list[str]:
        if offset < 0 or limit < 1 or limit > 1000:
            raise ValueError("offset must be non-negative and limit must be 1..1000")
        statement = select(LaunchIntent.record_id).where(LaunchIntent.record_id.is_not(None))
        if plan_id is not None:
            statement = statement.where(LaunchIntent.plan_id == plan_id)
        statement = (
            statement.order_by(LaunchIntent.accepted_at.desc(), LaunchIntent.id)
            .offset(offset)
            .limit(limit)
        )
        with self.database.session() as session:
            return [record_id for record_id in session.scalars(statement) if record_id]

    def release_expired_leases(self, now: datetime) -> int:
        """Mark an expired process-owned worker as historical UNKNOWN."""

        normalized = as_utc(now)
        with self.database.session() as session:
            result = session.execute(
                update(LaunchIntent)
                .where(
                    LaunchIntent.runner_state == RunnerState.RUNNING.value,
                    LaunchIntent.lease_expires_at.is_not(None),
                    LaunchIntent.lease_expires_at <= normalized,
                )
                .values(
                    runner_state=RunnerState.UNKNOWN.value,
                    worker_finished_at=normalized,
                    lease_owner=None,
                    lease_expires_at=None,
                    updated_at=normalized,
                )
            )
            return int(result.rowcount or 0)

    def recover_interrupted_runners(self, now: datetime) -> int:
        """Mark process-owned RUNNING work unknown before accepting new launches.

        A restarted process cannot claim the prior worker is still alive. The
        historical UNKNOWN state does not consume new-launch capacity or overlap.
        """

        normalized = as_utc(now)
        with self.database.session() as session:
            result = session.execute(
                update(LaunchIntent)
                .where(LaunchIntent.runner_state == RunnerState.RUNNING.value)
                .values(
                    runner_state=RunnerState.UNKNOWN.value,
                    worker_finished_at=normalized,
                    lease_owner=None,
                    lease_expires_at=None,
                    updated_at=normalized,
                )
            )
            return int(result.rowcount or 0)

    def set_lease(
        self, launch_id: str, *, owner: str, ttl: timedelta, now: datetime | None = None
    ) -> LaunchIntent:
        if ttl <= timedelta(0):
            raise ValueError("lease ttl must be positive")
        current = as_utc(now) if now else utc_now()
        with self.database.session() as session:
            intent = session.get(LaunchIntent, launch_id)
            if intent is None:
                raise NotFoundError(f"launch intent not found: {launch_id}")
            intent.lease_owner = _non_empty(owner, "owner")
            intent.lease_expires_at = current + ttl
            intent.updated_at = current
            session.flush()
            return intent


@dataclass(frozen=True, slots=True)
class TraceCorrelationSummary:
    """Aggregate Trace lookup progress exposed with a local launch."""

    state: str
    trace_ids: tuple[str, ...]
    total_tasks: int
    pending_tasks: int
    resolved_tasks: int
    reported_tasks: int
    failed_tasks: int
    total_attempts: int
    last_error_code: str | None
    last_error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None


_TRACE_CORRELATION_FIELDS = {
    "state",
    "trace_id",
    "source",
    "attempts",
    "error_code",
    "sanitized_error",
    "agent_started_at",
    "agent_completed_at",
    "first_polled_at",
    "deadline_at",
    "resolved_at",
    "reported_at",
}
_TRACE_TIMESTAMP_FIELDS = {
    "agent_started_at",
    "agent_completed_at",
    "first_polled_at",
    "deadline_at",
    "resolved_at",
    "reported_at",
}
_TRACE_FAILURE_STATES = {
    TraceCorrelationState.AMBIGUOUS.value,
    TraceCorrelationState.TIMED_OUT.value,
    TraceCorrelationState.FAILED.value,
}
_TRACE_ID = re.compile(r"^[0-9a-fA-F]{32}$")


def _normalize_trace_correlation_changes(changes: dict[str, Any]) -> dict[str, Any]:
    unknown = set(changes) - _TRACE_CORRELATION_FIELDS
    if unknown:
        raise ValueError(f"unsupported Trace correlation fields: {sorted(unknown)}")
    normalized = dict(changes)
    if "state" in normalized:
        normalized["state"] = _enum_value(normalized["state"], TraceCorrelationState)
    if "attempts" in normalized:
        attempts = normalized["attempts"]
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
            raise ValueError("attempts must be a non-negative integer")
    for field in _TRACE_TIMESTAMP_FIELDS:
        if field in normalized and normalized[field] is not None:
            normalized[field] = as_utc(normalized[field])
    if "trace_id" in normalized and normalized["trace_id"] is not None:
        trace_id = str(normalized["trace_id"]).strip()
        if not _TRACE_ID.fullmatch(trace_id):
            raise ValueError("trace_id must be exactly 32 hexadecimal characters")
        normalized["trace_id"] = trace_id.lower()
    for field, limit in (("source", 128), ("error_code", 128)):
        if field in normalized and normalized[field] is not None:
            value = str(normalized[field]).strip()
            if not value:
                raise ValueError(f"{field} must not be empty")
            if len(value) > limit:
                raise ValueError(f"{field} must not exceed {limit} characters")
            normalized[field] = value
    if "sanitized_error" in normalized and normalized["sanitized_error"] is not None:
        # Persist a compact diagnostic only. Callers must redact known exact
        # secrets before this boundary; this table has no credential columns.
        message = " ".join(str(normalized["sanitized_error"]).split())[:1000]
        normalized["sanitized_error"] = message or None
    return normalized


class TraceCorrelationRepository:
    """Persist payload-free Trace correlation progress for launched items."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def upsert(
        self,
        *,
        launch_id: str,
        dataset_item_id: str,
        experiment_run_id: str,
        **changes: Any,
    ) -> TraceCorrelation:
        """Create an item or apply an idempotent progress snapshot to it."""

        launch_id = _non_empty(launch_id, "launch_id")
        dataset_item_id = _non_empty(dataset_item_id, "dataset_item_id")
        experiment_run_id = _non_empty(experiment_run_id, "experiment_run_id")
        normalized = _normalize_trace_correlation_changes(changes)
        changed_at = utc_now()
        with self.database.session() as session:
            item = session.get(TraceCorrelation, (launch_id, dataset_item_id))
            if item is None:
                values = {
                    "launch_id": launch_id,
                    "dataset_item_id": dataset_item_id,
                    "experiment_run_id": experiment_run_id,
                    "state": TraceCorrelationState.PENDING.value,
                    **normalized,
                }
                item = TraceCorrelation(**values)
                self._apply_derived_timestamps(item, changed_at)
                self._validate_resolved_item(item)
                session.add(item)
            else:
                if item.experiment_run_id != experiment_run_id:
                    raise IdempotencyConflictError(
                        "Dataset item is already associated with another experiment run"
                    )
                for field, value in normalized.items():
                    setattr(item, field, value)
                item.updated_at = changed_at
                self._apply_derived_timestamps(item, changed_at)
                self._validate_resolved_item(item)
            session.flush()
            return item

    def update(
        self,
        launch_id: str,
        dataset_item_id: str,
        **changes: Any,
    ) -> TraceCorrelation:
        """Update one existing item; fields outside the audit contract are rejected."""

        if not changes:
            raise ValueError("at least one Trace correlation field must be provided")
        normalized = _normalize_trace_correlation_changes(changes)
        changed_at = utc_now()
        with self.database.session() as session:
            item = session.get(
                TraceCorrelation,
                (
                    _non_empty(launch_id, "launch_id"),
                    _non_empty(dataset_item_id, "dataset_item_id"),
                ),
            )
            if item is None:
                raise NotFoundError(f"Trace correlation not found: {launch_id}/{dataset_item_id}")
            for field, value in normalized.items():
                setattr(item, field, value)
            item.updated_at = changed_at
            self._apply_derived_timestamps(item, changed_at)
            self._validate_resolved_item(item)
            session.flush()
            return item

    def get(self, launch_id: str, dataset_item_id: str) -> TraceCorrelation | None:
        with self.database.session() as session:
            return session.get(TraceCorrelation, (launch_id, dataset_item_id))

    def list(self, launch_id: str) -> list[TraceCorrelation]:
        statement = (
            select(TraceCorrelation)
            .where(TraceCorrelation.launch_id == launch_id)
            .order_by(TraceCorrelation.created_at, TraceCorrelation.dataset_item_id)
        )
        with self.database.session() as session:
            return list(session.scalars(statement))

    def recover_interrupted(self, now: datetime) -> int:
        """Close non-terminal lookups left by a stopped launcher process.

        The launcher intentionally does not persist Agent input/output, so a
        process restart cannot safely recreate and upload a missing detail.
        Keeping these rows as POLLING/RESOLVED would imply work is still
        running; mark the local audit state failed instead.
        """

        changed_at = as_utc(now)
        with self.database.session() as session:
            result = session.execute(
                update(TraceCorrelation)
                .where(
                    TraceCorrelation.state.in_(
                        {
                            TraceCorrelationState.PENDING.value,
                            TraceCorrelationState.POLLING.value,
                            TraceCorrelationState.RESOLVED.value,
                        }
                    )
                )
                .values(
                    state=TraceCorrelationState.FAILED.value,
                    error_code="LOCAL_PROCESS_INTERRUPTED",
                    sanitized_error=(
                        "The launcher stopped before Trace correlation and detail reporting "
                        "completed"
                    ),
                    updated_at=changed_at,
                )
            )
            return int(result.rowcount or 0)

    def summary(self, launch_id: str) -> TraceCorrelationSummary:
        items = self.list(launch_id)
        if not items:
            return TraceCorrelationSummary(
                state="NOT_STARTED",
                trace_ids=(),
                total_tasks=0,
                pending_tasks=0,
                resolved_tasks=0,
                reported_tasks=0,
                failed_tasks=0,
                total_attempts=0,
                last_error_code=None,
                last_error_message=None,
                started_at=None,
                finished_at=None,
            )

        pending = sum(
            item.state in {TraceCorrelationState.PENDING.value, TraceCorrelationState.POLLING.value}
            for item in items
        )
        resolved = sum(item.state == TraceCorrelationState.RESOLVED.value for item in items)
        reported = sum(item.state == TraceCorrelationState.REPORTED.value for item in items)
        failed = sum(item.state in _TRACE_FAILURE_STATES for item in items)

        if reported == len(items):
            summary_state = TraceCorrelationState.REPORTED.value
        elif failed:
            failure_states = {item.state for item in items if item.state in _TRACE_FAILURE_STATES}
            summary_state = next(iter(failure_states)) if len(failure_states) == 1 else "FAILED"
        elif resolved + reported == len(items):
            summary_state = TraceCorrelationState.RESOLVED.value
        elif any(item.state == TraceCorrelationState.POLLING.value for item in items):
            summary_state = TraceCorrelationState.POLLING.value
        else:
            summary_state = TraceCorrelationState.PENDING.value

        last_error = next(
            (
                item
                for item in sorted(items, key=lambda current: current.updated_at, reverse=True)
                if item.error_code is not None or item.sanitized_error is not None
            ),
            None,
        )
        start_candidates = [
            item.agent_started_at or item.first_polled_at or item.created_at for item in items
        ]
        finished_at = None
        if summary_state in {
            TraceCorrelationState.REPORTED.value,
            TraceCorrelationState.AMBIGUOUS.value,
            TraceCorrelationState.TIMED_OUT.value,
            TraceCorrelationState.FAILED.value,
        }:
            finished_at = max(
                item.reported_at or item.resolved_at or item.updated_at for item in items
            )
        return TraceCorrelationSummary(
            state=summary_state,
            trace_ids=tuple(
                dict.fromkeys(item.trace_id for item in items if item.trace_id is not None)
            ),
            total_tasks=len(items),
            pending_tasks=pending,
            resolved_tasks=resolved,
            reported_tasks=reported,
            failed_tasks=failed,
            total_attempts=sum(item.attempts for item in items),
            last_error_code=last_error.error_code if last_error else None,
            last_error_message=last_error.sanitized_error if last_error else None,
            started_at=min(start_candidates),
            finished_at=finished_at,
        )

    @staticmethod
    def _apply_derived_timestamps(item: TraceCorrelation, changed_at: datetime) -> None:
        if item.state == TraceCorrelationState.POLLING.value and item.first_polled_at is None:
            item.first_polled_at = changed_at
        if (
            item.state
            in {
                TraceCorrelationState.RESOLVED.value,
                TraceCorrelationState.REPORTED.value,
            }
            and item.resolved_at is None
        ):
            item.resolved_at = changed_at
        if item.state == TraceCorrelationState.REPORTED.value and item.reported_at is None:
            item.reported_at = changed_at

    @staticmethod
    def _validate_resolved_item(item: TraceCorrelation) -> None:
        if (
            item.state
            in {
                TraceCorrelationState.RESOLVED.value,
                TraceCorrelationState.REPORTED.value,
            }
            and item.trace_id is None
        ):
            raise ValueError(f"{item.state} Trace correlation requires trace_id")
