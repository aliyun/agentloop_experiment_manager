from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

from app.agentloop import (
    AgentLoopCloudControl,
    AgentLoopGatewayError,
    AgentLoopOfflineRunner,
    ConnectionCheck,
    FakeAgentLoopGateway,
    PlanDetail,
    ProductionAgentLoopGateway,
)
from app.agentloop.trace_correlation import TraceCorrelationProgress
from app.config import Settings
from app.dashboards import PlanDashboardPublisher, SlsPlanDashboardPublisher
from app.domain.clock import SystemClock
from app.domain.instance_lock import InstanceFileLock
from app.scheduler import SchedulerWorker
from app.secrets import EncryptedFileSecretStore, MemorySecretStore, SecretStore
from app.services import (
    AgentConnectionHub,
    AgentConnectionService,
    BackgroundTaskSupervisor,
    ConsoleUrlResolver,
    LaunchService,
    ManagedAgentProcessController,
    ManagedAgentProcessManager,
    ManagedCodeService,
    PlanDashboardService,
    ProfileService,
    ScheduleService,
)
from app.storage import (
    AgentConnectionRepository,
    Database,
    LaunchIntentRepository,
    PlanDashboardRepository,
    ProfileRepository,
    ScheduleRepository,
    TraceCorrelationRepository,
)


class DisabledAgentLoopGateway:
    """Fail-closed adapter used until the public AgentLoop contract is available."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def _unavailable(self):
        raise AgentLoopGatewayError(self.reason)

    async def test_connection(self, connection):
        del connection
        self._unavailable()

    async def list_plans(self, query, *, connection):
        del query, connection
        self._unavailable()

    async def get_plan(self, plan_id, *, connection):
        del plan_id, connection
        self._unavailable()

    async def start_offline_experiment(
        self,
        plan_id,
        on_record_created,
        *,
        connection,
        execution_config,
    ):
        del plan_id, on_record_created, connection, execution_config
        self._unavailable()

    async def list_records(self, query, *, connection):
        del query, connection
        self._unavailable()

    async def get_record(self, record_id, *, connection):
        del record_id, connection
        self._unavailable()

    def plan_console_url(self, plan_id: str) -> str | None:
        del plan_id
        return None

    def record_console_url(self, record_id: str) -> str | None:
        del record_id
        return None


@dataclass(slots=True)
class AppContainer:
    settings: Settings
    database: Database
    secret_store: SecretStore
    gateway: object
    profile_repository: ProfileRepository
    schedule_repository: ScheduleRepository
    launch_repository: LaunchIntentRepository
    plan_dashboard_repository: PlanDashboardRepository
    trace_correlation_repository: TraceCorrelationRepository
    agent_connection_repository: AgentConnectionRepository
    agent_connection_hub: AgentConnectionHub
    managed_code_service: ManagedCodeService
    agent_connection_service: AgentConnectionService
    profile_service: ProfileService
    launch_service: LaunchService
    plan_dashboard_service: PlanDashboardService | None
    schedule_service: ScheduleService
    console_url_resolver: ConsoleUrlResolver
    background_tasks: BackgroundTaskSupervisor
    scheduler_worker: SchedulerWorker | None
    instance_lock: InstanceFileLock

    async def start(self) -> None:
        self.instance_lock.acquire()
        try:
            recovered_at = datetime.now(UTC)
            self.launch_repository.recover_interrupted_runners(recovered_at)
            self.trace_correlation_repository.recover_interrupted(recovered_at)
            self.background_tasks.start(self._restore_agents_and_start_scheduler())
        except BaseException:
            self.instance_lock.release()
            raise

    async def _restore_agents_and_start_scheduler(self) -> None:
        worker_host = self.settings.host
        if worker_host in {"0.0.0.0", "::"}:
            worker_host = "127.0.0.1"
        if ":" in worker_host and not worker_host.startswith("["):
            worker_host = f"[{worker_host}]"

        def connect_url(connection_id: str) -> str:
            encoded = quote(connection_id, safe="")
            return (
                f"ws://{worker_host}:{self.settings.port}/api/v1/"
                f"agent-connections/{encoded}/connect"
            )

        await self.managed_code_service.resume_configured(connect_url)
        if self.scheduler_worker is not None:
            await self.scheduler_worker.start()

    async def close(self) -> None:
        try:
            if self.scheduler_worker is not None:
                await self.scheduler_worker.stop()
            await self.background_tasks.close()
            await self.managed_code_service.close()
            self.database.dispose()
        finally:
            self.instance_lock.release()


def build_container(
    settings: Settings,
    *,
    database: Database | None = None,
    secret_store: SecretStore | None = None,
    gateway: object | None = None,
    instance_lock: InstanceFileLock | None = None,
    managed_process_manager: ManagedAgentProcessController | None = None,
    plan_dashboard_publisher: PlanDashboardPublisher | None = None,
) -> AppContainer:
    settings.prepare_directories()
    if database is None:
        database = Database.from_path(settings.data_dir / "launcher.db")
    database.initialize()

    if secret_store is None:
        if settings.master_key:
            secret_store = EncryptedFileSecretStore(settings.secret_file, settings.master_key)
        elif settings.environment == "test" or settings.gateway_mode == "fake":
            secret_store = MemorySecretStore()
        else:
            raise RuntimeError(
                "AGENTLOOP_LAUNCHER_MASTER_KEY is required when credentials must survive restarts"
            )

    profiles = ProfileRepository(database)
    schedules = ScheduleRepository(database)
    launches = LaunchIntentRepository(database)
    plan_dashboards = PlanDashboardRepository(database)
    trace_correlations = TraceCorrelationRepository(database)
    agent_connections = AgentConnectionRepository(database)
    agent_connection_hub = AgentConnectionHub(agent_connections)
    if managed_process_manager is None:
        managed_process_manager = ManagedAgentProcessManager(agent_connection_hub)
    managed_code_service = ManagedCodeService(
        agent_connections,
        secret_store,
        agent_connection_hub,
        managed_process_manager,
    )
    agent_connection_service = AgentConnectionService(
        agent_connections,
        secret_store,
        agent_connection_hub,
        managed_code_service,
    )
    if gateway is None:
        gateway = _build_gateway(settings, agent_connection_hub, trace_correlations)
    tasks = BackgroundTaskSupervisor()
    clock = SystemClock()
    profile_service = ProfileService(profiles, secret_store, gateway)  # type: ignore[arg-type]
    if plan_dashboard_publisher is None and settings.gateway_mode == "agentloop":
        plan_dashboard_publisher = SlsPlanDashboardPublisher(
            user_agent=settings.api_user_agent
        )
    plan_dashboard_service = (
        PlanDashboardService(
            dashboards=plan_dashboards,
            publisher=plan_dashboard_publisher,
            clock=clock,
        )
        if plan_dashboard_publisher is not None
        else None
    )
    launch_service = LaunchService(
        profiles=profiles,
        launches=launches,
        gateway=gateway,  # type: ignore[arg-type]
        clock=clock,
        tasks=tasks,
        connection_provider=profile_service.connection,
        agent_connections=agent_connections,
        agent_connection_online=agent_connection_hub.is_connected,
        require_agent_connection=settings.gateway_mode == "agentloop",
        plan_dashboards=plan_dashboard_service,
    )
    schedule_service = ScheduleService(
        profiles=profiles,
        schedules=schedules,
        gateway=gateway,  # type: ignore[arg-type]
        launches=launch_service,
        clock=clock,
        connection_provider=profile_service.connection,
        agent_connections=agent_connections,
        require_agent_connection=settings.gateway_mode == "agentloop",
    )
    scheduler_worker = None
    if settings.scheduler_enabled:
        scheduler_worker = SchedulerWorker(
            schedules=schedules,
            launches=launch_service,
            clock=clock,
            poll_seconds=settings.scheduler_poll_seconds,
            lock_path=settings.data_dir / "scheduler.lock",
        )
    return AppContainer(
        settings=settings,
        database=database,
        secret_store=secret_store,
        gateway=gateway,
        profile_repository=profiles,
        schedule_repository=schedules,
        launch_repository=launches,
        plan_dashboard_repository=plan_dashboards,
        trace_correlation_repository=trace_correlations,
        agent_connection_repository=agent_connections,
        agent_connection_hub=agent_connection_hub,
        managed_code_service=managed_code_service,
        agent_connection_service=agent_connection_service,
        profile_service=profile_service,
        launch_service=launch_service,
        plan_dashboard_service=plan_dashboard_service,
        schedule_service=schedule_service,
        console_url_resolver=ConsoleUrlResolver(profiles, gateway),  # type: ignore[arg-type]
        background_tasks=tasks,
        scheduler_worker=scheduler_worker,
        instance_lock=instance_lock or InstanceFileLock(settings.data_dir / "app.lock"),
    )


def _build_gateway(
    settings: Settings,
    agent_connection_hub: AgentConnectionHub,
    trace_correlations: TraceCorrelationRepository,
):
    if settings.gateway_mode == "agentloop":
        control = AgentLoopCloudControl(user_agent=settings.api_user_agent)
        runner = AgentLoopOfflineRunner(
            agent_connection_hub,
            result_dir=settings.data_dir / "results",
            task_timeout_seconds=settings.agent_task_timeout_seconds,
            trace_lookup_timeout_seconds=settings.trace_lookup_timeout_seconds,
            user_agent=settings.api_user_agent,
            trace_observer=_trace_correlation_observer(trace_correlations),
        )
        return ProductionAgentLoopGateway(control, runner)
    if settings.gateway_mode != "fake":
        return DisabledAgentLoopGateway("The real AgentLoop gateway is disabled by configuration")
    now = datetime.now(UTC)
    return FakeAgentLoopGateway(
        plans=(
            PlanDetail(
                plan_id="plan-offline-smoke",
                plan_name="Offline smoke test",
                experiment_type="OFFLINE",
                status="active",
                description="Local-only demo plan. No cloud API is called.",
                dataset_id="dataset-demo",
                evaluator_count=2,
                created_at=now,
                updated_at=now,
                can_launch_locally=True,
                dataset_name="Demo questions",
            ),
            PlanDetail(
                plan_id="plan-online-disabled",
                plan_name="Online plan (read-only)",
                experiment_type="ONLINE",
                status="active",
                created_at=now,
                updated_at=now,
                can_launch_locally=False,
                cannot_launch_reason="Only OFFLINE plans can run in the customer environment",
            ),
        ),
        connection_check=ConnectionCheck(ok=True, message="Fake gateway is ready"),
        console_base_url=settings.console_base_url,
        region_id="local",
        agent_space="demo",
    )


def _trace_correlation_observer(
    repository: TraceCorrelationRepository,
):
    """Project payload-free runner progress into the local SQLite audit log."""

    def observe(progress: TraceCorrelationProgress) -> None:
        changes: dict[str, object] = {
            "state": progress.status.value,
            "attempts": progress.attempts,
            "agent_started_at": _datetime_from_epoch_ms(progress.context.agent_started_at_ms),
            "agent_completed_at": _datetime_from_epoch_ms(progress.context.agent_completed_at_ms),
        }
        if progress.deadline_at_ms is not None:
            changes["deadline_at"] = _datetime_from_epoch_ms(progress.deadline_at_ms)
        if progress.trace_id is not None:
            changes["trace_id"] = progress.trace_id
        if progress.source is not None:
            changes["source"] = progress.source
        if progress.status.value == "RESOLVED":
            changes["resolved_at"] = _datetime_from_epoch_ms(progress.occurred_at_ms)
        elif progress.status.value == "REPORTED":
            changes["reported_at"] = _datetime_from_epoch_ms(progress.occurred_at_ms)
        if progress.error_code is not None:
            changes["error_code"] = progress.error_code
        if progress.sanitized_message is not None:
            changes["sanitized_error"] = progress.sanitized_message
        repository.upsert(
            launch_id=progress.context.launch_id,
            dataset_item_id=progress.context.dataset_item_id,
            experiment_run_id=progress.context.experiment_run_id,
            **changes,
        )

    return observe


def _datetime_from_epoch_ms(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=UTC)
