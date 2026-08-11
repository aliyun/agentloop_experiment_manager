"""Pure schedule-time calculations, including IANA timezone and DST rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.storage.models import CadenceType, as_utc


@dataclass(frozen=True, slots=True)
class ScheduleTiming:
    cadence_type: str
    timezone: str
    interval_hours: int | None = None
    interval_minutes: int | None = None
    daily_time: str | None = None
    anchor_at: datetime | None = None

    def validate(self) -> "ScheduleTiming":
        try:
            CadenceType(self.cadence_type)
        except ValueError as exc:
            raise ValueError(f"unsupported cadence_type: {self.cadence_type}") from exc
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"invalid IANA timezone: {self.timezone}") from exc

        if self.cadence_type == CadenceType.INTERVAL_MINUTES.value:
            if (
                not isinstance(self.interval_minutes, int)
                or isinstance(self.interval_minutes, bool)
                or self.interval_minutes < 5
            ):
                raise ValueError("interval_minutes must be an integer greater than or equal to 5")
            if self.interval_hours is not None:
                raise ValueError("interval_hours must be null for INTERVAL_MINUTES")
            if self.anchor_at is None:
                raise ValueError("anchor_at is required for INTERVAL_MINUTES")
            as_utc(self.anchor_at)
            if self.daily_time is not None:
                raise ValueError("daily_time must be null for INTERVAL_MINUTES")
        elif self.cadence_type == CadenceType.INTERVAL_HOURS.value:
            if (
                not isinstance(self.interval_hours, int)
                or isinstance(self.interval_hours, bool)
                or self.interval_hours <= 0
            ):
                raise ValueError("interval_hours must be a positive integer")
            if self.anchor_at is None:
                raise ValueError("anchor_at is required for INTERVAL_HOURS")
            as_utc(self.anchor_at)
            if self.daily_time is not None:
                raise ValueError("daily_time must be null for INTERVAL_HOURS")
            if self.interval_minutes is not None:
                raise ValueError("interval_minutes must be null for INTERVAL_HOURS")
        else:
            _parse_daily_time(self.daily_time)
            if self.interval_hours is not None:
                raise ValueError("interval_hours must be null for DAILY")
            if self.interval_minutes is not None:
                raise ValueError("interval_minutes must be null for DAILY")
        return self


def _parse_daily_time(value: str | None) -> time:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise ValueError("daily_time must use HH:mm")
    try:
        hour = int(value[:2])
        minute = int(value[3:])
        return time(hour=hour, minute=minute)
    except (ValueError, TypeError) as exc:
        raise ValueError("daily_time must use HH:mm (00:00-23:59)") from exc


def _valid_utc_instants(local_naive: datetime, zone: ZoneInfo) -> tuple[datetime, ...]:
    """Return UTC instants that round-trip to a local wall-clock value.

    There are zero instants during a DST gap, one normally, and two during a
    fallback fold.  Round-tripping is required because simply attaching a
    ``ZoneInfo`` object does not detect nonexistent local times.
    """

    candidates: set[datetime] = set()
    for fold in (0, 1):
        attached = local_naive.replace(tzinfo=zone, fold=fold)
        utc_candidate = attached.astimezone(UTC)
        round_trip = utc_candidate.astimezone(zone)
        if round_trip.replace(tzinfo=None) == local_naive and round_trip.fold == fold:
            candidates.add(utc_candidate)
    return tuple(sorted(candidates))


def resolve_daily_slot(local_date: date, daily_time: str, timezone: str) -> datetime:
    """Resolve one local daily slot to its unique UTC instant.

    - In a spring-forward gap, run at the first valid wall-clock minute after
      the requested time (for example 02:30 becomes 03:00).
    - In a fallback fold, run only the first occurrence (the earliest UTC
      instant), never both occurrences.
    """

    wall_time = _parse_daily_time(daily_time)
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"invalid IANA timezone: {timezone}") from exc

    requested = datetime.combine(local_date, wall_time)
    # HH:mm is minute-granular.  A 26-hour bound also handles unusual historic
    # transitions while remaining finite and deterministic.
    for minute_offset in range(26 * 60 + 1):
        candidate = requested + timedelta(minutes=minute_offset)
        instants = _valid_utc_instants(candidate, zone)
        if instants:
            return instants[0]
    raise ValueError(f"could not resolve local time {requested!s} in {timezone}")


def next_interval_fire(
    *,
    anchor_at: datetime,
    interval_hours: int,
    after_utc: datetime,
    inclusive: bool = False,
) -> datetime:
    """Return the next absolute-duration interval slot in UTC."""

    if (
        not isinstance(interval_hours, int)
        or isinstance(interval_hours, bool)
        or interval_hours <= 0
    ):
        raise ValueError("interval_hours must be a positive integer")
    anchor = as_utc(anchor_at)
    after = as_utc(after_utc)
    period = timedelta(hours=interval_hours)
    if after < anchor:
        return anchor
    elapsed = after - anchor
    periods = elapsed // period
    remainder = elapsed % period
    if inclusive and remainder == timedelta(0):
        return anchor + periods * period
    return anchor + (periods + 1) * period


def next_minute_interval_fire(
    *,
    anchor_at: datetime,
    interval_minutes: int,
    after_utc: datetime,
    inclusive: bool = False,
) -> datetime:
    """Return the next absolute-duration minute interval slot in UTC."""

    if (
        not isinstance(interval_minutes, int)
        or isinstance(interval_minutes, bool)
        or interval_minutes < 5
    ):
        raise ValueError("interval_minutes must be an integer greater than or equal to 5")
    anchor = as_utc(anchor_at)
    after = as_utc(after_utc)
    period = timedelta(minutes=interval_minutes)
    if after < anchor:
        return anchor
    elapsed = after - anchor
    periods = elapsed // period
    remainder = elapsed % period
    if inclusive and remainder == timedelta(0):
        return anchor + periods * period
    return anchor + (periods + 1) * period


def next_daily_fire(
    *,
    daily_time: str,
    timezone: str,
    after_utc: datetime,
    inclusive: bool = False,
) -> datetime:
    """Return the next daily wall-clock slot under the SPEC DST rules."""

    after = as_utc(after_utc)
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"invalid IANA timezone: {timezone}") from exc
    local_date = after.astimezone(zone).date()
    for day_offset in range(370):
        candidate = resolve_daily_slot(
            local_date + timedelta(days=day_offset), daily_time, timezone
        )
        if candidate > after or (inclusive and candidate == after):
            return candidate
    raise ValueError("could not find a daily slot in the next 370 local days")


def next_fire(
    timing: ScheduleTiming,
    *,
    after_utc: datetime,
    inclusive: bool = False,
) -> datetime:
    """Return the next UTC fire slot for either supported cadence."""

    timing.validate()
    if timing.cadence_type == CadenceType.INTERVAL_MINUTES.value:
        assert timing.anchor_at is not None and timing.interval_minutes is not None
        return next_minute_interval_fire(
            anchor_at=timing.anchor_at,
            interval_minutes=timing.interval_minutes,
            after_utc=after_utc,
            inclusive=inclusive,
        )
    if timing.cadence_type == CadenceType.INTERVAL_HOURS.value:
        assert timing.anchor_at is not None and timing.interval_hours is not None
        return next_interval_fire(
            anchor_at=timing.anchor_at,
            interval_hours=timing.interval_hours,
            after_utc=after_utc,
            inclusive=inclusive,
        )
    assert timing.daily_time is not None
    return next_daily_fire(
        daily_time=timing.daily_time,
        timezone=timing.timezone,
        after_utc=after_utc,
        inclusive=inclusive,
    )


def fire_times_between(
    timing: ScheduleTiming,
    *,
    after_exclusive_utc: datetime,
    through_inclusive_utc: datetime,
    max_slots: int = 10_000,
) -> tuple[datetime, ...]:
    """Enumerate UTC slots in a bounded interval.

    The bound prevents an accidental unbounded historical replay.  Misfire
    policy is applied separately to this deterministic list.
    """

    if max_slots < 1:
        raise ValueError("max_slots must be positive")
    after = as_utc(after_exclusive_utc)
    through = as_utc(through_inclusive_utc)
    if through <= after:
        return ()

    slots: list[datetime] = []
    cursor = after
    while True:
        slot = next_fire(timing, after_utc=cursor)
        if slot > through:
            return tuple(slots)
        if len(slots) >= max_slots:
            raise OverflowError(
                f"more than {max_slots} schedule slots fall inside the requested interval"
            )
        slots.append(slot)
        cursor = slot


def schedule_slot_idempotency_key(
    schedule_id: str, revision: int, scheduled_for_utc: datetime
) -> str:
    """Build the stable key shared by scheduler ticks and crash recovery."""

    if not schedule_id.strip():
        raise ValueError("schedule_id must not be empty")
    if revision < 1:
        raise ValueError("revision must be positive")
    slot = as_utc(scheduled_for_utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    return f"schedule:{schedule_id}:{revision}:{slot}"
