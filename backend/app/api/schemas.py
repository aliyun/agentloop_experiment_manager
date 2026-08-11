from __future__ import annotations

from datetime import datetime, time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


def to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        serialize_by_alias=True,
        from_attributes=True,
    )


class PageResponse(ApiModel):
    items: list[Any]
    total: int
    offset: int = 0
    limit: int = 50


class HealthResponse(ApiModel):
    status: Literal["ok", "degraded"]
    version: str
    gateway_mode: str
    scheduler_enabled: bool


class AgentLoopSettingsWrite(ApiModel):
    display_name: str = Field(default="Default", min_length=1, max_length=128)
    region_id: str = Field(min_length=1, max_length=64)
    agent_space: str = Field(min_length=1, max_length=256)
    api_endpoint: str | None = None
    console_base_url: str | None = None
    access_key_id: SecretStr | None = None
    access_key_secret: SecretStr | None = None
    default_timezone: str | None = Field(default=None, min_length=1, max_length=128)
    max_concurrency: int | None = Field(default=None, ge=1, le=32)

    @model_validator(mode="after")
    def credentials_are_a_pair(self) -> "AgentLoopSettingsWrite":
        if (self.access_key_id is None) != (self.access_key_secret is None):
            raise ValueError("accessKeyId and accessKeySecret must be provided together")
        return self


class AgentLoopSettingsRead(ApiModel):
    id: str
    display_name: str
    region_id: str
    agent_space: str
    api_endpoint: str | None
    console_base_url: str | None
    access_key_id_hint: str | None
    default_timezone: str
    max_concurrency: int
    validation_status: str
    validated_at: datetime | None
    updated_at: datetime


class ConnectionCheckResponse(ApiModel):
    ok: bool
    code: str
    message: str
    checked_at: datetime


class PlanResponse(ApiModel):
    plan_id: str
    plan_name: str
    experiment_type: str
    status: str
    description: str | None = None
    dataset_id: str | None = None
    dataset_name: str | None = None
    experiment_count: int = 0
    evaluator_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    can_launch_locally: bool
    cannot_launch_reason: str | None = None


class AgentConnectionCreate(ApiModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=1000)


class AgentConnectionResponse(ApiModel):
    agent_connection_id: str
    name: str
    description: str | None = None
    status: Literal["CONNECTED", "OFFLINE"]
    health: bool
    last_seen_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    connect_url: str | None = None
    token: str | None = None
    connect_command: str | None = None


class AgentConnectionHealthResponse(ApiModel):
    agent_connection_id: str
    status: Literal["CONNECTED", "OFFLINE"]
    healthy: bool
    last_seen_at: datetime | None = None
    message: str


class LocalCodeWrite(ApiModel):
    source: SecretStr
    expected_revision: int = Field(ge=0)
    activate: bool = True


class LocalCodeResponse(ApiModel):
    language: Literal["python"]
    source: str
    configured: bool
    revision: int
    source_sha256: str | None = None
    runtime_state: Literal["NOT_CONFIGURED", "STOPPED", "STARTING", "RUNNING"]
    updated_at: datetime | None = None


class LocalCodeTestRequest(ApiModel):
    message: str = Field(min_length=1)
    parameters: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=30, ge=1, le=120)


class SanitizedOutputResponse(ApiModel):
    type: Literal["null", "boolean", "number", "string", "array", "object"]
    preview: str
    truncated: bool


class LocalCodeTestResponse(ApiModel):
    test_id: str
    status: Literal["SUCCEEDED", "FAILED", "TIMED_OUT"]
    duration_ms: int
    code_revision: int
    response_received: bool
    sanitized_output: SanitizedOutputResponse | None = None
    sanitized_meta: dict[str, str]
    redaction_count: int
    tested_at: datetime
    error_code: str | None = None
    error_message: str | None = None


class ExecutionConfiguration(ApiModel):
    agent_connection_id: str | None = Field(default=None, min_length=1, max_length=256)
    version_mode: Literal["MANUAL", "AUTO"] = "AUTO"
    experiment_version: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    experiment_parameters: dict[str, Any] = Field(default_factory=dict)
    record_name: str | None = Field(default=None, max_length=256)
    context_schema_version: Literal[1] = 1

    @model_validator(mode="after")
    def validate_version_configuration(self) -> "ExecutionConfiguration":
        if self.version_mode == "MANUAL" and self.experiment_version is None:
            raise ValueError("experimentVersion is required when versionMode is MANUAL")
        if self.version_mode == "AUTO" and self.experiment_version is not None:
            raise ValueError("experimentVersion must be omitted when versionMode is AUTO")
        return self


class LaunchRequest(ExecutionConfiguration):
    pass


class TraceCorrelationSummaryResponse(ApiModel):
    state: Literal[
        "NOT_STARTED",
        "PENDING",
        "POLLING",
        "RESOLVED",
        "REPORTED",
        "AMBIGUOUS",
        "TIMED_OUT",
        "FAILED",
    ]
    trace_ids: list[str] = Field(default_factory=list)
    total_tasks: int = Field(ge=0)
    pending_tasks: int = Field(ge=0)
    resolved_tasks: int = Field(ge=0)
    reported_tasks: int = Field(ge=0)
    failed_tasks: int = Field(ge=0)
    total_attempts: int = Field(ge=0)
    last_error_code: str | None = None
    last_error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class PlanDashboardResponse(ApiModel):
    dashboard_name: str
    display_name: str
    state: Literal["PENDING", "READY", "FAILED"]
    url: str | None = None
    last_synced_at: datetime | None = None
    last_error_code: str | None = None


class LaunchResponse(ApiModel):
    launch_id: str
    plan_id: str
    plan_name_snapshot: str | None = None
    agent_connection_id: str | None = None
    agent_connection_name_snapshot: str | None = None
    version_mode: str
    experiment_version: str | None = None
    experiment_parameters: dict[str, Any]
    record_name: str | None = None
    context_schema_version: int
    launch_config_hash: str
    submission_state: str
    runner_state: str
    trigger_source: str
    schedule_id: str | None = None
    record_id: str | None = None
    requested_at: datetime
    submit_started_at: datetime | None = None
    accepted_at: datetime | None = None
    worker_finished_at: datetime | None = None
    scheduled_for: datetime | None = None
    skip_reason: str | None = None
    agent_loop_url: str | None = None
    error_code: str | None = None
    sanitized_error: str | None = None
    trace_correlation: TraceCorrelationSummaryResponse
    plan_dashboard: PlanDashboardResponse | None = None


class IntervalCadence(ApiModel):
    type: Literal["INTERVAL_HOURS"]
    interval_hours: int = Field(ge=1, le=8760)
    anchor_at: datetime


class MinuteIntervalCadence(ApiModel):
    type: Literal["INTERVAL_MINUTES"]
    interval_minutes: int = Field(ge=5, le=525600)
    anchor_at: datetime


class DailyCadence(ApiModel):
    type: Literal["DAILY"]
    time: time


class ScheduleCreate(ExecutionConfiguration):
    name: str = Field(min_length=1, max_length=128)
    plan_id: str = Field(min_length=1, max_length=256)
    cadence: MinuteIntervalCadence | IntervalCadence | DailyCadence = Field(
        discriminator="type"
    )
    timezone: str = Field(min_length=1, max_length=128)
    misfire_policy: Literal["SKIP", "FIRE_ONCE"] = "FIRE_ONCE"
    misfire_grace_seconds: int = Field(default=3600, ge=0, le=604800)
    enabled: bool = True


class ScheduleUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    cadence: MinuteIntervalCadence | IntervalCadence | DailyCadence | None = Field(
        default=None, discriminator="type"
    )
    timezone: str | None = Field(default=None, min_length=1, max_length=128)
    misfire_policy: Literal["SKIP", "FIRE_ONCE"] | None = None
    misfire_grace_seconds: int | None = Field(default=None, ge=0, le=604800)
    agent_connection_id: str | None = Field(default=None, min_length=1, max_length=256)
    version_mode: Literal["MANUAL", "AUTO"] | None = None
    experiment_version: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    experiment_parameters: dict[str, Any] | None = None
    record_name: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def reject_empty_update(self) -> "ScheduleUpdate":
        if not self.model_fields_set:
            raise ValueError("at least one schedule field is required")
        return self


class ScheduleResponse(ApiModel):
    id: str
    name: str
    plan_id: str
    plan_name_snapshot: str | None = None
    agent_connection_id: str | None = None
    agent_connection_name_snapshot: str | None = None
    version_mode: str
    manual_experiment_version: str | None = None
    experiment_parameters: dict[str, Any]
    record_name: str | None = None
    context_schema_version: int
    launch_config_hash: str
    enabled: bool
    cadence_type: str
    interval_hours: int | None = None
    interval_minutes: int | None = None
    daily_time: str | None = None
    timezone: str
    anchor_at: datetime | None = None
    misfire_policy: str
    misfire_grace_seconds: int
    overlap_policy: str
    revision: int
    last_fire_at_utc: datetime | None = None
    next_fire_at_utc: datetime | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    deleted_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("daily_time", mode="before")
    @classmethod
    def normalize_time(cls, value: object) -> object:
        if isinstance(value, time):
            return value.strftime("%H:%M")
        return value


class ErrorBody(ApiModel):
    code: str
    message: str
    launch_id: str | None = None
    retryable: bool = False
    request_id: str | None = None
    line: int | None = None
    column: int | None = None


class ErrorResponse(ApiModel):
    error: ErrorBody
