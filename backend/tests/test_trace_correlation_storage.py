from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect, text

from app.storage import (
    CURRENT_SCHEMA_VERSION,
    Database,
    IdempotencyConflictError,
    LaunchIntentRepository,
    ProfileRepository,
    TraceCorrelationRepository,
    TraceCorrelationState,
    TriggerSource,
    in_memory_database,
)


def _launch(database: Database, *, launch_id: str = "launch-trace") -> None:
    profile = ProfileRepository(database).create(
        profile_id="profile-trace",
        display_name="Trace test",
        region_id="cn-hangzhou",
        agent_space="space-trace",
        secret_ref="secret/profile-trace",
    )
    LaunchIntentRepository(database).create_or_get(
        launch_id=launch_id,
        profile_id=profile.id,
        plan_id="plan-trace",
        trigger_source=TriggerSource.MANUAL.value,
        idempotency_key=f"trace:{launch_id}",
    )


def test_trace_correlation_repository_lifecycle_and_summary() -> None:
    database = in_memory_database()
    _launch(database)
    repository = TraceCorrelationRepository(database)
    started_at = datetime(2026, 8, 9, 1, 0, tzinfo=UTC)

    pending = repository.upsert(
        launch_id="launch-trace",
        dataset_item_id="item-1",
        experiment_run_id="exp-run-1",
        agent_started_at=started_at,
        deadline_at=started_at + timedelta(minutes=1),
    )
    assert pending.state == TraceCorrelationState.PENDING.value
    assert repository.summary("launch-trace").pending_tasks == 1

    polling = repository.update(
        "launch-trace",
        "item-1",
        state=TraceCorrelationState.POLLING,
        attempts=1,
    )
    assert polling.first_polled_at is not None

    resolved = repository.update(
        "launch-trace",
        "item-1",
        state=TraceCorrelationState.RESOLVED,
        trace_id="ABCDEF0123456789ABCDEF0123456789",
        source="agentloop-trace-preview",
        attempts=2,
        agent_completed_at=started_at + timedelta(seconds=3),
    )
    assert resolved.trace_id == "abcdef0123456789abcdef0123456789"
    assert resolved.resolved_at is not None
    resolved_summary = repository.summary("launch-trace")
    assert resolved_summary.state == "RESOLVED"
    assert resolved_summary.trace_ids == ("abcdef0123456789abcdef0123456789",)
    assert resolved_summary.resolved_tasks == 1
    assert resolved_summary.total_attempts == 2
    assert resolved_summary.started_at == started_at

    reported = repository.update(
        "launch-trace",
        "item-1",
        state=TraceCorrelationState.REPORTED,
    )
    assert reported.reported_at is not None
    reported_summary = repository.summary("launch-trace")
    assert reported_summary.state == "REPORTED"
    assert reported_summary.reported_tasks == 1
    assert reported_summary.finished_at is not None

    repository.upsert(
        launch_id="launch-trace",
        dataset_item_id="item-2",
        experiment_run_id="exp-run-1",
        state=TraceCorrelationState.TIMED_OUT,
        attempts=3,
        error_code="TRACE_LOOKUP_TIMEOUT",
        sanitized_error="  Trace   was not indexed in time.\n",
    )
    failed_summary = repository.summary("launch-trace")
    assert failed_summary.state == "TIMED_OUT"
    assert failed_summary.total_tasks == 2
    assert failed_summary.reported_tasks == 1
    assert failed_summary.failed_tasks == 1
    assert failed_summary.total_attempts == 5
    assert failed_summary.last_error_code == "TRACE_LOOKUP_TIMEOUT"
    assert failed_summary.last_error_message == "Trace was not indexed in time."
    assert failed_summary.trace_ids == ("abcdef0123456789abcdef0123456789",)

    with pytest.raises(IdempotencyConflictError, match="another experiment run"):
        repository.upsert(
            launch_id="launch-trace",
            dataset_item_id="item-1",
            experiment_run_id="exp-run-other",
        )
    with pytest.raises(ValueError, match="unsupported Trace correlation fields"):
        repository.update("launch-trace", "item-1", raw_input="must-not-be-stored")
    with pytest.raises(ValueError, match="32 hexadecimal"):
        repository.update("launch-trace", "item-1", trace_id="not-a-trace-id")

    database.dispose()


def test_schema_v2_migrates_to_trace_correlation_table(tmp_path) -> None:
    path = tmp_path / "launcher" / "launcher.db"
    database = Database.from_path(path)
    database.initialize()
    with database.engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE trace_correlations")
        connection.execute(text("UPDATE schema_version SET version = 2 WHERE id = 1"))
    database.dispose()

    migrated = Database.from_path(path)
    migrated.initialize()
    schema = inspect(migrated.engine)
    assert "trace_correlations" in schema.get_table_names()
    with migrated.engine.connect() as connection:
        assert connection.execute(text("SELECT version FROM schema_version")).scalar_one() == (
            CURRENT_SCHEMA_VERSION
        )
    migrated.dispose()


def test_trace_correlation_recovery_closes_non_terminal_rows() -> None:
    database = in_memory_database()
    _launch(database)
    repository = TraceCorrelationRepository(database)
    for item_id, state in (
        ("pending", TraceCorrelationState.PENDING),
        ("polling", TraceCorrelationState.POLLING),
        ("resolved", TraceCorrelationState.RESOLVED),
    ):
        changes = {"state": state}
        if state is TraceCorrelationState.RESOLVED:
            changes["trace_id"] = "a" * 32
        repository.upsert(
            launch_id="launch-trace",
            dataset_item_id=item_id,
            experiment_run_id="exp-run-1",
            **changes,
        )
    repository.upsert(
        launch_id="launch-trace",
        dataset_item_id="reported",
        experiment_run_id="exp-run-1",
        state=TraceCorrelationState.REPORTED,
        trace_id="b" * 32,
    )

    recovered = repository.recover_interrupted(datetime(2026, 8, 9, 2, 0, tzinfo=UTC))

    assert recovered == 3
    for item_id in ("pending", "polling", "resolved"):
        item = repository.get("launch-trace", item_id)
        assert item is not None
        assert item.state == TraceCorrelationState.FAILED.value
        assert item.error_code == "LOCAL_PROCESS_INTERRUPTED"
    reported = repository.get("launch-trace", "reported")
    assert reported is not None
    assert reported.state == TraceCorrelationState.REPORTED.value
    database.dispose()
