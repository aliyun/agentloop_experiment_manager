export function formatDateTime(value?: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

export function formatCadence(cadence: { type: string; intervalMinutes?: number; intervalHours?: number; time?: string }): string {
  if (cadence.type === "INTERVAL_MINUTES") {
    return `每 ${cadence.intervalMinutes ?? "—"} 分钟`;
  }
  if (cadence.type === "INTERVAL_HOURS") {
    return `每 ${cadence.intervalHours ?? "—"} 小时`;
  }
  if (cadence.type === "DAILY") {
    return `每天 ${cadence.time ?? "—"}`;
  }
  return cadence.type;
}

export function formatProgress(value?: number | null): string {
  if (value === null || value === undefined) return "—";
  const normalized = value > 1 ? value : value * 100;
  return `${Math.max(0, Math.min(normalized, 100)).toFixed(0)}%`;
}

export function truncateMiddle(value: string, head = 10, tail = 7): string {
  if (value.length <= head + tail + 3) return value;
  return `${value.slice(0, head)}…${value.slice(-tail)}`;
}

export function triggerLabel(source?: string | null): string {
  const labels: Record<string, string> = {
    manual: "手动",
    schedule: "定时",
    schedule_run_now: "定时任务 · 立即执行",
    external: "外部",
  };
  return source ? labels[source] ?? source : "—";
}

export function skipReasonDescription(reason?: string | null): string {
  const descriptions: Record<string, string> = {
    AGENT_OFFLINE: "绑定的本地 Agent 离线",
    OVERLAP: "同一定时任务已有 Launch 正在执行",
    MISFIRE: "已错过允许的计划触发窗口",
  };
  return reason ? descriptions[reason.toUpperCase()] ?? "本次 Launch 已被跳过" : "";
}

export function createIdempotencyKey(): string {
  return globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function safeExternalUrl(value?: string | null): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:" ? url.toString() : null;
  } catch {
    return null;
  }
}
