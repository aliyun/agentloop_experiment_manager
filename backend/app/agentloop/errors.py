"""Stable error taxonomy for the AgentLoop gateway boundary."""

from __future__ import annotations


class AgentLoopGatewayError(RuntimeError):
    """Base class for failures crossing the AgentLoop gateway boundary."""

    code = "AGENTLOOP_GATEWAY_ERROR"
    retryable = False

    def __init__(self, message: str, *, request_id: str | None = None) -> None:
        super().__init__(message)
        self.request_id = request_id


class PlanNotFoundError(AgentLoopGatewayError):
    """The requested plan is not visible in the configured AgentSpace."""

    code = "PLAN_NOT_FOUND"

    def __init__(self, plan_id: str) -> None:
        super().__init__(f"experiment plan not found: {plan_id}")
        self.plan_id = plan_id


class RecordNotFoundError(AgentLoopGatewayError):
    """The requested experiment record is not visible."""

    code = "RECORD_NOT_FOUND"

    def __init__(self, record_id: str) -> None:
        super().__init__(f"experiment record not found: {record_id}")
        self.record_id = record_id


class AgentLoopTimeoutError(AgentLoopGatewayError):
    """A gateway operation timed out before a remote acceptance was observed."""

    code = "AGENTLOOP_TIMEOUT"
    retryable = True

    def __init__(self, operation: str) -> None:
        super().__init__(f"AgentLoop operation timed out: {operation}")
        self.operation = operation
        self.remote_acceptance_unknown = False


class SubmissionUnknownError(AgentLoopGatewayError):
    """The remote side may have accepted a start, but no record ID was returned."""

    code = "SUBMISSION_UNKNOWN"
    retryable = False

    def __init__(self) -> None:
        super().__init__(
            "experiment submission outcome is unknown; automatic resubmission is disabled"
        )
        self.remote_acceptance_unknown = True


class ExperimentPreparationError(AgentLoopGatewayError):
    """The customer-side Runner failed before any remote Run was created."""

    code = "EXPERIMENT_PREPARATION_FAILED"
    retryable = False
    remote_acceptance_unknown = False
