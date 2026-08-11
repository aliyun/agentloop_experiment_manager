from __future__ import annotations

import asyncio
import fcntl
import math
import os
from pathlib import Path
from typing import TYPE_CHECKING

from app.domain.clock import Clock
from app.storage import (
    ConcurrentUpdateError,
    ScheduleRepository,
    SkipReason,
)

from .policies import evaluate_misfires
from .timing import ScheduleTiming, fire_times_between, next_fire

if TYPE_CHECKING:
    from app.services.launches import LaunchService


class SchedulerAlreadyRunningError(RuntimeError):
    pass


class SchedulerWorker:
    """Single-process scheduler with a deployment-level exclusive file lock."""

    def __init__(
        self,
        *,
        schedules: ScheduleRepository,
        launches: LaunchService,
        clock: Clock,
        poll_seconds: float,
        lock_path: Path | None,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self.schedules = schedules
        self.launches = launches
        self.clock = clock
        self.poll_seconds = poll_seconds
        self.lock_path = lock_path
        self._lock_fd: int | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._acquire_lock()
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="agentloop-scheduler")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self._release_lock()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Per-schedule errors are persisted in tick. An unexpected loop
                # error is retried on the next bounded poll rather than killing
                # all future schedules.
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                continue

    async def tick(self) -> int:
        now = self.clock.now()
        due = self.schedules.due(now)
        for schedule in due:
            await self._process(schedule, now)
        return len(due)

    async def _process(self, schedule, now) -> None:
        first = schedule.next_fire_at_utc
        if first is None:
            return
        timing = ScheduleTiming(
            cadence_type=schedule.cadence_type,
            timezone=schedule.timezone,
            interval_hours=schedule.interval_hours,
            interval_minutes=schedule.interval_minutes,
            daily_time=schedule.daily_time,
            anchor_at=schedule.anchor_at,
        )
        try:
            later = fire_times_between(
                timing,
                after_exclusive_utc=first,
                through_inclusive_utc=now,
                max_slots=10_000,
            )
            slots = (first, *later)
            decision = evaluate_misfires(
                slots,
                now_utc=now,
                policy=schedule.misfire_policy,
                grace_seconds=schedule.misfire_grace_seconds,
                on_time_tolerance_seconds=max(1, math.ceil(self.poll_seconds * 2)),
            )
            for slot in decision.skipped_slots_utc:
                self.launches.skip_schedule(schedule, slot, SkipReason.MISFIRE.value)
            if decision.fire_slot_utc is not None:
                await self.launches.scheduled(schedule, decision.fire_slot_utc)
            following = next_fire(timing, after_utc=now)
            processed_slot = decision.fire_slot_utc or max(slots)
            self.schedules.record_fire(
                schedule.id,
                fired_at_utc=processed_slot,
                next_fire_at_utc=following,
                expected_revision=schedule.revision,
            )
        except ConcurrentUpdateError:
            # A newer schedule revision owns the next-fire calculation.
            return
        except Exception as exc:
            # A failed attempt must not pin next_fire to the same due slot and
            # retry it on every poll.  The immutable LaunchIntent captures the
            # failed slot; the schedule advances to the first future slot.
            try:
                following = next_fire(timing, after_utc=now)
                self.schedules.set_next_fire(
                    schedule.id,
                    following,
                    expected_revision=schedule.revision,
                )
            except ConcurrentUpdateError:
                return
            except Exception:
                pass
            self.schedules.record_error(
                schedule.id,
                code="SCHEDULE_EXECUTION_FAILED",
                message=f"{type(exc).__name__}: schedule execution failed",
            )

    def _acquire_lock(self) -> None:
        if self.lock_path is None or self._lock_fd is not None:
            return
        self.lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise SchedulerAlreadyRunningError(f"another scheduler owns {self.lock_path}") from exc
        self._lock_fd = descriptor

    def _release_lock(self) -> None:
        if self._lock_fd is None:
            return
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(self._lock_fd)
            self._lock_fd = None
