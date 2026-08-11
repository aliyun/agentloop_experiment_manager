"""Best-effort reconciliation of one cloud dashboard per Experiment Plan."""

from __future__ import annotations

import asyncio

from app.agentloop import AgentLoopConnection, PlanDetail
from app.dashboards.publisher import (
    PlanDashboardPublisher,
    PlanDashboardPublishError,
)
from app.domain.clock import Clock
from app.storage import PlanDashboard, PlanDashboardRepository


class PlanDashboardService:
    """Keep a stable dashboard in sync without owning Experiment Run state.

    The identity is deliberately scoped to ``profile + Experiment Plan``.  A
    Plan can have any number of manual or scheduled Runs; every one reconciles
    the same dashboard so template upgrades and externally deleted dashboards
    self-heal on the next launch.
    """

    def __init__(
        self,
        *,
        dashboards: PlanDashboardRepository,
        publisher: PlanDashboardPublisher,
        clock: Clock,
    ) -> None:
        self.dashboards = dashboards
        self.publisher = publisher
        self.clock = clock
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def ensure_dashboard(
        self,
        *,
        profile_id: str,
        plan: PlanDetail,
        connection: AgentLoopConnection,
    ) -> PlanDashboard:
        key = (profile_id, plan.plan_id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            description = self.publisher.describe(
                agent_space=connection.agent_space,
                plan_id=plan.plan_id,
                plan_name=plan.plan_name,
            )
            self.dashboards.begin_sync(
                profile_id=profile_id,
                plan_id=plan.plan_id,
                plan_name_snapshot=plan.plan_name,
                agent_space_snapshot=connection.agent_space,
                dashboard_name=description.dashboard_name,
                display_name=description.display_name,
                template_version=description.template_version,
                template_sha256=description.template_sha256,
                attempted_at=self.clock.now(),
            )
            try:
                publication = await self.publisher.publish(
                    connection=connection,
                    plan_id=plan.plan_id,
                    plan_name=plan.plan_name,
                )
            except PlanDashboardPublishError as exc:
                return self.dashboards.mark_failed(
                    profile_id=profile_id,
                    plan_id=plan.plan_id,
                    error_code=exc.code,
                    failed_at=self.clock.now(),
                )
            except Exception:
                # Do not persist raw SDK messages: they can contain endpoint,
                # request, or customer data.  Dashboard publication must also
                # never turn an otherwise valid Experiment launch into a
                # failure.
                return self.dashboards.mark_failed(
                    profile_id=profile_id,
                    plan_id=plan.plan_id,
                    error_code="PLAN_DASHBOARD_SYNC_FAILED",
                    failed_at=self.clock.now(),
                )
            return self.dashboards.mark_ready(
                profile_id=profile_id,
                plan_id=plan.plan_id,
                sls_project=publication.sls_project,
                dashboard_name=publication.dashboard_name,
                display_name=publication.display_name,
                template_version=publication.template_version,
                template_sha256=publication.template_sha256,
                synced_at=self.clock.now(),
            )
