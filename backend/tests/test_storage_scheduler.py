from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect, text

from app.scheduler import (
    ScheduleTiming,
    evaluate_misfires,
    evaluate_overlap,
    fire_times_between,
    next_daily_fire,
    next_fire,
    schedule_slot_idempotency_key,
)
from app.storage import (
    CURRENT_SCHEMA_VERSION,
    CadenceType,
    ConcurrentUpdateError,
    Database,
    IdempotencyConflictError,
    LaunchIntentRepository,
    MisfirePolicy,
    ProfileRepository,
    RunnerState,
    ScheduleRepository,
    SubmissionState,
    TriggerSource,
)


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "launcher-data" / "launcher.sqlite3"
    database = Database.from_path(path)
    database.initialize()
    try:
        yield database
    finally:
        database.dispose()


@pytest.fixture
def profile(database):
    return ProfileRepository(database).create(
        profile_id="profile-1",
        display_name="Demo workspace",
        region_id="cn-hangzhou",
        agent_space="demo-space",
        secret_ref="env://AGENTLOOP_PROFILE_DEMO",
        access_key_id_hint="LTAI****ABCD",
    )


def test_sqlite_pragmas_schema_and_permissions(database):
    with database.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one().lower() == "wal"
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000

    assert database.file_path is not None
    assert os.stat(database.file_path.parent).st_mode & 0o777 == 0o700
    assert os.stat(database.file_path).st_mode & 0o777 == 0o600

    schema = inspect(database.engine)
    assert set(schema.get_table_names()) == {
        "agent_connections",
        "agentloop_profile",
        "launch_intents",
        "plan_dashboards",
        "schedules",
        "schema_version",
        "trace_correlations",
    }
    profile_columns = {column["name"] for column in schema.get_columns("agentloop_profile")}
    assert "secret_ref" in profile_columns
    assert "access_key_id_hint" in profile_columns
    assert "access_key_id" not in profile_columns
    assert "access_key_secret" not in profile_columns
    assert "secret_key" not in profile_columns

    trace_columns = {column["name"] for column in schema.get_columns("trace_correlations")}
    assert {
        "launch_id",
        "dataset_item_id",
        "experiment_run_id",
        "state",
        "trace_id",
        "attempts",
        "sanitized_error",
    } <= trace_columns
    assert {
        "access_key_id",
        "access_key_secret",
        "input",
        "output",
        "trace_body",
    }.isdisjoint(trace_columns)

    slot_indexes = {index["name"]: index for index in schema.get_indexes("launch_intents")}
    assert slot_indexes["uq_schedule_slot"]["unique"] == 1


def test_schema_v3_to_v4_preserves_hourly_schedule_and_launch_foreign_key(tmp_path):
    path = tmp_path / "migration" / "launcher.sqlite3"
    database = Database.from_path(path)
    database.initialize()
    profile = ProfileRepository(database).create(
        profile_id="profile-migration",
        display_name="Migration",
        region_id="cn-hangzhou",
        agent_space="space-a",
        secret_ref="secret/migration",
    )
    schedule = ScheduleRepository(database).create(
        schedule_id="schedule-hourly-migration",
        profile_id=profile.id,
        name="legacy hourly",
        plan_id="plan-1",
        cadence_type=CadenceType.INTERVAL_HOURS.value,
        interval_hours=1,
        anchor_at=datetime(2026, 8, 7, 0, 0, tzinfo=UTC),
        timezone="UTC",
    )
    LaunchIntentRepository(database).create_or_get(
        launch_id="launch-migration",
        profile_id=profile.id,
        plan_id="plan-1",
        trigger_source=TriggerSource.SCHEDULE.value,
        schedule_id=schedule.id,
        schedule_revision=schedule.revision,
        scheduled_for_utc=datetime(2026, 8, 7, 1, 0, tzinfo=UTC),
        idempotency_key="schedule:migration",
    )
    with database.engine.begin() as connection:
        connection.execute(text("UPDATE schema_version SET version = 3 WHERE id = 1"))
    database.dispose()

    migrated = Database.from_path(path)
    migrated.initialize()
    stored = ScheduleRepository(migrated).get(schedule.id)
    assert stored is not None
    assert stored.cadence_type == CadenceType.INTERVAL_HOURS.value
    assert stored.interval_hours == 1
    assert stored.interval_minutes is None
    with migrated.engine.connect() as connection:
        assert (
            connection.execute(text("SELECT version FROM schema_version")).scalar_one()
            == CURRENT_SCHEMA_VERSION
        )
        assert list(connection.exec_driver_sql("PRAGMA foreign_key_check")) == []
        launch_schedule_id = connection.execute(
            text("SELECT schedule_id FROM launch_intents WHERE id = 'launch-migration'")
        ).scalar_one()
        assert launch_schedule_id == schedule.id
    migrated.dispose()


def test_profile_never_accepts_credential_secret(database, profile):
    repository = ProfileRepository(database)
    with pytest.raises(ValueError, match="unsupported profile fields"):
        repository.update(profile.id, access_key_secret="must-not-be-stored")


def test_schedule_revision_optimistic_update_and_soft_delete(database, profile):
    repository = ScheduleRepository(database)
    schedule = repository.create(
        schedule_id="schedule-daily",
        profile_id=profile.id,
        name="daily regression",
        plan_id="plan-1",
        cadence_type=CadenceType.DAILY.value,
        daily_time="09:00",
        timezone="Asia/Shanghai",
    )
    assert schedule.revision == 1
    assert schedule.enabled == 1

    changed = repository.update_config(schedule.id, expected_revision=1, daily_time="10:30")
    assert changed.revision == 2
    assert changed.daily_time == "10:30"

    next_slot = datetime(2026, 8, 8, 2, 30, tzinfo=UTC)
    rescheduled = repository.set_next_fire(schedule.id, next_slot, expected_revision=2)
    assert rescheduled.next_fire_at_utc == next_slot
    assert rescheduled.last_fire_at_utc is None
    assert rescheduled.revision == 2

    with pytest.raises(ConcurrentUpdateError):
        repository.update_config(schedule.id, expected_revision=1, daily_time="11:00")

    with pytest.raises(ConcurrentUpdateError):
        repository.set_next_fire(schedule.id, next_slot, expected_revision=1)

    disabled = repository.set_enabled(schedule.id, enabled=False, expected_revision=2)
    assert disabled.revision == 3
    assert disabled.enabled == 0

    deleted = repository.soft_delete(schedule.id, expected_revision=3)
    assert deleted.revision == 4
    assert deleted.enabled == 0
    assert deleted.deleted_at is not None
    assert repository.get(schedule.id) is None
    assert repository.get(schedule.id, include_deleted=True) is not None
    assert repository.list() == []


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {
                "cadence_type": CadenceType.DAILY.value,
                "daily_time": "25:00",
                "timezone": "Asia/Shanghai",
            },
            "daily_time",
        ),
        (
            {
                "cadence_type": CadenceType.DAILY.value,
                "daily_time": "09:00",
                "timezone": "Mars/Olympus",
            },
            "IANA timezone",
        ),
        (
            {
                "cadence_type": CadenceType.INTERVAL_HOURS.value,
                "interval_hours": 1,
                "timezone": "UTC",
            },
            "anchor_at",
        ),
    ],
)
def test_schedule_validation(database, profile, kwargs, message):
    with pytest.raises(ValueError, match=message):
        ScheduleRepository(database).create(
            profile_id=profile.id,
            name="invalid",
            plan_id="plan-1",
            **kwargs,
        )


def test_five_minute_schedule_is_the_minimum_supported_interval(database, profile):
    repository = ScheduleRepository(database)
    anchor = datetime(2026, 8, 7, 0, 0, tzinfo=UTC)
    schedule = repository.create(
        profile_id=profile.id,
        name="five-minute regression",
        plan_id="plan-1",
        cadence_type=CadenceType.INTERVAL_MINUTES.value,
        interval_minutes=5,
        anchor_at=anchor,
        timezone="UTC",
    )
    assert schedule.interval_minutes == 5
    assert schedule.interval_hours is None
    assert next_fire(
        ScheduleTiming(
            cadence_type=schedule.cadence_type,
            timezone=schedule.timezone,
            interval_minutes=schedule.interval_minutes,
            anchor_at=schedule.anchor_at,
        ),
        after_utc=anchor,
    ) == anchor + timedelta(minutes=5)

    with pytest.raises(ValueError, match="greater than or equal to 5"):
        repository.create(
            profile_id=profile.id,
            name="too frequent",
            plan_id="plan-1",
            cadence_type=CadenceType.INTERVAL_MINUTES.value,
            interval_minutes=4,
            anchor_at=anchor,
            timezone="UTC",
        )


def test_schedule_slot_is_idempotent_across_tick_retries(database, profile):
    schedules = ScheduleRepository(database)
    launches = LaunchIntentRepository(database)
    schedule = schedules.create(
        schedule_id="schedule-hourly",
        profile_id=profile.id,
        name="hourly",
        plan_id="plan-1",
        cadence_type=CadenceType.INTERVAL_HOURS.value,
        interval_hours=1,
        anchor_at=datetime(2026, 8, 7, 0, 0, tzinfo=UTC),
        timezone="Asia/Shanghai",
    )
    slot = datetime(2026, 8, 7, 1, 0, tzinfo=UTC)
    key = schedule_slot_idempotency_key(schedule.id, schedule.revision, slot)

    first, created = launches.create_or_get(
        launch_id="launch-1",
        profile_id=profile.id,
        plan_id=schedule.plan_id,
        trigger_source=TriggerSource.SCHEDULE.value,
        schedule_id=schedule.id,
        schedule_revision=schedule.revision,
        scheduled_for_utc=slot,
        idempotency_key=key,
    )
    assert created is True

    retried, created = launches.create_or_get(
        profile_id=profile.id,
        plan_id=schedule.plan_id,
        trigger_source=TriggerSource.SCHEDULE.value,
        schedule_id=schedule.id,
        schedule_revision=schedule.revision,
        scheduled_for_utc=slot,
        idempotency_key=key,
    )
    assert created is False
    assert retried.id == first.id

    # The slot uniqueness is stronger than a caller-provided key: a buggy tick
    # with a different key still reads back the one existing logical launch.
    by_slot, created = launches.create_or_get(
        profile_id=profile.id,
        plan_id=schedule.plan_id,
        trigger_source=TriggerSource.SCHEDULE.value,
        schedule_id=schedule.id,
        schedule_revision=schedule.revision,
        scheduled_for_utc=slot,
        idempotency_key="different-but-same-slot",
    )
    assert created is False
    assert by_slot.id == first.id

    with pytest.raises(IdempotencyConflictError):
        launches.create_or_get(
            profile_id=profile.id,
            plan_id="another-plan",
            trigger_source=TriggerSource.SCHEDULE.value,
            schedule_id=schedule.id,
            schedule_revision=schedule.revision,
            scheduled_for_utc=slot,
            idempotency_key=key,
        )


def test_launch_submission_and_runner_lifecycles_are_independent(database, profile):
    launches = LaunchIntentRepository(database)
    intent, _ = launches.create_or_get(
        launch_id="launch-manual",
        profile_id=profile.id,
        plan_id="plan-1",
        trigger_source=TriggerSource.MANUAL.value,
        idempotency_key="manual:request-1",
    )
    assert intent.submission_state == SubmissionState.CREATED.value
    assert intent.runner_state == RunnerState.NOT_STARTED.value

    submitting = launches.transition_submission(intent.id, SubmissionState.SUBMITTING.value)
    assert submitting.attempt_count == 1
    accepted = launches.transition_submission(
        intent.id,
        SubmissionState.ACCEPTED.value,
        record_id="record-1",
        request_id="request-1",
    )
    assert accepted.record_id == "record-1"
    assert accepted.runner_state == RunnerState.NOT_STARTED.value

    running = launches.transition_runner(intent.id, RunnerState.RUNNING.value)
    assert running.submission_state == SubmissionState.ACCEPTED.value
    assert launches.has_local_overlap("missing-schedule") is False
    handed_off = launches.transition_runner(intent.id, RunnerState.HANDED_OFF.value)
    assert handed_off.worker_finished_at is not None
    assert launches.record_ids(plan_id="plan-1") == ["record-1"]

    with pytest.raises(IdempotencyConflictError):
        launches.transition_submission(
            intent.id, SubmissionState.ACCEPTED.value, record_id="record-2"
        )


def test_restart_recovery_marks_interrupted_runner_unknown(database, profile):
    launches = LaunchIntentRepository(database)
    schedule = ScheduleRepository(database).create(
        profile_id=profile.id,
        name="restart-recovery",
        plan_id="plan-1",
        cadence_type=CadenceType.INTERVAL_HOURS.value,
        timezone="UTC",
        interval_hours=1,
        anchor_at=datetime(2026, 8, 7, 0, 0, tzinfo=UTC),
    )
    intent, _ = launches.create_or_get(
        profile_id=profile.id,
        plan_id="plan-1",
        trigger_source=TriggerSource.SCHEDULE_RUN_NOW.value,
        schedule_id=schedule.id,
        schedule_revision=schedule.revision,
        idempotency_key="manual:interrupted",
    )
    launches.transition_submission(intent.id, SubmissionState.SUBMITTING.value)
    launches.transition_runner(intent.id, RunnerState.RUNNING.value)
    assert launches.has_local_overlap(schedule.id) is True
    recovered_at = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)

    assert launches.recover_interrupted_runners(recovered_at) == 1
    recovered = launches.get(intent.id)
    assert recovered is not None
    assert recovered.runner_state == RunnerState.UNKNOWN.value
    assert recovered.worker_finished_at == recovered_at
    assert launches.active_runner_count() == 0
    assert launches.has_local_overlap(schedule.id) is False


def test_daily_dst_gap_runs_at_first_valid_time_and_fold_runs_once():
    # 2024-03-10 02:30 does not exist in New York.  The first valid local
    # instant is 03:00 EDT == 07:00 UTC.
    spring = next_daily_fire(
        daily_time="02:30",
        timezone="America/New_York",
        after_utc=datetime(2024, 3, 10, 6, 59, tzinfo=UTC),
    )
    assert spring == datetime(2024, 3, 10, 7, 0, tzinfo=UTC)

    # 2024-11-03 01:30 occurs twice.  Only the first (EDT) occurrence is a slot.
    first_fall = next_daily_fire(
        daily_time="01:30",
        timezone="America/New_York",
        after_utc=datetime(2024, 11, 3, 5, 0, tzinfo=UTC),
    )
    assert first_fall == datetime(2024, 11, 3, 5, 30, tzinfo=UTC)
    next_day = next_daily_fire(
        daily_time="01:30",
        timezone="America/New_York",
        after_utc=first_fall,
    )
    assert next_day == datetime(2024, 11, 4, 6, 30, tzinfo=UTC)


def test_interval_is_absolute_across_dst_and_slot_enumeration_is_bounded():
    timing = ScheduleTiming(
        cadence_type=CadenceType.INTERVAL_HOURS.value,
        timezone="America/New_York",
        interval_hours=24,
        anchor_at=datetime(2024, 3, 10, 5, 0, tzinfo=UTC),
    )
    assert next_fire(timing, after_utc=timing.anchor_at) == datetime(2024, 3, 11, 5, 0, tzinfo=UTC)

    hourly = ScheduleTiming(
        cadence_type=CadenceType.INTERVAL_HOURS.value,
        timezone="UTC",
        interval_hours=1,
        anchor_at=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
    )
    slots = fire_times_between(
        hourly,
        after_exclusive_utc=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        through_inclusive_utc=datetime(2026, 1, 1, 3, 0, tzinfo=UTC),
    )
    assert slots == (
        datetime(2026, 1, 1, 1, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 2, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 3, 0, tzinfo=UTC),
    )
    with pytest.raises(OverflowError):
        fire_times_between(
            hourly,
            after_exclusive_utc=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
            through_inclusive_utc=datetime(2026, 1, 1, 3, 0, tzinfo=UTC),
            max_slots=2,
        )


def test_misfire_and_overlap_policy_decisions():
    now = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
    old = now - timedelta(hours=2)
    recent = now - timedelta(minutes=10)

    fire_once = evaluate_misfires(
        [recent, old],
        now_utc=now,
        policy=MisfirePolicy.FIRE_ONCE.value,
        grace_seconds=3600,
    )
    assert fire_once.fire_slot_utc == recent
    assert fire_once.skipped_slots_utc == (old,)

    expired = evaluate_misfires(
        [old],
        now_utc=now,
        policy=MisfirePolicy.FIRE_ONCE.value,
        grace_seconds=60,
    )
    assert expired.fire_slot_utc is None
    assert expired.skipped_slots_utc == (old,)

    skip_all = evaluate_misfires(
        [old, recent],
        now_utc=now,
        policy=MisfirePolicy.SKIP.value,
        grace_seconds=3600,
    )
    assert skip_all.fire_slot_utc is None
    assert skip_all.skipped_slots_utc == (old, recent)

    current = now - timedelta(seconds=2)
    skip_missed_but_fire_current = evaluate_misfires(
        [old, current],
        now_utc=now,
        policy=MisfirePolicy.SKIP.value,
        grace_seconds=3600,
        on_time_tolerance_seconds=5,
    )
    assert skip_missed_but_fire_current.fire_slot_utc == current
    assert skip_missed_but_fire_current.skipped_slots_utc == (old,)

    assert evaluate_overlap([RunnerState.RUNNING.value]).should_skip is True
    assert evaluate_overlap([RunnerState.UNKNOWN.value]).should_skip is False
    assert evaluate_overlap([RunnerState.HANDED_OFF.value]).should_skip is False
    assert evaluate_overlap(None).reason == "RUNNER_STATUS_UNAVAILABLE"
    assert evaluate_overlap(["NEW_LOCAL_STATE"]).reason == "UNRECOGNIZED_RUNNER_STATE"
