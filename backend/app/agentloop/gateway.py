"""Stable application-facing AgentLoop gateway protocols."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from app.domain.launch_config import LaunchExecutionConfig

from .models import (
    AgentLoopConnection,
    ConnectionCheck,
    LocalHandoffOutcome,
    Page,
    PlanDetail,
    PlanQuery,
    PlanSummary,
    RecordDetail,
    RecordQuery,
    RecordSummary,
)

RecordCreatedCallback = Callable[[str], Awaitable[None]]


@runtime_checkable
class ExperimentHandle(Protocol):
    """Handle returned as soon as AgentLoop exposes an authoritative record ID."""

    @property
    def record_id(self) -> str: ...

    async def wait_local_handoff(self) -> LocalHandoffOutcome: ...


@runtime_checkable
class AgentLoopGateway(Protocol):
    """Only AgentLoop interface that application services may depend on."""

    async def test_connection(self, connection: AgentLoopConnection) -> ConnectionCheck: ...

    async def list_plans(
        self, query: PlanQuery, *, connection: AgentLoopConnection
    ) -> Page[PlanSummary]: ...

    async def get_plan(self, plan_id: str, *, connection: AgentLoopConnection) -> PlanDetail: ...

    async def start_offline_experiment(
        self,
        plan_id: str,
        on_record_created: RecordCreatedCallback,
        *,
        connection: AgentLoopConnection,
        execution_config: LaunchExecutionConfig,
    ) -> ExperimentHandle: ...

    async def list_records(
        self, query: RecordQuery, *, connection: AgentLoopConnection
    ) -> Page[RecordSummary]: ...

    async def get_record(
        self, record_id: str, *, connection: AgentLoopConnection
    ) -> RecordDetail: ...

    def plan_console_url(self, plan_id: str) -> str | None: ...

    def record_console_url(self, record_id: str) -> str | None: ...
