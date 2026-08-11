from __future__ import annotations

import time

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from app.agentloop import (
    AgentLoopGatewayError,
    ConnectionCheck,
    FakeAgentLoopGateway,
    FakeStartBehavior,
    LocalHandoffOutcome,
    PlanDetail,
)
from app.config import Settings
from app.dashboards import (
    PlanDashboardDescription,
    PlanDashboardPublication,
    render_plan_dashboard,
)
from app.main import create_app
from app.runtime import build_container
from app.secrets import MemorySecretStore
from app.services.launches import _safe_runner_outcome_message
from app.storage import in_memory_database


def make_client(
    tmp_path,
    *,
    plan_dashboard_publisher=None,
) -> tuple[TestClient, FakeAgentLoopGateway]:
    gateway = FakeAgentLoopGateway(
        plans=(
            PlanDetail(
                plan_id="plan-1",
                plan_name="Regression",
                experiment_type="OFFLINE",
                status="active",
                dataset_id="dataset-1",
                can_launch_locally=True,
            ),
            PlanDetail(
                plan_id="plan-online",
                plan_name="Online",
                experiment_type="ONLINE",
                status="active",
                can_launch_locally=False,
                cannot_launch_reason="Only OFFLINE plans are supported",
            ),
        ),
        connection_check=ConnectionCheck(ok=True, message="connected"),
        region_id="cn-hangzhou",
        agent_space="space-a",
    )
    settings = Settings(
        environment="test",
        data_dir=tmp_path,
        gateway_mode="fake",
    )
    container = build_container(
        settings,
        database=in_memory_database(),
        secret_store=MemorySecretStore(),
        gateway=gateway,
        plan_dashboard_publisher=plan_dashboard_publisher,
    )
    return TestClient(create_app(settings=settings, container=container)), gateway


def test_runtime_passes_trace_lookup_timeout_to_offline_runner(tmp_path) -> None:
    settings = Settings(
        environment="test",
        data_dir=tmp_path,
        gateway_mode="agentloop",
        trace_lookup_timeout_seconds=321,
    )
    container = build_container(
        settings,
        database=in_memory_database(),
        secret_store=MemorySecretStore(),
    )

    assert container.gateway.runner.trace_polling_policy.timeout_seconds == 321


def configure(client: TestClient) -> None:
    response = client.put(
        "/api/v1/settings/agentloop",
        json={
            "displayName": "Demo workspace",
            "regionId": "cn-hangzhou",
            "agentSpace": "space-a",
            "consoleBaseUrl": "https://agentloop.example.test",
            "accessKeyId": "test-access-key",
            "accessKeySecret": "test-secret-value",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accessKeyIdHint"] == "****-key"
    assert "accessKeySecret" not in body
    assert "test-secret-value" not in response.text


def test_profile_update_preserves_omitted_runtime_settings(tmp_path) -> None:
    client, _ = make_client(tmp_path)
    with client:
        first = client.put(
            "/api/v1/settings/agentloop",
            json={
                "displayName": "Demo workspace",
                "regionId": "cn-hangzhou",
                "agentSpace": "space-a",
                "accessKeyId": "test-access-key",
                "accessKeySecret": "test-secret-value",
                "defaultTimezone": "Asia/Singapore",
                "maxConcurrency": 5,
            },
        )
        assert first.status_code == 200, first.text

        updated = client.put(
            "/api/v1/settings/agentloop",
            json={
                "displayName": "Demo workspace renamed",
                "regionId": "cn-hangzhou",
                "agentSpace": "space-a",
            },
        )

        assert updated.status_code == 200, updated.text
        assert updated.json()["displayName"] == "Demo workspace renamed"
        assert updated.json()["defaultTimezone"] == "Asia/Singapore"
        assert updated.json()["maxConcurrency"] == 5


def test_manual_launch_is_idempotent_and_visible_in_launch_history(tmp_path) -> None:
    class Publisher:
        def describe(self, *, agent_space, plan_id, plan_name):
            rendered = render_plan_dashboard(
                project="description-only",
                agent_space=agent_space,
                plan_id=plan_id,
                plan_name=plan_name,
            )
            return PlanDashboardDescription(
                dashboard_name=rendered.dashboard_name,
                display_name=rendered.display_name,
                template_version=rendered.template_version,
                template_sha256=rendered.template_sha256,
            )

        async def publish(self, *, connection, plan_id, plan_name):
            rendered = render_plan_dashboard(
                project="agentloop-project",
                agent_space=connection.agent_space,
                plan_id=plan_id,
                plan_name=plan_name,
            )
            return PlanDashboardPublication(
                sls_project=rendered.project,
                dashboard_name=rendered.dashboard_name,
                display_name=rendered.display_name,
                template_version=rendered.template_version,
                template_sha256=rendered.template_sha256,
                action="CREATED",
            )

    client, gateway = make_client(tmp_path, plan_dashboard_publisher=Publisher())
    with client:
        configure(client)
        assert client.post("/api/v1/settings/agentloop/test").json()["ok"] is True

        plans = client.get("/api/v1/plans").json()
        assert plans["total"] == 2
        assert plans["items"][0]["planId"] == "plan-1"
        assert gateway.last_connection_scope == (
            "cn-hangzhou",
            "space-a",
            None,
            "-key",
        )
        assert "test-access-key" not in repr(
            client.app.state.container.profile_service.connection()
        )
        assert "test-secret-value" not in repr(
            client.app.state.container.profile_service.connection()
        )

        headers = {"Idempotency-Key": "browser-request-1"}
        first = client.post("/api/v1/plans/plan-1/launch", json={}, headers=headers)
        assert first.status_code == 202, first.text
        first_body = first.json()
        assert first_body["submissionState"] == "ACCEPTED"
        assert first_body["recordId"]
        assert first_body["agentLoopUrl"].startswith("https://agentloop.example.test/")
        assert first_body["planDashboard"]["state"] == "READY"
        assert first_body["planDashboard"]["url"] == (
            "https://agentloop.example.test/agentloop/region/cn-hangzhou/"
            "agentspace/space-a/app/dashboard/detail/"
            f"{first_body['planDashboard']['dashboardName']}"
        )
        assert first_body["traceCorrelation"] == {
            "state": "NOT_STARTED",
            "traceIds": [],
            "totalTasks": 0,
            "pendingTasks": 0,
            "resolvedTasks": 0,
            "reportedTasks": 0,
            "failedTasks": 0,
            "totalAttempts": 0,
            "lastErrorCode": None,
            "lastErrorMessage": None,
            "startedAt": None,
            "finishedAt": None,
        }

        second = client.post("/api/v1/plans/plan-1/launch", json={}, headers=headers)
        assert second.status_code == 202
        assert second.json()["recordId"] == first_body["recordId"]
        assert gateway.created_record_count == 1

        history = client.get("/api/v1/launches", params={"planId": "plan-1"})
        assert history.status_code == 200, history.text
        assert history.json()["total"] == 1
        assert history.json()["items"][0]["recordId"] == first_body["recordId"]
        assert history.json()["items"][0]["triggerSource"] == "manual"
        assert history.json()["items"][0]["planDashboard"] == first_body["planDashboard"]


def test_launch_api_exposes_trace_correlation_summary(tmp_path) -> None:
    client, _ = make_client(tmp_path)
    with client:
        configure(client)
        assert client.post("/api/v1/settings/agentloop/test").json()["ok"] is True
        launched = client.post(
            "/api/v1/plans/plan-1/launch",
            json={},
            headers={"Idempotency-Key": "trace-summary"},
        )
        assert launched.status_code == 202, launched.text
        launch_id = launched.json()["launchId"]
        repository = client.app.state.container.trace_correlation_repository
        repository.upsert(
            launch_id=launch_id,
            dataset_item_id="dataset-item-1",
            experiment_run_id=launched.json()["recordId"],
            state="REPORTED",
            trace_id="0123456789abcdef0123456789abcdef",
            attempts=2,
        )

        response = client.get(f"/api/v1/launches/{launch_id}")
        assert response.status_code == 200, response.text
        summary = response.json()["traceCorrelation"]
        assert summary["state"] == "REPORTED"
        assert summary["traceIds"] == ["0123456789abcdef0123456789abcdef"]
        assert summary["totalTasks"] == 1
        assert summary["pendingTasks"] == 0
        assert summary["totalAttempts"] == 2
        assert summary["startedAt"] is not None


def test_schedule_crud_and_run_now(tmp_path) -> None:
    client, gateway = make_client(tmp_path)
    with client:
        configure(client)
        assert client.post("/api/v1/settings/agentloop/test").status_code == 200
        created = client.post(
            "/api/v1/schedules",
            json={
                "name": "daily-regression",
                "planId": "plan-1",
                "cadence": {"type": "DAILY", "time": "09:00"},
                "timezone": "Asia/Shanghai",
                "misfirePolicy": "FIRE_ONCE",
                "enabled": True,
            },
        )
        assert created.status_code == 201, created.text
        schedule = created.json()
        assert schedule["revision"] == 1
        assert schedule["nextFireAtUtc"] is not None

        updated = client.patch(
            f"/api/v1/schedules/{schedule['id']}",
            params={"expectedRevision": 1},
            json={"name": "daily-regression-v2", "cadence": {"type": "DAILY", "time": "10:00"}},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["revision"] == 2
        assert updated.json()["dailyTime"] == "10:00"

        run = client.post(
            f"/api/v1/schedules/{schedule['id']}/run-now",
            headers={"Idempotency-Key": "run-now-1"},
        )
        assert run.status_code == 202, run.text
        assert run.json()["triggerSource"] == "schedule_run_now"

        duplicate = client.post(
            f"/api/v1/schedules/{schedule['id']}/run-now",
            headers={"Idempotency-Key": "run-now-1"},
        )
        assert duplicate.json()["recordId"] == run.json()["recordId"]
        assert gateway.created_record_count == 1

        disabled = client.post(
            f"/api/v1/schedules/{schedule['id']}/disable",
            params={"expectedRevision": 2},
        )
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["enabled"] is False
        assert disabled.json()["nextFireAtUtc"] is None


def test_schedule_api_accepts_five_minutes_and_rejects_four(tmp_path) -> None:
    client, _ = make_client(tmp_path)
    with client:
        configure(client)
        assert client.post("/api/v1/settings/agentloop/test").status_code == 200
        payload = {
            "name": "five-minute-regression",
            "planId": "plan-1",
            "cadence": {
                "type": "INTERVAL_MINUTES",
                "intervalMinutes": 5,
                "anchorAt": "2026-08-09T12:00:00Z",
            },
            "timezone": "UTC",
            "enabled": True,
        }
        created = client.post("/api/v1/schedules", json=payload)
        assert created.status_code == 201, created.text
        assert created.json()["cadenceType"] == "INTERVAL_MINUTES"
        assert created.json()["intervalMinutes"] == 5
        assert created.json()["intervalHours"] is None

        payload["cadence"]["intervalMinutes"] = 4
        rejected = client.post("/api/v1/schedules", json=payload)
        assert rejected.status_code == 422


def test_schedule_create_maps_agentloop_read_failure_to_service_unavailable(tmp_path) -> None:
    client, gateway = make_client(tmp_path)
    with client:
        configure(client)
        assert client.post("/api/v1/settings/agentloop/test").status_code == 200

        async def unavailable(plan_id: str, *, connection):
            del plan_id, connection
            raise AgentLoopGatewayError("AgentLoop is unavailable")

        gateway.get_plan = unavailable  # type: ignore[method-assign]
        response = client.post(
            "/api/v1/schedules",
            json={
                "name": "unavailable-plan",
                "planId": "plan-1",
                "cadence": {"type": "DAILY", "time": "09:00"},
                "timezone": "Asia/Shanghai",
                "misfirePolicy": "FIRE_ONCE",
                "enabled": True,
            },
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "AGENTLOOP_GATEWAY_ERROR"


def test_rejects_online_plan_and_never_exposes_results_endpoint(tmp_path) -> None:
    client, gateway = make_client(tmp_path)
    with client:
        configure(client)
        assert client.post("/api/v1/settings/agentloop/test").status_code == 200
        response = client.post(
            "/api/v1/plans/plan-online/launch",
            json={},
            headers={"Idempotency-Key": "online-1"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "PLAN_NOT_RUNNABLE"
        assert gateway.created_record_count == 0
        assert client.get("/api/v1/experiment-records/record-1/results").status_code == 404


def test_launch_is_blocked_until_saved_connection_is_validated(tmp_path) -> None:
    client, gateway = make_client(tmp_path)
    with client:
        configure(client)
        response = client.post(
            "/api/v1/plans/plan-1/launch",
            json={},
            headers={"Idempotency-Key": "not-validated"},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "CONNECTION_NOT_VALIDATED"
        assert gateway.created_record_count == 0


def test_global_customer_agent_concurrency_limit_fails_closed(tmp_path) -> None:
    client, gateway = make_client(tmp_path)
    with client:
        configure(client)
        assert client.post("/api/v1/settings/agentloop/test").status_code == 200
        app_container = client.app.state.container
        profile = app_container.profile_repository.get_configured()
        assert profile is not None
        active, _ = app_container.launch_repository.create_or_get(
            profile_id=profile.id,
            plan_id="plan-1",
            trigger_source="manual",
            idempotency_key="manual:already-running",
        )
        app_container.launch_repository.transition_submission(active.id, "SUBMITTING")
        app_container.launch_repository.transition_runner(active.id, "RUNNING")

        response = client.post(
            "/api/v1/plans/plan-1/launch",
            json={},
            headers={"Idempotency-Key": "blocked-by-capacity"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "LOCAL_CONCURRENCY_LIMIT"
        assert gateway.created_record_count == 0


def test_submission_unknown_same_key_returns_local_state_without_remote_retry(tmp_path) -> None:
    client, gateway = make_client(tmp_path)
    with client:
        configure(client)
        assert client.post("/api/v1/settings/agentloop/test").status_code == 200
        gateway.enqueue_start_behavior(
            FakeStartBehavior.submission_unknown(record_id="record-response-lost")
        )
        headers = {"Idempotency-Key": "unknown-retry"}

        first = client.post("/api/v1/plans/plan-1/launch", json={}, headers=headers)
        assert first.status_code == 202
        assert first.json()["submissionState"] == "SUBMISSION_UNKNOWN"
        launch_id = first.json()["launchId"]
        stored = client.app.state.container.launch_repository.get(launch_id)
        assert stored is not None and stored.attempt_count == 1

        second = client.post("/api/v1/plans/plan-1/launch", json={}, headers=headers)
        assert second.status_code == 202
        assert second.json()["launchId"] == launch_id
        assert second.json()["submissionState"] == "SUBMISSION_UNKNOWN"
        assert second.json()["recordId"] is None
        assert "idempotency" not in second.json()["sanitizedError"]
        assert "reconcile" not in second.json()["sanitizedError"]
        stored = client.app.state.container.launch_repository.get(launch_id)
        assert stored is not None and stored.attempt_count == 1
        assert gateway.start_attempt_count == 1
        assert gateway.created_record_count == 1


def test_local_runner_failure_is_visible_in_launch_history(tmp_path) -> None:
    client, gateway = make_client(tmp_path)
    with client:
        configure(client)
        assert client.post("/api/v1/settings/agentloop/test").status_code == 200
        gateway.enqueue_start_behavior(
            FakeStartBehavior.succeed(
                record_id="record-runner-failed",
                handoff_outcome=LocalHandoffOutcome.definitive_failure("three SLS uploads failed"),
            )
        )

        response = client.post(
            "/api/v1/plans/plan-1/launch",
            json={},
            headers={"Idempotency-Key": "runner-failed"},
        )
        assert response.status_code == 202
        launch_id = response.json()["launchId"]

        for _ in range(50):
            launch = client.get(f"/api/v1/launches/{launch_id}").json()
            if launch["runnerState"] == "FAILED":
                break
            time.sleep(0.01)
        assert launch["submissionState"] == "ACCEPTED"
        assert launch["recordId"] == "record-runner-failed"
        assert launch["errorCode"] == "RUNNER_DEFINITIVE_FAILURE"
        assert launch["sanitizedError"] == (
            "Local Runner failed; inspect customer-side service logs"
        )
        assert "three SLS uploads failed" not in launch["sanitizedError"]


def test_local_runner_error_category_accepts_only_allow_listed_values() -> None:
    assert _safe_runner_outcome_message(
        "FAILED",
        "1 of 1 customer Agent task(s) failed (ReadTimeout)",
    ) == "1 of 1 customer Agent task(s) failed (ReadTimeout)"
    assert _safe_runner_outcome_message(
        "FAILED",
        "1 of 1 customer Agent task(s) failed (SecretBearingError)",
    ) == "Local Runner failed; inspect customer-side service logs"


def test_restarted_submitting_launch_is_not_resubmitted_for_same_local_key(tmp_path) -> None:
    client, gateway = make_client(tmp_path)
    with client:
        configure(client)
        assert client.post("/api/v1/settings/agentloop/test").status_code == 200
        app_container = client.app.state.container
        profile = app_container.profile_repository.get_configured()
        assert profile is not None
        interrupted, _ = app_container.launch_repository.create_or_get(
            profile_id=profile.id,
            plan_id="plan-1",
            trigger_source="manual",
            idempotency_key="manual:interrupted-request",
        )
        app_container.launch_repository.transition_submission(interrupted.id, "SUBMITTING")
        app_container.launch_repository.transition_runner(interrupted.id, "RUNNING")
        assert (
            app_container.launch_repository.recover_interrupted_runners(interrupted.updated_at) == 1
        )

        response = client.post(
            "/api/v1/plans/plan-1/launch",
            json={},
            headers={"Idempotency-Key": "interrupted-request"},
        )
        assert response.status_code == 202
        assert response.json()["launchId"] == interrupted.id
        assert response.json()["submissionState"] == "SUBMITTING"
        assert response.json()["runnerState"] == "UNKNOWN"
        assert gateway.start_attempt_count == 0
        assert gateway.created_record_count == 0


def test_schedule_run_now_fails_closed_when_same_schedule_may_overlap(tmp_path) -> None:
    client, gateway = make_client(tmp_path)
    with client:
        configure(client)
        assert client.post("/api/v1/settings/agentloop/test").status_code == 200
        created = client.post(
            "/api/v1/schedules",
            json={
                "name": "overlap-guard",
                "planId": "plan-1",
                "cadence": {"type": "DAILY", "time": "09:00"},
                "timezone": "Asia/Shanghai",
                "misfirePolicy": "FIRE_ONCE",
                "enabled": True,
            },
        ).json()
        app_container = client.app.state.container
        profile = app_container.profile_repository.get_configured()
        assert profile is not None
        active, _ = app_container.launch_repository.create_or_get(
            profile_id=profile.id,
            plan_id="plan-1",
            trigger_source="schedule_run_now",
            schedule_id=created["id"],
            schedule_revision=created["revision"],
            idempotency_key="schedule-run-now:active",
        )
        app_container.launch_repository.transition_submission(active.id, "SUBMITTING")
        app_container.launch_repository.transition_runner(active.id, "RUNNING")

        response = client.post(
            f"/api/v1/schedules/{created['id']}/run-now",
            headers={"Idempotency-Key": "overlap-attempt"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "LOCAL_SCHEDULE_OVERLAP"
        assert gateway.created_record_count == 0


def test_experiment_record_views_are_not_exposed_locally(tmp_path) -> None:
    client, _ = make_client(tmp_path)
    with client:
        assert client.get("/api/v1/experiment-records").status_code == 404
        assert client.get("/api/v1/experiment-records/external-record").status_code == 404


def test_settings_secret_is_not_stored_in_sqlite(tmp_path) -> None:
    client, _ = make_client(tmp_path)
    with client:
        configure(client)
        app_container = client.app.state.container
        profile = app_container.profile_repository.get_configured()
        assert profile is not None
        serialized = " ".join(str(value) for value in vars(profile).values())
        assert "test-secret-value" not in serialized
        assert not hasattr(profile, "access_key_secret")

        secret_before = app_container.secret_store.get(profile.secret_ref)
        updated = client.put(
            "/api/v1/settings/agentloop",
            json={
                "displayName": "Renamed",
                "regionId": "cn-hangzhou",
                "agentSpace": "space-a",
                "consoleBaseUrl": "https://new-console.example.test",
            },
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["displayName"] == "Renamed"
        assert app_container.secret_store.get(profile.secret_ref) == secret_before


def test_agent_connection_api_returns_secret_once_and_rotation_revokes_it(tmp_path) -> None:
    client, _ = make_client(tmp_path)
    with client:
        created = client.post(
            "/api/v1/agent-connections",
            json={"name": "Calculator", "description": "Reference local Agent"},
        )
        assert created.status_code == 201, created.text
        body = created.json()
        connection_id = body["agentConnectionId"]
        first_token = body["token"]
        assert first_token
        assert body["status"] == "OFFLINE"
        assert body["health"] is False
        assert body["connectUrl"].endswith(f"/api/v1/agent-connections/{connection_id}/connect")
        assert "agentloop-launcher reference-agent " in body["connectCommand"]
        assert first_token not in body["connectCommand"]

        persisted = client.app.state.container.agent_connection_repository.get(connection_id)
        assert persisted is not None
        assert first_token not in " ".join(str(value) for value in vars(persisted).values())
        assert (
            client.app.state.container.secret_store.get(persisted.token_secret_ref) == first_token
        )

        fetched = client.get(f"/api/v1/agent-connections/{connection_id}")
        assert fetched.status_code == 200
        assert fetched.json()["token"] is None
        assert fetched.json()["connectCommand"] is None
        listed = client.get("/api/v1/agent-connections").json()
        assert listed["total"] == 1
        assert listed["items"][0]["agentConnectionId"] == connection_id
        assert listed["items"][0]["token"] is None

        rotated = client.post(f"/api/v1/agent-connections/{connection_id}/rotate-token")
        assert rotated.status_code == 200, rotated.text
        second_token = rotated.json()["token"]
        assert second_token and second_token != first_token
        service = client.app.state.container.agent_connection_service
        assert service.authenticate(connection_id, first_token) is False
        assert service.authenticate(connection_id, second_token) is True

        deleted = client.delete(f"/api/v1/agent-connections/{connection_id}")
        assert deleted.status_code == 204
        assert client.get(f"/api/v1/agent-connections/{connection_id}").status_code == 404
        assert service.authenticate(connection_id, second_token) is False


def test_agent_websocket_bearer_handshake_and_health(tmp_path) -> None:
    client, _ = make_client(tmp_path)
    with client:
        created = client.post(
            "/api/v1/agent-connections",
            json={"name": "Connected calculator"},
        ).json()
        connection_id = created["agentConnectionId"]
        path = f"/api/v1/agent-connections/{connection_id}/connect"

        with client.websocket_connect(
            path,
            headers={"Authorization": f"Bearer {created['token']}"},
        ) as websocket:
            assert websocket.receive_json() == {
                "type": "registered",
                "agentConnectionId": connection_id,
                "protocolVersion": 1,
            }
            websocket.send_json({"type": "ready"})
            websocket.send_json({"type": "heartbeat"})
            assert websocket.receive_json() == {"type": "heartbeat_ack"}
            health = client.get(f"/api/v1/agent-connections/{connection_id}/health")
            assert health.status_code == 200
            assert health.json()["status"] == "CONNECTED"
            assert health.json()["healthy"] is True
            assert health.json()["lastSeenAt"] is not None

        health = client.get(f"/api/v1/agent-connections/{connection_id}/health").json()
        assert health["status"] == "OFFLINE"
        assert health["healthy"] is False

        with pytest.raises(WebSocketDisconnect) as rejected:
            with client.websocket_connect(
                path,
                headers={"Authorization": "Bearer wrong-token"},
            ) as websocket:
                websocket.receive_json()
        assert rejected.value.code == 4401


def test_launch_and_schedule_api_persist_agent_version_and_parameters(tmp_path) -> None:
    client, gateway = make_client(tmp_path)
    with client:
        configure(client)
        assert client.post("/api/v1/settings/agentloop/test").json()["ok"] is True
        agent = client.post(
            "/api/v1/agent-connections",
            json={"name": "Calculator"},
        ).json()
        payload = {
            "agentConnectionId": agent["agentConnectionId"],
            "versionMode": "MANUAL",
            "experimentVersion": "1.2.3",
            "experimentParameters": {"temperature": 0, "nested": {"b": 2, "a": 1}},
            "recordName": "calculator-smoke",
            "contextSchemaVersion": 1,
        }
        with client.websocket_connect(
            f"/api/v1/agent-connections/{agent['agentConnectionId']}/connect",
            headers={"Authorization": f"Bearer {agent['token']}"},
        ) as websocket:
            assert websocket.receive_json()["type"] == "registered"
            websocket.send_json({"type": "ready"})
            launched = client.post(
                "/api/v1/plans/plan-1/launch",
                json=payload,
                headers={"Idempotency-Key": "configured-launch"},
            )
        assert launched.status_code == 202, launched.text
        launch = launched.json()
        assert launch["agentConnectionId"] == agent["agentConnectionId"]
        assert launch["agentConnectionNameSnapshot"] == "Calculator"
        assert launch["versionMode"] == "MANUAL"
        assert launch["experimentVersion"] == "1.2.3"
        assert launch["experimentParameters"] == {
            "nested": {"a": 1, "b": 2},
            "temperature": 0,
        }
        assert launch["launchConfigHash"]
        assert gateway.last_execution_config is not None
        assert gateway.last_execution_config.launch_id == launch["launchId"]
        assert gateway.last_execution_config.experiment_version == "1.2.3"

        conflict_payload = {**payload, "experimentVersion": "1.2.4"}
        conflict = client.post(
            "/api/v1/plans/plan-1/launch",
            json=conflict_payload,
            headers={"Idempotency-Key": "configured-launch"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"

        scheduled = client.post(
            "/api/v1/schedules",
            json={
                **payload,
                "name": "configured-hourly",
                "planId": "plan-1",
                "cadence": {
                    "type": "INTERVAL_HOURS",
                    "intervalHours": 1,
                    "anchorAt": "2026-08-08T00:00:00Z",
                },
                "timezone": "UTC",
            },
        )
        assert scheduled.status_code == 201, scheduled.text
        schedule = scheduled.json()
        assert schedule["agentConnectionId"] == agent["agentConnectionId"]
        assert schedule["agentConnectionNameSnapshot"] == "Calculator"
        assert schedule["versionMode"] == "MANUAL"
        assert schedule["manualExperimentVersion"] == "1.2.3"
        assert schedule["experimentParameters"] == payload["experimentParameters"]
        assert schedule["launchConfigHash"] == launch["launchConfigHash"]
