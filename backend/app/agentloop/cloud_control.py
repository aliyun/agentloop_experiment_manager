"""Real AgentLoop control-plane adapter built on the public generated SDK.

This module intentionally contains no experiment execution logic.  The
customer-side runner owns creation and upload of an offline Experiment Run;
the control plane is used for authoritative Plan and Run readback only.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from alibabacloud_agentloop20260520 import models as api_models
from alibabacloud_agentloop20260520.client import Client
from alibabacloud_tea_openapi import models as open_api_models

from .errors import AgentLoopGatewayError, PlanNotFoundError, RecordNotFoundError
from .models import (
    AgentLoopConnection,
    ConnectionCheck,
    Page,
    PlanDetail,
    PlanQuery,
    PlanSummary,
    RecordDetail,
    RecordQuery,
    RecordSummary,
)

_USER_AGENT = "agentloop-local-experiment-launcher/0.1.0"


class AgentLoopCloudControl:
    """Read-only application adapter for AgentLoop's public OpenAPI SDK."""

    def __init__(self, *, user_agent: str = _USER_AGENT) -> None:
        self.user_agent = user_agent

    async def test_connection(self, connection: AgentLoopConnection) -> ConnectionCheck:
        try:
            body = await asyncio.to_thread(self._get_agent_space, connection)
        except AgentLoopGatewayError as exc:
            return ConnectionCheck(
                ok=False,
                message=str(exc),
                request_id=exc.request_id,
            )
        if not body or body.agent_space != connection.agent_space:
            return ConnectionCheck(
                ok=False,
                message="AgentSpace response did not match the request",
            )
        return ConnectionCheck(
            ok=True,
            message=f"Connected to AgentSpace {body.agent_space}",
            request_id=getattr(body, "request_id", None),
        )

    async def list_plans(
        self,
        query: PlanQuery,
        *,
        connection: AgentLoopConnection,
    ) -> Page[PlanSummary]:
        summaries = await asyncio.to_thread(self._list_plan_summaries, query, connection)
        filtered = [
            item
            for item in summaries
            if query.experiment_type is None
            or item.experiment_type.casefold() == query.experiment_type.casefold()
        ]
        page_items = filtered[query.offset : query.offset + query.limit]

        # ListExperimentPlans does not contain evaluator definitions.  Resolve
        # only the visible page so the UI never advertises a Plan as runnable
        # without proving that its Dataset and evaluator bindings exist.
        details = await asyncio.gather(
            *(self.get_plan(item.plan_id, connection=connection) for item in page_items)
        )
        return Page(
            items=tuple(details),
            total=len(filtered),
            offset=query.offset,
            limit=query.limit,
        )

    async def get_plan(
        self,
        plan_id: str,
        *,
        connection: AgentLoopConnection,
    ) -> PlanDetail:
        return await asyncio.to_thread(self._get_plan, plan_id, connection)

    async def list_records(
        self,
        query: RecordQuery,
        *,
        connection: AgentLoopConnection,
    ) -> Page[RecordSummary]:
        # The service does not expose an exact plan-id filter.  Local record
        # IDs are therefore the only stable authority for this launcher.
        unique_ids = tuple(dict.fromkeys(query.record_ids))
        records: list[RecordDetail] = []
        for record_id in unique_ids:
            try:
                item = await self.get_record(record_id, connection=connection)
            except RecordNotFoundError:
                continue
            if query.plan_id and item.plan_id != query.plan_id:
                continue
            records.append(item)
        total = len(records)
        return Page(
            items=tuple(records[query.offset : query.offset + query.limit]),
            total=total,
            offset=query.offset,
            limit=query.limit,
        )

    async def get_record(
        self,
        record_id: str,
        *,
        connection: AgentLoopConnection,
    ) -> RecordDetail:
        return await asyncio.to_thread(self._get_record, record_id, connection)

    def _client(self, connection: AgentLoopConnection) -> Client:
        config = open_api_models.Config(
            access_key_id=connection.access_key_id,
            access_key_secret=connection.access_key_secret,
            region_id=connection.region_id,
            endpoint=connection.api_endpoint or f"agentloop.{connection.region_id}.aliyuncs.com",
            connect_timeout=10_000,
            read_timeout=30_000,
            user_agent=self.user_agent,
        )
        return Client(config)

    def _get_agent_space(self, connection: AgentLoopConnection) -> Any:
        try:
            response = self._client(connection).get_agent_space(
                connection.agent_space,
                api_models.GetAgentSpaceRequest(),
            )
        except Exception as exc:  # generated SDK exposes several Tea exception classes
            raise _gateway_error("GetAgentSpace", exc) from None
        return response.body

    def _list_plan_summaries(
        self,
        query: PlanQuery,
        connection: AgentLoopConnection,
    ) -> list[PlanSummary]:
        client = self._client(connection)
        token: str | None = None
        results: list[PlanSummary] = []
        while True:
            request = api_models.ListExperimentPlansRequest(
                max_results=100,
                next_token=token,
                plan_name=query.name_search,
                status=query.status,
            )
            try:
                response = client.list_experiment_plans(connection.agent_space, request)
            except Exception as exc:
                raise _gateway_error("ListExperimentPlans", exc) from None
            body = response.body
            for plan in body.plans if body and body.plans else []:
                results.append(
                    PlanSummary(
                        plan_id=plan.plan_id or "",
                        plan_name=plan.plan_name or plan.plan_id or "",
                        experiment_type=plan.experiment_type or "unknown",
                        status=plan.status or "unknown",
                        description=plan.description,
                        dataset_id=plan.dataset_id,
                        experiment_count=plan.experiment_count or 0,
                        created_at=_timestamp(plan.created_at),
                        updated_at=_timestamp(plan.updated_at),
                        can_launch_locally=False,
                        cannot_launch_reason=(
                            "Open the Plan to validate its Dataset and evaluator bindings"
                        ),
                    )
                )
            next_token = body.next_token if body else None
            if not next_token or next_token == token:
                break
            token = next_token
            if len(results) >= 10_000:
                raise AgentLoopGatewayError(
                    "ListExperimentPlans exceeded the 10,000 Plan safety cap"
                )
        return results

    def _get_plan(self, plan_id: str, connection: AgentLoopConnection) -> PlanDetail:
        try:
            response = self._client(connection).get_experiment_plan(
                connection.agent_space,
                plan_id,
                api_models.GetExperimentPlanRequest(),
            )
        except Exception as exc:
            if _is_not_found(exc):
                raise PlanNotFoundError(plan_id) from None
            raise _gateway_error("GetExperimentPlan", exc) from None
        body = response.body
        if not body or not body.plan_id:
            raise PlanNotFoundError(plan_id)

        evaluators = tuple(body.evaluators or ())
        reasons: list[str] = []
        if (body.experiment_type or "").casefold() != "offline":
            reasons.append("Only OFFLINE experiment Plans can run in the customer environment")
        if not body.dataset_id:
            reasons.append("The Plan has no Dataset binding")
        if not evaluators:
            reasons.append("The Plan has no evaluator binding")
        for evaluator in evaluators:
            variables = (evaluator.config or {}).get("variables", [])
            required = {
                value.get("name")
                for value in variables
                if isinstance(value, dict) and value.get("name")
            }
            mapped = set((evaluator.variable_mapping or {}).keys())
            missing = sorted(required - mapped)
            if missing:
                name = evaluator.evaluator_ref or evaluator.name or "unnamed evaluator"
                reasons.append(
                    f"Evaluator {name} is missing variable mappings: {', '.join(missing)}"
                )

        return PlanDetail(
            plan_id=body.plan_id,
            plan_name=body.plan_name or body.plan_id,
            experiment_type=body.experiment_type or "unknown",
            status=body.status or "unknown",
            description=body.description,
            dataset_id=body.dataset_id,
            experiment_count=len(body.experiments or ()),
            evaluator_count=len(evaluators),
            created_at=_timestamp(body.created_at),
            updated_at=_timestamp(body.updated_at),
            can_launch_locally=not reasons,
            cannot_launch_reason="; ".join(reasons) if reasons else None,
            evaluator_names=tuple(
                evaluator.evaluator_ref or evaluator.name or "unnamed evaluator"
                for evaluator in evaluators
            ),
        )

    def _get_record(self, record_id: str, connection: AgentLoopConnection) -> RecordDetail:
        try:
            response = self._client(connection).get_experiment_run(
                connection.agent_space,
                record_id,
                api_models.GetExperimentRunRequest(),
            )
        except Exception as exc:
            if _is_not_found(exc):
                raise RecordNotFoundError(record_id) from None
            raise _gateway_error("GetExperimentRun", exc) from None
        body = response.body
        record = body.record if body else None
        if not record or not record.record_id:
            raise RecordNotFoundError(record_id)
        return RecordDetail(
            record_id=record.record_id,
            record_name=record.record_name,
            plan_id=record.experiment_plan_id,
            plan_name=record.plan_name,
            status=record.status or "unknown",
            progress=record.progress,
            total_tasks=record.total_tasks or 0,
            completed_tasks=record.completed_tasks or 0,
            failed_tasks=record.failed_tasks or 0,
            executed_at=_timestamp(record.executed_at),
            completed_at=_timestamp(record.completed_at),
            evaluation_task_id=record.evaluation_task_id,
            message=(
                "AgentLoop reported an experiment error; open AgentLoop for details"
                if record.error_message
                else None
            ),
        )


def _timestamp(value: int | None) -> datetime | None:
    if value is None:
        return None
    seconds = value / 1000 if value >= 100_000_000_000 else value
    return datetime.fromtimestamp(seconds, tz=UTC)


def _is_not_found(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    code = str(getattr(exc, "code", "")).casefold()
    return status == 404 or "notfound" in code or "not_found" in code


def _gateway_error(operation: str, exc: Exception) -> AgentLoopGatewayError:
    code = getattr(exc, "code", None)
    request_id = getattr(exc, "request_id", None)
    if request_id is None and isinstance(getattr(exc, "data", None), dict):
        request_id = exc.data.get("RequestId") or exc.data.get("requestId")
    if request_id is not None and not _is_safe_request_id(request_id):
        request_id = None
    rendered = f"{operation} failed"
    if code and _is_safe_error_code(code):
        rendered += f" [{code}]"
    return AgentLoopGatewayError(rendered, request_id=request_id)


def _is_safe_error_code(value: Any) -> bool:
    candidate = str(value)
    return (
        bool(candidate)
        and len(candidate) <= 80
        and all(character.isalnum() or character in "._-" for character in candidate)
    )


def _is_safe_request_id(value: Any) -> bool:
    candidate = str(value)
    return (
        bool(candidate)
        and len(candidate) <= 128
        and all(
            character.isascii() and (character.isalnum() or character in "._-")
            for character in candidate
        )
    )
