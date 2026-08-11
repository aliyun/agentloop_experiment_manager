"""Composite real gateway: public control plane plus runner-owned execution."""

from __future__ import annotations

from app.domain.launch_config import LaunchExecutionConfig

from .cloud_control import AgentLoopCloudControl
from .gateway import ExperimentHandle, RecordCreatedCallback
from .models import (
    AgentLoopConnection,
    ConnectionCheck,
    Page,
    PlanDetail,
    PlanQuery,
    PlanSummary,
    RecordDetail,
    RecordQuery,
    RecordSummary,
)
from .offline_runner import AgentLoopOfflineRunner, OfflineExperimentSpec


class ProductionAgentLoopGateway:
    """The production boundary used when ``gateway_mode=agentloop``."""

    def __init__(
        self,
        control: AgentLoopCloudControl,
        runner: AgentLoopOfflineRunner,
    ) -> None:
        self.control = control
        self.runner = runner

    async def test_connection(self, connection: AgentLoopConnection) -> ConnectionCheck:
        return await self.control.test_connection(connection)

    async def list_plans(
        self,
        query: PlanQuery,
        *,
        connection: AgentLoopConnection,
    ) -> Page[PlanSummary]:
        return await self.control.list_plans(query, connection=connection)

    async def get_plan(
        self,
        plan_id: str,
        *,
        connection: AgentLoopConnection,
    ) -> PlanDetail:
        return await self.control.get_plan(plan_id, connection=connection)

    async def start_offline_experiment(
        self,
        plan_id: str,
        on_record_created: RecordCreatedCallback,
        *,
        connection: AgentLoopConnection,
        execution_config: LaunchExecutionConfig,
    ) -> ExperimentHandle:
        if not execution_config.agent_connection_id:
            raise ValueError("A connected local Agent is required for offline execution")
        return await self.runner.start(
            OfflineExperimentSpec(
                launch_id=execution_config.launch_id,
                plan_id=plan_id,
                agent_connection_id=execution_config.agent_connection_id,
                experiment_version=execution_config.experiment_version,
                context_xml_template=execution_config.context_xml_template,
                parameters=execution_config.experiment_parameters,
                context_schema_version=execution_config.context_schema_version,
                record_name=execution_config.record_name,
            ),
            on_record_created,
            connection=connection,
        )

    async def list_records(
        self,
        query: RecordQuery,
        *,
        connection: AgentLoopConnection,
    ) -> Page[RecordSummary]:
        return await self.control.list_records(query, connection=connection)

    async def get_record(
        self,
        record_id: str,
        *,
        connection: AgentLoopConnection,
    ) -> RecordDetail:
        return await self.control.get_record(record_id, connection=connection)

    def plan_console_url(self, plan_id: str) -> str | None:
        del plan_id
        return None

    def record_console_url(self, record_id: str) -> str | None:
        del record_id
        return None
