from __future__ import annotations

import asyncio
import json
from xml.etree import ElementTree

import pytest

import app.agentloop.trace_correlation as trace_module
from app.agentloop.models import AgentLoopConnection
from app.agentloop.trace_correlation import (
    AgentSpaceTrajectoryLogStoreClient,
    TraceCandidate,
    TraceCorrelationContext,
    TraceCorrelationError,
    TraceCorrelationService,
    TraceCorrelationStatus,
    TracePollingPolicy,
    TraceSearchError,
    TraceSearchRequest,
    _build_input_context_query,
    _build_line_context_query,
    _build_trace_query,
)


def _context() -> TraceCorrelationContext:
    return TraceCorrelationContext(
        launch_id='launch-"one',
        experiment_plan_id="plan-1",
        experiment_run_id="run-1",
        dataset_item_id="item\\one",
        agent_started_at_ms=1_800_000_000_000,
        agent_completed_at_ms=1_800_000_000_500,
    )


def _connection() -> AgentLoopConnection:
    return AgentLoopConnection(
        region_id="cn-hangzhou",
        agent_space="space-1",
        access_key_id="test-ak",
        access_key_secret="test-sk",
    )


def _context_xml(context: TraceCorrelationContext) -> str:
    root = ElementTree.Element("experiment_context", {"schema_version": "1"})
    values = {
        "experiment_plan_id": context.experiment_plan_id,
        "experiment_run_id": context.experiment_run_id,
        "experiment_version": "1.0.0",
        "dataset_item_id": context.dataset_item_id,
        "local_launch_id": context.launch_id,
    }
    for name, value in values.items():
        ElementTree.SubElement(root, name).text = value
    return ElementTree.tostring(root, encoding="unicode")


class _SequenceSearchClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def search(self, request):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _FakeTime:
    def __init__(self) -> None:
        self.elapsed = 0.0

    def monotonic(self) -> float:
        return self.elapsed

    def wall_time_ms(self) -> int:
        return 1_800_000_001_000 + int(self.elapsed * 1000)

    async def sleep(self, seconds: float) -> None:
        self.elapsed += seconds


def _service(client, fake_time: _FakeTime, *, timeout=10.0):
    return TraceCorrelationService(
        client,
        policy=TracePollingPolicy(
            timeout_seconds=timeout,
            initial_interval_seconds=1,
            max_interval_seconds=2,
            backoff_multiplier=2,
            jitter_ratio=0,
            time_padding_seconds=600,
        ),
        monotonic=fake_time.monotonic,
        wall_time_ms=fake_time.wall_time_ms,
        sleep=fake_time.sleep,
    )


class _Log:
    def __init__(self, contents: dict) -> None:
        self.contents = contents

    def get_contents(self):
        return self.contents


class _Meta:
    def __init__(self, *, accurate=True, limited=False) -> None:
        self.accurate = accurate
        self.limited = limited

    def get_is_accurate(self):
        return self.accurate

    def get_limited(self):
        return self.limited


class _LogsResponse:
    def __init__(self, rows, *, complete=True, accurate=True, limited=False, count=None) -> None:
        self.logs = [_Log(row) for row in rows]
        self.complete = complete
        self.meta = _Meta(accurate=accurate, limited=limited)
        self.count = len(self.logs) if count is None else count

    def get_meta(self):
        return self.meta

    def is_completed(self):
        return self.complete

    def get_count(self):
        return self.count

    def get_logs(self):
        return self.logs


class _SlsClient:
    def __init__(self, responses, *, source_error=None, query_error=None) -> None:
        self.responses = list(responses)
        self.source_error = source_error
        self.query_error = query_error
        self.source_calls = []
        self.query_calls = []
        self.timeout = 120

    def get_logstore(self, project, logstore):
        self.source_calls.append((project, logstore))
        if self.source_error is not None:
            raise self.source_error
        return object()

    def get_logs(self, request):
        self.query_calls.append(request)
        if self.query_error is not None:
            raise self.query_error
        return self.responses.pop(0)


def _trajectory_row(trace_id: str, *, context=None, field="input", output=None) -> dict:
    row = {"trace_id": trace_id, field: context or "another invocation"}
    if output is not None:
        row["output"] = output
    return row


def _trajectory_client(sls_client, *, project="project-1", agentloop_client=None):
    return AgentSpaceTrajectoryLogStoreClient(
        _connection(),
        user_agent="test-session/1",
        sls_project=project,
        agentloop_client=agentloop_client,
        sls_client=sls_client,
    )


def test_trace_query_prefers_exact_agent_trace_id() -> None:
    query = _build_trace_query(_context(), "a" * 32)

    assert query == f'trace_id: "{"a" * 32}"'
    assert "input:" not in query


def test_context_queries_escape_and_require_root_plus_four_ids() -> None:
    input_query = _build_input_context_query(_context())
    line_query = _build_line_context_query(_context())

    assert input_query.count("input:") == 5
    assert 'input: "experiment_context"' in input_query
    assert 'input: "plan-1"' in input_query
    assert 'input: "run-1"' in input_query
    assert 'input: "item\\\\one"' in input_query
    assert 'input: "launch-\\"one"' in input_query
    assert line_query.count(" and ") == 4


def test_trajectory_client_reads_bound_project_store_and_verifies_hint() -> None:
    response = _LogsResponse([_trajectory_row("A" * 32, context=_context_xml(_context()))])
    sls = _SlsClient([response, response, response])
    result = _trajectory_client(sls).search(
        TraceSearchRequest(_context(), 1001, 2001, hinted_trace_id="a" * 32)
    )

    assert result == [TraceCandidate("a" * 32, source="agent_trajectory_trace_id")]
    assert sls.source_calls == [("project-1", "agent-trajectory")]
    request = sls.query_calls[0]
    assert request.get_project() == "project-1"
    assert request.get_logstore() == "agent-trajectory"
    assert request.get_from() == 1
    assert request.get_to() == 3
    assert request.get_query() == f'trace_id: "{"a" * 32}"'
    assert request.get_line() == 100
    assert request.get_offset() == 0


def test_trajectory_client_checks_full_context_query_after_a_valid_hint() -> None:
    first = _LogsResponse([_trajectory_row("a" * 32, context=_context_xml(_context()))])
    second = _LogsResponse([_trajectory_row("b" * 32, context=_context_xml(_context()))])
    sls = _SlsClient([first, first, second])

    result = _trajectory_client(sls).search(
        TraceSearchRequest(_context(), 1000, 2000, hinted_trace_id="a" * 32)
    )

    assert {candidate.trace_id for candidate in result} == {"a" * 32, "b" * 32}
    assert len(sls.query_calls) == 3


def test_trajectory_client_resolves_project_through_get_agent_space() -> None:
    class AgentLoopClient:
        def __init__(self) -> None:
            self.calls = []

        def get_agent_space(self, agent_space, request):
            self.calls.append((agent_space, request))
            return type("Response", (), {"body": type("Body", (), {"sls_project": "p-1"})()})()

    agentloop = AgentLoopClient()
    sls = _SlsClient([_LogsResponse([]), _LogsResponse([]), _LogsResponse([])])
    client = AgentSpaceTrajectoryLogStoreClient(
        _connection(), user_agent="test", agentloop_client=agentloop, sls_client=sls
    )

    assert client.search(TraceSearchRequest(_context(), 1000, 2000)) == []
    assert len(agentloop.calls) == 1
    assert agentloop.calls[0][0] == "space-1"
    assert sls.source_calls == [("p-1", "agent-trajectory")]


def test_trajectory_client_matches_json_encoded_input_without_retaining_payload() -> None:
    trace_id = "b" * 32
    encoded = json.dumps(
        [{"role": "user", "parts": [{"content": f"q\n{_context_xml(_context())}"}]}]
    )
    response = _LogsResponse([_trajectory_row(trace_id, context=encoded)])
    sls = _SlsClient([response, response])

    result = _trajectory_client(sls).search(TraceSearchRequest(_context(), 1000, 2000))

    assert result == [TraceCandidate(trace_id, source="agent_trajectory_input_context")]
    assert encoded not in repr(result)


def test_trajectory_client_falls_back_to_line_query_and_checks_atif() -> None:
    trace_id = "c" * 32
    atif = json.dumps({"steps": [{"message": _context_xml(_context())}]})
    sls = _SlsClient(
        [
            _LogsResponse([]),
            _LogsResponse([_trajectory_row(trace_id, context=atif, field="atif_content")]),
        ]
    )

    result = _trajectory_client(sls).search(TraceSearchRequest(_context(), 1000, 2000))

    assert result == [TraceCandidate(trace_id, source="agent_trajectory_line_context")]
    assert sls.query_calls[0].get_query() == _build_input_context_query(_context())
    assert sls.query_calls[1].get_query() == _build_line_context_query(_context())


def test_trajectory_client_scans_bounded_pages_when_indexes_do_not_match(monkeypatch) -> None:
    monkeypatch.setattr(trace_module, "_TRAJECTORY_PAGE_SIZE", 2)
    trace_id = "d" * 32
    encoded = json.dumps([{"message": _context_xml(_context())}])
    sls = _SlsClient(
        [
            _LogsResponse([]),
            _LogsResponse([]),
            _LogsResponse(
                [
                    _trajectory_row("e" * 32),
                    _trajectory_row("f" * 32),
                ]
            ),
            _LogsResponse([_trajectory_row(trace_id, context=encoded)]),
        ]
    )

    result = _trajectory_client(sls).search(TraceSearchRequest(_context(), 1000, 2000))

    assert result == [TraceCandidate(trace_id, source="agent_trajectory_scan_context")]
    assert sls.query_calls[2].get_query() == "*"
    assert sls.query_calls[2].get_offset() == 0
    assert sls.query_calls[3].get_offset() == 2


def test_trajectory_client_scans_all_filtered_pages_before_claiming_unique(monkeypatch) -> None:
    monkeypatch.setattr(trace_module, "_TRAJECTORY_PAGE_SIZE", 1)
    sls = _SlsClient(
        [
            _LogsResponse([_trajectory_row("a" * 32, context=_context_xml(_context()))]),
            _LogsResponse([_trajectory_row("b" * 32, context=_context_xml(_context()))]),
        ]
    )

    result = _trajectory_client(sls).search(TraceSearchRequest(_context(), 1000, 2000))

    assert {candidate.trace_id for candidate in result} == {"a" * 32, "b" * 32}
    assert sls.query_calls[1].get_offset() == 1


def test_trajectory_client_never_uses_output_as_context_proof() -> None:
    row = _trajectory_row("a" * 32, output=_context_xml(_context()))
    sls = _SlsClient([_LogsResponse([row]), _LogsResponse([row]), _LogsResponse([row])])

    result = _trajectory_client(sls).search(TraceSearchRequest(_context(), 1000, 2000))

    assert result == []


@pytest.mark.parametrize(
    "response,code,retryable",
    [
        (_LogsResponse([], complete=False), "TRAJECTORY_QUERY_INCOMPLETE", True),
        (_LogsResponse([], accurate=False), "TRAJECTORY_QUERY_INCOMPLETE", True),
        (_LogsResponse([], limited=True), "TRAJECTORY_QUERY_LIMITED", False),
        (_LogsResponse([], count=1), "TRAJECTORY_INVALID_RESPONSE", True),
    ],
)
def test_trajectory_client_rejects_incomplete_or_invalid_query(response, code, retryable) -> None:
    with pytest.raises(TraceSearchError) as caught:
        _trajectory_client(_SlsClient([response])).search(
            TraceSearchRequest(_context(), 1000, 2000)
        )

    assert caught.value.code == code
    assert caught.value.retryable is retryable


def test_hint_is_verified_after_delayed_trace_visibility() -> None:
    trace_id = "b" * 32
    client = _SequenceSearchClient(
        [[], [TraceCandidate(trace_id, source="agent_trajectory_trace_id")]]
    )
    fake_time = _FakeTime()
    events = []

    result = asyncio.run(
        _service(client, fake_time).resolve(
            _context(),
            hinted_trace_id=trace_id,
            on_progress=events.append,
        )
    )

    assert result.trace_id == trace_id
    assert result.source == "agent_trajectory_trace_id"
    assert result.attempts == 2
    assert [event.status for event in events] == [
        TraceCorrelationStatus.PENDING,
        TraceCorrelationStatus.POLLING,
        TraceCorrelationStatus.POLLING,
        TraceCorrelationStatus.RESOLVED,
    ]
    assert all(request.hinted_trace_id == trace_id for request in client.requests)
    assert client.requests[0].start_time_ms == _context().agent_started_at_ms - 600_000


def test_reverse_lookup_resolves_without_agent_trace_id() -> None:
    trace_id = "c" * 32
    fake_time = _FakeTime()
    result = asyncio.run(
        _service(_SequenceSearchClient([[TraceCandidate(trace_id)]]), fake_time).resolve(_context())
    )

    assert result.trace_id == trace_id
    assert result.source == "agent_trajectory_input_context"
    assert result.attempts == 1


def test_multiple_trace_ids_fail_closed_as_ambiguous() -> None:
    fake_time = _FakeTime()
    events = []
    client = _SequenceSearchClient([[TraceCandidate("a" * 32), TraceCandidate("b" * 32)]])

    with pytest.raises(TraceCorrelationError) as caught:
        asyncio.run(_service(client, fake_time).resolve(_context(), on_progress=events.append))

    assert caught.value.code == "TRACE_AMBIGUOUS"
    assert caught.value.status is TraceCorrelationStatus.AMBIGUOUS
    assert events[-1].status is TraceCorrelationStatus.AMBIGUOUS


def test_missing_trace_times_out_without_returning_a_detail_candidate() -> None:
    fake_time = _FakeTime()
    client = _SequenceSearchClient([[], [], []])

    with pytest.raises(TraceCorrelationError) as caught:
        asyncio.run(_service(client, fake_time, timeout=2).resolve(_context()))

    assert caught.value.code == "TRACE_LOOKUP_TIMED_OUT"
    assert caught.value.status is TraceCorrelationStatus.TIMED_OUT
    assert caught.value.attempts == 3


def test_non_retryable_search_error_fails_immediately_and_is_sanitized() -> None:
    fake_time = _FakeTime()
    client = _SequenceSearchClient(
        [
            TraceSearchError(
                "TRACE_SEARCH_PERMISSION_DENIED",
                "The configured credentials cannot query the AgentSpace Trace Store",
                retryable=False,
            )
        ]
    )

    with pytest.raises(TraceCorrelationError) as caught:
        asyncio.run(_service(client, fake_time).resolve(_context()))

    assert caught.value.code == "TRACE_SEARCH_PERMISSION_DENIED"
    assert caught.value.attempts == 1
    assert "test-sk" not in repr(caught.value)


def test_trajectory_client_classifies_permission_and_transport_failures() -> None:
    class PermissionError(RuntimeError):
        status_code = 403

    request = TraceSearchRequest(
        context=_context(),
        start_time_ms=1000,
        end_time_ms=2000,
    )
    permission = _trajectory_client(_SlsClient([], source_error=PermissionError("secret")))
    with pytest.raises(TraceSearchError) as denied:
        permission.search(request)
    assert denied.value.code == "TRACE_SEARCH_PERMISSION_DENIED"
    assert denied.value.retryable is False
    assert "secret" not in repr(denied.value)

    transport = _trajectory_client(_SlsClient([], query_error=RuntimeError("secret")))
    with pytest.raises(TraceSearchError) as unavailable:
        transport.search(request)
    assert unavailable.value.code == "TRACE_SEARCH_UNAVAILABLE"
    assert unavailable.value.retryable is True
    assert "secret" not in repr(unavailable.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"launch_id": ""},
        {"experiment_plan_id": " "},
        {"experiment_run_id": ""},
        {"dataset_item_id": ""},
        {"agent_started_at_ms": 0},
        {"agent_completed_at_ms": 1},
    ],
)
def test_correlation_context_rejects_missing_ids_and_invalid_time(changes) -> None:
    values = {
        "launch_id": "launch-1",
        "experiment_plan_id": "plan-1",
        "experiment_run_id": "run-1",
        "dataset_item_id": "item-1",
        "agent_started_at_ms": 1000,
        "agent_completed_at_ms": 2000,
    }
    values.update(changes)

    with pytest.raises(ValueError):
        TraceCorrelationContext(**values)
