"""Resolve a customer Agent invocation to one authoritative AgentLoop Trace.

The local launcher must not write an ``experiment_detail`` row until a Trace
from the AgentSpace-bound Trace Store has been found.  This module owns that
lookup boundary and deliberately returns only Trace identifiers and safe
diagnostics; Trace payloads never enter local persistence.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol
from xml.etree import ElementTree

from .models import AgentLoopConnection

_TRACE_ID_LENGTH = 32
_TRAJECTORY_LOGSTORE = "agent-trajectory"
_TRAJECTORY_PAGE_SIZE = 100
_TRAJECTORY_MAX_SCAN_PAGES = 5
_MAX_XML_SCAN_CHARS = 2_000_000
_MAX_TRAJECTORY_TOTAL_CHARS = 4_000_000
_MAX_TRAJECTORY_NODES = 20_000
_MAX_TRAJECTORY_DEPTH = 64


class TraceCorrelationStatus(str, Enum):
    PENDING = "PENDING"
    POLLING = "POLLING"
    RESOLVED = "RESOLVED"
    REPORTED = "REPORTED"
    AMBIGUOUS = "AMBIGUOUS"
    TIMED_OUT = "TIMED_OUT"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class TraceCorrelationContext:
    launch_id: str
    experiment_plan_id: str
    experiment_run_id: str
    dataset_item_id: str
    agent_started_at_ms: int
    agent_completed_at_ms: int

    def __post_init__(self) -> None:
        identifiers = {
            "launch_id": self.launch_id,
            "experiment_plan_id": self.experiment_plan_id,
            "experiment_run_id": self.experiment_run_id,
            "dataset_item_id": self.dataset_item_id,
        }
        for field, value in identifiers.items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must be a non-empty string")
        for field, value in (
            ("agent_started_at_ms", self.agent_started_at_ms),
            ("agent_completed_at_ms", self.agent_completed_at_ms),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{field} must be a positive integer")
        if self.agent_completed_at_ms < self.agent_started_at_ms:
            raise ValueError("agent_completed_at_ms cannot precede agent_started_at_ms")


@dataclass(frozen=True, slots=True)
class TracePollingPolicy:
    timeout_seconds: float = 180.0
    initial_interval_seconds: float = 2.0
    max_interval_seconds: float = 8.0
    backoff_multiplier: float = 2.0
    jitter_ratio: float = 0.2
    time_padding_seconds: int = 120

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.initial_interval_seconds < 0:
            raise ValueError("initial_interval_seconds cannot be negative")
        if self.max_interval_seconds < self.initial_interval_seconds:
            raise ValueError("max_interval_seconds cannot be smaller than the initial interval")
        if self.backoff_multiplier < 1:
            raise ValueError("backoff_multiplier must be at least one")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between zero and one")
        if self.time_padding_seconds < 0:
            raise ValueError("time_padding_seconds cannot be negative")


@dataclass(frozen=True, slots=True)
class TraceSearchRequest:
    context: TraceCorrelationContext
    start_time_ms: int
    end_time_ms: int
    hinted_trace_id: str | None = None
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        for field, value in (
            ("start_time_ms", self.start_time_ms),
            ("end_time_ms", self.end_time_ms),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{field} must be a positive integer")
        if self.end_time_ms < self.start_time_ms:
            raise ValueError("end_time_ms cannot precede start_time_ms")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


@dataclass(frozen=True, slots=True)
class TraceCandidate:
    trace_id: str
    source: str = "agent_trajectory_input_context"


@dataclass(frozen=True, slots=True)
class TraceCorrelationProgress:
    context: TraceCorrelationContext
    status: TraceCorrelationStatus
    attempts: int
    occurred_at_ms: int
    trace_id: str | None = None
    source: str | None = None
    deadline_at_ms: int | None = None
    error_code: str | None = None
    sanitized_message: str | None = None


@dataclass(frozen=True, slots=True)
class TraceCorrelationResult:
    trace_id: str
    source: str
    attempts: int
    started_at_ms: int
    resolved_at_ms: int


class TraceSearchError(RuntimeError):
    """A safe, classified Trace Store query failure."""

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.sanitized_message = message


class TraceCorrelationError(RuntimeError):
    """Terminal failure that prevents an experiment detail from being written."""

    def __init__(
        self,
        code: str,
        status: TraceCorrelationStatus,
        attempts: int,
        sanitized_message: str,
    ) -> None:
        super().__init__(sanitized_message)
        self.code = code
        self.status = status
        self.attempts = attempts
        self.sanitized_message = sanitized_message


class TraceSearchClient(Protocol):
    def search(self, request: TraceSearchRequest) -> list[TraceCandidate]: ...


ProgressCallback = Callable[[TraceCorrelationProgress], Awaitable[None] | None]


def validate_trace_id(value: Any) -> str | None:
    candidate = str(value or "")
    if len(candidate) != _TRACE_ID_LENGTH:
        return None
    if any(character not in "0123456789abcdefABCDEF" for character in candidate):
        return None
    if not any(character != "0" for character in candidate):
        return None
    return candidate.lower()


class AgentSpaceTrajectoryLogStoreClient:
    """Resolve Trace IDs from the AgentSpace-bound SLS trajectory Logstore.

    The project is obtained from ``GetAgentSpace.slsProject`` and the source is
    the fixed ``agent-trajectory`` Logstore in that project.  XTrace and
    ``ListTracePreview`` are deliberately not part of this lookup path.
    """

    def __init__(
        self,
        connection: AgentLoopConnection,
        *,
        user_agent: str,
        sls_project: str | None = None,
        agentloop_client: Any | None = None,
        sls_client: Any | None = None,
    ) -> None:
        self.connection = connection
        self.user_agent = user_agent
        self._sls_project = sls_project.strip() if sls_project else None
        self._agentloop_client = agentloop_client
        self._sls_client = sls_client
        self._source_verified = False

    def _get_agentloop_client(self) -> Any:
        if self._agentloop_client is not None:
            return self._agentloop_client
        from agentloop_sdk._vendor.alibabacloud_agentloop20260520.client import Client
        from alibabacloud_tea_openapi import utils_models as open_api_models

        self._agentloop_client = Client(
            open_api_models.Config(
                access_key_id=self.connection.access_key_id,
                access_key_secret=self.connection.access_key_secret,
                region_id=self.connection.region_id,
                endpoint=self.connection.api_endpoint
                or f"agentloop.{self.connection.region_id}.aliyuncs.com",
                connect_timeout=10_000,
                read_timeout=30_000,
                user_agent=self.user_agent,
            )
        )
        return self._agentloop_client

    def _resolve_sls_project(self) -> str:
        if self._sls_project is not None:
            return self._sls_project
        from agentloop_sdk._vendor.alibabacloud_agentloop20260520 import (
            models as api_models,
        )

        try:
            response = self._get_agentloop_client().get_agent_space(
                self.connection.agent_space,
                api_models.GetAgentSpaceRequest(),
            )
        except Exception as exc:
            raise _classify_search_exception(exc) from None
        project = str(getattr(getattr(response, "body", None), "sls_project", "") or "").strip()
        if not project:
            raise TraceSearchError(
                "TRACE_SOURCE_NOT_FOUND",
                "The AgentSpace has no bound SLS project",
                retryable=False,
            )
        self._sls_project = project
        return project

    def _get_sls_client(self) -> Any:
        if self._sls_client is not None:
            return self._sls_client
        from aliyun.log import LogClient

        self._sls_client = LogClient(
            f"{self.connection.region_id}.log.aliyuncs.com",
            self.connection.access_key_id,
            self.connection.access_key_secret,
            region=self.connection.region_id,
        )
        self._sls_client.set_user_agent(self.user_agent)
        return self._sls_client

    def _verify_source(self, project: str, client: Any) -> None:
        if self._source_verified:
            return
        try:
            client.get_logstore(project, _TRAJECTORY_LOGSTORE)
        except Exception as exc:
            raise _classify_search_exception(exc) from None
        self._source_verified = True

    def search(self, request: TraceSearchRequest) -> list[TraceCandidate]:
        deadline_monotonic = (
            time.monotonic() + request.timeout_seconds
            if request.timeout_seconds is not None
            else None
        )
        project = self._resolve_sls_project()
        client = self._get_sls_client()
        remaining_seconds = _remaining_search_seconds(deadline_monotonic)
        if remaining_seconds is not None and hasattr(client, "timeout"):
            client.timeout = max(0.1, min(30.0, remaining_seconds))
        self._verify_source(project, client)

        strategies: list[tuple[str, str]] = []
        if request.hinted_trace_id is not None:
            strategies.append(
                (
                    "agent_trajectory_trace_id",
                    _build_trace_id_query(request.hinted_trace_id),
                )
            )
        strategies.extend(
            (
                (
                    "agent_trajectory_input_context",
                    _build_input_context_query(request.context),
                ),
                (
                    "agent_trajectory_line_context",
                    _build_line_context_query(request.context),
                ),
            )
        )

        discovered: dict[str, str] = {}
        line_query_found = False
        for source, query in strategies:
            trace_ids = self._query_trajectory_context(
                client,
                project,
                request,
                query=query,
                deadline_monotonic=deadline_monotonic,
            )
            if source == "agent_trajectory_line_context" and trace_ids:
                line_query_found = True
            for trace_id in trace_ids:
                discovered.setdefault(trace_id, source)
            if len(discovered) > 1:
                return [TraceCandidate(value, source=found) for value, found in discovered.items()]

        # A complete and accurate full-line query is a superset of the input
        # query and proves that no second indexed trajectory carries the same
        # five context terms.  If it found nothing, perform the bounded raw
        # scan as the final proof path (including when a narrower index behaved
        # inconsistently and did return a row).
        if line_query_found:
            return [TraceCandidate(value, source=found) for value, found in discovered.items()]

        trace_ids = self._query_trajectory_context(
            client,
            project,
            request,
            query="*",
            deadline_monotonic=deadline_monotonic,
        )
        if discovered and not set(discovered).issubset(trace_ids):
            raise TraceSearchError(
                "TRAJECTORY_QUERY_INCONSISTENT",
                "SLS trajectory query strategies returned inconsistent results",
                retryable=True,
            )
        for trace_id in trace_ids:
            discovered.setdefault(trace_id, "agent_trajectory_scan_context")
        return [TraceCandidate(value, source=found) for value, found in discovered.items()]

    def _query_trajectory_context(
        self,
        client: Any,
        project: str,
        request: TraceSearchRequest,
        *,
        query: str,
        deadline_monotonic: float | None,
    ) -> list[str]:
        from aliyun.log import GetLogsRequest

        from_time = max(1, request.start_time_ms // 1000)
        to_time = max(from_time + 1, (request.end_time_ms + 999) // 1000)
        offset = 0
        trace_ids: list[str] = []
        for _ in range(_TRAJECTORY_MAX_SCAN_PAGES):
            remaining_seconds = _remaining_search_seconds(deadline_monotonic)
            if remaining_seconds is not None and hasattr(client, "timeout"):
                client.timeout = max(0.1, min(30.0, remaining_seconds))
            api_request = GetLogsRequest(
                project,
                _TRAJECTORY_LOGSTORE,
                from_time,
                to_time,
                query=query,
                line=_TRAJECTORY_PAGE_SIZE,
                offset=offset,
                reverse=False,
                accurate_query=True,
            )
            try:
                response = client.get_logs(api_request)
            except Exception as exc:
                raise _classify_search_exception(exc) from None
            logs = _validated_trajectory_logs(response)
            for log in logs:
                contents = log.get_contents()
                if not isinstance(contents, dict):
                    raise _trajectory_response_error()
                if _trajectory_log_matches_context(contents, request.context):
                    trace_id = _trace_id_from_trajectory(contents)
                    if trace_id not in trace_ids:
                        trace_ids.append(trace_id)
                    if len(trace_ids) > 1:
                        return trace_ids
            if len(logs) < _TRAJECTORY_PAGE_SIZE:
                return trace_ids
            offset += len(logs)
        raise TraceSearchError(
            "TRAJECTORY_SCAN_LIMIT_EXCEEDED",
            "Agent trajectory lookup reached the bounded SLS scan limit",
            retryable=False,
        )


class TraceCorrelationService:
    def __init__(
        self,
        search_client: TraceSearchClient,
        *,
        policy: TracePollingPolicy | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time_ms: Callable[[], int] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random_value: Callable[[], float] = random.random,
    ) -> None:
        self.search_client = search_client
        self.policy = policy or TracePollingPolicy()
        self.monotonic = monotonic
        self.wall_time_ms = wall_time_ms or (lambda: int(time.time() * 1000))
        self.sleep = sleep
        self.random_value = random_value

    async def resolve(
        self,
        context: TraceCorrelationContext,
        *,
        hinted_trace_id: str | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> TraceCorrelationResult:
        normalized_hint = validate_trace_id(hinted_trace_id)
        if hinted_trace_id is not None and normalized_hint is None:
            raise TraceCorrelationError(
                "TRACE_ID_INVALID",
                TraceCorrelationStatus.FAILED,
                0,
                "Customer Agent returned an invalid Trace ID",
            )

        started_monotonic = self.monotonic()
        started_at_ms = self.wall_time_ms()
        deadline_monotonic = started_monotonic + self.policy.timeout_seconds
        deadline_at_ms = started_at_ms + int(self.policy.timeout_seconds * 1000)
        await _notify(
            on_progress,
            TraceCorrelationProgress(
                context=context,
                status=TraceCorrelationStatus.PENDING,
                attempts=0,
                occurred_at_ms=started_at_ms,
                deadline_at_ms=deadline_at_ms,
            ),
        )

        attempts = 0
        interval = self.policy.initial_interval_seconds
        last_retryable_error: TraceSearchError | None = None
        while True:
            attempts += 1
            now_ms = self.wall_time_ms()
            await _notify(
                on_progress,
                TraceCorrelationProgress(
                    context=context,
                    status=TraceCorrelationStatus.POLLING,
                    attempts=attempts,
                    occurred_at_ms=now_ms,
                    deadline_at_ms=deadline_at_ms,
                ),
            )
            request = TraceSearchRequest(
                context=context,
                start_time_ms=max(
                    1,
                    context.agent_started_at_ms - self.policy.time_padding_seconds * 1000,
                ),
                # SLS filters the trajectory Logstore on event time, not
                # ingestion time. Keep the invocation window fixed while
                # polling so a busy AgentSpace cannot make every retry
                # progressively more expensive.
                end_time_ms=context.agent_completed_at_ms + self.policy.time_padding_seconds * 1000,
                hinted_trace_id=normalized_hint,
                timeout_seconds=max(0.001, deadline_monotonic - self.monotonic()),
            )
            try:
                candidates = await asyncio.to_thread(self.search_client.search, request)
                last_retryable_error = None
            except TraceSearchError as exc:
                if not exc.retryable:
                    await self._raise_terminal(
                        context,
                        on_progress,
                        code=exc.code,
                        status=TraceCorrelationStatus.FAILED,
                        attempts=attempts,
                        message=exc.sanitized_message,
                        deadline_at_ms=deadline_at_ms,
                    )
                last_retryable_error = exc
                candidates = []
            except Exception:
                last_retryable_error = TraceSearchError(
                    "TRACE_SEARCH_FAILED",
                    "AgentLoop Trace lookup failed",
                    retryable=True,
                )
                candidates = []

            distinct = {candidate.trace_id for candidate in candidates}
            if len(distinct) > 1:
                await self._raise_terminal(
                    context,
                    on_progress,
                    code="TRACE_AMBIGUOUS",
                    status=TraceCorrelationStatus.AMBIGUOUS,
                    attempts=attempts,
                    message="Multiple Trace IDs matched one experiment item",
                    deadline_at_ms=deadline_at_ms,
                )
            if len(distinct) == 1:
                trace_id = next(iter(distinct))
                if normalized_hint is not None and trace_id != normalized_hint:
                    await self._raise_terminal(
                        context,
                        on_progress,
                        code="TRACE_HINT_MISMATCH",
                        status=TraceCorrelationStatus.FAILED,
                        attempts=attempts,
                        message="Customer Agent Trace ID did not match the Trace Store",
                        deadline_at_ms=deadline_at_ms,
                    )
                resolved_at_ms = self.wall_time_ms()
                source = next(
                    candidate.source for candidate in candidates if candidate.trace_id == trace_id
                )
                await _notify(
                    on_progress,
                    TraceCorrelationProgress(
                        context=context,
                        status=TraceCorrelationStatus.RESOLVED,
                        attempts=attempts,
                        occurred_at_ms=resolved_at_ms,
                        trace_id=trace_id,
                        source=source,
                        deadline_at_ms=deadline_at_ms,
                    ),
                )
                return TraceCorrelationResult(
                    trace_id=trace_id,
                    source=source,
                    attempts=attempts,
                    started_at_ms=started_at_ms,
                    resolved_at_ms=resolved_at_ms,
                )

            remaining = deadline_monotonic - self.monotonic()
            if remaining <= 0:
                message = (
                    last_retryable_error.sanitized_message
                    if last_retryable_error is not None
                    else "No Trace became visible before the lookup deadline"
                )
                await self._raise_terminal(
                    context,
                    on_progress,
                    code="TRACE_LOOKUP_TIMED_OUT",
                    status=TraceCorrelationStatus.TIMED_OUT,
                    attempts=attempts,
                    message=message,
                    deadline_at_ms=deadline_at_ms,
                )

            delay = min(interval, remaining)
            if delay > 0 and self.policy.jitter_ratio:
                factor = 1 + (self.random_value() * 2 - 1) * self.policy.jitter_ratio
                delay = min(max(0.0, delay * factor), remaining)
            if delay > 0:
                await self.sleep(delay)
            interval = min(
                self.policy.max_interval_seconds,
                max(interval, 0.001) * self.policy.backoff_multiplier,
            )

    async def _raise_terminal(
        self,
        context: TraceCorrelationContext,
        on_progress: ProgressCallback | None,
        *,
        code: str,
        status: TraceCorrelationStatus,
        attempts: int,
        message: str,
        deadline_at_ms: int,
    ) -> None:
        await _notify(
            on_progress,
            TraceCorrelationProgress(
                context=context,
                status=status,
                attempts=attempts,
                occurred_at_ms=self.wall_time_ms(),
                deadline_at_ms=deadline_at_ms,
                error_code=code,
                sanitized_message=message,
            ),
        )
        raise TraceCorrelationError(code, status, attempts, message)


async def _notify(
    callback: ProgressCallback | None,
    progress: TraceCorrelationProgress,
) -> None:
    if callback is None:
        return
    result = callback(progress)
    if inspect.isawaitable(result):
        await result


def _build_trace_query(
    context: TraceCorrelationContext,
    hinted_trace_id: str | None,
) -> str:
    if hinted_trace_id is not None:
        return _build_trace_id_query(hinted_trace_id)
    return _build_input_context_query(context)


def _build_trace_id_query(trace_id: str) -> str:
    return f'trace_id: "{_escape_search_value(trace_id)}"'


def _context_search_values(context: TraceCorrelationContext) -> tuple[str, ...]:
    return (
        "experiment_context",
        context.experiment_plan_id,
        context.experiment_run_id,
        context.dataset_item_id,
        context.launch_id,
    )


def _build_input_context_query(context: TraceCorrelationContext) -> str:
    return " and ".join(
        f'input: "{_escape_search_value(value)}"' for value in _context_search_values(context)
    )


def _build_line_context_query(context: TraceCorrelationContext) -> str:
    return " and ".join(
        f'"{_escape_search_value(value)}"' for value in _context_search_values(context)
    )


def _validated_trajectory_logs(response: Any) -> list[Any]:
    try:
        meta = response.get_meta()
        completed = response.is_completed()
        accurate = meta.get_is_accurate()
        limited = meta.get_limited()
        count = response.get_count()
        logs = response.get_logs()
    except Exception:
        raise _trajectory_response_error() from None
    if completed is not True or accurate is not True:
        raise TraceSearchError(
            "TRAJECTORY_QUERY_INCOMPLETE",
            "The SLS trajectory query was not complete and accurate",
            retryable=True,
        )
    if limited:
        raise TraceSearchError(
            "TRAJECTORY_QUERY_LIMITED",
            "The SLS trajectory query returned a limited result",
            retryable=False,
        )
    if (
        not isinstance(logs, list)
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count != len(logs)
        or any(not callable(getattr(log, "get_contents", None)) for log in logs)
    ):
        raise _trajectory_response_error()
    return logs


def _remaining_search_seconds(deadline_monotonic: float | None) -> float | None:
    if deadline_monotonic is None:
        return None
    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        raise TraceSearchError(
            "TRACE_SEARCH_DEADLINE_EXCEEDED",
            "Agent trajectory lookup exceeded the polling deadline",
            retryable=True,
        )
    return remaining


def _trajectory_response_error() -> TraceSearchError:
    return TraceSearchError(
        "TRAJECTORY_INVALID_RESPONSE",
        "The SLS trajectory query returned an invalid response",
        retryable=True,
    )


def _payload_limit_error() -> TraceSearchError:
    return TraceSearchError(
        "TRAJECTORY_PAYLOAD_LIMIT_EXCEEDED",
        "The agent trajectory exceeded the safe in-memory inspection limit",
        retryable=False,
    )


def _trace_id_from_trajectory(contents: dict[str, Any]) -> str:
    trace_id = validate_trace_id(contents.get("trace_id") or contents.get("traceId"))
    if trace_id is None:
        raise TraceSearchError(
            "TRAJECTORY_TRACE_ID_INVALID",
            "A matching agent trajectory has no valid Trace ID",
            retryable=False,
        )
    return trace_id


def _trajectory_log_matches_context(
    contents: dict[str, Any],
    context: TraceCorrelationContext,
) -> bool:
    # ``output`` is intentionally excluded: an Agent echoing another request's
    # context in its answer is not proof that this trajectory executed it.
    for field in ("input", "atif_content"):
        value = contents.get(field)
        for candidate in _walk_trajectory_values(value):
            if isinstance(candidate, str) and _xml_context_matches(candidate, context):
                return True
    return False


def _walk_trajectory_values(value: Any):
    pending: list[tuple[Any, int]] = [(value, 0)]
    visited = 0
    total_chars = 0
    while pending:
        current, depth = pending.pop()
        visited += 1
        if visited > _MAX_TRAJECTORY_NODES or depth > _MAX_TRAJECTORY_DEPTH:
            raise _payload_limit_error()
        if isinstance(current, str):
            total_chars += len(current)
            if total_chars > _MAX_TRAJECTORY_TOTAL_CHARS:
                raise _payload_limit_error()
        yield current
        if isinstance(current, dict):
            for child in current.values():
                pending.append((child, depth + 1))
        elif isinstance(current, (list, tuple)):
            for child in current:
                pending.append((child, depth + 1))


def _xml_context_matches(text: str, context: TraceCorrelationContext) -> bool:
    for candidate in _decoded_trajectory_strings(text):
        if _xml_fragment_matches(candidate, context):
            return True
    return False


def _decoded_trajectory_strings(text: str):
    """Yield raw and JSON-decoded strings without retaining a payload copy."""

    pending: list[tuple[Any, int]] = [(text, 0)]
    visited = 0
    total_chars = 0
    while pending:
        value, depth = pending.pop()
        visited += 1
        if visited > _MAX_TRAJECTORY_NODES:
            raise _payload_limit_error()
        if isinstance(value, str):
            total_chars += len(value)
            if len(value) > _MAX_XML_SCAN_CHARS or total_chars > _MAX_TRAJECTORY_TOTAL_CHARS:
                raise _payload_limit_error()
            yield value
            stripped = value.lstrip()
            if stripped[:1] in {"[", "{"}:
                if depth >= 3:
                    raise _payload_limit_error()
                try:
                    decoded = json.loads(value)
                except (TypeError, ValueError):
                    continue
                pending.append((decoded, depth + 1))
        elif isinstance(value, dict):
            pending.extend((child, depth) for child in value.values())
        elif isinstance(value, (list, tuple)):
            pending.extend((child, depth) for child in value)


def _xml_fragment_matches(text: str, context: TraceCorrelationContext) -> bool:
    if "experiment_context" not in text:
        return False
    opening = "<experiment_context"
    closing = "</experiment_context>"
    start = 0
    while True:
        begin = text.find(opening, start)
        if begin < 0:
            return False
        end = text.find(closing, begin)
        if end < 0:
            return False
        fragment = text[begin : end + len(closing)]
        start = end + len(closing)
        try:
            root = ElementTree.fromstring(fragment)
        except ElementTree.ParseError:
            continue
        required_tags = {
            "experiment_plan_id",
            "experiment_run_id",
            "dataset_item_id",
            "local_launch_id",
        }
        values: dict[str, str] = {}
        malformed = False
        for child in root:
            if child.tag not in required_tags:
                continue
            if child.tag in values:
                malformed = True
                break
            values[child.tag] = child.text or ""
        if (
            not malformed
            and root.tag == "experiment_context"
            and values.get("experiment_plan_id") == context.experiment_plan_id
            and values.get("experiment_run_id") == context.experiment_run_id
            and values.get("dataset_item_id") == context.dataset_item_id
            and values.get("local_launch_id") == context.launch_id
        ):
            return True


def _escape_search_value(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _classify_search_exception(exc: Exception) -> TraceSearchError:
    status_getter = getattr(exc, "get_resp_status", None)
    raw_status = status_getter() if callable(status_getter) else getattr(exc, "status_code", None)
    try:
        status = None if isinstance(raw_status, bool) else int(raw_status)
    except (TypeError, ValueError):
        status = None
    code_getter = getattr(exc, "get_error_code", None)
    raw_code = str(
        code_getter() if callable(code_getter) else getattr(exc, "code", "") or ""
    ).casefold()
    if status in {401, 403} or any(
        token in raw_code for token in ("accessdenied", "permission", "forbidden", "unauthorized")
    ):
        return TraceSearchError(
            "TRACE_SEARCH_PERMISSION_DENIED",
            "The configured credentials cannot query the AgentSpace trajectory Logstore",
            retryable=False,
        )
    if status == 404 or any(
        token in raw_code
        for token in ("notfound", "not_found", "notexist", "projectnotexist", "logstorenotexist")
    ):
        return TraceSearchError(
            "TRACE_SOURCE_NOT_FOUND",
            "The AgentSpace trajectory Logstore could not be resolved",
            retryable=False,
        )
    if status in {408, 429} or (status is not None and status >= 500):
        return TraceSearchError(
            "TRACE_SEARCH_UNAVAILABLE",
            "The AgentSpace trajectory Logstore is temporarily unavailable",
            retryable=True,
        )
    if status is not None and 400 <= status < 500:
        return TraceSearchError(
            "TRACE_SEARCH_REJECTED",
            "SLS rejected the agent trajectory lookup request",
            retryable=False,
        )
    return TraceSearchError(
        "TRACE_SEARCH_UNAVAILABLE",
        "The AgentSpace trajectory Logstore is temporarily unavailable",
        retryable=True,
    )


__all__ = [
    "AgentSpaceTrajectoryLogStoreClient",
    "ProgressCallback",
    "TraceCandidate",
    "TraceCorrelationContext",
    "TraceCorrelationError",
    "TraceCorrelationProgress",
    "TraceCorrelationResult",
    "TraceCorrelationService",
    "TraceCorrelationStatus",
    "TracePollingPolicy",
    "TraceSearchClient",
    "TraceSearchError",
    "TraceSearchRequest",
    "validate_trace_id",
]
