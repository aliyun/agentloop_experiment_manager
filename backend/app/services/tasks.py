from __future__ import annotations

import asyncio
from collections.abc import Awaitable


class BackgroundTaskSupervisor:
    """Tracks handoff tasks so shutdown never silently abandons them."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[object]] = set()

    def start(self, awaitable: Awaitable[object]) -> asyncio.Task[object]:
        task = asyncio.create_task(awaitable)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def close(self, timeout_seconds: float = 5.0) -> None:
        if not self._tasks:
            return
        done, pending = await asyncio.wait(self._tasks, timeout=timeout_seconds)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            try:
                task.result()
            except BaseException:
                # State persistence belongs to the supervised coroutine. Shutdown
                # should not hide it by replacing the original outcome.
                pass
