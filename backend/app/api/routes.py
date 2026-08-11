from __future__ import annotations

import shlex
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from fastapi import APIRouter, Header, Query, Request, Response, WebSocket, status

from app.agentloop import AgentLoopGatewayError, PlanNotFoundError, PlanQuery
from app.domain.errors import NotFoundError, ServiceUnavailableError
from app.runtime import AppContainer

from .schemas import (
    AgentConnectionCreate,
    AgentConnectionHealthResponse,
    AgentConnectionResponse,
    AgentLoopSettingsRead,
    AgentLoopSettingsWrite,
    ConnectionCheckResponse,
    HealthResponse,
    LaunchRequest,
    LaunchResponse,
    LocalCodeResponse,
    LocalCodeTestRequest,
    LocalCodeTestResponse,
    LocalCodeWrite,
    PageResponse,
    PlanDashboardResponse,
    PlanResponse,
    ScheduleCreate,
    ScheduleResponse,
    ScheduleUpdate,
)

router = APIRouter(prefix="/api/v1")


def container(request: Request) -> AppContainer:
    return request.app.state.container


def profile_response(profile) -> AgentLoopSettingsRead:
    return AgentLoopSettingsRead.model_validate(profile)


def plan_response(plan) -> PlanResponse:
    return PlanResponse.model_validate(plan)


def launch_response(app: AppContainer, launch) -> LaunchResponse:
    url = (
        app.console_url_resolver.record_url(launch.record_id, launch.plan_id)
        if launch.record_id is not None
        else None
    )
    dashboard = app.plan_dashboard_repository.get(launch.profile_id, launch.plan_id)
    dashboard_response = (
        PlanDashboardResponse(
            dashboard_name=dashboard.dashboard_name,
            display_name=dashboard.display_name,
            state=dashboard.sync_state,
            url=app.console_url_resolver.dashboard_url(dashboard.dashboard_name),
            last_synced_at=dashboard.last_synced_at,
            last_error_code=dashboard.last_error_code,
        )
        if dashboard is not None
        else None
    )
    return LaunchResponse(
        launch_id=launch.id,
        plan_id=launch.plan_id,
        plan_name_snapshot=launch.plan_name_snapshot,
        agent_connection_id=launch.agent_connection_id,
        agent_connection_name_snapshot=launch.agent_connection_name_snapshot,
        version_mode=launch.version_mode,
        experiment_version=launch.experiment_version,
        experiment_parameters=launch.experiment_parameters,
        record_name=launch.record_name,
        context_schema_version=launch.context_schema_version,
        launch_config_hash=launch.launch_config_hash,
        submission_state=launch.submission_state,
        runner_state=launch.runner_state,
        trigger_source=launch.trigger_source,
        schedule_id=launch.schedule_id,
        record_id=launch.record_id,
        requested_at=launch.requested_at,
        submit_started_at=launch.submit_started_at,
        accepted_at=launch.accepted_at,
        worker_finished_at=launch.worker_finished_at,
        scheduled_for=launch.scheduled_for_utc,
        skip_reason=launch.skip_reason,
        agent_loop_url=url,
        error_code=launch.error_code,
        sanitized_error=launch.sanitized_error,
        trace_correlation=app.trace_correlation_repository.summary(launch.id),
        plan_dashboard=dashboard_response,
    )


def schedule_response(schedule) -> ScheduleResponse:
    return ScheduleResponse.model_validate(schedule)


def agent_connect_url(request: Request, connection_id: str) -> str:
    parsed = urlsplit(str(request.base_url))
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = f"/api/v1/agent-connections/{quote(connection_id, safe='')}/connect"
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


def agent_connection_response(
    request: Request,
    connection,
    *,
    token: str | None = None,
) -> AgentConnectionResponse:
    health = container(request).agent_connection_service.health(connection.id)
    connect_url = agent_connect_url(request, connection.id) if token is not None else None
    connect_command = None
    if token is not None and connect_url is not None:
        connect_command = (
            f"{shlex.quote(_launcher_executable())} reference-agent "
            f"--url {shlex.quote(connect_url)}"
        )
    return AgentConnectionResponse(
        agent_connection_id=connection.id,
        name=connection.name,
        description=connection.description,
        status=health.status,
        health=health.healthy,
        last_seen_at=health.last_seen_at,
        created_at=connection.created_at,
        updated_at=connection.updated_at,
        connect_url=connect_url,
        token=token,
        connect_command=connect_command,
    )


def _launcher_executable() -> str:
    if getattr(sys, "frozen", False):
        return sys.executable
    return str(Path(__file__).resolve().parents[3] / "agentloop-launcher")


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    app = container(request)
    return HealthResponse(
        status="degraded" if app.settings.gateway_mode == "disabled" else "ok",
        version="0.1.0",
        gateway_mode=app.settings.gateway_mode,
        scheduler_enabled=app.settings.scheduler_enabled,
    )


@router.get("/settings/agentloop", response_model=AgentLoopSettingsRead)
def get_settings(request: Request) -> AgentLoopSettingsRead:
    return profile_response(container(request).profile_service.get())


@router.put("/settings/agentloop", response_model=AgentLoopSettingsRead)
def put_settings(request: Request, payload: AgentLoopSettingsWrite) -> AgentLoopSettingsRead:
    profile = container(request).profile_service.configure(
        display_name=payload.display_name,
        region_id=payload.region_id,
        agent_space=payload.agent_space,
        api_endpoint=payload.api_endpoint,
        console_base_url=payload.console_base_url,
        default_timezone=payload.default_timezone,
        max_concurrency=payload.max_concurrency,
        access_key_id=(payload.access_key_id.get_secret_value() if payload.access_key_id else None),
        access_key_secret=(
            payload.access_key_secret.get_secret_value() if payload.access_key_secret else None
        ),
    )
    return profile_response(profile)


@router.post("/settings/agentloop/test", response_model=ConnectionCheckResponse)
async def test_settings(request: Request) -> ConnectionCheckResponse:
    result = await container(request).profile_service.test_connection()
    return ConnectionCheckResponse(
        ok=result.ok,
        code="OK" if result.ok else "AGENTLOOP_AUTH_FAILED",
        message=result.message or ("Connection is valid" if result.ok else "Connection failed"),
        checked_at=datetime.now(UTC),
    )


@router.post(
    "/agent-connections",
    response_model=AgentConnectionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_agent_connection(
    request: Request, payload: AgentConnectionCreate
) -> AgentConnectionResponse:
    credential = container(request).agent_connection_service.create(
        name=payload.name,
        description=payload.description,
    )
    return agent_connection_response(
        request,
        credential.connection,
        token=credential.token,
    )


@router.get("/agent-connections", response_model=PageResponse)
def list_agent_connections(request: Request) -> PageResponse:
    service = container(request).agent_connection_service
    items = service.list()
    return PageResponse(
        items=[
            agent_connection_response(request, item).model_dump(by_alias=True, mode="json")
            for item in items
        ],
        total=len(items),
        offset=0,
        limit=max(len(items), 1),
    )


@router.get("/agent-connections/{connection_id}", response_model=AgentConnectionResponse)
def get_agent_connection(request: Request, connection_id: str) -> AgentConnectionResponse:
    service = container(request).agent_connection_service
    return agent_connection_response(request, service.get(connection_id))


@router.get(
    "/agent-connections/{connection_id}/health",
    response_model=AgentConnectionHealthResponse,
)
def get_agent_connection_health(
    request: Request, connection_id: str
) -> AgentConnectionHealthResponse:
    health = container(request).agent_connection_service.health(connection_id)
    return AgentConnectionHealthResponse(
        agent_connection_id=health.connection_id,
        status=health.status,
        healthy=health.healthy,
        last_seen_at=health.last_seen_at,
        message=health.message,
    )


@router.get(
    "/agent-connections/{connection_id}/local-code",
    response_model=LocalCodeResponse,
)
def get_local_agent_code(
    request: Request,
    response: Response,
    connection_id: str,
) -> LocalCodeResponse:
    response.headers["Cache-Control"] = "no-store"
    configuration = container(request).managed_code_service.get(connection_id)
    return LocalCodeResponse.model_validate(configuration)


@router.put(
    "/agent-connections/{connection_id}/local-code",
    response_model=LocalCodeResponse,
)
async def put_local_agent_code(
    request: Request,
    response: Response,
    connection_id: str,
    payload: LocalCodeWrite,
) -> LocalCodeResponse:
    response.headers["Cache-Control"] = "no-store"
    configuration = await container(request).managed_code_service.put(
        connection_id,
        source=payload.source.get_secret_value(),
        expected_revision=payload.expected_revision,
        activate=payload.activate,
        connect_url=agent_connect_url(request, connection_id),
    )
    return LocalCodeResponse.model_validate(configuration)


@router.post(
    "/agent-connections/{connection_id}/test",
    response_model=LocalCodeTestResponse,
)
async def test_local_agent_code(
    request: Request,
    response: Response,
    connection_id: str,
    payload: LocalCodeTestRequest,
) -> LocalCodeTestResponse:
    response.headers["Cache-Control"] = "no-store"
    result = await container(request).managed_code_service.test(
        connection_id,
        message=payload.message,
        parameters=payload.parameters,
        timeout_seconds=payload.timeout_seconds,
    )
    return LocalCodeTestResponse.model_validate(result)


@router.post(
    "/agent-connections/{connection_id}/rotate-token",
    response_model=AgentConnectionResponse,
)
async def rotate_agent_connection_token(
    request: Request, connection_id: str
) -> AgentConnectionResponse:
    credential = await container(request).agent_connection_service.rotate_token(connection_id)
    return agent_connection_response(
        request,
        credential.connection,
        token=credential.token,
    )


@router.delete("/agent-connections/{connection_id}", status_code=204)
async def delete_agent_connection(request: Request, connection_id: str) -> Response:
    await container(request).agent_connection_service.delete(connection_id)
    return Response(status_code=204)


@router.websocket("/agent-connections/{connection_id}/connect")
async def connect_local_agent(websocket: WebSocket, connection_id: str) -> None:
    authorization = websocket.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.casefold() != "bearer" or not token:
        await websocket.close(code=4401, reason="Bearer token is required")
        return
    service = websocket.app.state.container.agent_connection_service
    await service.connect(connection_id, token, websocket)


@router.get("/plans", response_model=PageResponse)
async def list_plans(
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    search: str | None = None,
    plan_status: str | None = Query(default=None, alias="status"),
    experiment_type: str | None = Query(default=None, alias="experimentType"),
) -> PageResponse:
    try:
        page = await container(request).gateway.list_plans(
            PlanQuery(
                offset=offset,
                limit=limit,
                name_search=search,
                status=plan_status,
                experiment_type=experiment_type,
            ),
            connection=container(request).profile_service.connection(),
        )
    except AgentLoopGatewayError as exc:
        raise ServiceUnavailableError(exc.code, str(exc), retryable=exc.retryable) from exc
    return PageResponse(
        items=[plan_response(item).model_dump(by_alias=True, mode="json") for item in page.items],
        total=page.total,
        offset=page.offset,
        limit=page.limit,
    )


@router.get("/plans/{plan_id}", response_model=PlanResponse)
async def get_plan(request: Request, plan_id: str) -> PlanResponse:
    try:
        app = container(request)
        plan = await app.gateway.get_plan(plan_id, connection=app.profile_service.connection())
    except PlanNotFoundError as exc:
        raise NotFoundError("PLAN_NOT_FOUND", str(exc)) from exc
    except AgentLoopGatewayError as exc:
        raise ServiceUnavailableError(exc.code, str(exc), retryable=exc.retryable) from exc
    return plan_response(plan)


@router.post(
    "/plans/{plan_id}/launch",
    response_model=LaunchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def launch_plan(
    request: Request,
    plan_id: str,
    payload: LaunchRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=256),
) -> LaunchResponse:
    app = container(request)
    launch = await app.launch_service.manual(
        plan_id,
        idempotency_key,
        agent_connection_id=payload.agent_connection_id,
        version_mode=payload.version_mode,
        experiment_version=payload.experiment_version,
        experiment_parameters=payload.experiment_parameters,
        record_name=payload.record_name,
        context_schema_version=payload.context_schema_version,
    )
    return launch_response(app, launch)


@router.get("/launches", response_model=PageResponse)
def list_launches(
    request: Request,
    plan_id: str | None = Query(default=None, alias="planId"),
    schedule_id: str | None = Query(default=None, alias="scheduleId"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> PageResponse:
    app = container(request)
    items = app.launch_repository.list(
        plan_id=plan_id, schedule_id=schedule_id, offset=offset, limit=limit
    )
    total = app.launch_repository.count(plan_id=plan_id, schedule_id=schedule_id)
    return PageResponse(
        items=[launch_response(app, item).model_dump(by_alias=True, mode="json") for item in items],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get("/launches/{launch_id}", response_model=LaunchResponse)
def get_launch(request: Request, launch_id: str) -> LaunchResponse:
    app = container(request)
    launch = app.launch_repository.get(launch_id)
    if launch is None:
        raise NotFoundError("LAUNCH_NOT_FOUND", f"Launch not found: {launch_id}")
    return launch_response(app, launch)


@router.get("/schedules", response_model=PageResponse)
def list_schedules(request: Request) -> PageResponse:
    items = container(request).schedule_service.list()
    return PageResponse(
        items=[schedule_response(item).model_dump(by_alias=True, mode="json") for item in items],
        total=len(items),
        offset=0,
        limit=max(len(items), 1),
    )


@router.post("/schedules", response_model=ScheduleResponse, status_code=201)
async def create_schedule(request: Request, payload: ScheduleCreate) -> ScheduleResponse:
    cadence = payload.cadence
    schedule = await container(request).schedule_service.create(
        name=payload.name,
        plan_id=payload.plan_id,
        cadence_type=cadence.type,
        timezone=payload.timezone,
        interval_hours=getattr(cadence, "interval_hours", None),
        interval_minutes=getattr(cadence, "interval_minutes", None),
        daily_time=(cadence.time.strftime("%H:%M") if cadence.type == "DAILY" else None),
        anchor_at=getattr(cadence, "anchor_at", None),
        misfire_policy=payload.misfire_policy,
        misfire_grace_seconds=payload.misfire_grace_seconds,
        enabled=payload.enabled,
        agent_connection_id=payload.agent_connection_id,
        version_mode=payload.version_mode,
        experiment_version=payload.experiment_version,
        experiment_parameters=payload.experiment_parameters,
        record_name=payload.record_name,
        context_schema_version=payload.context_schema_version,
    )
    return schedule_response(schedule)


@router.get("/schedules/{schedule_id}", response_model=ScheduleResponse)
def get_schedule(request: Request, schedule_id: str) -> ScheduleResponse:
    return schedule_response(container(request).schedule_service.get(schedule_id))


@router.patch("/schedules/{schedule_id}", response_model=ScheduleResponse)
def update_schedule(
    request: Request,
    schedule_id: str,
    payload: ScheduleUpdate,
    expected_revision: int = Query(alias="expectedRevision", ge=1),
) -> ScheduleResponse:
    service = container(request).schedule_service
    changes: dict[str, object] = {}
    if "name" in payload.model_fields_set:
        changes["name"] = payload.name
    if "timezone" in payload.model_fields_set:
        changes["timezone"] = payload.timezone
    if "misfire_policy" in payload.model_fields_set:
        changes["misfire_policy"] = payload.misfire_policy
    if "misfire_grace_seconds" in payload.model_fields_set:
        changes["misfire_grace_seconds"] = payload.misfire_grace_seconds
    if "agent_connection_id" in payload.model_fields_set:
        changes["agent_connection_id"] = payload.agent_connection_id
    if "version_mode" in payload.model_fields_set:
        changes["version_mode"] = payload.version_mode
        if payload.version_mode == "AUTO":
            changes["manual_experiment_version"] = None
    if "experiment_version" in payload.model_fields_set:
        changes["manual_experiment_version"] = payload.experiment_version
    if "experiment_parameters" in payload.model_fields_set:
        changes["experiment_parameters_json"] = payload.experiment_parameters or {}
    if "record_name" in payload.model_fields_set:
        changes["record_name"] = payload.record_name
    if "cadence" in payload.model_fields_set and payload.cadence is not None:
        changes.update(
            {
                "cadence_type": payload.cadence.type,
                "interval_hours": getattr(payload.cadence, "interval_hours", None),
                "interval_minutes": getattr(payload.cadence, "interval_minutes", None),
                "daily_time": (
                    payload.cadence.time.strftime("%H:%M")
                    if payload.cadence.type == "DAILY"
                    else None
                ),
                "anchor_at": getattr(payload.cadence, "anchor_at", None),
            }
        )
    return schedule_response(
        service.update(schedule_id, changes, expected_revision=expected_revision)
    )


@router.delete("/schedules/{schedule_id}", status_code=204)
def delete_schedule(
    request: Request,
    schedule_id: str,
    expected_revision: int = Query(alias="expectedRevision", ge=1),
) -> Response:
    container(request).schedule_service.delete(schedule_id, expected_revision=expected_revision)
    return Response(status_code=204)


@router.post("/schedules/{schedule_id}/run-now", response_model=LaunchResponse, status_code=202)
async def run_schedule_now(
    request: Request,
    schedule_id: str,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=256),
) -> LaunchResponse:
    app = container(request)
    launch = await app.schedule_service.run_now(schedule_id, idempotency_key)
    return launch_response(app, launch)


@router.post("/schedules/{schedule_id}/enable", response_model=ScheduleResponse)
def enable_schedule(
    request: Request,
    schedule_id: str,
    expected_revision: int = Query(alias="expectedRevision", ge=1),
) -> ScheduleResponse:
    return schedule_response(
        container(request).schedule_service.set_enabled(
            schedule_id, enabled=True, expected_revision=expected_revision
        )
    )


@router.post("/schedules/{schedule_id}/disable", response_model=ScheduleResponse)
def disable_schedule(
    request: Request,
    schedule_id: str,
    expected_revision: int = Query(alias="expectedRevision", ge=1),
) -> ScheduleResponse:
    return schedule_response(
        container(request).schedule_service.set_enabled(
            schedule_id, enabled=False, expected_revision=expected_revision
        )
    )
