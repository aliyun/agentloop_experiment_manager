"""SQLite ORM models for the local experiment launch ledger.

Only local control-plane metadata belongs here.  In particular, this module must
never grow columns for an access-key secret, Dataset rows, experiment output, or
evaluation results.  AgentLoop remains the source of truth for those objects.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    and_,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""

    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """Normalize an aware datetime to UTC, rejecting ambiguous naive values."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


class UTCDateTimeText(TypeDecorator[datetime]):
    """Persist aware datetimes as fixed-width RFC3339 UTC text.

    The explicit text representation follows the SPEC DDL and remains sortable
    in SQLite because every stored value is UTC with six fractional digits.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: datetime | str | None, dialect: object) -> str | None:
        del dialect
        if value is None:
            return None
        if isinstance(value, str):
            parsed = value[:-1] + "+00:00" if value.endswith("Z") else value
            value = datetime.fromisoformat(parsed)
        normalized = as_utc(value)
        return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")

    def process_result_value(self, value: str | None, dialect: object) -> datetime | None:
        del dialect
        if value is None:
            return None
        parsed = value[:-1] + "+00:00" if value.endswith("Z") else value
        return as_utc(datetime.fromisoformat(parsed))


class Base(DeclarativeBase):
    pass


class ValidationStatus(StrEnum):
    UNVALIDATED = "UNVALIDATED"
    VALID = "VALID"
    INVALID = "INVALID"


class CadenceType(StrEnum):
    INTERVAL_MINUTES = "INTERVAL_MINUTES"
    INTERVAL_HOURS = "INTERVAL_HOURS"
    DAILY = "DAILY"


class MisfirePolicy(StrEnum):
    SKIP = "SKIP"
    FIRE_ONCE = "FIRE_ONCE"


class OverlapPolicy(StrEnum):
    FORBID_LOCAL_OVERLAP = "FORBID_LOCAL_OVERLAP"


class TriggerSource(StrEnum):
    MANUAL = "manual"
    SCHEDULE = "schedule"
    SCHEDULE_RUN_NOW = "schedule_run_now"
    EXTERNAL = "external"


class VersionMode(StrEnum):
    MANUAL = "MANUAL"
    AUTO = "AUTO"


class SubmissionState(StrEnum):
    CREATED = "CREATED"
    SUBMITTING = "SUBMITTING"
    ACCEPTED = "ACCEPTED"
    DEFINITIVE_FAILURE = "DEFINITIVE_FAILURE"
    SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"
    SKIPPED = "SKIPPED"


class RunnerState(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    RUNNING = "RUNNING"
    HANDED_OFF = "HANDED_OFF"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class TraceCorrelationState(StrEnum):
    """Lifecycle of one Dataset item while its authoritative Trace is located."""

    PENDING = "PENDING"
    POLLING = "POLLING"
    RESOLVED = "RESOLVED"
    REPORTED = "REPORTED"
    AMBIGUOUS = "AMBIGUOUS"
    TIMED_OUT = "TIMED_OUT"
    FAILED = "FAILED"


class PlanDashboardSyncState(StrEnum):
    """Last known reconciliation state for one Experiment Plan dashboard."""

    PENDING = "PENDING"
    READY = "READY"
    FAILED = "FAILED"


class SkipReason(StrEnum):
    MISFIRE = "MISFIRE"
    OVERLAP = "OVERLAP"
    PLAN_UNAVAILABLE = "PLAN_UNAVAILABLE"
    AGENT_OFFLINE = "AGENT_OFFLINE"


class AgentLoopProfile(Base):
    __tablename__ = "agentloop_profile"
    __table_args__ = (
        CheckConstraint(
            "max_concurrency >= 1 AND max_concurrency <= 32", name="ck_profile_max_concurrency"
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    region_id: Mapped[str] = mapped_column(Text, nullable=False)
    agent_space: Mapped[str] = mapped_column(Text, nullable=False)
    api_endpoint: Mapped[Optional[str]] = mapped_column(Text)
    console_base_url: Mapped[Optional[str]] = mapped_column(Text)
    access_key_id_hint: Mapped[Optional[str]] = mapped_column(Text)
    secret_ref: Mapped[str] = mapped_column(Text, nullable=False)
    default_timezone: Mapped[str] = mapped_column(Text, nullable=False, default="UTC")
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    validation_status: Mapped[str] = mapped_column(
        Text, nullable=False, default=ValidationStatus.UNVALIDATED.value
    )
    validated_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTimeText)
    last_error_code: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTimeText, nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTimeText, nullable=False, default=utc_now, onupdate=utc_now
    )

    def __repr__(self) -> str:
        return f"AgentLoopProfile(id={self.id!r}, display_name={self.display_name!r})"


class AgentConnection(Base):
    """A local Agent registration; the bearer token lives only in SecretStore."""

    __tablename__ = "agent_connections"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    token_secret_ref: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    token_hint: Mapped[str] = mapped_column(Text, nullable=False)
    last_connected_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTimeText)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTimeText)
    last_disconnected_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTimeText)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTimeText)
    created_at: Mapped[datetime] = mapped_column(UTCDateTimeText, nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTimeText, nullable=False, default=utc_now, onupdate=utc_now
    )

    def __repr__(self) -> str:
        return f"AgentConnection(id={self.id!r}, name={self.name!r})"


class Schedule(Base):
    __tablename__ = "schedules"
    __table_args__ = (
        CheckConstraint("enabled IN (0, 1)", name="ck_schedule_enabled"),
        CheckConstraint("revision >= 1", name="ck_schedule_revision"),
        CheckConstraint("misfire_grace_seconds >= 0", name="ck_schedule_misfire_grace"),
        CheckConstraint(
            "(cadence_type = 'INTERVAL_MINUTES' "
            "AND interval_minutes IS NOT NULL AND interval_minutes >= 5 "
            "AND interval_hours IS NULL AND daily_time IS NULL) OR "
            "(cadence_type = 'INTERVAL_HOURS' "
            "AND interval_hours IS NOT NULL AND interval_hours > 0 "
            "AND interval_minutes IS NULL AND daily_time IS NULL) OR "
            "(cadence_type = 'DAILY' AND interval_hours IS NULL "
            "AND interval_minutes IS NULL AND daily_time IS NOT NULL)",
            name="ck_schedule_cadence_fields",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    profile_id: Mapped[str] = mapped_column(
        String, ForeignKey("agentloop_profile.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    plan_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    plan_name_snapshot: Mapped[Optional[str]] = mapped_column(Text)
    agent_connection_id: Mapped[Optional[str]] = mapped_column(
        String, ForeignKey("agent_connections.id"), index=True
    )
    agent_connection_name_snapshot: Mapped[Optional[str]] = mapped_column(Text)
    version_mode: Mapped[str] = mapped_column(Text, nullable=False, default=VersionMode.AUTO.value)
    manual_experiment_version: Mapped[Optional[str]] = mapped_column(Text)
    experiment_parameters_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    record_name: Mapped[Optional[str]] = mapped_column(Text)
    context_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    launch_config_hash: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    cadence_type: Mapped[str] = mapped_column(Text, nullable=False)
    interval_hours: Mapped[Optional[int]] = mapped_column(Integer)
    interval_minutes: Mapped[Optional[int]] = mapped_column(Integer)
    daily_time: Mapped[Optional[str]] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text, nullable=False)
    anchor_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTimeText)
    misfire_policy: Mapped[str] = mapped_column(
        Text, nullable=False, default=MisfirePolicy.FIRE_ONCE.value
    )
    misfire_grace_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=3600)
    overlap_policy: Mapped[str] = mapped_column(
        Text, nullable=False, default=OverlapPolicy.FORBID_LOCAL_OVERLAP.value
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_fire_at_utc: Mapped[Optional[datetime]] = mapped_column(UTCDateTimeText)
    next_fire_at_utc: Mapped[Optional[datetime]] = mapped_column(UTCDateTimeText)
    last_error_code: Mapped[Optional[str]] = mapped_column(Text)
    last_error_message: Mapped[Optional[str]] = mapped_column(Text)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTimeText)
    created_at: Mapped[datetime] = mapped_column(UTCDateTimeText, nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTimeText, nullable=False, default=utc_now, onupdate=utc_now
    )

    def __repr__(self) -> str:
        return f"Schedule(id={self.id!r}, plan_id={self.plan_id!r}, revision={self.revision})"

    @property
    def experiment_parameters(self) -> dict:
        return json.loads(self.experiment_parameters_json)


class LaunchIntent(Base):
    __tablename__ = "launch_intents"
    __table_args__ = (CheckConstraint("attempt_count >= 0", name="ck_launch_attempt_count"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    profile_id: Mapped[str] = mapped_column(
        String, ForeignKey("agentloop_profile.id"), nullable=False, index=True
    )
    plan_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    plan_name_snapshot: Mapped[Optional[str]] = mapped_column(Text)
    plan_updated_at_snapshot: Mapped[Optional[datetime]] = mapped_column(UTCDateTimeText)
    agent_connection_id: Mapped[Optional[str]] = mapped_column(
        String, ForeignKey("agent_connections.id"), index=True
    )
    agent_connection_name_snapshot: Mapped[Optional[str]] = mapped_column(Text)
    version_mode: Mapped[str] = mapped_column(Text, nullable=False, default=VersionMode.AUTO.value)
    manual_experiment_version: Mapped[Optional[str]] = mapped_column(Text)
    experiment_version: Mapped[Optional[str]] = mapped_column(Text)
    experiment_parameters_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    record_name: Mapped[Optional[str]] = mapped_column(Text)
    context_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    context_xml_template: Mapped[Optional[str]] = mapped_column(Text)
    launch_config_hash: Mapped[str] = mapped_column(Text, nullable=False, default="")
    trigger_source: Mapped[str] = mapped_column(Text, nullable=False)
    schedule_id: Mapped[Optional[str]] = mapped_column(
        String, ForeignKey("schedules.id"), index=True
    )
    schedule_revision: Mapped[Optional[int]] = mapped_column(Integer)
    scheduled_for_utc: Mapped[Optional[datetime]] = mapped_column(UTCDateTimeText)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    submission_state: Mapped[str] = mapped_column(
        Text, nullable=False, default=SubmissionState.CREATED.value
    )
    runner_state: Mapped[str] = mapped_column(
        Text, nullable=False, default=RunnerState.NOT_STARTED.value
    )
    skip_reason: Mapped[Optional[str]] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(UTCDateTimeText, nullable=False, default=utc_now)
    submit_started_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTimeText)
    accepted_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTimeText)
    worker_finished_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTimeText)
    record_id: Mapped[Optional[str]] = mapped_column(Text, unique=True)
    request_id: Mapped[Optional[str]] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[Optional[str]] = mapped_column(Text)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTimeText)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTimeText)
    error_code: Mapped[Optional[str]] = mapped_column(Text)
    sanitized_error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTimeText, nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTimeText, nullable=False, default=utc_now, onupdate=utc_now
    )

    def __repr__(self) -> str:
        return (
            f"LaunchIntent(id={self.id!r}, plan_id={self.plan_id!r}, "
            f"submission_state={self.submission_state!r})"
        )

    @property
    def experiment_parameters(self) -> dict:
        return json.loads(self.experiment_parameters_json)


class PlanDashboard(Base):
    """Payload-free local metadata for one cloud dashboard per Experiment Plan."""

    __tablename__ = "plan_dashboards"
    __table_args__ = (
        CheckConstraint(
            "sync_state IN ('PENDING', 'READY', 'FAILED')",
            name="ck_plan_dashboard_sync_state",
        ),
        Index("ix_plan_dashboards_sync_state", "sync_state"),
    )

    profile_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("agentloop_profile.id", ondelete="CASCADE"),
        primary_key=True,
    )
    plan_id: Mapped[str] = mapped_column(Text, primary_key=True)
    plan_name_snapshot: Mapped[Optional[str]] = mapped_column(Text)
    agent_space_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    sls_project: Mapped[Optional[str]] = mapped_column(Text)
    dashboard_name: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    template_version: Mapped[str] = mapped_column(Text, nullable=False)
    template_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    sync_state: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=PlanDashboardSyncState.PENDING.value,
    )
    last_error_code: Mapped[Optional[str]] = mapped_column(Text)
    last_attempt_at: Mapped[datetime] = mapped_column(
        UTCDateTimeText,
        nullable=False,
        default=utc_now,
    )
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTimeText)
    created_at: Mapped[datetime] = mapped_column(UTCDateTimeText, nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTimeText,
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )

    def __repr__(self) -> str:
        return (
            "PlanDashboard("
            f"profile_id={self.profile_id!r}, plan_id={self.plan_id!r}, "
            f"sync_state={self.sync_state!r})"
        )


Index(
    "uq_schedule_slot",
    LaunchIntent.schedule_id,
    LaunchIntent.schedule_revision,
    LaunchIntent.scheduled_for_utc,
    unique=True,
    sqlite_where=and_(
        LaunchIntent.schedule_id.is_not(None),
        LaunchIntent.scheduled_for_utc.is_not(None),
    ),
)


class TraceCorrelation(Base):
    """Trace lookup audit metadata for one launched Dataset item.

    This table deliberately contains neither credentials nor the Dataset input,
    Agent output, or Trace body.  Those payloads remain in their authoritative
    customer/AgentLoop stores.
    """

    __tablename__ = "trace_correlations"
    __table_args__ = (
        UniqueConstraint(
            "launch_id",
            "dataset_item_id",
            name="uq_trace_correlation_launch_dataset_item",
        ),
        CheckConstraint("attempts >= 0", name="ck_trace_correlation_attempts"),
        CheckConstraint(
            "state IN ('PENDING', 'POLLING', 'RESOLVED', 'REPORTED', "
            "'AMBIGUOUS', 'TIMED_OUT', 'FAILED')",
            name="ck_trace_correlation_state",
        ),
        Index("ix_trace_correlations_launch_state", "launch_id", "state"),
    )

    launch_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("launch_intents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    dataset_item_id: Mapped[str] = mapped_column(Text, primary_key=True)
    experiment_run_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    state: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=TraceCorrelationState.PENDING.value,
    )
    trace_id: Mapped[Optional[str]] = mapped_column(Text)
    source: Mapped[Optional[str]] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[Optional[str]] = mapped_column(Text)
    sanitized_error: Mapped[Optional[str]] = mapped_column(Text)
    agent_started_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTimeText)
    agent_completed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTimeText)
    first_polled_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTimeText)
    deadline_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTimeText)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTimeText)
    reported_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTimeText)
    created_at: Mapped[datetime] = mapped_column(UTCDateTimeText, nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTimeText,
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )

    def __repr__(self) -> str:
        return (
            "TraceCorrelation("
            f"launch_id={self.launch_id!r}, dataset_item_id={self.dataset_item_id!r}, "
            f"state={self.state!r})"
        )
