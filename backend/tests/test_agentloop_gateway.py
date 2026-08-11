from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from app.agentloop import (
    AgentLoopGateway,
    AgentLoopTimeoutError,
    FakeAgentLoopGateway,
    FakeStartBehavior,
    LocalHandoffOutcome,
    LocalHandoffState,
    PlanDetail,
    PlanNotFoundError,
    PlanQuery,
    RecordDetail,
    RecordNotFoundError,
    RecordQuery,
    SubmissionUnknownError,
)


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def plan(
    plan_id: str,
    name: str,
    *,
    status: str = "active",
    experiment_type: str = "OFFLINE",
) -> PlanDetail:
    return PlanDetail(
        plan_id=plan_id,
        plan_name=name,
        experiment_type=experiment_type,
        status=status,
        dataset_id=f"dataset-{plan_id}",
        can_launch_locally=experiment_type == "OFFLINE",
    )


def test_fake_implements_runtime_gateway_protocol() -> None:
    gateway = FakeAgentLoopGateway()
    assert isinstance(gateway, AgentLoopGateway)


def test_plan_list_supports_filtering_and_stable_pagination() -> None:
    gateway = FakeAgentLoopGateway(
        plans=(
            plan("p-1", "Shooter Alpha"),
            plan("p-2", "Puzzle", status="stopped"),
            plan("p-3", "Shooter Beta"),
        )
    )

    first = run(gateway.list_plans(PlanQuery(name_search="SHOOTER", offset=0, limit=1)))
    second = run(gateway.list_plans(PlanQuery(name_search="shooter", offset=1, limit=1)))

    assert [item.plan_id for item in first.items] == ["p-1"]
    assert first.total == 2
    assert first.has_more is True
    assert [item.plan_id for item in second.items] == ["p-3"]
    assert second.total == 2
    assert second.has_more is False

    stopped = run(gateway.list_plans(PlanQuery(status="stopped")))
    assert [item.plan_id for item in stopped.items] == ["p-2"]
    assert stopped.items[0].can_launch_locally is True

    offline = run(gateway.list_plans(PlanQuery(experiment_type="offline")))
    assert [item.plan_id for item in offline.items] == ["p-1", "p-2", "p-3"]


def test_get_plan_returns_detail_and_missing_plan_has_stable_error() -> None:
    expected = plan("p-1", "FPS")
    gateway = FakeAgentLoopGateway(plans=(expected,))

    assert run(gateway.get_plan("p-1")) == expected
    with pytest.raises(PlanNotFoundError) as raised:
        run(gateway.get_plan("missing"))
    assert raised.value.code == "PLAN_NOT_FOUND"
    assert raised.value.plan_id == "missing"


def test_record_reads_are_driven_by_record_id_and_preserve_unknown_status() -> None:
    gateway = FakeAgentLoopGateway(
        records=(
            RecordDetail("r-1", "one", "p-1", "A", "completed"),
            RecordDetail("r-2", "two", "p-2", "B", "future_remote_state"),
            RecordDetail("r-3", "three", "p-1", "A", "running"),
        )
    )

    page = run(
        gateway.list_records(RecordQuery(record_ids=("r-2", "missing", "r-1", "r-2"), limit=10))
    )
    assert [(item.record_id, item.status) for item in page.items] == [
        ("r-2", "future_remote_state"),
        ("r-1", "completed"),
    ]
    assert page.total == 2

    guarded = run(
        gateway.list_records(RecordQuery(record_ids=("r-2", "r-3", "r-1"), plan_id="p-1", limit=1))
    )
    assert [item.record_id for item in guarded.items] == ["r-3"]
    assert guarded.total == 2
    assert guarded.has_more is True
    assert run(gateway.get_record("r-2")).status == "future_remote_state"

    with pytest.raises(RecordNotFoundError):
        run(gateway.get_record("missing"))


def test_successful_start_calls_record_callback_and_returns_handoff() -> None:
    async def scenario() -> None:
        gateway = FakeAgentLoopGateway(plans=(plan("p-1", "FPS"),))
        gateway.enqueue_start_behavior(
            FakeStartBehavior.succeed(record_id="r-created", record_status="evaluating")
        )
        callback_observations: list[tuple[str, str]] = []

        async def on_record_created(record_id: str) -> None:
            # The callback observes a readable record, proving creation precedes it.
            remote = await gateway.get_record(record_id)
            callback_observations.append((record_id, remote.status))

        handle = await gateway.start_offline_experiment("p-1", on_record_created)

        assert handle.record_id == "r-created"
        assert callback_observations == [("r-created", "evaluating")]
        assert await handle.wait_local_handoff() == LocalHandoffOutcome.succeeded()
        assert gateway.created_record_count == 1

    run(scenario())


def test_fake_does_not_implement_remote_idempotency_binding() -> None:
    async def scenario() -> None:
        gateway = FakeAgentLoopGateway(plans=(plan("p-1", "FPS"), plan("p-2", "Puzzle")))
        gateway.enqueue_start_behavior(FakeStartBehavior.succeed(record_id="r-one"))
        gateway.enqueue_start_behavior(FakeStartBehavior.succeed(record_id="r-two"))
        callbacks: list[str] = []

        async def callback(record_id: str) -> None:
            callbacks.append(record_id)

        first = await gateway.start_offline_experiment("p-1", callback)
        second = await gateway.start_offline_experiment("p-1", callback)

        assert first.record_id == "r-one"
        assert second.record_id == "r-two"
        assert callbacks == ["r-one", "r-two"]
        assert gateway.start_attempt_count == 2
        assert gateway.created_record_count == 2

    run(scenario())


def test_timeout_before_create_does_not_fabricate_a_record() -> None:
    async def scenario() -> None:
        gateway = FakeAgentLoopGateway(plans=(plan("p-1", "FPS"),))
        gateway.enqueue_start_behavior(FakeStartBehavior.timeout())

        async def callback(record_id: str) -> None:
            raise AssertionError(f"unexpected callback: {record_id}")

        with pytest.raises(AgentLoopTimeoutError) as raised:
            await gateway.start_offline_experiment("p-1", callback)
        assert raised.value.remote_acceptance_unknown is False
        assert raised.value.retryable is True
        assert gateway.created_record_count == 0
        assert (await gateway.list_records(RecordQuery(("anything",)))).total == 0

    run(scenario())


def test_unknown_submission_has_no_remote_key_reconciliation_semantics() -> None:
    async def scenario() -> None:
        gateway = FakeAgentLoopGateway(plans=(plan("p-1", "FPS"),))
        gateway.enqueue_start_behavior(FakeStartBehavior.submission_unknown(record_id="r-unknown"))
        callbacks: list[str] = []

        async def callback(record_id: str) -> None:
            callbacks.append(record_id)

        with pytest.raises(SubmissionUnknownError) as raised:
            await gateway.start_offline_experiment("p-1", callback)
        assert raised.value.remote_acceptance_unknown is True
        assert raised.value.retryable is False
        assert "idempotency" not in str(raised.value)
        assert "reconcile" not in str(raised.value)
        assert callbacks == []
        assert (await gateway.get_record("r-unknown")).record_id == "r-unknown"

        gateway.enqueue_start_behavior(FakeStartBehavior.succeed(record_id="r-new"))
        new_run = await gateway.start_offline_experiment("p-1", callback)
        assert new_run.record_id == "r-new"
        assert callbacks == ["r-new"]
        assert gateway.created_record_count == 2

    run(scenario())


def test_callback_failure_does_not_create_a_remote_idempotency_binding() -> None:
    async def scenario() -> None:
        gateway = FakeAgentLoopGateway(plans=(plan("p-1", "FPS"),))
        gateway.enqueue_start_behavior(FakeStartBehavior.succeed(record_id="r-created"))

        async def broken_callback(record_id: str) -> None:
            raise RuntimeError(f"local database unavailable for {record_id}")

        with pytest.raises(RuntimeError, match="local database unavailable"):
            await gateway.start_offline_experiment("p-1", broken_callback)

        gateway.enqueue_start_behavior(FakeStartBehavior.succeed(record_id="r-second"))
        recovered_ids: list[str] = []

        async def recovered_callback(record_id: str) -> None:
            recovered_ids.append(record_id)

        second = await gateway.start_offline_experiment("p-1", recovered_callback)
        assert second.record_id == "r-second"
        assert recovered_ids == ["r-second"]
        assert gateway.created_record_count == 2

    run(scenario())


def test_local_handoff_unknown_is_not_converted_to_success() -> None:
    async def scenario() -> None:
        gateway = FakeAgentLoopGateway(plans=(plan("p-1", "FPS"),))
        gateway.enqueue_start_behavior(
            FakeStartBehavior.succeed(
                record_id="r-1",
                record_status="mystery_remote_state",
                handoff_outcome=LocalHandoffOutcome.unknown("upload acknowledgement lost"),
            )
        )

        async def callback(record_id: str) -> None:
            return None

        handle = await gateway.start_offline_experiment("p-1", callback)
        outcome = await handle.wait_local_handoff()
        assert outcome.state is LocalHandoffState.UNKNOWN
        assert (await gateway.get_record("r-1")).status == "mystery_remote_state"

    run(scenario())


def test_console_url_is_optional_and_values_are_encoded() -> None:
    without_console = FakeAgentLoopGateway(plans=(plan("p-1", "FPS"),))
    assert without_console.plan_console_url("p-1") is None
    assert without_console.record_console_url("r-1") is None

    gateway = FakeAgentLoopGateway(
        records=(RecordDetail("r /1", "run", "p ?1", "A", "running"),),
        console_base_url="https://console.example.test/",
        region_id="cn test",
        agent_space="space/one",
    )
    url = gateway.record_console_url("r /1")
    assert url is not None
    parsed = urlparse(url)
    assert parsed.path.endswith("/agentloop/region/cn%20test/agentspace/space%2Fone/app/playground")
    assert parse_qs(parsed.query) == {"recordId": ["r /1"], "planId": ["p ?1"]}


@pytest.mark.parametrize(
    "query",
    [
        PlanQuery,
    ],
)
def test_query_type_is_importable(query: type[PlanQuery]) -> None:
    # A small API-stability assertion that also keeps static exports exercised.
    assert query() == PlanQuery()


def test_query_validation_rejects_unbounded_or_invalid_pages() -> None:
    with pytest.raises(ValueError):
        PlanQuery(offset=-1)
    with pytest.raises(ValueError):
        PlanQuery(limit=0)
    with pytest.raises(ValueError):
        PlanQuery(limit=201)
    with pytest.raises(ValueError):
        RecordQuery(record_ids=("",))
