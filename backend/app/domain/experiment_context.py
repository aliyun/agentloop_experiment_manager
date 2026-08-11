"""Safe two-stage XML context construction for offline experiment items."""

from __future__ import annotations

from dataclasses import dataclass
from xml.etree import ElementTree

CONTEXT_SCHEMA_VERSION = 1
_RUN_ID_PLACEHOLDER = "__AGENTLOOP_EXPERIMENT_RUN_ID__"
_DATASET_ITEM_ID_PLACEHOLDER = "__AGENTLOOP_DATASET_ITEM_ID__"
_FIELDS = (
    "experiment_plan_id",
    "experiment_run_id",
    "experiment_version",
    "dataset_item_id",
    "local_launch_id",
)


@dataclass(frozen=True, slots=True)
class ExperimentContextTemplate:
    experiment_plan_id: str
    experiment_version: str
    local_launch_id: str
    schema_version: int = CONTEXT_SCHEMA_VERSION

    def to_xml_template(self) -> str:
        return _build_xml(
            schema_version=self.schema_version,
            experiment_plan_id=self.experiment_plan_id,
            experiment_run_id=_RUN_ID_PLACEHOLDER,
            experiment_version=self.experiment_version,
            dataset_item_id=_DATASET_ITEM_ID_PLACEHOLDER,
            local_launch_id=self.local_launch_id,
        )

    def render(self, *, experiment_run_id: str, dataset_item_id: str) -> str:
        return render_experiment_context(
            self.to_xml_template(),
            experiment_run_id=experiment_run_id,
            dataset_item_id=dataset_item_id,
        )


def build_experiment_context_template(
    *,
    experiment_plan_id: str,
    experiment_version: str,
    local_launch_id: str,
    schema_version: int = CONTEXT_SCHEMA_VERSION,
) -> str:
    return ExperimentContextTemplate(
        experiment_plan_id=experiment_plan_id,
        experiment_version=experiment_version,
        local_launch_id=local_launch_id,
        schema_version=schema_version,
    ).to_xml_template()


def render_experiment_context(
    xml_template: str,
    *,
    experiment_run_id: str,
    dataset_item_id: str,
) -> str:
    """Fill dynamic identifiers after AgentLoop creates the authoritative Run."""

    try:
        root = ElementTree.fromstring(xml_template)
    except ElementTree.ParseError as exc:
        raise ValueError("invalid experiment context template") from exc
    if root.tag != "experiment_context":
        raise ValueError("experiment context root must be experiment_context")
    children = {child.tag: child for child in root}
    if set(children) != set(_FIELDS):
        raise ValueError("experiment context template fields are incomplete or unsupported")
    if children["experiment_run_id"].text != _RUN_ID_PLACEHOLDER:
        raise ValueError("experiment context template run placeholder is missing")
    if children["dataset_item_id"].text != _DATASET_ITEM_ID_PLACEHOLDER:
        raise ValueError("experiment context template Dataset placeholder is missing")
    children["experiment_run_id"].text = _non_empty(experiment_run_id, "experiment_run_id")
    children["dataset_item_id"].text = _non_empty(dataset_item_id, "dataset_item_id")
    return ElementTree.tostring(root, encoding="unicode", short_empty_elements=False)


def append_experiment_context(user_input: str, context_xml: str) -> str:
    """Append structured context without mutating the user's original input."""

    if not isinstance(user_input, str):
        raise TypeError("user_input must be a string")
    # Validate before injection so malformed or attacker-controlled fragments
    # can never be appended as platform context.
    try:
        root = ElementTree.fromstring(context_xml)
    except ElementTree.ParseError as exc:
        raise ValueError("context_xml must be valid XML") from exc
    if root.tag != "experiment_context":
        raise ValueError("context_xml root must be experiment_context")
    separator = "\n" if user_input else ""
    return f"{user_input}{separator}{context_xml}"


def _build_xml(*, schema_version: int, **values: str) -> str:
    if schema_version < 1:
        raise ValueError("schema_version must be positive")
    root = ElementTree.Element("experiment_context", {"schema_version": str(schema_version)})
    for field in _FIELDS:
        child = ElementTree.SubElement(root, field)
        child.text = _non_empty(values[field], field)
    return ElementTree.tostring(root, encoding="unicode", short_empty_elements=False)


def _non_empty(value: str, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must not be empty")
    return value
