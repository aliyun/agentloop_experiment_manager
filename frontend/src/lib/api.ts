import type {
  AgentCodeConfig,
  AgentConnection,
  AgentConnectionSecret,
  AgentLoopSettings,
  AgentTestInput,
  AgentTestResult,
  ApiErrorPayload,
  ConnectionTestResult,
  CreateAgentConnectionInput,
  CreateScheduleInput,
  ExperimentExecutionInput,
  ExperimentParameters,
  Launch,
  LaunchResult,
  LauncherHealth,
  PageResult,
  Plan,
  PlanDashboard,
  SaveAgentCodeInput,
  Schedule,
  UpdateAgentLoopSettingsInput,
} from "./types";
import { traceCorrelationFromApi } from "./traceCorrelation";

const API_BASE = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/$/, "");

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryable: boolean;
  readonly launchId?: string;
  readonly requestId?: string;
  readonly line?: number;
  readonly column?: number;

  constructor(status: number, payload: ApiErrorPayload) {
    const detail = payload.error;
    super(detail?.message || payload.message || `请求失败（HTTP ${status}）`);
    this.name = "ApiError";
    this.status = status;
    this.code = detail?.code || "UNKNOWN_ERROR";
    this.retryable = Boolean(detail?.retryable);
    this.launchId = detail?.launchId;
    this.requestId = detail?.requestId;
    this.line = detail?.line;
    this.column = detail?.column;
  }
}

function csrfToken(): string | undefined {
  const meta = document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content;
  if (meta) return meta;
  const match = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : undefined;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const method = (init.method || "GET").toUpperCase();
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const token = csrfToken();
    if (token) headers.set("X-CSRF-Token", token);
  }

  const response = await fetch(`${API_BASE}${path}`, { ...init, headers, credentials: "same-origin" });
  const isJson = response.headers.get("content-type")?.includes("application/json");
  const payload = isJson ? await response.json() : undefined;
  if (!response.ok) throw new ApiError(response.status, (payload || {}) as ApiErrorPayload);
  if (response.status === 204) return undefined as T;
  return payload as T;
}

function queryString(values: Record<string, string | number | boolean | undefined | null>): string {
  const params = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
  });
  const query = params.toString();
  return query ? `?${query}` : "";
}

function normalizePage<T>(
  payload: unknown,
  page: number,
  pageSize: number,
  mapItem?: (item: Record<string, unknown>) => T,
): PageResult<T> {
  if (Array.isArray(payload)) {
    const items = mapItem ? payload.map((item) => mapItem(item as Record<string, unknown>)) : payload as T[];
    return { items, total: items.length, page, pageSize };
  }
  const root = (payload || {}) as Record<string, unknown>;
  const data = (root.data && typeof root.data === "object" ? root.data : root) as Record<string, unknown>;
  const candidates = data.items ?? data.records ?? data.plans ?? data.schedules ?? data.launches
    ?? data.agentConnections ?? data.connections ?? [];
  const items = Array.isArray(candidates)
    ? (mapItem ? candidates.map((item) => mapItem(item as Record<string, unknown>)) : candidates as T[])
    : [];
  const normalizedPageSize = Number(data.pageSize ?? data.limit ?? pageSize);
  const offset = Number(data.offset ?? (page - 1) * normalizedPageSize);
  return {
    items,
    total: Number(data.total ?? data.totalCount ?? items.length),
    page: Number(data.page ?? Math.floor(offset / Math.max(normalizedPageSize, 1)) + 1),
    pageSize: normalizedPageSize,
  };
}

function parametersFromApi(value: unknown): ExperimentParameters {
  if (value && typeof value === "object" && !Array.isArray(value)) return value as ExperimentParameters;
  if (typeof value === "string") {
    try {
      const parsed: unknown = JSON.parse(value);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed as ExperimentParameters;
    } catch {
      // Old rows may contain malformed snapshots; display them without treating them as executable JSON.
    }
  }
  return {};
}

function agentConnectionFromApi(raw: Record<string, unknown>): AgentConnection {
  const online = raw.online;
  const status = String(raw.status ?? raw.connectionStatus ?? (online === true ? "ONLINE" : online === false ? "OFFLINE" : "UNKNOWN"));
  const health = raw.healthStatus ?? raw.health ?? raw.healthy;
  return {
    agentConnectionId: String(raw.agentConnectionId ?? raw.connectionId ?? raw.id ?? ""),
    name: String(raw.name ?? raw.displayName ?? ""),
    description: raw.description as string | null | undefined,
    status,
    healthStatus: typeof health === "boolean" ? (health ? "HEALTHY" : "UNHEALTHY") : health as string | null | undefined,
    lastSeenAt: raw.lastSeenAt as string | null | undefined ?? raw.lastHeartbeatAt as string | null | undefined,
    connectedAt: raw.connectedAt as string | null | undefined,
    createdAt: raw.createdAt as string | null | undefined,
    updatedAt: raw.updatedAt as string | null | undefined,
    tokenHint: raw.tokenHint as string | null | undefined,
  };
}

function agentConnectionSecretFromApi(raw: Record<string, unknown>): AgentConnectionSecret {
  const nested = raw.connection && typeof raw.connection === "object"
    ? raw.connection as Record<string, unknown>
    : raw;
  const connection = agentConnectionFromApi(nested);
  return {
    ...connection,
    token: raw.token as string | null | undefined ?? raw.connectionToken as string | null | undefined
      ?? raw.registrationToken as string | null | undefined,
    connectUrl: raw.connectUrl as string | null | undefined ?? raw.websocketUrl as string | null | undefined,
    connectCommand: raw.connectCommand as string | null | undefined ?? raw.command as string | null | undefined,
  };
}

function responseData(raw: Record<string, unknown>): Record<string, unknown> {
  return raw.data && typeof raw.data === "object" && !Array.isArray(raw.data)
    ? raw.data as Record<string, unknown>
    : raw;
}

function agentCodeConfigFromApi(raw: Record<string, unknown>): AgentCodeConfig {
  const data = responseData(raw);
  return {
    language: String(data.language ?? "python"),
    source: String(data.source ?? ""),
    configured: Boolean(data.configured),
    revision: Number(data.revision ?? 0),
    sourceSha256: data.sourceSha256 as string | null | undefined
      ?? data.source_sha256 as string | null | undefined,
    runtimeState: String(data.runtimeState ?? data.runtime_state ?? "STOPPED"),
    updatedAt: data.updatedAt as string | null | undefined
      ?? data.updated_at as string | null | undefined,
  };
}

function agentTestResultFromApi(raw: Record<string, unknown>): AgentTestResult {
  const data = responseData(raw);
  const output = data.sanitizedOutput && typeof data.sanitizedOutput === "object" && !Array.isArray(data.sanitizedOutput)
    ? data.sanitizedOutput as Record<string, unknown>
    : data.sanitized_output && typeof data.sanitized_output === "object" && !Array.isArray(data.sanitized_output)
      ? data.sanitized_output as Record<string, unknown>
      : null;
  const meta = data.sanitizedMeta && typeof data.sanitizedMeta === "object" && !Array.isArray(data.sanitizedMeta)
    ? data.sanitizedMeta as Record<string, unknown>
    : data.sanitized_meta && typeof data.sanitized_meta === "object" && !Array.isArray(data.sanitized_meta)
      ? data.sanitized_meta as Record<string, unknown>
      : {};
  return {
    testId: String(data.testId ?? data.test_id ?? ""),
    status: String(data.status ?? "UNKNOWN"),
    durationMs: Number(data.durationMs ?? data.duration_ms ?? 0),
    codeRevision: data.codeRevision as number | null | undefined
      ?? data.code_revision as number | null | undefined,
    responseReceived: Boolean(
      data.responseReceived
      ?? data.response_received
      ?? (String(data.status ?? "").toUpperCase() === "SUCCEEDED" || output !== null),
    ),
    sanitizedOutput: output ? {
      type: String(output.type ?? "unknown"),
      preview: String(output.preview ?? ""),
      truncated: Boolean(output.truncated),
    } : null,
    sanitizedMeta: meta,
    redactionCount: Number(data.redactionCount ?? data.redaction_count ?? 0),
    testedAt: String(data.testedAt ?? data.tested_at ?? ""),
    errorCode: data.errorCode as string | null | undefined
      ?? data.error_code as string | null | undefined,
    errorMessage: data.errorMessage as string | null | undefined
      ?? data.error_message as string | null | undefined,
  };
}

function scheduleFromApi(raw: Record<string, unknown>): Schedule {
  const cadenceType = String(raw.cadenceType ?? (raw.cadence as Record<string, unknown> | undefined)?.type ?? "DAILY");
  const cadence = raw.cadence
    ? raw.cadence as Schedule["cadence"]
    : cadenceType === "INTERVAL_MINUTES"
      ? {
          type: "INTERVAL_MINUTES" as const,
          intervalMinutes: Number(raw.intervalMinutes ?? 5),
          anchorAt: String(raw.anchorAt ?? new Date().toISOString()),
        }
      : cadenceType === "INTERVAL_HOURS"
      ? {
          type: "INTERVAL_HOURS" as const,
          intervalHours: Number(raw.intervalHours ?? 1),
          anchorAt: String(raw.anchorAt ?? new Date().toISOString()),
        }
      : { type: "DAILY" as const, time: String(raw.dailyTime ?? "09:00") };
  const versionMode = String(raw.versionMode ?? "AUTO").toUpperCase() === "MANUAL" ? "MANUAL" as const : "AUTO" as const;
  return {
    scheduleId: String(raw.scheduleId ?? raw.id ?? ""),
    name: String(raw.name ?? ""),
    planId: String(raw.planId ?? ""),
    planNameSnapshot: raw.planNameSnapshot as string | null | undefined,
    agentConnectionId: String(raw.agentConnectionId ?? raw.connectionId ?? ""),
    agentConnectionNameSnapshot: raw.agentConnectionNameSnapshot as string | null | undefined
      ?? raw.agentNameSnapshot as string | null | undefined,
    versionMode,
    experimentVersion: raw.experimentVersion as string | null | undefined
      ?? raw.manualExperimentVersion as string | null | undefined,
    experimentParameters: parametersFromApi(raw.experimentParameters),
    recordName: raw.recordName as string | null | undefined,
    cadence,
    timezone: String(raw.timezone ?? "UTC"),
    misfirePolicy: String(raw.misfirePolicy ?? "FIRE_ONCE"),
    enabled: Boolean(raw.enabled),
    revision: Number(raw.revision ?? 1),
    lastScheduledAt: raw.lastScheduledAt as string | null | undefined ?? raw.lastFireAtUtc as string | null | undefined,
    nextScheduledAt: raw.nextScheduledAt as string | null | undefined ?? raw.nextFireAtUtc as string | null | undefined,
    lastErrorMessage: raw.lastErrorMessage as string | null | undefined ?? raw.lastErrorCode as string | null | undefined,
    createdAt: raw.createdAt as string | null | undefined,
    updatedAt: raw.updatedAt as string | null | undefined,
    lastLaunch: raw.lastLaunch as Schedule["lastLaunch"],
  };
}

function launchFromApi(raw: Record<string, unknown>): Launch {
  const traceCorrelation = traceCorrelationFromApi(raw.traceCorrelation ?? raw.trace_correlation);
  const planDashboard = planDashboardFromApi(raw.planDashboard ?? raw.plan_dashboard);
  return {
    launchId: String(raw.launchId ?? ""),
    planId: String(raw.planId ?? ""),
    planNameSnapshot: raw.planNameSnapshot as string | null | undefined,
    submissionState: String(raw.submissionState ?? "UNKNOWN"),
    runnerState: String(raw.runnerState ?? "UNKNOWN"),
    skipReason: raw.skipReason as string | null | undefined,
    triggerSource: String(raw.triggerSource ?? "unknown"),
    requestedAt: String(raw.requestedAt ?? ""),
    acceptedAt: raw.acceptedAt as string | null | undefined,
    scheduledFor: raw.scheduledFor as string | null | undefined,
    submitStartedAt: raw.submitStartedAt as string | null | undefined,
    workerFinishedAt: raw.workerFinishedAt as string | null | undefined,
    recordId: raw.recordId as string | null | undefined,
    scheduleId: raw.scheduleId as string | null | undefined,
    agentLoopUrl: raw.agentLoopUrl as string | null | undefined,
    errorCode: raw.errorCode as string | null | undefined,
    errorMessage: raw.errorMessage as string | null | undefined ?? raw.sanitizedError as string | null | undefined,
    agentConnectionId: raw.agentConnectionId as string | null | undefined,
    agentConnectionNameSnapshot: raw.agentConnectionNameSnapshot as string | null | undefined
      ?? raw.agentNameSnapshot as string | null | undefined,
    versionMode: raw.versionMode as string | null | undefined,
    experimentVersion: raw.experimentVersion as string | null | undefined
      ?? raw.resolvedExperimentVersion as string | null | undefined,
    experimentParameters: parametersFromApi(raw.experimentParameters),
    recordName: raw.recordName as string | null | undefined,
    traceCorrelation,
    planDashboard,
  };
}

function planDashboardFromApi(value: unknown): PlanDashboard | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const raw = value as Record<string, unknown>;
  return {
    dashboardName: String(raw.dashboardName ?? raw.dashboard_name ?? ""),
    displayName: String(raw.displayName ?? raw.display_name ?? ""),
    state: String(raw.state ?? raw.syncState ?? raw.sync_state ?? "PENDING"),
    url: raw.url as string | null | undefined,
    lastSyncedAt: raw.lastSyncedAt as string | null | undefined
      ?? raw.last_synced_at as string | null | undefined,
    lastErrorCode: raw.lastErrorCode as string | null | undefined
      ?? raw.last_error_code as string | null | undefined,
  };
}

function launchResultFromApi(raw: Record<string, unknown>): LaunchResult {
  return launchFromApi(raw);
}

export const api = {
  health: {
    async get() {
      const raw = await request<Record<string, unknown>>("/api/v1/health");
      return {
        status: String(raw.status ?? "UNKNOWN"),
        version: String(raw.version ?? ""),
        gatewayMode: String(raw.gatewayMode ?? raw.gateway_mode ?? ""),
        schedulerEnabled: Boolean(raw.schedulerEnabled ?? raw.scheduler_enabled),
      } satisfies LauncherHealth;
    },
  },

  plans: {
    async list(input: { search?: string; status?: string; page?: number; pageSize?: number } = {}) {
      const page = input.page ?? 1;
      const pageSize = input.pageSize ?? 20;
      const payload = await request<unknown>(
        `/api/v1/plans${queryString({ search: input.search, status: input.status, offset: (page - 1) * pageSize, limit: pageSize })}`,
      );
      return normalizePage<Plan>(payload, page, pageSize);
    },
    get(planId: string) {
      return request<Plan>(`/api/v1/plans/${encodeURIComponent(planId)}`);
    },
    async launch(planId: string, idempotencyKey: string, input: ExperimentExecutionInput) {
      const raw = await request<Record<string, unknown>>(`/api/v1/plans/${encodeURIComponent(planId)}/launch`, {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey },
        body: JSON.stringify(input),
      });
      return launchResultFromApi(raw);
    },
  },

  agentConnections: {
    async list(input: { page?: number; pageSize?: number } = {}) {
      const page = input.page ?? 1;
      const pageSize = input.pageSize ?? 100;
      const payload = await request<unknown>(
        `/api/v1/agent-connections${queryString({ offset: (page - 1) * pageSize, limit: pageSize })}`,
      );
      return normalizePage<AgentConnection>(payload, page, pageSize, agentConnectionFromApi);
    },
    async create(input: CreateAgentConnectionInput) {
      const raw = await request<Record<string, unknown>>("/api/v1/agent-connections", {
        method: "POST",
        body: JSON.stringify(input),
      });
      return agentConnectionSecretFromApi(raw);
    },
    remove(agentConnectionId: string) {
      return request<void>(`/api/v1/agent-connections/${encodeURIComponent(agentConnectionId)}`, { method: "DELETE" });
    },
    async rotateToken(agentConnectionId: string) {
      const raw = await request<Record<string, unknown>>(
        `/api/v1/agent-connections/${encodeURIComponent(agentConnectionId)}/rotate-token`,
        { method: "POST" },
      );
      return agentConnectionSecretFromApi(raw);
    },
    async getLocalCode(agentConnectionId: string) {
      const raw = await request<Record<string, unknown>>(
        `/api/v1/agent-connections/${encodeURIComponent(agentConnectionId)}/local-code`,
      );
      return agentCodeConfigFromApi(raw);
    },
    async saveLocalCode(agentConnectionId: string, input: SaveAgentCodeInput) {
      const raw = await request<Record<string, unknown>>(
        `/api/v1/agent-connections/${encodeURIComponent(agentConnectionId)}/local-code`,
        { method: "PUT", body: JSON.stringify(input) },
      );
      return agentCodeConfigFromApi(raw);
    },
    async test(agentConnectionId: string, input: AgentTestInput) {
      const raw = await request<Record<string, unknown>>(
        `/api/v1/agent-connections/${encodeURIComponent(agentConnectionId)}/test`,
        { method: "POST", body: JSON.stringify(input) },
      );
      return agentTestResultFromApi(raw);
    },
  },

  launches: {
    async list(input: { planId?: string; scheduleId?: string; page?: number; pageSize?: number } = {}) {
      const page = input.page ?? 1;
      const pageSize = input.pageSize ?? 20;
      const payload = await request<unknown>(
        `/api/v1/launches${queryString({ planId: input.planId, scheduleId: input.scheduleId, offset: (page - 1) * pageSize, limit: pageSize })}`,
      );
      return normalizePage<Launch>(payload, page, pageSize, launchFromApi);
    },
    async get(launchId: string) {
      const raw = await request<Record<string, unknown>>(`/api/v1/launches/${encodeURIComponent(launchId)}`);
      return launchFromApi(raw);
    },
  },

  schedules: {
    async list(input: { page?: number; pageSize?: number } = {}) {
      const page = input.page ?? 1;
      const pageSize = input.pageSize ?? 50;
      const payload = await request<unknown>(
        `/api/v1/schedules${queryString({ offset: (page - 1) * pageSize, limit: pageSize })}`,
      );
      return normalizePage<Schedule>(payload, page, pageSize, scheduleFromApi);
    },
    get(scheduleId: string) {
      return request<Record<string, unknown>>(`/api/v1/schedules/${encodeURIComponent(scheduleId)}`).then(scheduleFromApi);
    },
    create(input: CreateScheduleInput) {
      return request<Record<string, unknown>>("/api/v1/schedules", { method: "POST", body: JSON.stringify(input) }).then(scheduleFromApi);
    },
    update(scheduleId: string, input: Partial<CreateScheduleInput>, expectedRevision?: number) {
      return request<Record<string, unknown>>(`/api/v1/schedules/${encodeURIComponent(scheduleId)}${queryString({ expectedRevision })}`, {
        method: "PATCH",
        body: JSON.stringify(input),
      }).then(scheduleFromApi);
    },
    remove(scheduleId: string, expectedRevision?: number) {
      return request<void>(`/api/v1/schedules/${encodeURIComponent(scheduleId)}${queryString({ expectedRevision })}`, { method: "DELETE" });
    },
    enable(scheduleId: string, expectedRevision?: number) {
      return request<Record<string, unknown>>(`/api/v1/schedules/${encodeURIComponent(scheduleId)}/enable${queryString({ expectedRevision })}`, { method: "POST" }).then(scheduleFromApi);
    },
    disable(scheduleId: string, expectedRevision?: number) {
      return request<Record<string, unknown>>(`/api/v1/schedules/${encodeURIComponent(scheduleId)}/disable${queryString({ expectedRevision })}`, { method: "POST" }).then(scheduleFromApi);
    },
    async runNow(scheduleId: string, idempotencyKey: string) {
      const raw = await request<Record<string, unknown>>(`/api/v1/schedules/${encodeURIComponent(scheduleId)}/run-now`, {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey },
      });
      return launchResultFromApi(raw);
    },
  },

  settings: {
    async get() {
      const raw = await request<Record<string, unknown>>("/api/v1/settings/agentloop");
      return {
        configured: Boolean(raw.id || raw.accessKeyIdHint || raw.accessKeyIdMasked),
        displayName: raw.displayName as string | null | undefined,
        region: String(raw.region ?? raw.regionId ?? ""),
        agentSpace: String(raw.agentSpace ?? ""),
        apiEndpoint: raw.apiEndpoint as string | null | undefined,
        consoleBaseUrl: raw.consoleBaseUrl as string | null | undefined,
        defaultTimezone: String(raw.defaultTimezone ?? "UTC"),
        maxConcurrency: Number(raw.maxConcurrency ?? 1),
        accessKeyIdMasked: raw.accessKeyIdMasked as string | null | undefined ?? raw.accessKeyIdHint as string | null | undefined,
        secretConfigured: Boolean(raw.id || raw.accessKeyIdHint || raw.secretConfigured),
        lastVerifiedAt: raw.lastVerifiedAt as string | null | undefined ?? raw.validatedAt as string | null | undefined,
        lastVerificationStatus: raw.lastVerificationStatus as string | null | undefined ?? raw.validationStatus as string | null | undefined,
        lastVerificationMessage: raw.lastVerificationMessage as string | null | undefined,
      } satisfies AgentLoopSettings;
    },
    async update(input: UpdateAgentLoopSettingsInput) {
      await request<Record<string, unknown>>("/api/v1/settings/agentloop", {
        method: "PUT",
        body: JSON.stringify({ ...input, displayName: input.displayName || "Default", regionId: input.region }),
      });
      return this.get();
    },
    async test(input?: Partial<UpdateAgentLoopSettingsInput>) {
      const raw = await request<Record<string, unknown>>("/api/v1/settings/agentloop/test", {
        method: "POST",
        body: JSON.stringify(input || {}),
      });
      return {
        success: Boolean(raw.success ?? raw.ok),
        message: String(raw.message ?? ""),
        verifiedAt: raw.verifiedAt as string | null | undefined ?? raw.checkedAt as string | null | undefined,
        permissions: raw.permissions as string[] | undefined,
      } satisfies ConnectionTestResult;
    },
  },
};

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    const location = error.line
      ? `（第 ${error.line} 行${error.column ? `，第 ${error.column} 列` : ""}）`
      : "";
    const code = error.code !== "UNKNOWN_ERROR" ? `（${error.code}）` : "";
    return `${error.message}${location}${code}`;
  }
  return error instanceof Error ? error.message : "发生未知错误";
}
