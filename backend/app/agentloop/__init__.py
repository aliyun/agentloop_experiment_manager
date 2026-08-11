"""Application-facing AgentLoop gateway contract and offline fake."""

from .cloud_control import AgentLoopCloudControl
from .errors import (
    AgentLoopGatewayError,
    AgentLoopTimeoutError,
    ExperimentPreparationError,
    PlanNotFoundError,
    RecordNotFoundError,
    SubmissionUnknownError,
)
from .fake import (
    FakeAgentLoopGateway,
    FakeStartBehavior,
    FakeStartKind,
    InMemoryAgentLoopGateway,
    InMemoryExperimentHandle,
)
from .gateway import (
    AgentLoopGateway,
    ExperimentHandle,
    RecordCreatedCallback,
)
from .models import (
    AgentLoopConnection,
    ConnectionCheck,
    LocalHandoffOutcome,
    LocalHandoffState,
    Page,
    PlanDetail,
    PlanQuery,
    PlanSummary,
    RecordDetail,
    RecordQuery,
    RecordSummary,
)
from .offline_runner import (
    AgentLoopOfflineRunner,
    AgentTaskResult,
    OfflineExperimentSpec,
)
from .production import ProductionAgentLoopGateway
from .trace_correlation import (
    AgentSpaceTrajectoryLogStoreClient,
    TraceCandidate,
    TraceCorrelationContext,
    TraceCorrelationError,
    TraceCorrelationProgress,
    TraceCorrelationResult,
    TraceCorrelationService,
    TraceCorrelationStatus,
    TracePollingPolicy,
    TraceSearchError,
)

__all__ = [
    "AgentLoopGateway",
    "AgentLoopGatewayError",
    "AgentLoopCloudControl",
    "AgentLoopOfflineRunner",
    "AgentLoopConnection",
    "AgentSpaceTrajectoryLogStoreClient",
    "AgentLoopTimeoutError",
    "ExperimentPreparationError",
    "ConnectionCheck",
    "ExperimentHandle",
    "FakeAgentLoopGateway",
    "FakeStartBehavior",
    "FakeStartKind",
    "InMemoryAgentLoopGateway",
    "InMemoryExperimentHandle",
    "LocalHandoffOutcome",
    "LocalHandoffState",
    "AgentTaskResult",
    "OfflineExperimentSpec",
    "Page",
    "PlanDetail",
    "PlanNotFoundError",
    "PlanQuery",
    "PlanSummary",
    "RecordCreatedCallback",
    "RecordDetail",
    "RecordNotFoundError",
    "RecordQuery",
    "RecordSummary",
    "ProductionAgentLoopGateway",
    "SubmissionUnknownError",
    "TraceCandidate",
    "TraceCorrelationContext",
    "TraceCorrelationError",
    "TraceCorrelationProgress",
    "TraceCorrelationResult",
    "TraceCorrelationService",
    "TraceCorrelationStatus",
    "TracePollingPolicy",
    "TraceSearchError",
]
