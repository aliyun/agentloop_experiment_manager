from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from xml.etree import ElementTree

import pytest
from fastapi import WebSocketDisconnect
from sqlalchemy import inspect, text

from app.agentloop import AgentLoopConnection, FakeAgentLoopGateway, PlanDetail
from app.domain import (
    append_experiment_context,
    build_experiment_context_template,
    canonical_launch_config_hash,
    render_experiment_context,
    resolve_experiment_version,
)
from app.domain.clock import FixedClock
from app.domain.errors import ConflictError
from app.secrets import MemorySecretStore
from app.services import (
    AgentConnectionHub,
    AgentConnectionService,
    BackgroundTaskSupervisor,
    LaunchService,
)
from app.storage import (
    CURRENT_SCHEMA_VERSION,
    AgentConnectionRepository,
    Database,
    LaunchIntentRepository,
    ProfileRepository,
    in_memory_database,
)


def test_experiment_context_is_two_stage_and_xml_escaped() -> None:
    template = build_experiment_context_template(
        experiment_plan_id='plan<&"',
        experiment_version="1.2.3",
        local_launch_id="launch<&",
    )
    assert 'plan&lt;&amp;"' in template
    rendered = render_experiment_context(
        template,
        experiment_run_id="run<&",
        dataset_item_id='item<&"',
    )
    root = ElementTree.fromstring(rendered)
    values = {child.tag: child.text for child in root}
    assert root.attrib == {"schema_version": "1"}
    assert values == {
        "experiment_plan_id": 'plan<&"',
        "experiment_run_id": "run<&",
        "experiment_version": "1.2.3",
        "dataset_item_id": 'item<&"',
        "local_launch_id": "launch<&",
    }
    assert append_experiment_context("where am I?", rendered).startswith("where am I?\n")


def test_version_is_resolved_once_and_config_hash_is_canonical() -> None:
    now = datetime(2026, 8, 8, 1, 2, 3, 456789, tzinfo=UTC)
    mode, manual, final = resolve_experiment_version(
        version_mode="AUTO",
        requested_version=None,
        now=now,
        launch_id="12345678-abcd-ef00-1111-222233334444",
    )
    assert mode == "AUTO"
    assert manual is None
    assert final == "auto-20260808T010203456789Z-12345678abcd"

    first = canonical_launch_config_hash(
        agent_connection_id="agent-1",
        version_mode="AUTO",
        requested_version=None,
        experiment_parameters_json={"b": 2, "a": {"z": 1}},
        record_name=" run ",
        context_schema_version=1,
    )
    second = canonical_launch_config_hash(
        agent_connection_id="agent-1",
        version_mode="AUTO",
        requested_version=None,
        experiment_parameters_json='{"a":{"z":1},"b":2}',
        record_name="run",
        context_schema_version=1,
    )
    assert first == second
    with pytest.raises(ValueError, match="required"):
        resolve_experiment_version(
            version_mode="MANUAL",
            requested_version=None,
            now=now,
            launch_id="launch-1",
        )


def test_agent_connection_token_never_enters_sqlite_and_rotation_revokes_old_token() -> None:
    database = in_memory_database()
    repository = AgentConnectionRepository(database)
    secrets_store = MemorySecretStore()
    hub = AgentConnectionHub(repository)
    service = AgentConnectionService(repository, secrets_store, hub)

    created = service.create(name="calculator", description="real local test Agent")
    connection = created.connection
    assert service.authenticate(connection.id, created.token) is True
    persisted = repository.get(connection.id)
    assert persisted is not None
    serialized = json.dumps({key: str(value) for key, value in vars(persisted).items()})
    assert created.token not in serialized
    assert persisted.token_secret_ref != created.token

    async def rotate_and_delete() -> None:
        rotated = await service.rotate_token(connection.id)
        assert rotated.token != created.token
        assert service.authenticate(connection.id, created.token) is False
        assert service.authenticate(connection.id, rotated.token) is True
        await service.delete(connection.id)
        assert service.authenticate(connection.id, rotated.token) is False

    asyncio.run(rotate_and_delete())
    database.dispose()


class _FakeWebSocket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.outgoing: asyncio.Queue = asyncio.Queue()
        self.accepted = False
        self.closed = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, value) -> None:
        await self.outgoing.put(value)

    async def receive_json(self):
        value = await self.incoming.get()
        if value is _DISCONNECT:
            raise WebSocketDisconnect(code=1000)
        return value

    async def close(self, code: int, reason: str) -> None:
        del code, reason
        self.closed = True
        await self.incoming.put(_DISCONNECT)


_DISCONNECT = object()


def test_websocket_hub_dispatches_real_task_result_protocol() -> None:
    async def scenario() -> None:
        database = in_memory_database()
        repository = AgentConnectionRepository(database)
        connection = repository.create(
            connection_id="agent-1",
            name="calculator",
            token_secret_ref="secret/ref",
            token_hint="****test",
        )
        hub = AgentConnectionHub(repository)
        websocket = _FakeWebSocket()
        serving = asyncio.create_task(hub.connect(connection.id, websocket))  # type: ignore[arg-type]
        registered = await websocket.outgoing.get()
        assert registered == {
            "type": "registered",
            "agentConnectionId": "agent-1",
            "protocolVersion": 1,
        }

        executing = asyncio.create_task(
            hub.execute("agent-1", {"input": "2+3", "context": "<version>v1</version>"})
        )
        task = await websocket.outgoing.get()
        assert task["type"] == "task"
        assert task["payload"]["input"] == "2+3"
        await websocket.incoming.put(
            {
                "type": "result",
                "taskId": task["taskId"],
                "success": False,
                "output": "5",
                "trajectory": [{"type": "text", "text": "partial-answer"}],
                "meta": {"agent": "calculator"},
            }
        )
        result = await executing
        assert result.success is False
        assert result.output == "5"
        assert result.trajectory == [{"type": "text", "text": "partial-answer"}]
        assert result.meta == {"agent": "calculator"}
        await hub.disconnect("agent-1", reason="test complete")
        await serving
        database.dispose()

    asyncio.run(scenario())


def test_legacy_database_is_migrated_in_order_without_dropping_rows(tmp_path) -> None:
    path = tmp_path / "legacy" / "launcher.db"
    database = Database.from_path(path)
    with database.engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE schedules (id TEXT PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO schedules (id) VALUES ('schedule-old')")
        connection.exec_driver_sql("CREATE TABLE launch_intents (id TEXT PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO launch_intents (id) VALUES ('launch-old')")
    database.initialize()

    schema = inspect(database.engine)
    schedule_columns = {column["name"] for column in schema.get_columns("schedules")}
    launch_columns = {column["name"] for column in schema.get_columns("launch_intents")}
    assert {"agent_connection_id", "version_mode", "launch_config_hash"} <= schedule_columns
    assert {
        "agent_connection_id",
        "experiment_version",
        "context_xml_template",
        "launch_config_hash",
    } <= launch_columns
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT version FROM schema_version")).scalar_one() == (
            CURRENT_SCHEMA_VERSION
        )
        assert connection.execute(
            text("SELECT launch_config_hash FROM schedules WHERE id='schedule-old'")
        ).scalar_one()
        assert connection.execute(
            text("SELECT launch_config_hash FROM launch_intents WHERE id='launch-old'")
        ).scalar_one()
    database.dispose()


def test_launch_service_persists_immutable_config_and_detects_idempotency_conflict() -> None:
    async def scenario() -> None:
        database = in_memory_database()
        profiles = ProfileRepository(database)
        profile = profiles.create(
            display_name="test",
            region_id="cn-hangzhou",
            agent_space="space-a",
            secret_ref="secret/profile",
            validation_status="VALID",
        )
        agent_connections = AgentConnectionRepository(database)
        agent = agent_connections.create(
            connection_id="agent-real",
            name="calculator",
            token_secret_ref="secret/agent",
            token_hint="****test",
        )
        gateway = FakeAgentLoopGateway(
            plans=(
                PlanDetail(
                    plan_id="plan-1",
                    plan_name="smoke",
                    experiment_type="OFFLINE",
                    status="active",
                    can_launch_locally=True,
                ),
            )
        )
        tasks = BackgroundTaskSupervisor()
        now = datetime(2026, 8, 8, 2, 0, tzinfo=UTC)
        service = LaunchService(
            profiles=profiles,
            launches=LaunchIntentRepository(database),
            gateway=gateway,
            clock=FixedClock(now),
            tasks=tasks,
            connection_provider=lambda: AgentLoopConnection(
                region_id=profile.region_id,
                agent_space=profile.agent_space,
                access_key_id="test-ak",
                access_key_secret="test-sk",
            ),
            agent_connections=agent_connections,
            agent_connection_online=lambda connection_id: connection_id == agent.id,
            require_agent_connection=True,
        )
        launch = await service.manual(
            "plan-1",
            "request-1",
            agent_connection_id=agent.id,
            version_mode="MANUAL",
            experiment_version="1.2.3",
            experiment_parameters={"temperature": 0, "nested": {"b": 2, "a": 1}},
            record_name="smoke-v1",
        )
        assert launch.agent_connection_id == agent.id
        assert launch.agent_connection_name_snapshot == "calculator"
        assert launch.experiment_version == "1.2.3"
        assert launch.experiment_parameters == {
            "nested": {"a": 1, "b": 2},
            "temperature": 0,
        }
        assert launch.context_xml_template is not None
        assert launch.launch_config_hash
        assert gateway.last_execution_config is not None
        assert gateway.last_execution_config.launch_id == launch.id
        assert gateway.last_execution_config.experiment_version == "1.2.3"

        replay = await service.manual(
            "plan-1",
            "request-1",
            agent_connection_id=agent.id,
            version_mode="MANUAL",
            experiment_version="1.2.3",
            experiment_parameters={"nested": {"a": 1, "b": 2}, "temperature": 0},
            record_name="smoke-v1",
        )
        assert replay.id == launch.id
        assert gateway.created_record_count == 1
        with pytest.raises(ConflictError, match="another launch configuration"):
            await service.manual(
                "plan-1",
                "request-1",
                agent_connection_id=agent.id,
                version_mode="MANUAL",
                experiment_version="1.2.4",
                experiment_parameters={"temperature": 0},
                record_name="smoke-v1",
            )
        await tasks.close()
        database.dispose()

    asyncio.run(scenario())
