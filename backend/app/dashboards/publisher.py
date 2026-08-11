"""Publish one managed SLS dashboard for each AgentLoop Experiment Plan.

The publisher deliberately owns only the AgentSpace-to-SLS lookup and the
idempotent SLS resource upsert.  Launch scheduling, persistence, and retry
policy remain outside this module.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from alibabacloud_agentloop20260520 import models as api_models
from alibabacloud_agentloop20260520.client import Client
from alibabacloud_tea_openapi import models as open_api_models

from app.agentloop.models import AgentLoopConnection

from .template import render_plan_dashboard, validate_rendered_dashboard

_DEFAULT_USER_AGENT = "agentloop-local-experiment-launcher/0.1.0"
_DESCRIPTION_PROJECT = "agentloop-plan-dashboard-description"


@dataclass(frozen=True, slots=True)
class PlanDashboardDescription:
    """Stable local identity available before any cloud request is made."""

    dashboard_name: str
    display_name: str
    template_version: str
    template_sha256: str


@dataclass(frozen=True, slots=True)
class PlanDashboardPublication:
    """Verified result of an SLS dashboard create or update."""

    sls_project: str
    dashboard_name: str
    display_name: str
    template_version: str
    template_sha256: str
    action: str

    @property
    def project(self) -> str:
        """Compatibility alias for callers that use the SLS API term."""

        return self.sls_project


class PlanDashboardPublishError(RuntimeError):
    """A credential- and payload-free dashboard publication failure."""

    def __init__(
        self,
        operation: str,
        code: str,
        *,
        request_id: str | None = None,
    ) -> None:
        safe_operation = (
            operation if _is_safe_token(operation, max_length=80) else "PublishDashboard"
        )
        safe_code = code if _is_safe_token(code, max_length=80) else "CloudRequestFailed"
        safe_request_id = (
            request_id if _is_safe_token(request_id, max_length=128) else None
        )
        super().__init__(f"{safe_operation} failed [{safe_code}]")
        self.operation = safe_operation
        self.code = safe_code
        self.request_id = safe_request_id


class PlanDashboardPublisher(Protocol):
    """Cloud publisher contract consumed by the launch orchestration layer."""

    def describe(
        self,
        *,
        agent_space: str,
        plan_id: str,
        plan_name: str,
    ) -> PlanDashboardDescription: ...

    async def publish(
        self,
        *,
        connection: AgentLoopConnection,
        plan_id: str,
        plan_name: str,
    ) -> PlanDashboardPublication: ...


class SlsPlanDashboardPublisher:
    """Create or update the Plan-scoped dashboard in the AgentSpace SLS project."""

    def __init__(
        self,
        *,
        user_agent: str = _DEFAULT_USER_AGENT,
        agentloop_client_factory: Callable[[AgentLoopConnection], Any] | None = None,
        sls_client_factory: Callable[[AgentLoopConnection], Any] | None = None,
    ) -> None:
        self.user_agent = user_agent
        self._agentloop_client_factory = agentloop_client_factory or self._new_agentloop_client
        self._sls_client_factory = sls_client_factory or self._new_sls_client

    def describe(
        self,
        *,
        agent_space: str,
        plan_id: str,
        plan_name: str,
    ) -> PlanDashboardDescription:
        """Return the immutable Plan key and current template identity locally."""

        try:
            rendered = render_plan_dashboard(
                project=_DESCRIPTION_PROJECT,
                agent_space=agent_space,
                plan_id=plan_id,
                plan_name=plan_name,
            )
        except (OSError, TypeError, ValueError) as exc:
            raise PlanDashboardPublishError(
                "RenderDashboard",
                _render_error_code(exc),
            ) from None
        return PlanDashboardDescription(
            dashboard_name=rendered.dashboard_name,
            display_name=rendered.display_name,
            template_version=rendered.template_version,
            template_sha256=rendered.template_sha256,
        )

    async def publish(
        self,
        *,
        connection: AgentLoopConnection,
        plan_id: str,
        plan_name: str,
    ) -> PlanDashboardPublication:
        """Upsert and read back one dashboard without blocking the event loop."""

        return await asyncio.to_thread(
            self._publish,
            connection=connection,
            plan_id=plan_id,
            plan_name=plan_name,
        )

    def _publish(
        self,
        *,
        connection: AgentLoopConnection,
        plan_id: str,
        plan_name: str,
    ) -> PlanDashboardPublication:
        project = self._resolve_sls_project(connection)
        try:
            rendered = render_plan_dashboard(
                project=project,
                agent_space=connection.agent_space,
                plan_id=plan_id,
                plan_name=plan_name,
            )
        except (OSError, TypeError, ValueError) as exc:
            raise PlanDashboardPublishError(
                "RenderDashboard",
                _render_error_code(exc),
            ) from None

        try:
            client = self._sls_client_factory(connection)
        except Exception as exc:
            raise _cloud_error("CreateSlsClient", exc) from None

        action = self._upsert(
            client,
            project=project,
            body=rendered.body,
            dashboard_name=rendered.dashboard_name,
            agent_space=connection.agent_space,
            plan_id=plan_id,
        )
        readback = self._get_dashboard(
            client,
            project=project,
            dashboard_name=rendered.dashboard_name,
            operation="ReadbackDashboard",
        )
        try:
            _validate_readback(
                readback,
                expected_body=rendered.body,
                project=project,
                logstore=rendered.logstore,
                plan_id=plan_id,
                template_version=rendered.template_version,
                template_sha256=rendered.template_sha256,
            )
        except (TypeError, ValueError):
            raise PlanDashboardPublishError(
                "ReadbackDashboard",
                "DashboardMismatch",
            ) from None

        return PlanDashboardPublication(
            sls_project=project,
            dashboard_name=rendered.dashboard_name,
            display_name=rendered.display_name,
            template_version=rendered.template_version,
            template_sha256=rendered.template_sha256,
            action=action,
        )

    def _resolve_sls_project(self, connection: AgentLoopConnection) -> str:
        try:
            client = self._agentloop_client_factory(connection)
            response = client.get_agent_space(
                connection.agent_space,
                api_models.GetAgentSpaceRequest(),
            )
        except Exception as exc:
            raise _cloud_error("GetAgentSpace", exc) from None
        body = getattr(response, "body", None)
        project = _field(body, "sls_project", "slsProject")
        if not project:
            raise PlanDashboardPublishError(
                "GetAgentSpace",
                "SlsProjectMissing",
            )
        return project

    def _upsert(
        self,
        client: Any,
        *,
        project: str,
        body: dict[str, Any],
        dashboard_name: str,
        agent_space: str,
        plan_id: str,
    ) -> str:
        try:
            response = client.get_dashboard(project, dashboard_name)
        except Exception as exc:
            if not _is_dashboard_not_found(exc):
                raise _cloud_error("GetDashboard", exc) from None
        else:
            existing = _response_entity(response, operation="GetDashboard")
            _validate_owned_resource(
                existing,
                dashboard_name=dashboard_name,
                agent_space=agent_space,
                plan_id=plan_id,
            )
            self._update_dashboard(client, project=project, body=body)
            return "UPDATED"

        try:
            client.create_dashboard(project, copy.deepcopy(body))
            return "CREATED"
        except Exception as exc:
            if not _is_already_exists(exc):
                raise _cloud_error("CreateDashboard", exc) from None

        # A concurrent launcher may have created the deterministic Plan resource
        # after our initial 404.  Prove ownership before applying our template.
        existing = self._get_dashboard(
            client,
            project=project,
            dashboard_name=dashboard_name,
            operation="GetDashboardAfterConflict",
        )
        _validate_owned_resource(
            existing,
            dashboard_name=dashboard_name,
            agent_space=agent_space,
            plan_id=plan_id,
        )
        self._update_dashboard(client, project=project, body=body)
        return "UPDATED"

    @staticmethod
    def _get_dashboard(
        client: Any,
        *,
        project: str,
        dashboard_name: str,
        operation: str,
    ) -> dict[str, Any]:
        try:
            response = client.get_dashboard(project, dashboard_name)
            return _response_entity(response, operation=operation)
        except PlanDashboardPublishError:
            raise
        except Exception as exc:
            raise _cloud_error(operation, exc) from None

    @staticmethod
    def _update_dashboard(client: Any, *, project: str, body: dict[str, Any]) -> None:
        try:
            client.update_dashboard(project, copy.deepcopy(body))
        except Exception as exc:
            raise _cloud_error("UpdateDashboard", exc) from None

    def _new_agentloop_client(self, connection: AgentLoopConnection) -> Client:
        config = open_api_models.Config(
            access_key_id=connection.access_key_id,
            access_key_secret=connection.access_key_secret,
            region_id=connection.region_id,
            endpoint=connection.api_endpoint
            or f"agentloop.{connection.region_id}.aliyuncs.com",
            connect_timeout=10_000,
            read_timeout=30_000,
            user_agent=self.user_agent,
        )
        return Client(config)

    def _new_sls_client(self, connection: AgentLoopConnection) -> Any:
        from aliyun.log import LogClient

        client = LogClient(
            f"{connection.region_id}.log.aliyuncs.com",
            connection.access_key_id,
            connection.access_key_secret,
            region=connection.region_id,
        )
        client.set_user_agent(self.user_agent)
        return client


def _response_entity(response: Any, *, operation: str) -> dict[str, Any]:
    if hasattr(response, "get_entity"):
        entity = response.get_entity()
    elif isinstance(response, Mapping):
        entity = response
    else:
        entity = getattr(response, "body", None)
    if not isinstance(entity, Mapping):
        raise PlanDashboardPublishError(operation, "MalformedResponse")
    return dict(entity)


def _validate_owned_resource(
    body: dict[str, Any],
    *,
    dashboard_name: str,
    agent_space: str,
    plan_id: str,
) -> None:
    attributes = body.get("attribute")
    owned = (
        body.get("dashboardName") == dashboard_name
        and isinstance(attributes, Mapping)
        and attributes.get("managedBy") == "agentloop-local-experiment-launcher"
        and attributes.get("agentSpace") == agent_space
        and attributes.get("experimentPlanId") == plan_id
    )
    if not owned:
        raise PlanDashboardPublishError("GetDashboard", "DashboardOwnershipMismatch")


def _validate_readback(
    body: dict[str, Any],
    *,
    expected_body: dict[str, Any],
    project: str,
    logstore: str,
    plan_id: str,
    template_version: str,
    template_sha256: str,
) -> None:
    if body.get("dashboardName") != expected_body["dashboardName"]:
        raise ValueError("dashboard name does not match")
    if body.get("displayName") != expected_body["displayName"]:
        raise ValueError("dashboard display name does not match")
    expected_attributes = expected_body["attribute"]
    attributes = body.get("attribute")
    if not isinstance(attributes, Mapping):
        raise ValueError("dashboard attributes are absent")
    if attributes.get("agentSpace") != expected_attributes["agentSpace"]:
        raise ValueError("dashboard AgentSpace does not match")
    validate_rendered_dashboard(
        body,
        project=project,
        logstore=logstore,
        plan_id=plan_id,
        template_version=template_version,
        template_sha256=template_sha256,
    )


def _field(body: Any, snake_case: str, camel_case: str) -> str:
    if isinstance(body, Mapping):
        value = body.get(snake_case) or body.get(camel_case)
    else:
        value = getattr(body, snake_case, None)
    return str(value or "").strip()


def _cloud_error(operation: str, exc: Exception) -> PlanDashboardPublishError:
    return PlanDashboardPublishError(
        operation,
        _error_code(exc) or "CloudRequestFailed",
        request_id=_request_id(exc),
    )


def _error_code(exc: Exception) -> str | None:
    value = getattr(exc, "code", None)
    if value is None and hasattr(exc, "get_error_code"):
        value = exc.get_error_code()
    candidate = str(value or "")
    return candidate if _is_safe_token(candidate, max_length=80) else None


def _request_id(exc: Exception) -> str | None:
    value = getattr(exc, "request_id", None)
    if value is None and hasattr(exc, "get_request_id"):
        value = exc.get_request_id()
    if value is None and isinstance(getattr(exc, "data", None), Mapping):
        value = exc.data.get("RequestId") or exc.data.get("requestId")
    candidate = str(value or "")
    return candidate if _is_safe_token(candidate, max_length=128) else None


def _status_code(exc: Exception) -> int | None:
    value = getattr(exc, "status_code", None)
    if value is None and hasattr(exc, "get_resp_status"):
        value = exc.get_resp_status()
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _is_dashboard_not_found(exc: Exception) -> bool:
    code = (_error_code(exc) or "").casefold().replace("_", "")
    if "project" in code or "logstore" in code:
        return False
    return (
        code in {"dashboardnotexist", "resourcenotexist", "resourcenotfound", "notfound"}
        or ("dashboard" in code and ("notexist" in code or "notfound" in code))
        or _status_code(exc) == 404
    )


def _is_already_exists(exc: Exception) -> bool:
    code = (_error_code(exc) or "").casefold().replace("_", "")
    return _status_code(exc) == 409 or "alreadyexist" in code or "conflict" in code


def _render_error_code(exc: Exception) -> str:
    if isinstance(exc, OSError):
        return "TemplateUnavailable"
    return "InvalidDashboardInput"


def _is_safe_token(value: Any, *, max_length: int) -> bool:
    candidate = str(value or "")
    return (
        bool(candidate)
        and len(candidate) <= max_length
        and all(
            character.isascii() and (character.isalnum() or character in "._-")
            for character in candidate
        )
    )
