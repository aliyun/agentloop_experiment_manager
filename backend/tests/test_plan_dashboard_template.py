from __future__ import annotations

import copy

import pytest

from app.dashboards.template import (
    dashboard_name,
    render_plan_dashboard,
    validate_rendered_dashboard,
)


def _queries(body: dict) -> list[dict]:
    return [
        query
        for chart in body["charts"]
        for query in (chart.get("search") or {}).get("chartQueries") or []
        if query.get("tokenQuery") or query.get("query")
    ]


def _chart(body: dict, title: str) -> dict:
    return next(chart for chart in body["charts"] if chart["title"] == title)


def test_dashboard_identity_is_stable_per_plan_and_ignores_plan_rename() -> None:
    original = render_plan_dashboard(
        project="agentloop-project",
        agent_space="customer-support",
        plan_id="plan-123",
        plan_name="Initial name",
    )
    renamed = render_plan_dashboard(
        project="agentloop-project",
        agent_space="customer-support",
        plan_id="plan-123",
        plan_name="Renamed plan",
    )
    another_plan = render_plan_dashboard(
        project="agentloop-project",
        agent_space="customer-support",
        plan_id="plan-456",
        plan_name="Initial name",
    )

    assert original.dashboard_name == renamed.dashboard_name
    assert original.body["dashboardName"] == renamed.body["dashboardName"]
    assert original.display_name != renamed.display_name
    assert original.dashboard_name != another_plan.dashboard_name
    assert original.dashboard_name == dashboard_name("customer-support", "plan-123")


def test_every_evaluation_query_is_immutably_scoped_and_no_plan_selector_exists() -> None:
    rendered = render_plan_dashboard(
        project="agentloop-project",
        agent_space="customer-support",
        plan_id="plan-123",
        plan_name="Support regression",
    )
    queries = _queries(rendered.body)

    assert len(rendered.body["charts"]) == 19
    assert len(queries) == 18
    assert all(
        rendered.plan_filter in (query.get("tokenQuery") or query["query"])
        for query in queries
    )
    assert all(query["project"] == "agentloop-project" for query in queries)
    assert all(query["logstore"] == "evaluation_detail" for query in queries)
    selector_keys = {
        (chart.get("display") or {}).get("dropListOption", {}).get("key")
        for chart in rendered.body["charts"]
    }
    assert not selector_keys & {
        "data_link.planId",
        "data_link.plan_id",
        "data_link.planName",
    }
    assert rendered.body["attribute"]["experimentPlanId"] == "plan-123"
    assert rendered.body["attribute"]["templateVersion"] == "2"


def test_top_filters_use_one_compact_row_without_gaps_or_overlaps() -> None:
    rendered = render_plan_dashboard(
        project="agentloop-project",
        agent_space="customer-support",
        plan_id="plan-123",
        plan_name="Support regression",
    )
    filter_titles = {
        "filter_run",
        "filter_experiment_version",
        "filter_item",
        "filter_evaluator",
        "filter_evaluator_version",
        "filter_rubric_id",
        "filter_score_interval",
    }
    controls = [
        chart for chart in rendered.body["charts"] if chart["title"] in filter_titles
    ]

    assert len(controls) == 7
    assert all(chart["display"]["yPos"] == 0 for chart in controls)
    assert all(chart["display"]["height"] == 2 for chart in controls)
    occupied_columns = [
        column
        for chart in controls
        for column in range(
            chart["display"]["xPos"],
            chart["display"]["xPos"] + chart["display"]["width"],
        )
    ]
    assert sorted(occupied_columns) == list(range(24))
    assert min(
        chart["display"]["yPos"]
        for chart in rendered.body["charts"]
        if chart["title"].startswith("kpi_")
    ) == 2


def test_per_item_trend_uses_sls_classification_series_contract() -> None:
    rendered = render_plan_dashboard(
        project="agentloop-project",
        agent_space="customer-support",
        plan_id="plan-123",
        plan_name="Support regression",
    )
    chart = _chart(rendered.body, "trend_score_by_item")
    query = chart["search"]["chartQueries"][0]["tokenQuery"]

    assert chart["type"] == "aggpro"
    assert chart["display"]["aggChartOption"] == {"chartType": "line"}
    assert chart["display"]["queryOptionMap"] == {
        "A": {
            "name": "A",
            "xAxisKey": "dt",
            "yAxisKey": "avg_score",
            "aggField": "item_series",
        }
    }
    assert chart["display"]["dataOption"]["maxClassifyGroupNum"] == 50
    assert "PARTITION BY experiment_run_id" in query
    assert "GROUP BY dt, item_series" in query
    assert "status = 'success'" in query
    assert "dataset_item_id IS NOT NULL" in query
    assert "${{score_interval_seconds|3600}}" in query


def test_per_item_average_bar_is_horizontal_and_low_score_first() -> None:
    rendered = render_plan_dashboard(
        project="agentloop-project",
        agent_space="customer-support",
        plan_id="plan-123",
        plan_name="Support regression",
    )
    chart = _chart(rendered.body, "avg_score_by_item")
    query = chart["search"]["chartQueries"][0]["tokenQuery"]

    assert chart["type"] == "barpro"
    assert chart["display"]["barOptions"]["orientation"] == "horizontal"
    assert chart["display"]["legendOption"]["show"] is False
    assert chart["display"]["queryOptionMap"] == {
        "A": {
            "name": "A",
            "xAxisKey": "item_label",
            "yAxisKeys": ["avg_score"],
        }
    }
    assert "GROUP BY dataset_item_id" in query
    assert "ORDER BY avg_score ASC, dataset_item_id ASC" in query
    assert "status = 'success'" in query
    assert "coalesce(normalized_score_value, score_value) IS NOT NULL" in query


def test_plan_id_special_characters_are_escaped_in_every_query() -> None:
    plan_id = 'plan "quoted" \\ folder'
    rendered = render_plan_dashboard(
        project="agentloop-project",
        agent_space="customer-support",
        plan_id=plan_id,
        plan_name="Escaping test",
    )

    expected = (
        'channel: experiment and (data_link.planId: "plan \\"quoted\\" \\\\ folder" '
        'or data_link.plan_id: "plan \\"quoted\\" \\\\ folder")'
    )
    assert rendered.plan_filter == expected
    assert all(
        expected in (query.get("tokenQuery") or query["query"])
        for query in _queries(rendered.body)
    )


def test_validator_rejects_a_query_that_loses_the_fixed_plan_filter() -> None:
    rendered = render_plan_dashboard(
        project="agentloop-project",
        agent_space="customer-support",
        plan_id="plan-123",
        plan_name="Support regression",
    )
    tampered = copy.deepcopy(rendered.body)
    first_query = _queries(tampered)[0]
    first_query["tokenQuery"] = "channel: experiment | SELECT count(*)"

    with pytest.raises(ValueError, match="not permanently Plan-scoped"):
        validate_rendered_dashboard(
            tampered,
            project=rendered.project,
            logstore=rendered.logstore,
            plan_id="plan-123",
            template_version=rendered.template_version,
            template_sha256=rendered.template_sha256,
        )


def test_validator_rejects_a_readback_that_loses_item_series_mapping() -> None:
    rendered = render_plan_dashboard(
        project="agentloop-project",
        agent_space="customer-support",
        plan_id="plan-123",
        plan_name="Support regression",
    )
    tampered = copy.deepcopy(rendered.body)
    _chart(tampered, "trend_score_by_item")["display"]["queryOptionMap"]["A"].pop(
        "aggField"
    )

    with pytest.raises(ValueError, match="one line per dataset item"):
        validate_rendered_dashboard(
            tampered,
            project=rendered.project,
            logstore=rendered.logstore,
            plan_id="plan-123",
            template_version=rendered.template_version,
            template_sha256=rendered.template_sha256,
        )


@pytest.mark.parametrize("plan_id", ["", "   ", "plan\nother"])
def test_invalid_plan_ids_are_rejected(plan_id: str) -> None:
    with pytest.raises(ValueError, match="plan_id"):
        render_plan_dashboard(
            project="agentloop-project",
            agent_space="customer-support",
            plan_id=plan_id,
            plan_name="Invalid",
        )
