"""Render the versioned, immutable-scope dashboard for one Experiment Plan."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "experiment-plan-dashboard-v2.json"
INTERVAL_TOKEN = "${{score_interval_seconds|3600}}"


def _scalar(path: str) -> str:
    return f"json_extract_scalar(custom_outputs, '$.{path}')"


def _json(path: str) -> str:
    return f"nullif(json_format(json_extract(custom_outputs, '$.{path}')), 'null')"


RUBRIC_POINT_COUNT = f"try_cast({_scalar('rubric_point_count')} AS bigint)"
COVERED_POINT_COUNT = f"try_cast({_scalar('covered_point_count')} AS bigint)"
MISSING_POINT_COUNT = f"try_cast({_scalar('missing_point_count')} AS bigint)"
CONCEPTUAL_ERROR_COUNT = f"try_cast({_scalar('conceptual_error_count')} AS bigint)"
SCORE_CAP_COUNT = f"try_cast({_scalar('score_cap_count')} AS bigint)"


def _array_json_or_empty(path: str, count_expression: str) -> str:
    return (
        f"coalesce({_json(path)}, "
        f"CASE WHEN {count_expression} = 0 THEN '[]' END)"
    )


RANKED_CTE = f"""WITH ranked AS (
  SELECT
    coalesce(timestamp, cast(__time__ AS bigint) * 1000) AS event_time_ms,
    eval_id,
    task_id,
    run_id,
    evaluator_name,
    evaluator_display_name,
    score_name,
    status,
    result_type,
    score_value,
    normalized_score_value,
    explanation,
    error_code,
    error_message,
    eval_latency,
    coalesce(
      nullif(json_extract_scalar(data_link, '$.planId'), ''),
      nullif(json_extract_scalar(data_link, '$.plan_id'), '')
    ) AS experiment_plan_id,
    json_extract_scalar(data_link, '$.experiment_id') AS experiment_run_id,
    json_extract_scalar(data_link, '$.experiment_version') AS experiment_version,
    json_extract_scalar(data_link, '$.dataset_id') AS dataset_id,
    json_extract_scalar(data_link, '$.dataset_item_id') AS dataset_item_id,
    json_extract_scalar(data_link, '$.trace_id') AS trace_id,
    nullif({_scalar('rubric_id')}, '') AS rubric_id,
    nullif({_scalar('primary_issue_category')}, '') AS primary_issue_category,
    nullif({_scalar('improvement_suggestion')}, '') AS improvement_suggestion,
    nullif({_scalar('evaluator_version')}, '') AS evaluator_version,
    nullif({_scalar('rubric_version')}, '') AS rubric_version,
    {RUBRIC_POINT_COUNT} AS rubric_point_count,
    {COVERED_POINT_COUNT} AS covered_point_count,
    {MISSING_POINT_COUNT} AS missing_point_count,
    {CONCEPTUAL_ERROR_COUNT} AS conceptual_error_count,
    {SCORE_CAP_COUNT} AS score_cap_count,
    {_array_json_or_empty('covered_points', COVERED_POINT_COUNT)} AS covered_points,
    {_array_json_or_empty('missing_points', MISSING_POINT_COUNT)} AS missing_points,
    {_array_json_or_empty('conceptual_errors', CONCEPTUAL_ERROR_COUNT)} AS conceptual_errors,
    {_array_json_or_empty('score_caps_applied', SCORE_CAP_COUNT)} AS score_caps_applied,
    custom_outputs AS custom_outputs_json,
    row_number() OVER (
      PARTITION BY
        coalesce(json_extract_scalar(data_link, '$.experiment_id'), ''),
        coalesce(json_extract_scalar(data_link, '$.dataset_item_id'), ''),
        coalesce(evaluator_name, ''),
        coalesce(score_name, ''),
        coalesce(nullif({_scalar('evaluator_version')}, ''), 'legacy')
      ORDER BY timestamp DESC, eval_id DESC
    ) AS row_num
  FROM log
)"""


@dataclass(frozen=True, slots=True)
class RenderedPlanDashboard:
    body: dict[str, Any]
    dashboard_name: str
    display_name: str
    template_version: str
    template_sha256: str
    project: str
    logstore: str
    plan_filter: str


def dashboard_name(agent_space: str, plan_id: str) -> str:
    identity = f"{agent_space}\0{plan_id}".encode()
    return f"agentloop-exp-plan-{hashlib.sha256(identity).hexdigest()[:24]}"


def display_name(plan_name: str, plan_id: str) -> str:
    normalized = " ".join((plan_name or plan_id).split()) or plan_id
    return f"实验计划 · {normalized}"[:128]


def render_plan_dashboard(
    *,
    project: str,
    agent_space: str,
    plan_id: str,
    plan_name: str,
) -> RenderedPlanDashboard:
    template_bytes = TEMPLATE_PATH.read_bytes()
    template = json.loads(template_bytes)
    template_version = str(template["templateVersion"])
    template_sha256 = hashlib.sha256(template_bytes).hexdigest()
    logstore = str(template["logstore"])
    time_range = str(template["timeRange"])
    name = dashboard_name(agent_space, plan_id)
    title = display_name(plan_name, plan_id)
    plan_filter = _plan_filter(plan_id)

    charts: list[dict[str, Any]] = []
    filter_positions = {
        "filter_run": (0, 0, 4),
        "filter_experiment_version": (4, 0, 3),
        "filter_item": (7, 0, 5),
        "filter_evaluator": (12, 0, 3),
        "filter_evaluator_version": (15, 0, 3),
        "filter_rubric_id": (18, 0, 3),
    }
    for item in template["filters"]:
        x, y, width = filter_positions[item["id"]]
        query = f"{plan_filter} | {item['sql']}"
        charts.append(
            _filter_chart(
                item,
                project=project,
                logstore=logstore,
                time_range=time_range,
                query=query,
                x=x,
                y=y,
                width=width,
            )
        )
    charts.append(_grain_control(time_range=time_range))

    time_bucket = _time_bucket_expression()
    for panel in template["panels"]:
        sql = str(panel["sql"]).replace("${time_bucket}", time_bucket)
        query = f"{plan_filter} | {RANKED_CTE}\n{sql}"
        charts.append(
            _panel_chart(
                panel,
                project=project,
                logstore=logstore,
                time_range=time_range,
                query=query,
            )
        )

    body = {
        "dashboardName": name,
        "displayName": title,
        "description": str(template["description"]),
        "attribute": {
            "type": "grid",
            "managedBy": "agentloop-local-experiment-launcher",
            "experimentPlanId": plan_id,
            "agentSpace": agent_space,
            "templateVersion": template_version,
            "templateSha256": template_sha256,
        },
        "charts": charts,
    }
    validate_rendered_dashboard(
        body,
        project=project,
        logstore=logstore,
        plan_id=plan_id,
        template_version=template_version,
        template_sha256=template_sha256,
    )
    return RenderedPlanDashboard(
        body=body,
        dashboard_name=name,
        display_name=title,
        template_version=template_version,
        template_sha256=template_sha256,
        project=project,
        logstore=logstore,
        plan_filter=plan_filter,
    )


def validate_rendered_dashboard(
    body: dict[str, Any],
    *,
    project: str,
    logstore: str,
    plan_id: str,
    template_version: str,
    template_sha256: str,
) -> None:
    attribute = body.get("attribute") or {}
    expected = {
        "managedBy": "agentloop-local-experiment-launcher",
        "experimentPlanId": plan_id,
        "templateVersion": template_version,
        "templateSha256": template_sha256,
    }
    if any(attribute.get(key) != value for key, value in expected.items()):
        raise ValueError("dashboard attributes do not match the rendered Plan template")
    charts = body.get("charts")
    if not isinstance(charts, list) or len(charts) != 19:
        raise ValueError("Experiment Plan dashboard must contain exactly 19 components")
    _validate_chart_contract(charts)
    plan_filter = _plan_filter(plan_id)
    query_count = 0
    for chart in charts:
        drop_list = (chart.get("display") or {}).get("dropListOption") or {}
        if drop_list.get("key") in {"data_link.planId", "data_link.plan_id", "data_link.planName"}:
            raise ValueError("Plan-specific dashboard must not expose a Plan selector")
        for query in (chart.get("search") or {}).get("chartQueries") or []:
            token_query = query.get("tokenQuery") or query.get("query") or ""
            if not token_query:
                continue
            query_count += 1
            if plan_filter not in token_query:
                raise ValueError(f"chart {chart.get('title')} is not permanently Plan-scoped")
            if query.get("project") != project or query.get("logstore") != logstore:
                raise ValueError(f"chart {chart.get('title')} has an unexpected datasource")
    if query_count != 18:
        raise ValueError(f"expected 18 scoped chart queries, found {query_count}")


def _validate_chart_contract(charts: list[dict[str, Any]]) -> None:
    by_title = {chart.get("title"): chart for chart in charts}
    if len(by_title) != len(charts):
        raise ValueError("dashboard component titles must be unique")

    compact_controls = {
        "filter_run": (0, 4),
        "filter_experiment_version": (4, 3),
        "filter_item": (7, 5),
        "filter_evaluator": (12, 3),
        "filter_evaluator_version": (15, 3),
        "filter_rubric_id": (18, 3),
        "filter_score_interval": (21, 3),
    }
    for title, (x_position, width) in compact_controls.items():
        display = (by_title.get(title) or {}).get("display") or {}
        if (
            display.get("xPos") != x_position
            or display.get("yPos") != 0
            or display.get("width") != width
            or display.get("height") != 2
        ):
            raise ValueError(f"dashboard control {title} is not in the compact top row")

    item_trend = by_title.get("trend_score_by_item") or {}
    item_trend_display = item_trend.get("display") or {}
    if (
        item_trend.get("type") != "aggpro"
        or item_trend_display.get("aggChartOption") != {"chartType": "line"}
        or item_trend_display.get("queryOptionMap")
        != {
            "A": {
                "name": "A",
                "xAxisKey": "dt",
                "yAxisKey": "avg_score",
                "aggField": "item_series",
            }
        }
        or (item_trend_display.get("dataOption") or {}).get("maxClassifyGroupNum")
        != 50
    ):
        raise ValueError("per-item trend must classify one line per dataset item")

    item_average = by_title.get("avg_score_by_item") or {}
    item_average_display = item_average.get("display") or {}
    if (
        item_average.get("type") != "barpro"
        or (item_average_display.get("barOptions") or {}).get("orientation")
        != "horizontal"
        or (item_average_display.get("legendOption") or {}).get("show") is not False
        or item_average_display.get("queryOptionMap")
        != {
            "A": {
                "name": "A",
                "xAxisKey": "item_label",
                "yAxisKeys": ["avg_score"],
            }
        }
    ):
        raise ValueError("per-item average must be a low-score-first horizontal bar chart")


def _plan_filter(plan_id: str) -> str:
    if not isinstance(plan_id, str) or not plan_id.strip() or len(plan_id) > 256:
        raise ValueError("plan_id must be a non-empty string of at most 256 characters")
    if any(ord(character) < 0x20 for character in plan_id):
        raise ValueError("plan_id must not contain control characters")
    escaped = plan_id.replace("\\", "\\\\").replace('"', '\\"')
    return (
        "channel: experiment and "
        f"(data_link.planId: \"{escaped}\" or data_link.plan_id: \"{escaped}\")"
    )


def _time_bucket_expression() -> str:
    seconds = "cast(floor(event_time_ms / 1000.0) AS bigint)"
    return f"from_unixtime({seconds} - {seconds} % {INTERVAL_TOKEN})"


def _filter_chart(
    item: dict[str, Any],
    *,
    project: str,
    logstore: str,
    time_range: str,
    query: str,
    x: int,
    y: int,
    width: int,
) -> dict[str, Any]:
    return {
        "title": item["id"],
        "type": "droplistpro",
        "search": _search(
            project=project,
            logstore=logstore,
            time_range=time_range,
            query=query,
            inherit_filter=False,
        ),
        "display": {
            "xPos": x,
            "yPos": y,
            "width": width,
            "height": 2,
            "version": "2",
            "displayName": item["id"],
            "bindQuery": True,
            "showDropListChart": True,
            "basicOptions": _basic_options(str(item["alias"]), show_title=False),
            "dropListOption": {
                "autoFilter": False,
                "alias": item["alias"],
                "globalFilter": False,
                "logic": "and",
                "list": [],
                "type": "filter",
                "listAlias": [],
                "key": item["key"],
                "listDefault": [],
            },
        },
        "action": {},
    }


def _grain_control(*, time_range: str) -> dict[str, Any]:
    return {
        "title": "filter_score_interval",
        "type": "droplistpro",
        "search": _search(
            project="",
            logstore="",
            time_range=time_range,
            query="",
            inherit_filter=False,
        ),
        "display": {
            "xPos": 21,
            "yPos": 0,
            "width": 3,
            "height": 2,
            "version": "2",
            "displayName": "filter_score_interval",
            "bindQuery": False,
            "showDropListChart": True,
            "basicOptions": _basic_options("分数趋势粒度", show_title=False),
            "dropListOption": {
                "tokenDefault": [False, False, True, False, False],
                "alias": "score_interval_seconds",
                "multipleToken": False,
                "showType": "select",
                "list": ["300", "900", "3600", "21600", "86400"],
                "type": "token",
                "listAlias": ["5 分钟", "15 分钟", "1 小时", "6 小时", "1 天"],
                "key": "score_interval_seconds",
                "multipleTokenKey": "",
            },
        },
        "action": {},
    }


def _panel_chart(
    panel: dict[str, Any],
    *,
    project: str,
    logstore: str,
    time_range: str,
    query: str,
) -> dict[str, Any]:
    chart_type = str(panel["type"])
    x, y, width, height = panel["position"]
    display = _display_template(chart_type)
    query_option = _query_option(panel, chart_type=chart_type)
    if query_option is not None:
        display["queryOptionMap"] = {"A": query_option}
    if chart_type == "barpro" and panel.get("orientation"):
        display["barOptions"]["orientation"] = str(panel["orientation"])
    if "showLegend" in panel:
        display["legendOption"]["show"] = bool(panel["showLegend"])
    if chart_type == "aggpro" and panel.get("maxClassifyGroupNum") is not None:
        display["dataOption"]["maxClassifyGroupNum"] = int(
            panel["maxClassifyGroupNum"]
        )
    display.update(
        {
            "xPos": x,
            "yPos": y,
            "width": width,
            "height": height,
            "version": "2",
            "displayName": panel["id"],
            "basicOptions": _basic_options(str(panel["title"]), show_title=True),
        }
    )
    return {
        "title": panel["id"],
        "type": chart_type,
        "search": _search(
            project=project,
            logstore=logstore,
            time_range=time_range,
            query=query,
            inherit_filter=True,
        ),
        "display": display,
        "action": {},
    }


def _query_option(panel: dict[str, Any], *, chart_type: str) -> dict[str, Any] | None:
    x_axis_key = panel.get("xAxisKey")
    if not x_axis_key:
        return None
    option: dict[str, Any] = {"name": "A", "xAxisKey": str(x_axis_key)}
    if chart_type == "aggpro":
        y_axis_key = panel.get("yAxisKey")
        agg_field = panel.get("aggField")
        if not y_axis_key or not agg_field:
            raise ValueError("aggpro panels require yAxisKey and aggField")
        option["yAxisKey"] = str(y_axis_key)
        option["aggField"] = str(agg_field)
        return option
    y_axis_keys = panel.get("yAxisKeys")
    if not isinstance(y_axis_keys, list) or not y_axis_keys:
        raise ValueError(f"{chart_type} panels with xAxisKey require yAxisKeys")
    option["yAxisKeys"] = [str(value) for value in y_axis_keys]
    return option


def _search(
    *,
    project: str,
    logstore: str,
    time_range: str,
    query: str,
    inherit_filter: bool,
) -> dict[str, Any]:
    return {
        "isInheritFilter": inherit_filter,
        "query": "@",
        "start": time_range,
        "timeSpanType": "",
        "topic": "",
        "end": "now",
        "chartQueries": [
            {
                "datasource": "logstore",
                "query": query,
                "name": "A",
                "tokenQuery": query,
                "project": project,
                "logstore": logstore,
            }
        ],
        "logstore": logstore or "@",
        "dataSourceType": "current" if query else "mixed",
    }


def _basic_options(title: str, *, show_title: bool) -> dict[str, Any]:
    return {
        "displayName": title,
        "showTitle": show_title,
        "showBackground": show_title,
        "showTime": show_title,
        "showBorder": show_title,
    }


def _display_template(chart_type: str) -> dict[str, Any]:
    common = {
        "standardOption": {
            "colorSchame": {"schema": "threshold", "color": "#0070cc"},
            "unit": {"unit": "none"},
            "format": "none",
        },
        "queryOptionMap": {"A": {}},
        "innerTokenOption": [],
        "documentLinkOption": {"showIcon": True, "documentLinks": []},
    }
    if chart_type == "statpro":
        common.update(
            {
                "isTimeSeries": False,
                "statValueOption": {
                    "calculationType": "first",
                    "showMode": "calculate",
                    "layoutOrientation": "auto",
                },
                "statStyleOptions": {
                    "textAlignment": "auto",
                    "colorMode": "value",
                    "graphMode": "none",
                    "textMode": "auto",
                },
                "thresholdOption": {"thresholds": [], "thresholdMode": "absolute"},
            }
        )
    elif chart_type == "linepro":
        common.update(
            {
                "isTimeSeries": True,
                "graphOptions": {
                    "showPoint": "none",
                    "lineInterpolation": "smooth",
                    "lineWidth": 1.5,
                    "seriesStyle": "lines",
                },
                "xAxisOption": {"timeRangeMode": "dataTime", "show": True},
                "yAxisOption": {"stackingMode": "none", "show": True},
                "legendOption": {"show": True, "position": "right"},
                "tooltipOption": {"mode": "all"},
            }
        )
    elif chart_type == "tablepro":
        common.update(
            {
                "tableOptions": {
                    "showHeader": True,
                    "showTotal": True,
                    "pageSize": 20,
                    "showMode": "pagination",
                    "rowHeight": 36,
                },
                "columnOptions": {
                    "showAll": False,
                    "columnMinWidth": 100,
                    "fontSize": 12,
                    "columnAlign": "left",
                },
                "fieldOptions": [],
                "valueMappingOption": [],
                "actionOptions": [],
            }
        )
    elif chart_type == "barpro":
        common.update(
            {
                "isTimeSeries": False,
                "barOptions": {
                    "orientation": "vertical",
                    "showValues": "auto",
                    "stackingMode": "none",
                },
                "xAxisOption": {"show": True},
                "yAxisOption": {"show": True},
                "legendOption": {"show": True, "position": "right"},
                "tooltipOption": {"mode": "all"},
            }
        )
    elif chart_type == "aggpro":
        common.update(
            {
                "isTimeSeries": True,
                "graphOptions": {
                    "showPoint": "none",
                    "lineInterpolation": "smooth",
                    "lineWidth": 1.5,
                    "seriesStyle": "lines",
                    "pointSize": 6,
                    "fillOpacity": 20,
                    "gradientMode": "opacity",
                    "barStyle": "middle",
                },
                "xAxisOption": {
                    "timeRangeMode": "dataTime",
                    "show": True,
                    "zoomTarget": "global",
                    "format": {"value": "auto"},
                },
                "yAxisOption": {
                    "stackingMode": "none",
                    "show": True,
                    "position": 3,
                },
                "legendOption": {
                    "show": True,
                    "position": "bottom",
                    "actionMode": "toggle",
                    "sortOrder": "none",
                    "maxContent": 30,
                },
                "tooltipOption": {"mode": "all", "sortOrder": "none"},
                "dataOption": {
                    "autoFill": False,
                    "maxClassifyGroupNum": 50,
                    "autoFillNumber": 0,
                },
                "aggChartOption": {"chartType": "line"},
                "barOptions": {
                    "barWidth": 0.9,
                    "orientation": "vertical",
                    "showValues": "auto",
                    "fillOpacity": 85,
                    "stackingMode": "none",
                    "valueSize": 12,
                    "groupWidth": 0.7,
                    "labelLocation": "xAxis",
                    "gradientMode": "none",
                    "lineWidth": 1,
                },
            }
        )
    else:
        raise ValueError(f"unsupported dashboard chart type: {chart_type}")
    return copy.deepcopy(common)
