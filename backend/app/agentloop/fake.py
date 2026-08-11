"""Deterministic, network-free AgentLoop gateway for tests and local demos."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field, replace
from enum import Enum
from urllib.parse import quote, urlencode
from uuid import uuid4

from app.domain.launch_config import LaunchExecutionConfig

from .errors import (
    AgentLoopTimeoutError,
    PlanNotFoundError,
    RecordNotFoundError,
    SubmissionUnknownError,
)
from .gateway import ExperimentHandle, RecordCreatedCallback
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


class FakeStartKind(str, Enum):
    SUCCEED = "succeed"
    TIMEOUT_BEFORE_CREATE = "timeout_before_create"
    UNKNOWN_AFTER_CREATE = "unknown_after_create"


@dataclass(frozen=True, slots=True)
class FakeStartBehavior:
    """One deterministic start outcome consumed by the fake gateway."""

    kind: FakeStartKind = FakeStartKind.SUCCEED
    record_id: str | None = None
    record_status: str = "pending"
    handoff_outcome: LocalHandoffOutcome = field(default_factory=LocalHandoffOutcome.succeeded)

    @classmethod
    def succeed(
        cls,
        *,
        record_id: str | None = None,
        record_status: str = "pending",
        handoff_outcome: LocalHandoffOutcome | None = None,
    ) -> "FakeStartBehavior":
        return cls(
            kind=FakeStartKind.SUCCEED,
            record_id=record_id,
            record_status=record_status,
            handoff_outcome=handoff_outcome or LocalHandoffOutcome.succeeded(),
        )

    @classmethod
    def timeout(cls) -> "FakeStartBehavior":
        return cls(kind=FakeStartKind.TIMEOUT_BEFORE_CREATE)

    @classmethod
    def submission_unknown(
        cls,
        *,
        record_id: str | None = None,
        record_status: str = "pending",
        handoff_outcome: LocalHandoffOutcome | None = None,
    ) -> "FakeStartBehavior":
        return cls(
            kind=FakeStartKind.UNKNOWN_AFTER_CREATE,
            record_id=record_id,
            record_status=record_status,
            handoff_outcome=handoff_outcome or LocalHandoffOutcome.unknown(),
        )


@dataclass(frozen=True, slots=True)
class InMemoryExperimentHandle:
    record_id: str
    _outcome: LocalHandoffOutcome

    async def wait_local_handoff(self) -> LocalHandoffOutcome:
        # Yield once so callers exercise an asynchronous handoff boundary.
        await asyncio.sleep(0)
        return self._outcome


class InMemoryAgentLoopGateway:
    """An AgentLoopGateway implementation that never calls an SDK or network.

    It can be seeded with cloud-shaped plans and records. Start behavior is
    queued explicitly; otherwise a successful pending record is created. Local
    idempotency is deliberately outside this fake and belongs to LaunchService.
    """

    def __init__(
        self,
        *,
        plans: tuple[PlanDetail, ...] = (),
        records: tuple[RecordDetail, ...] = (),
        connection_check: ConnectionCheck | None = None,
        console_base_url: str | None = None,
        region_id: str = "cn-hangzhou",
        agent_space: str = "default",
    ) -> None:
        self._plans = {plan.plan_id: plan for plan in plans}
        self._plan_order = [plan.plan_id for plan in plans]
        self._records = {record.record_id: record for record in records}
        self._record_order = [record.record_id for record in records]
        self._connection_check = connection_check or ConnectionCheck(ok=True)
        self._console_base_url = console_base_url.rstrip("/") if console_base_url else None
        self._region_id = region_id
        self._agent_space = agent_space
        self._behaviors: deque[FakeStartBehavior] = deque()
        self._lock = asyncio.Lock()
        self.start_attempt_count = 0
        self.created_record_count = 0
        self.last_execution_config: LaunchExecutionConfig | None = None
        self.last_connection_scope: tuple[str, str, str | None, str] | None = None

    def _observe_connection(self, connection: AgentLoopConnection | None) -> None:
        if connection is None:
            return
        self.last_connection_scope = (
            connection.region_id,
            connection.agent_space,
            connection.api_endpoint,
            connection.access_key_id[-4:],
        )

    def enqueue_start_behavior(self, behavior: FakeStartBehavior) -> None:
        self._behaviors.append(behavior)

    def put_plan(self, plan: PlanDetail) -> None:
        if plan.plan_id not in self._plans:
            self._plan_order.append(plan.plan_id)
        self._plans[plan.plan_id] = plan

    def put_record(self, record: RecordDetail) -> None:
        if record.record_id not in self._records:
            self._record_order.append(record.record_id)
        self._records[record.record_id] = record

    async def test_connection(self, connection: AgentLoopConnection) -> ConnectionCheck:
        self._observe_connection(connection)
        return self._connection_check

    async def list_plans(
        self, query: PlanQuery, *, connection: AgentLoopConnection | None = None
    ) -> Page[PlanSummary]:
        self._observe_connection(connection)
        plans = [self._plans[plan_id] for plan_id in self._plan_order]
        if query.name_search:
            needle = query.name_search.casefold()
            plans = [plan for plan in plans if needle in plan.plan_name.casefold()]
        if query.status is not None:
            plans = [plan for plan in plans if plan.status == query.status]
        if query.experiment_type is not None:
            requested_type = query.experiment_type.casefold()
            plans = [plan for plan in plans if plan.experiment_type.casefold() == requested_type]

        page_items = plans[query.offset : query.offset + query.limit]
        return Page(
            items=tuple(self._as_plan_summary(plan) for plan in page_items),
            total=len(plans),
            offset=query.offset,
            limit=query.limit,
        )

    async def get_plan(
        self, plan_id: str, *, connection: AgentLoopConnection | None = None
    ) -> PlanDetail:
        self._observe_connection(connection)
        try:
            return self._plans[plan_id]
        except KeyError as error:
            raise PlanNotFoundError(plan_id) from error

    async def start_offline_experiment(
        self,
        plan_id: str,
        on_record_created: RecordCreatedCallback,
        *,
        connection: AgentLoopConnection | None = None,
        execution_config: LaunchExecutionConfig | None = None,
    ) -> ExperimentHandle:
        self._observe_connection(connection)
        self.last_execution_config = execution_config

        async with self._lock:
            self.start_attempt_count += 1
            plan = await self.get_plan(plan_id)
            behavior = self._behaviors.popleft() if self._behaviors else FakeStartBehavior.succeed()
            if behavior.kind is FakeStartKind.TIMEOUT_BEFORE_CREATE:
                raise AgentLoopTimeoutError("start_offline_experiment")

            record_id = behavior.record_id or f"fake-record-{uuid4().hex}"
            record = RecordDetail(
                record_id=record_id,
                record_name=f"Local run of {plan.plan_name}",
                plan_id=plan.plan_id,
                plan_name=plan.plan_name,
                status=behavior.record_status,
            )
            self.put_record(record)
            handle = InMemoryExperimentHandle(record_id, behavior.handoff_outcome)
            self.created_record_count += 1

            if behavior.kind is FakeStartKind.UNKNOWN_AFTER_CREATE:
                # Model a lost create response. The local ledger must retain UNKNOWN
                # without issuing another remote start for the same local key.
                raise SubmissionUnknownError()

        # The record exists before the callback, but application code is never
        # awaited while the fake's lock is held.
        await on_record_created(handle.record_id)
        return handle

    async def list_records(
        self, query: RecordQuery, *, connection: AgentLoopConnection | None = None
    ) -> Page[RecordSummary]:
        self._observe_connection(connection)
        # De-duplicate without changing the caller's authoritative ID order.
        unique_ids = tuple(dict.fromkeys(query.record_ids))
        records = [
            self._records[record_id] for record_id in unique_ids if record_id in self._records
        ]
        if query.plan_id is not None:
            records = [record for record in records if record.plan_id == query.plan_id]
        page_items = records[query.offset : query.offset + query.limit]
        return Page(
            items=tuple(self._as_record_summary(record) for record in page_items),
            total=len(records),
            offset=query.offset,
            limit=query.limit,
        )

    async def get_record(
        self, record_id: str, *, connection: AgentLoopConnection | None = None
    ) -> RecordDetail:
        self._observe_connection(connection)
        try:
            record = self._records[record_id]
        except KeyError as error:
            raise RecordNotFoundError(record_id) from error

        if record.console_url is None:
            console_url = self.record_console_url(record_id)
            if console_url is not None:
                return replace(record, console_url=console_url)
        return record

    def plan_console_url(self, plan_id: str) -> str | None:
        if self._console_base_url is None:
            return None
        query = urlencode({"planId": plan_id})
        return f"{self._playground_base()}?{query}"

    def record_console_url(self, record_id: str) -> str | None:
        if self._console_base_url is None:
            return None
        record = self._records.get(record_id)
        query_values = {"recordId": record_id}
        if record is not None and record.plan_id is not None:
            query_values["planId"] = record.plan_id
        return f"{self._playground_base()}?{urlencode(query_values)}"

    def _playground_base(self) -> str:
        assert self._console_base_url is not None
        return (
            f"{self._console_base_url}/agentloop/region/{quote(self._region_id, safe='')}"
            f"/agentspace/{quote(self._agent_space, safe='')}/app/playground"
        )

    @staticmethod
    def _as_plan_summary(plan: PlanDetail) -> PlanSummary:
        return PlanSummary(
            plan_id=plan.plan_id,
            plan_name=plan.plan_name,
            experiment_type=plan.experiment_type,
            status=plan.status,
            description=plan.description,
            dataset_id=plan.dataset_id,
            experiment_count=plan.experiment_count,
            evaluator_count=plan.evaluator_count,
            created_at=plan.created_at,
            updated_at=plan.updated_at,
            can_launch_locally=plan.can_launch_locally,
            cannot_launch_reason=plan.cannot_launch_reason,
        )

    @staticmethod
    def _as_record_summary(record: RecordDetail) -> RecordSummary:
        return RecordSummary(
            record_id=record.record_id,
            record_name=record.record_name,
            plan_id=record.plan_id,
            plan_name=record.plan_name,
            status=record.status,
            progress=record.progress,
            total_tasks=record.total_tasks,
            completed_tasks=record.completed_tasks,
            failed_tasks=record.failed_tasks,
            executed_at=record.executed_at,
            completed_at=record.completed_at,
            evaluation_task_id=record.evaluation_task_id,
            console_url=record.console_url or None,
        )


# Concise alias for dependency-injection sites and tests.
FakeAgentLoopGateway = InMemoryAgentLoopGateway
