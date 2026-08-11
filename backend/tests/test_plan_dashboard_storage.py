from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, inspect, select, text

from app.dashboards.template import dashboard_name
from app.storage import (
    CURRENT_SCHEMA_VERSION,
    Database,
    PlanDashboard,
    PlanDashboardRepository,
    PlanDashboardSyncState,
    ProfileRepository,
    in_memory_database,
)


def _profile(database: Database, profile_id: str = "profile-dashboard") -> None:
    ProfileRepository(database).create(
        profile_id=profile_id,
        display_name="Dashboard test",
        region_id="cn-hangzhou",
        agent_space="customer-support",
        secret_ref=f"secret/{profile_id}",
    )


def test_repository_keeps_one_row_per_profile_and_plan_across_plan_rename() -> None:
    database = in_memory_database()
    _profile(database)
    repository = PlanDashboardRepository(database)
    now = datetime(2026, 8, 9, 1, 0, tzinfo=UTC)
    stable_name = dashboard_name("customer-support", "plan-123")

    created = repository.begin_sync(
        profile_id="profile-dashboard",
        plan_id="plan-123",
        plan_name_snapshot="Original name",
        agent_space_snapshot="customer-support",
        dashboard_name=stable_name,
        display_name="实验计划 · Original name",
        template_version="1",
        template_sha256="a" * 64,
        attempted_at=now,
    )
    repository.mark_ready(
        profile_id="profile-dashboard",
        plan_id="plan-123",
        sls_project="agentloop-project",
        dashboard_name=stable_name,
        display_name=created.display_name,
        template_version="1",
        template_sha256="a" * 64,
        synced_at=now + timedelta(seconds=1),
    )
    renamed = repository.begin_sync(
        profile_id="profile-dashboard",
        plan_id="plan-123",
        plan_name_snapshot="Renamed plan",
        agent_space_snapshot="customer-support",
        dashboard_name=stable_name,
        display_name="实验计划 · Renamed plan",
        template_version="1",
        template_sha256="a" * 64,
        attempted_at=now + timedelta(seconds=2),
    )
    repository.begin_sync(
        profile_id="profile-dashboard",
        plan_id="plan-456",
        plan_name_snapshot="Other plan",
        agent_space_snapshot="customer-support",
        dashboard_name=dashboard_name("customer-support", "plan-456"),
        display_name="实验计划 · Other plan",
        template_version="1",
        template_sha256="a" * 64,
        attempted_at=now + timedelta(seconds=3),
    )

    assert renamed.dashboard_name == stable_name
    assert renamed.plan_name_snapshot == "Renamed plan"
    assert renamed.sync_state == PlanDashboardSyncState.PENDING.value
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(PlanDashboard)) == 2
    primary_key = inspect(database.engine).get_pk_constraint("plan_dashboards")
    assert primary_key["constrained_columns"] == ["profile_id", "plan_id"]
    database.dispose()


def test_schema_v4_migrates_to_v5_without_losing_existing_profile(tmp_path) -> None:
    path = tmp_path / "launcher" / "launcher.db"
    legacy = Database.from_path(path)
    legacy.initialize()
    _profile(legacy, profile_id="profile-v4")
    with legacy.engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE plan_dashboards")
        connection.execute(text("UPDATE schema_version SET version = 4 WHERE id = 1"))
    legacy.dispose()

    migrated = Database.from_path(path)
    migrated.initialize()
    schema = inspect(migrated.engine)

    assert "plan_dashboards" in schema.get_table_names()
    assert schema.get_pk_constraint("plan_dashboards")["constrained_columns"] == [
        "profile_id",
        "plan_id",
    ]
    assert ProfileRepository(migrated).get("profile-v4") is not None
    with migrated.engine.connect() as connection:
        assert connection.execute(text("SELECT version FROM schema_version")).scalar_one() == (
            CURRENT_SCHEMA_VERSION
        )
    migrated.dispose()
