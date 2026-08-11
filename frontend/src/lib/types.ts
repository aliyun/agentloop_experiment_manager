export interface PageResult<T> {
  items: T[];
  total: number;
  page: number;
  pageSize: number;
}

export interface Plan {
  planId: string;
  planName: string;
  experimentType: string;
  status: string;
  description?: string | null;
  datasetId?: string | null;
  datasetName?: string | null;
  experimentCount?: number;
  evaluatorCount?: number;
  createdAt?: string | null;
  updatedAt?: string | null;
  canLaunchLocally: boolean;
  cannotLaunchReason?: string | null;
  consoleUrl?: string | null;
}

export type AgentConnectionStatus = "ONLINE" | "OFFLINE" | "UNKNOWN" | string;

export interface AgentConnection {
  agentConnectionId: string;
  name: string;
  description?: string | null;
  status: AgentConnectionStatus;
  healthStatus?: string | null;
  lastSeenAt?: string | null;
  connectedAt?: string | null;
  createdAt?: string | null;
  updatedAt?: string | null;
  tokenHint?: string | null;
}

export function isAgentConnectionOnline(connection: Pick<AgentConnection, "status">): boolean {
  return ["ONLINE", "CONNECTED"].includes(connection.status.toUpperCase());
}

/** Secret material is returned only by create/rotate and must never enter list responses. */
export interface AgentConnectionSecret extends AgentConnection {
  token?: string | null;
  connectUrl?: string | null;
  connectCommand?: string | null;
}

export interface CreateAgentConnectionInput {
  name: string;
  description?: string | null;
}

export type ManagedAgentRuntimeState = "RUNNING" | "STOPPED" | "STARTING" | "FAILED" | string;

export interface AgentCodeConfig {
  language: string;
  source: string;
  configured: boolean;
  revision: number;
  sourceSha256?: string | null;
  runtimeState: ManagedAgentRuntimeState;
  updatedAt?: string | null;
}

export interface SaveAgentCodeInput {
  source: string;
  expectedRevision: number;
  activate: boolean;
}

export interface AgentTestInput {
  message: string;
  parameters?: Record<string, unknown>;
  timeoutSeconds?: number;
}

export interface SanitizedAgentTestOutput {
  type: string;
  preview: string;
  truncated: boolean;
}

export interface AgentTestResult {
  testId: string;
  status: "SUCCEEDED" | "FAILED" | "TIMED_OUT" | string;
  durationMs: number;
  codeRevision?: number | null;
  responseReceived: boolean;
  sanitizedOutput: SanitizedAgentTestOutput | null;
  sanitizedMeta: Record<string, unknown>;
  redactionCount: number;
  testedAt: string;
  errorCode?: string | null;
  errorMessage?: string | null;
}

export type VersionMode = "MANUAL" | "AUTO";
export type ExperimentParameters = Record<string, unknown>;

export interface ExperimentExecutionInput {
  agentConnectionId: string;
  versionMode: VersionMode;
  experimentVersion?: string | null;
  experimentParameters: ExperimentParameters;
  recordName?: string | null;
  contextSchemaVersion: 1;
}

export type SubmissionState =
  | "CREATED"
  | "SUBMITTING"
  | "ACCEPTED"
  | "DEFINITIVE_FAILURE"
  | "SUBMISSION_UNKNOWN"
  | "SKIPPED"
  | string;

export type RunnerState =
  | "NOT_STARTED"
  | "RUNNING"
  | "HANDED_OFF"
  | "FAILED"
  | "UNKNOWN"
  | string;

export interface TraceCorrelationSummary {
  state: string;
  traceIds: string[];
  totalTasks: number;
  pendingTasks: number;
  resolvedTasks: number;
  reportedTasks: number;
  failedTasks: number;
  totalAttempts: number;
  lastErrorCode?: string | null;
  lastErrorMessage?: string | null;
  startedAt?: string | null;
  finishedAt?: string | null;
}

export type PlanDashboardState = "PENDING" | "READY" | "FAILED" | string;

/**
 * One dashboard is owned by one AgentLoop Experiment Plan. Every launch of the
 * same plan points at the same dashboard; this is not a per-launch resource.
 */
export interface PlanDashboard {
  dashboardName: string;
  displayName: string;
  state: PlanDashboardState;
  url?: string | null;
  lastSyncedAt?: string | null;
  lastErrorCode?: string | null;
}

export interface LaunchResult {
  launchId: string;
  submissionState: SubmissionState;
  runnerState: RunnerState;
  skipReason?: string | null;
  recordId?: string | null;
  requestedAt: string;
  acceptedAt?: string | null;
  agentLoopUrl?: string | null;
  agentConnectionId?: string | null;
  agentConnectionNameSnapshot?: string | null;
  versionMode?: VersionMode | string | null;
  experimentVersion?: string | null;
  experimentParameters?: ExperimentParameters;
  recordName?: string | null;
  traceCorrelation?: TraceCorrelationSummary;
  planDashboard?: PlanDashboard | null;
}

export interface Launch extends LaunchResult {
  planId: string;
  planNameSnapshot?: string | null;
  triggerSource: "manual" | "schedule" | "schedule_run_now" | string;
  scheduledFor?: string | null;
  submitStartedAt?: string | null;
  workerFinishedAt?: string | null;
  errorCode?: string | null;
  errorMessage?: string | null;
  scheduleId?: string | null;
}

export type Cadence =
  | {
      type: "INTERVAL_MINUTES";
      intervalMinutes: number;
      anchorAt: string;
    }
  | {
      type: "INTERVAL_HOURS";
      intervalHours: number;
      anchorAt: string;
    }
  | {
      type: "DAILY";
      time: string;
    };

export interface Schedule {
  scheduleId: string;
  name: string;
  planId: string;
  planNameSnapshot?: string | null;
  agentConnectionId: string;
  agentConnectionNameSnapshot?: string | null;
  versionMode: VersionMode;
  experimentVersion?: string | null;
  experimentParameters: ExperimentParameters;
  recordName?: string | null;
  cadence: Cadence;
  timezone: string;
  misfirePolicy: "FIRE_ONCE" | "SKIP" | string;
  enabled: boolean;
  revision?: number;
  lastScheduledAt?: string | null;
  nextScheduledAt?: string | null;
  lastLaunch?: Launch | null;
  lastErrorMessage?: string | null;
  createdAt?: string | null;
  updatedAt?: string | null;
}

export interface CreateScheduleInput {
  name: string;
  planId: string;
  agentConnectionId: string;
  versionMode: VersionMode;
  experimentVersion?: string | null;
  experimentParameters: ExperimentParameters;
  recordName?: string | null;
  contextSchemaVersion: 1;
  cadence: Cadence;
  timezone: string;
  misfirePolicy: "FIRE_ONCE" | "SKIP";
  enabled: boolean;
}

export interface AgentLoopSettings {
  configured: boolean;
  displayName?: string | null;
  region: string;
  agentSpace: string;
  apiEndpoint?: string | null;
  consoleBaseUrl?: string | null;
  defaultTimezone: string;
  maxConcurrency: number;
  accessKeyIdMasked?: string | null;
  secretConfigured?: boolean;
  secretRef?: string | null;
  lastVerifiedAt?: string | null;
  lastVerificationStatus?: "SUCCESS" | "FAILED" | "UNKNOWN" | string | null;
  lastVerificationMessage?: string | null;
}

export interface UpdateAgentLoopSettingsInput {
  displayName?: string;
  region: string;
  agentSpace: string;
  apiEndpoint?: string | null;
  consoleBaseUrl?: string | null;
  defaultTimezone: string;
  maxConcurrency: number;
  accessKeyId?: string;
  accessKeySecret?: string;
}

export interface ConnectionTestResult {
  success: boolean;
  message: string;
  verifiedAt?: string | null;
  permissions?: string[];
}

export interface LauncherHealth {
  status: string;
  version: string;
  gatewayMode: string;
  schedulerEnabled: boolean;
}

export interface ApiErrorPayload {
  error?: {
    code?: string;
    message?: string;
    launchId?: string;
    retryable?: boolean;
    requestId?: string;
    line?: number;
    column?: number;
  };
  message?: string;
}
