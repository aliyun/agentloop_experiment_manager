from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.agentloop import AgentLoopConnection, FakeAgentLoopGateway, PlanDetail
from app.dashboards import (
    PlanDashboardDescription,
    PlanDashboardPublication,
    PlanDashboardPublishError,
    dashboard_name,
    display_name,
    render_plan_dashboard,
)
from app.domain.clock import FixedClock
from app.services import BackgroundTaskSupervisor, LaunchService, PlanDashboardService
from app.storage import (
    LaunchIntentRepository,
    PlanDashboardRepository,
    ProfileRepository,
    in_memory_database,
)


class _Publisher:
    def __init__(self, *, failure: PlanDashboardPublishError | None = None) -> None:
        self.failure = failure
        self.publish_count = 0

    def describe(
        self,
        *,
        agent_space: str,
        plan_id: str,
        plan_name: str,
    ) -> PlanDashboardDescription:
        rendered = render_plan_dashboard(
            project="description-only",
            agent_space=agent_space,
            plan_id=plan_id,
            plan_name=plan_name,
        )
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
        self.publish_count += 1
        if self.failure is not None:
            raise self.failure
        rendered = render_plan_dashboard(
            project="agentloop-project",
            agent_space=connection.agent_space,
            plan_id=plan_id,
            plan_name=plan_name,
        )
        return PlanDashboardPublication(
            sls_project=rendered.project,
            dashboard_name=rendered.dashboard_name,
            display_name=rendered.display_name,
            template_version=rendered.template_version,
            template_sha256=rendered.template_sha256,
            action="UPDATED",
        )


def _setup(publisher: _Publisher):
    database = in_memory_database()
    profiles = ProfileRepository(database)
    profile = profiles.create(
        profile_id="profile-1",
        display_name="test",
        region_id="cn-hangzhou",
        agent_space="space-a",
        secret_ref="secret/profile",
        validation_status="VALID",
        max_concurrency=2,
    )
    plan = PlanDetail(
        plan_id="plan-1",
        plan_name="Regression",
        experiment_type="OFFLINE",
        status="active",
        dataset_id="dataset-1",
        evaluator_count=1,
        can_launch_locally=True,
    )
    gateway = FakeAgentLoopGateway(plans=(plan,))
    connection = AgentLoopConnection(
        region_id=profile.region_id,
        agent_space=profile.agent_space,
        access_key_id="test-ak",
        access_key_secret="test-sk",
    )
    dashboard_repository = PlanDashboardRepository(database)
    dashboard_service = PlanDashboardService(
        dashboards=dashboard_repository,
        publisher=publisher,
        clock=FixedClock(datetime(2026, 8, 9, 1, 2, 3, tzinfo=UTC)),
    )
    tasks = BackgroundTaskSupervisor()
    launch_service = LaunchService(
        profiles=profiles,
        launches=LaunchIntentRepository(database),
        gateway=gateway,
        clock=FixedClock(datetime(2026, 8, 9, 1, 2, 3, tzinfo=UTC)),
        tasks=tasks,
        connection_provider=lambda: connection,
        plan_dashboards=dashboard_service,
    )
    return (
        database,
        profile,
        gateway,
        tasks,
        launch_service,
        dashboard_repository,
    )


def test_each_logical_launch_updates_the_same_plan_dashboard() -> None:
    async def scenario() -> None:
        publisher = _Publisher()
        database, profile, gateway, tasks, launches, dashboards = _setup(publisher)
        first = await launches.manual("plan-1", "request-1")
        replay = await launches.manual("plan-1", "request-1")
        await asyncio.sleep(0)
        second = await launches.manual("plan-1", "request-2")

        assert replay.id == first.id
        assert second.id != first.id
        assert gateway.created_record_count == 2
        assert publisher.publish_count == 2
        stored = dashboards.get(profile.id, "plan-1")
        assert stored is not None
        assert stored.dashboard_name == dashboard_name("space-a", "plan-1")
        assert stored.display_name == display_name("Regression", "plan-1")
        assert stored.sync_state == "READY"

        await tasks.close()
        database.dispose()

    asyncio.run(scenario())


def test_dashboard_failure_is_recorded_but_experiment_still_starts() -> None:
    async def scenario() -> None:
        publisher = _Publisher(
            failure=PlanDashboardPublishError("UpdateDashboard", "Throttling")
        )
        database, profile, gateway, tasks, launches, dashboards = _setup(publisher)
        launch = await launches.manual("plan-1", "request-failed-dashboard")

        assert launch.submission_state == "ACCEPTED"
        assert gateway.created_record_count == 1
        stored = dashboards.get(profile.id, "plan-1")
        assert stored is not None
        assert stored.sync_state == "FAILED"
        assert stored.last_error_code == "Throttling"

        await tasks.close()
        database.dispose()

    asyncio.run(scenario())
