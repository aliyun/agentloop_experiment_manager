"""Small customer-side connector SDK for attaching a local Agent.

The Agent initiates the WebSocket, so the experiment platform never needs an
inbound route to the customer's Agent process.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import websockets


@dataclass(frozen=True, slots=True)
class LocalAgentResponse:
    output: Any
    meta: dict[str, Any] = field(default_factory=dict)
    success: bool = True
    trajectory: list[Any] = field(default_factory=list)


LocalAgentHandler = Callable[
    [dict[str, Any]],
    Awaitable[LocalAgentResponse | Any],
]


class AgentConnector:
    """Maintain an authenticated Agent-initiated connection to the launcher."""

    def __init__(
        self,
        *,
        connect_url: str,
        token: str,
        handler: LocalAgentHandler,
        reconnect: bool = True,
    ) -> None:
        if not token:
            raise ValueError("Agent connection token is required")
        self.connect_url = _websocket_url(connect_url)
        self._token = token
        self.handler = handler
        self.reconnect = reconnect

    async def run(self) -> None:
        delay = 1.0
        while True:
            try:
                await self._connected_loop()
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception:
                if not self.reconnect:
                    raise
            if not self.reconnect:
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def _connected_loop(self) -> None:
        headers = {"Authorization": f"Bearer {self._token}"}
        async with websockets.connect(
            self.connect_url,
            additional_headers=headers,
            open_timeout=10,
            ping_interval=20,
            ping_timeout=20,
            max_size=4 * 1024 * 1024,
        ) as websocket:
            registered = await websocket.recv()
            message = _decode_message(registered)
            if message.get("type") != "registered" or message.get("protocolVersion") != 1:
                raise RuntimeError("Launcher returned an unsupported Agent protocol handshake")
            await websocket.send(_encode_message({"type": "ready"}))
            async for raw in websocket:
                message = _decode_message(raw)
                if message.get("type") == "heartbeat_ack":
                    continue
                if message.get("type") != "task":
                    raise RuntimeError("Launcher sent an unsupported Agent protocol message")
                await self._handle_task(websocket, message)

    async def _handle_task(self, websocket: Any, message: dict[str, Any]) -> None:
        task_id = message.get("taskId")
        payload = message.get("payload")
        if not isinstance(task_id, str) or not task_id or not isinstance(payload, dict):
            raise RuntimeError("Launcher task envelope is malformed")
        try:
            response = await self.handler(payload)
            if isinstance(response, LocalAgentResponse):
                output = response.output
                meta = response.meta
                success = response.success
                trajectory = response.trajectory
            else:
                output = response
                meta = {}
                success = True
                trajectory = []
            await websocket.send(
                _encode_message(
                    {
                        "type": "result",
                        "taskId": task_id,
                        "success": success,
                        "output": output,
                        "trajectory": trajectory,
                        "meta": meta,
                    }
                )
            )
        except Exception as exc:
            await websocket.send(
                _encode_message(
                    {
                        "type": "error",
                        "taskId": task_id,
                        "error": f"{type(exc).__name__}: {str(exc)[:800]}",
                    }
                )
            )


def _websocket_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.netloc:
        raise ValueError("connect_url must be an absolute HTTP(S) or WebSocket URL")
    scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme)
    return urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment))


def _decode_message(raw: str | bytes) -> dict[str, Any]:
    import json

    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError("Agent protocol message must be a JSON object")
    return value


def _encode_message(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
