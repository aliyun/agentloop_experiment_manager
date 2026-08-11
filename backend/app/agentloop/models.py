"""Provider-neutral data models used by the AgentLoop gateway."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Generic, TypeVar

T = TypeVar("T")


def _validate_paging(offset: int, limit: int) -> None:
    if offset < 0:
        raise ValueError("offset must be greater than or equal to zero")
    if limit < 1 or limit > 200:
        raise ValueError("limit must be between 1 and 200")


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    """An immutable, offset-based page returned by AgentLoop."""

    items: tuple[T, ...]
    total: int
    offset: int
    limit: int

    def __post_init__(self) -> None:
        _validate_paging(self.offset, self.limit)
        if self.total < 0:
            raise ValueError("total must be greater than or equal to zero")
        if len(self.items) > self.limit:
            raise ValueError("page contains more items than its limit")

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


@dataclass(frozen=True, slots=True)
class PlanQuery:
    """Read-only filters supported by the stable plan-list contract."""

    offset: int = 0
    limit: int = 50
    name_search: str | None = None
    status: str | None = None
    experiment_type: str | None = None

    def __post_init__(self) -> None:
        _validate_paging(self.offset, self.limit)


@dataclass(frozen=True, slots=True)
class RecordQuery:
    """A bounded record lookup driven by authoritative record IDs.

    The MVP intentionally does not expose a plan-name filter. ``record_ids``
    normally comes from local LaunchIntent rows; ``plan_id`` is an additional
    exact-match guard against a mismatched remote response.
    """

    record_ids: tuple[str, ...]
    offset: int = 0
    limit: int = 50
    plan_id: str | None = None

    def __post_init__(self) -> None:
        _validate_paging(self.offset, self.limit)
        if any(not record_id for record_id in self.record_ids):
            raise ValueError("record_ids cannot contain empty values")


@dataclass(frozen=True, slots=True)
class ConnectionCheck:
    ok: bool
    message: str | None = None
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class AgentLoopConnection:
    """One verified local Profile projection passed explicitly to every gateway call."""

    region_id: str
    agent_space: str
    access_key_id: str = field(repr=False)
    access_key_secret: str = field(repr=False)
    api_endpoint: str | None = None

    def __post_init__(self) -> None:
        if not self.region_id.strip():
            raise ValueError("region_id is required")
        if not self.agent_space.strip():
            raise ValueError("agent_space is required")
        if not self.access_key_id:
            raise ValueError("access_key_id is required")
        if not self.access_key_secret:
            raise ValueError("access_key_secret is required")


@dataclass(frozen=True, slots=True)
class PlanSummary:
    plan_id: str
    plan_name: str
    experiment_type: str
    status: str
    description: str | None = None
    dataset_id: str | None = None
    experiment_count: int = 0
    evaluator_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    can_launch_locally: bool = False
    cannot_launch_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.plan_id:
            raise ValueError("plan_id is required")
        if not self.plan_name:
            raise ValueError("plan_name is required")
        if self.experiment_count < 0 or self.evaluator_count < 0:
            raise ValueError("plan counts cannot be negative")


@dataclass(frozen=True, slots=True)
class PlanDetail(PlanSummary):
    dataset_name: str | None = None
    evaluator_names: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class RecordSummary:
    """A result-free projection of an AgentLoop Experiment Record.

    ``status`` deliberately remains a string. New AgentLoop states must pass
    through unchanged instead of being coerced to success or rejected.
    """

    record_id: str
    record_name: str | None
    plan_id: str | None
    plan_name: str | None
    status: str
    progress: float | None = None
    total_tasks: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0
    executed_at: datetime | None = None
    completed_at: datetime | None = None
    evaluation_task_id: str | None = None
    console_url: str | None = None

    def __post_init__(self) -> None:
        if not self.record_id:
            raise ValueError("record_id is required")
        if not self.status:
            raise ValueError("status is required")
        if self.progress is not None and not 0.0 <= self.progress <= 1.0:
            raise ValueError("progress must be between 0 and 1")
        if min(self.total_tasks, self.completed_tasks, self.failed_tasks) < 0:
            raise ValueError("record task counts cannot be negative")


@dataclass(frozen=True, slots=True)
class RecordDetail(RecordSummary):
    """Record details that remain safe for the local launcher to display."""

    message: str | None = None


class LocalHandoffState(str, Enum):
    """Outcome of the customer-side execution/upload responsibility only."""

    SUCCEEDED = "succeeded"
    DEFINITIVE_FAILURE = "definitive_failure"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class LocalHandoffOutcome:
    state: LocalHandoffState
    message: str | None = None

    @classmethod
    def succeeded(cls, message: str | None = None) -> "LocalHandoffOutcome":
        return cls(LocalHandoffState.SUCCEEDED, message)

    @classmethod
    def definitive_failure(cls, message: str | None = None) -> "LocalHandoffOutcome":
        return cls(LocalHandoffState.DEFINITIVE_FAILURE, message)

    @classmethod
    def unknown(cls, message: str | None = None) -> "LocalHandoffOutcome":
        return cls(LocalHandoffState.UNKNOWN, message)
