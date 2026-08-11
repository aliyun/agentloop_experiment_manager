from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest
from aliyun.log import LogException

from app.agentloop.models import AgentLoopConnection
from app.dashboards import (
    PlanDashboardPublishError,
    SlsPlanDashboardPublisher,
    render_plan_dashboard,
)


def _connection() -> AgentLoopConnection:
    return AgentLoopConnection(
        region_id="cn-hangzhou",
        agent_space="space-one",
        access_key_id="test-ak",
        access_key_secret="test-sk",
    )


class _AgentLoopClient:
    def __init__(self, *, project: str = "project-one", error: Exception | None = None) -> None:
        self.project = project
        self.error = error
        self.calls = []

    def get_agent_space(self, agent_space, request):
        self.calls.append((agent_space, request))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(body=SimpleNamespace(sls_project=self.project))


class _DashboardResponse:
    def __init__(self, entity) -> None:
        self.entity = copy.deepcopy(entity)

    def get_entity(self):
        return copy.deepcopy(self.entity)


class _SlsClient:
    def __init__(
        self,
        entity=None,
        *,
        get_error: Exception | None = None,
        create_error: Exception | None = None,
        conflict_entity=None,
        corrupt_final_readback: bool = False,
    ) -> None:
        self.entity = copy.deepcopy(entity)
        self.get_error = get_error
        self.create_error = create_error
        self.conflict_entity = copy.deepcopy(conflict_entity)
        self.corrupt_final_readback = corrupt_final_readback
        self.get_calls = []
        self.create_calls = []
        self.update_calls = []
        self._write_completed = False

    def get_dashboard(self, project, dashboard_name):
        self.get_calls.append((project, dashboard_name))
        if self.get_error is not None:
            raise self.get_error
        if self.entity is None:
            raise LogException(
                "DashboardNotExist",
                "dashboard is absent",
                "req-not-found",
                resp_status=404,
            )
        entity = copy.deepcopy(self.entity)
        if self._write_completed and self.corrupt_final_readback:
            entity["attribute"]["experimentPlanId"] = "another-plan"
        return _DashboardResponse(entity)

    def create_dashboard(self, project, body):
        self.create_calls.append((project, copy.deepcopy(body)))
        if self.create_error is not None:
            if self.conflict_entity is not None:
                self.entity = copy.deepcopy(self.conflict_entity)
            raise self.create_error
        self.entity = copy.deepcopy(body)
        self._write_completed = True

    def update_dashboard(self, project, body):
        self.update_calls.append((project, copy.deepcopy(body)))
        self.entity = copy.deepcopy(body)
        self._write_completed = True


def _publisher(agentloop: _AgentLoopClient, sls: _SlsClient) -> SlsPlanDashboardPublisher:
    return SlsPlanDashboardPublisher(
        user_agent="test-session/1",
        agentloop_client_factory=lambda connection: agentloop,
        sls_client_factory=lambda connection: sls,
    )


def _render(*, plan_name: str = "Plan One") -> dict:
    return render_plan_dashboard(
        project="project-one",
        agent_space="space-one",
        plan_id="plan-one",
        plan_name=plan_name,
    ).body


def test_describe_has_one_stable_resource_key_per_plan() -> None:
    publisher = SlsPlanDashboardPublisher()

    original = publisher.describe(
        agent_space="space-one",
        plan_id="plan-one",
        plan_name="Original name",
    )
    renamed = publisher.describe(
        agent_space="space-one",
        plan_id="plan-one",
        plan_name="Renamed Plan",
    )
    another_plan = publisher.describe(
        agent_space="space-one",
        plan_id="plan-two",
        plan_name="Original name",
    )

    assert original.dashboard_name == renamed.dashboard_name
    assert original.template_version == renamed.template_version
    assert original.template_sha256 == renamed.template_sha256
    assert original.display_name != renamed.display_name
    assert original.dashboard_name != another_plan.dashboard_name


def test_publish_creates_after_404_and_reads_back_from_bound_project() -> None:
    agentloop = _AgentLoopClient()
    sls = _SlsClient()

    result = asyncio.run(
        _publisher(agentloop, sls).publish(
            connection=_connection(),
            plan_id="plan-one",
            plan_name="Plan One",
        )
    )

    assert result.action == "CREATED"
    assert result.sls_project == "project-one"
    assert result.project == "project-one"
    assert len(agentloop.calls) == 1
    assert agentloop.calls[0][0] == "space-one"
    assert sls.get_calls == [
        ("project-one", result.dashboard_name),
        ("project-one", result.dashboard_name),
    ]
    assert len(sls.create_calls) == 1
    assert sls.create_calls[0][0] == "project-one"
    assert sls.create_calls[0][1]["attribute"]["experimentPlanId"] == "plan-one"
    assert sls.update_calls == []


def test_publish_updates_owned_dashboard_then_reads_back() -> None:
    agentloop = _AgentLoopClient()
    sls = _SlsClient(_render(plan_name="Old Plan Name"))

    result = asyncio.run(
        _publisher(agentloop, sls).publish(
            connection=_connection(),
            plan_id="plan-one",
            plan_name="Current Plan Name",
        )
    )

    assert result.action == "UPDATED"
    assert sls.create_calls == []
    assert len(sls.update_calls) == 1
    assert sls.update_calls[0][1]["displayName"] == "实验计划 · Current Plan Name"
    assert len(sls.get_calls) == 2


def test_publish_resolves_create_race_with_get_update_and_readback() -> None:
    agentloop = _AgentLoopClient()
    conflict = LogException(
        "DashboardAlreadyExist",
        "another launcher won the race",
        "req-conflict",
        resp_status=409,
    )
    sls = _SlsClient(
        create_error=conflict,
        conflict_entity=_render(plan_name="Concurrent name"),
    )

    result = asyncio.run(
        _publisher(agentloop, sls).publish(
            connection=_connection(),
            plan_id="plan-one",
            plan_name="Current Plan Name",
        )
    )

    assert result.action == "UPDATED"
    assert len(sls.create_calls) == 1
    assert len(sls.update_calls) == 1
    assert len(sls.get_calls) == 3


def test_project_not_found_does_not_attempt_dashboard_create() -> None:
    agentloop = _AgentLoopClient()
    sls = _SlsClient(
        get_error=LogException(
            "ProjectNotExist",
            "project is absent",
            "req-project",
            resp_status=404,
        )
    )

    with pytest.raises(PlanDashboardPublishError) as raised:
        asyncio.run(
            _publisher(agentloop, sls).publish(
                connection=_connection(),
                plan_id="plan-one",
                plan_name="Plan One",
            )
        )

    assert raised.value.operation == "GetDashboard"
    assert raised.value.code == "ProjectNotExist"
    assert raised.value.request_id == "req-project"
    assert sls.create_calls == []


def test_existing_unmanaged_resource_is_never_overwritten() -> None:
    body = _render()
    body["attribute"]["managedBy"] = "someone-else"
    sls = _SlsClient(body)

    with pytest.raises(PlanDashboardPublishError) as raised:
        asyncio.run(
            _publisher(_AgentLoopClient(), sls).publish(
                connection=_connection(),
                plan_id="plan-one",
                plan_name="Plan One",
            )
        )

    assert raised.value.code == "DashboardOwnershipMismatch"
    assert sls.create_calls == []
    assert sls.update_calls == []


def test_cloud_error_does_not_expose_message_credentials_or_payload() -> None:
    secret = "test-sk"
    error = LogException(
        "InternalError",
        f"authorization failed for {secret}; input={{customer payload}}",
        "req-safe-1",
        resp_status=500,
    )
    publisher = _publisher(_AgentLoopClient(error=error), _SlsClient())

    with pytest.raises(PlanDashboardPublishError) as raised:
        asyncio.run(
            publisher.publish(
                connection=_connection(),
                plan_id="plan-one",
                plan_name="Plan One",
            )
        )

    rendered = str(raised.value)
    assert rendered == "GetAgentSpace failed [InternalError]"
    assert secret not in rendered
    assert "customer payload" not in rendered
    assert raised.value.request_id == "req-safe-1"


def test_final_readback_must_match_plan_scoped_template() -> None:
    sls = _SlsClient(corrupt_final_readback=True)

    with pytest.raises(PlanDashboardPublishError) as raised:
        asyncio.run(
            _publisher(_AgentLoopClient(), sls).publish(
                connection=_connection(),
                plan_id="plan-one",
                plan_name="Plan One",
            )
        )

    assert raised.value.operation == "ReadbackDashboard"
    assert raised.value.code == "DashboardMismatch"
    assert len(sls.create_calls) == 1
    assert len(sls.get_calls) == 2
