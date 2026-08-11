"""Pure misfire and local-overlap policy decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from app.storage.models import MisfirePolicy, OverlapPolicy, RunnerState, as_utc


@dataclass(frozen=True, slots=True)
class MisfireDecision:
    fire_slot_utc: datetime | None
    skipped_slots_utc: tuple[datetime, ...]


def evaluate_misfires(
    missed_slots_utc: Iterable[datetime],
    *,
    now_utc: datetime,
    policy: str,
    grace_seconds: int,
    on_time_tolerance_seconds: int = 0,
) -> MisfireDecision:
    """Choose at most one recovery launch and identify every skipped slot."""

    try:
        selected_policy = MisfirePolicy(policy)
    except ValueError as exc:
        raise ValueError(f"unsupported misfire policy: {policy}") from exc
    if grace_seconds < 0:
        raise ValueError("grace_seconds must be non-negative")
    if on_time_tolerance_seconds < 0:
        raise ValueError("on_time_tolerance_seconds must be non-negative")
    now = as_utc(now_utc)
    slots = tuple(sorted({as_utc(slot) for slot in missed_slots_utc}))
    if any(slot > now for slot in slots):
        raise ValueError("misfire input cannot contain a future slot")
    if not slots:
        return MisfireDecision(fire_slot_utc=None, skipped_slots_utc=())
    latest = slots[-1]
    if selected_policy == MisfirePolicy.SKIP:
        # A scheduler always observes a due slot slightly after its exact wall
        # clock instant.  SKIP applies to genuinely missed slots, not to the
        # current slot seen within the bounded polling tolerance.
        if now - latest <= timedelta(seconds=on_time_tolerance_seconds):
            return MisfireDecision(fire_slot_utc=latest, skipped_slots_utc=slots[:-1])
        return MisfireDecision(fire_slot_utc=None, skipped_slots_utc=slots)

    if now - latest <= timedelta(seconds=grace_seconds):
        return MisfireDecision(fire_slot_utc=latest, skipped_slots_utc=slots[:-1])
    return MisfireDecision(fire_slot_utc=None, skipped_slots_utc=slots)


@dataclass(frozen=True, slots=True)
class OverlapDecision:
    should_skip: bool
    reason: str | None = None


def evaluate_overlap(
    prior_runner_states: Iterable[str] | None,
    *,
    policy: str = OverlapPolicy.FORBID_LOCAL_OVERLAP.value,
    status_lookup_failed: bool = False,
) -> OverlapDecision:
    """Skip only while a previous local runner is known to still be running."""

    try:
        OverlapPolicy(policy)
    except ValueError as exc:
        raise ValueError(f"unsupported overlap policy: {policy}") from exc
    if status_lookup_failed or prior_runner_states is None:
        return OverlapDecision(True, "RUNNER_STATUS_UNAVAILABLE")

    states = set(prior_runner_states)
    known = {state.value for state in RunnerState}
    if states - known:
        return OverlapDecision(True, "UNRECOGNIZED_RUNNER_STATE")
    if RunnerState.RUNNING.value in states:
        return OverlapDecision(True, "OVERLAP")
    return OverlapDecision(False, None)
