"""Local Agent registration, authentication, and WebSocket task dispatch."""

from __future__ import annotations

import asyncio
import hmac
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import uuid4

from fastapi import WebSocket, WebSocketDisconnect

from app.domain.errors import NotFoundError
from app.secrets import SecretStore
from app.storage import AgentConnectionRepository
from app.storage import NotFoundError as StorageNotFoundError


class AgentConnectionUnavailableError(RuntimeError):
    pass


class AgentExecutionTimeoutError(AgentConnectionUnavailableError):
    pass


class AgentExecutionError(RuntimeError):
    """Agent execution failed without exposing provider details through logs."""

    def __init__(self, message: str, *, provider_error: str | None = None) -> None:
        super().__init__(message)
        self.provider_error = provider_error


@dataclass(frozen=True, slots=True)
class AgentExecutionResult:
    task_id: str
    output: Any
    meta: dict[str, Any]
    success: bool = True
    trajectory: list[Any] = field(default_factory=list)


class AgentTaskDispatcher(Protocol):
    async def execute(
        self,
        connection_id: str,
        payload: dict[str, Any],
        timeout_seconds: float = 300,
    ) -> AgentExecutionResult: ...


class ManagedCodeLifecycle(Protocol):
    async def delete(self, connection_id: str) -> None: ...

    async def stop(self, connection_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class AgentConnectionCredential:
    connection: Any
    token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class AgentConnectionHealth:
    connection_id: str
    status: str
    healthy: bool
    last_seen_at: datetime | None
    message: str


@dataclass(slots=True)
class _ConnectedAgent:
    websocket: WebSocket
    pending: dict[str, asyncio.Future[AgentExecutionResult]]
    send_lock: asyncio.Lock


class AgentConnectionHub(AgentTaskDispatcher):
    """In-memory live-session registry used by the offline Runner.

    Durable registration remains in SQLite.  A live WebSocket cannot survive a
    process restart, so pending task futures deliberately stay process-local.
    """

    def __init__(self, repository: AgentConnectionRepository) -> None:
        self.repository = repository
        self._sessions: dict[str, _ConnectedAgent] = {}
        self._lock = asyncio.Lock()

    def is_connected(self, connection_id: str) -> bool:
        return connection_id in self._sessions

    async def connect(self, connection_id: str, websocket: WebSocket) -> None:
        """Accept one Agent-initiated socket and process protocol messages."""

        session = _ConnectedAgent(websocket=websocket, pending={}, send_lock=asyncio.Lock())
        async with self._lock:
            if connection_id in self._sessions:
                await websocket.close(code=4409, reason="Agent connection is already online")
                raise AgentConnectionUnavailableError("Agent connection is already online")
            await websocket.accept()
            self._sessions[connection_id] = session
        self.repository.record_connected(connection_id)
        await websocket.send_json(
            {
                "type": "registered",
                "agentConnectionId": connection_id,
                "protocolVersion": 1,
            }
        )
        try:
            while True:
                message = await websocket.receive_json()
                await self._handle_message(connection_id, session, message)
        except WebSocketDisconnect:
            pass
        except AgentExecutionError as exc:
            await websocket.close(code=4400, reason=str(exc)[:120])
        finally:
            await self._remove_session(connection_id, session)

    async def execute(
        self,
        connection_id: str,
        payload: dict[str, Any],
        timeout_seconds: float = 300,
    ) -> AgentExecutionResult:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not isinstance(payload, dict):
            raise TypeError("payload must be a dict")
        session = self._sessions.get(connection_id)
        if session is None:
            raise AgentConnectionUnavailableError(f"Agent connection is offline: {connection_id}")
        task_id = str(uuid4())
        future = asyncio.get_running_loop().create_future()
        session.pending[task_id] = future
        try:
            async with session.send_lock:
                await session.websocket.send_json(
                    {
                        "type": "task",
                        "taskId": task_id,
                        "payload": payload,
                    }
                )
            try:
                return await asyncio.wait_for(future, timeout=timeout_seconds)
            except TimeoutError as exc:
                raise AgentExecutionTimeoutError(
                    f"Agent task timed out after {timeout_seconds:g} seconds"
                ) from exc
        finally:
            session.pending.pop(task_id, None)

    async def disconnect(self, connection_id: str, *, reason: str) -> None:
        session = self._sessions.get(connection_id)
        if session is not None:
            await session.websocket.close(code=4401, reason=reason[:120])
            await self._remove_session(connection_id, session)

    async def _handle_message(
        self,
        connection_id: str,
        session: _ConnectedAgent,
        message: Any,
    ) -> None:
        if not isinstance(message, dict):
            raise AgentExecutionError("Agent protocol message must be an object")
        message_type = message.get("type")
        self.repository.record_seen(connection_id)
        if message_type in {"ready", "heartbeat"}:
            if message_type == "heartbeat":
                async with session.send_lock:
                    await session.websocket.send_json({"type": "heartbeat_ack"})
            return
        if message_type not in {"result", "error"}:
            raise AgentExecutionError(f"Unsupported Agent protocol message type: {message_type}")
        task_id = message.get("taskId")
        if not isinstance(task_id, str) or not task_id:
            raise AgentExecutionError("Agent result is missing taskId")
        future = session.pending.get(task_id)
        if future is None or future.done():
            return
        if message_type == "error":
            raw_error = message.get("error")
            provider_error = (
                raw_error[:800] if isinstance(raw_error, str) and raw_error.strip() else None
            )
            future.set_exception(
                AgentExecutionError(
                    "Customer Agent reported task failure",
                    provider_error=provider_error,
                )
            )
            return
        meta = message.get("meta", {})
        if not isinstance(meta, dict):
            future.set_exception(AgentExecutionError("Agent result meta must be an object"))
            return
        success = message.get("success", True)
        if not isinstance(success, bool):
            future.set_exception(AgentExecutionError("Agent result success must be a boolean"))
            return
        trajectory = message.get("trajectory", [])
        if not isinstance(trajectory, list):
            future.set_exception(AgentExecutionError("Agent result trajectory must be an array"))
            return
        future.set_result(
            AgentExecutionResult(
                task_id=task_id,
                output=message.get("output"),
                meta=meta,
                success=success,
                trajectory=trajectory,
            )
        )

    async def _remove_session(self, connection_id: str, expected: _ConnectedAgent) -> None:
        removed = False
        async with self._lock:
            if self._sessions.get(connection_id) is expected:
                self._sessions.pop(connection_id, None)
                removed = True
        if not removed:
            return
        error = AgentConnectionUnavailableError("Agent connection disconnected")
        for future in expected.pending.values():
            if not future.done():
                future.set_exception(error)
        expected.pending.clear()
        try:
            self.repository.record_disconnected(connection_id)
        except StorageNotFoundError:
            pass


class AgentConnectionService:
    def __init__(
        self,
        repository: AgentConnectionRepository,
        secrets_store: SecretStore,
        hub: AgentConnectionHub,
        managed_code: ManagedCodeLifecycle | None = None,
    ) -> None:
        self.repository = repository
        self.secrets = secrets_store
        self.hub = hub
        self.managed_code = managed_code

    def create(self, *, name: str, description: str | None = None) -> AgentConnectionCredential:
        connection_id = str(uuid4())
        secret_ref = self._secret_ref(connection_id)
        token = secrets.token_urlsafe(32)
        self.secrets.put(secret_ref, token)
        try:
            connection = self.repository.create(
                connection_id=connection_id,
                name=name,
                description=description,
                token_secret_ref=secret_ref,
                token_hint=self._token_hint(token),
            )
        except BaseException:
            self.secrets.delete(secret_ref)
            raise
        return AgentConnectionCredential(connection=connection, token=token)

    def get(self, connection_id: str):
        connection = self.repository.get(connection_id)
        if connection is None:
            raise NotFoundError(
                "AGENT_CONNECTION_NOT_FOUND",
                f"Agent connection not found: {connection_id}",
            )
        return connection

    def list(self):
        return self.repository.list()

    def health(self, connection_id: str) -> AgentConnectionHealth:
        connection = self.get(connection_id)
        online = self.hub.is_connected(connection_id)
        return AgentConnectionHealth(
            connection_id=connection_id,
            status="CONNECTED" if online else "OFFLINE",
            healthy=online,
            last_seen_at=connection.last_seen_at,
            message="Agent WebSocket is connected" if online else "Agent WebSocket is offline",
        )

    async def delete(self, connection_id: str) -> None:
        connection = self.get(connection_id)
        if self.managed_code is not None:
            await self.managed_code.delete(connection_id)
        self.repository.soft_delete(connection_id)
        await self.hub.disconnect(connection_id, reason="Agent connection was deleted")
        self.secrets.delete(connection.token_secret_ref)

    async def rotate_token(self, connection_id: str) -> AgentConnectionCredential:
        current = self.get(connection_id)
        token = secrets.token_urlsafe(32)
        secret_ref = self._secret_ref(connection_id)
        self.secrets.put(secret_ref, token)
        try:
            updated = self.repository.rotate_token(
                connection_id,
                token_secret_ref=secret_ref,
                token_hint=self._token_hint(token),
            )
        except BaseException:
            self.secrets.delete(secret_ref)
            raise
        if self.managed_code is not None:
            await self.managed_code.stop(connection_id)
        await self.hub.disconnect(connection_id, reason="Agent connection token was rotated")
        if current.token_secret_ref != secret_ref:
            self.secrets.delete(current.token_secret_ref)
        return AgentConnectionCredential(connection=updated, token=token)

    def authenticate(self, connection_id: str, token: str) -> bool:
        connection = self.repository.get(connection_id)
        if connection is None or not token:
            return False
        try:
            expected = self.secrets.get(connection.token_secret_ref)
        except (KeyError, RuntimeError):
            return False
        return hmac.compare_digest(expected, token)

    async def connect(self, connection_id: str, token: str, websocket: WebSocket) -> None:
        try:
            self.get(connection_id)
        except NotFoundError:
            await websocket.close(code=4404, reason="Agent connection not found")
            return
        if not self.authenticate(connection_id, token):
            await websocket.close(code=4401, reason="Invalid Agent connection token")
            return
        try:
            await self.hub.connect(connection_id, websocket)
        except AgentConnectionUnavailableError:
            # The Hub already closed the competing socket with 4409.
            return

    @staticmethod
    def _secret_ref(connection_id: str) -> str:
        return f"agent-connection/{connection_id}/token/{uuid4()}"

    @staticmethod
    def _token_hint(token: str) -> str:
        return f"****{token[-4:]}"
