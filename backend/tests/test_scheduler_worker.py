from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from app.agentloop import AgentLoopConnection, FakeAgentLoopGateway, PlanDetail
from app.domain.clock import FixedClock
from app.scheduler import SchedulerWorker
from app.services import BackgroundTaskSupervisor, LaunchService
from app.storage import (
    LaunchIntentRepository,
    ProfileRepository,
    ScheduleRepository,
    in_memory_database,
)


def test_scheduler_tick_starts_one_due_slot_and_advances_next_fire() -> None:
    database = in_memory_database()
    profiles = ProfileRepository(database)
    schedules = ScheduleRepository(database)
    launches = LaunchIntentRepository(database)
    profile = profiles.create(
        display_name="test",
        region_id="cn-hangzhou",
        agent_space="space-a",
        secret_ref="profile/test/credentials",
        validation_status="VALID",
    )
    now = datetime(2026, 8, 7, 1, 0, tzinfo=UTC)
    schedule = schedules.create(
        profile_id=profile.id,
        name="hourly",
        plan_id="plan-1",
        plan_name_snapshot="Regression",
        cadence_type="INTERVAL_HOURS",
        timezone="Asia/Shanghai",
        interval_hours=1,
        anchor_at=now,
        next_fire_at_utc=now,
        misfire_policy="SKIP",
    )
    gateway = FakeAgentLoopGateway(
        plans=(
            PlanDetail(
                plan_id="plan-1",
                plan_name="Regression",
                experiment_type="OFFLINE",
                status="active",
                can_launch_locally=True,
            ),
        )
    )
    tasks = BackgroundTaskSupervisor()
    launch_service = LaunchService(
        profiles=profiles,
        launches=launches,
        gateway=gateway,
        clock=FixedClock(now),
        tasks=tasks,
        connection_provider=lambda: AgentLoopConnection(
            region_id="cn-hangzhou",
            agent_space="space-a",
            access_key_id="test-ak",
            access_key_secret="test-sk",
        ),
    )
    worker = SchedulerWorker(
        schedules=schedules,
        launches=launch_service,
        clock=FixedClock(now),
        poll_seconds=1,
        lock_path=None,
    )

    async def exercise() -> None:
        assert await worker.tick() == 1
        await asyncio.sleep(0)
        await tasks.close()

    asyncio.run(exercise())

    stored = schedules.get(schedule.id)
    assert stored is not None
    assert stored.next_fire_at_utc == now + timedelta(hours=1)
    assert stored.last_fire_at_utc == now
    history = launches.list(schedule_id=schedule.id)
    assert len(history) == 1
    assert history[0].record_id is not None
    assert gateway.created_record_count == 1


def test_scheduler_advances_a_five_minute_schedule_by_five_minutes() -> None:
    database = in_memory_database()
    profiles = ProfileRepository(database)
    schedules = ScheduleRepository(database)
    profile = profiles.create(
        display_name="test",
        region_id="cn-hangzhou",
        agent_space="space-a",
        secret_ref="profile/test/credentials",
        validation_status="VALID",
    )
    now = datetime(2026, 8, 7, 1, 0, tzinfo=UTC)
    schedule = schedules.create(
        profile_id=profile.id,
        name="five-minute failure",
        plan_id="plan-1",
        cadence_type="INTERVAL_MINUTES",
        timezone="UTC",
        interval_minutes=5,
        anchor_at=now,
        next_fire_at_utc=now,
    )

    class FailingLaunches:
        def skip_schedule(self, schedule, slot, reason):
            del schedule, slot, reason

        async def scheduled(self, schedule, slot):
            del schedule, slot
            raise RuntimeError("Agent is offline")

    worker = SchedulerWorker(
        schedules=schedules,
        launches=FailingLaunches(),  # type: ignore[arg-type]
        clock=FixedClock(now),
        poll_seconds=1,
        lock_path=None,
    )
    assert asyncio.run(worker.tick()) == 1
    stored = schedules.get(schedule.id)
    assert stored is not None
    assert stored.next_fire_at_utc == now + timedelta(minutes=5)
    database.dispose()


def test_scheduler_failure_advances_slot_instead_of_retrying_forever() -> None:
    database = in_memory_database()
    profiles = ProfileRepository(database)
    schedules = ScheduleRepository(database)
    profile = profiles.create(
        display_name="test",
        region_id="cn-hangzhou",
        agent_space="space-a",
        secret_ref="profile/test/credentials",
        validation_status="VALID",
    )
    now = datetime(2026, 8, 7, 1, 0, tzinfo=UTC)
    schedule = schedules.create(
        profile_id=profile.id,
        name="hourly failure",
        plan_id="plan-1",
        cadence_type="INTERVAL_HOURS",
        timezone="UTC",
        interval_hours=1,
        anchor_at=now,
        next_fire_at_utc=now,
    )

    class FailingLaunches:
        def skip_schedule(self, schedule, slot, reason):
            del schedule, slot, reason

        async def scheduled(self, schedule, slot):
            del schedule, slot
            raise RuntimeError("Agent is offline")

    worker = SchedulerWorker(
        schedules=schedules,
        launches=FailingLaunches(),  # type: ignore[arg-type]
        clock=FixedClock(now),
        poll_seconds=1,
        lock_path=None,
    )
    assert asyncio.run(worker.tick()) == 1
    stored = schedules.get(schedule.id)
    assert stored is not None
    assert stored.next_fire_at_utc == now + timedelta(hours=1)
    assert stored.last_error_code == "SCHEDULE_EXECUTION_FAILED"
