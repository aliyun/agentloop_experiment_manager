import { useMemo, useState, type FormEvent } from "react";
import type { AgentConnection, CreateScheduleInput, Schedule } from "../lib/types";
import {
  createExperimentConfigDraft,
  executionInputFromDraft,
  ExperimentConfigFields,
  type ExperimentConfigDraft,
} from "./ExperimentConfigFields";
import { normalizeIanaTimezone, utcInstantToZonedLocalInput, zonedLocalDateTimeToUtc } from "../lib/timezone";
import { Field } from "./UI";

interface ScheduleFormProps {
  planId: string;
  planName?: string | null;
  connections: AgentConnection[];
  initial?: Schedule | null;
  defaultTimezone?: string;
  busy?: boolean;
  onSubmit: (input: CreateScheduleInput) => Promise<void> | void;
  onCancel: () => void;
}

function initialExperimentConfig(initial: Schedule | null | undefined, connections: AgentConnection[]): ExperimentConfigDraft {
  return createExperimentConfigDraft(connections, initial ? {
    agentConnectionId: initial.agentConnectionId,
    versionMode: initial.versionMode,
    experimentVersion: initial.experimentVersion || "",
    recordName: initial.recordName || "",
    experimentParametersJson: JSON.stringify(initial.experimentParameters || {}, null, 2),
  } : undefined);
}

export function ScheduleForm({ planId, planName, connections, initial, defaultTimezone, busy, onSubmit, onCancel }: ScheduleFormProps) {
  const initialType = initial?.cadence.type === "DAILY" ? "DAILY" : "INTERVAL_MINUTES";
  const initialTimezone = initial?.timezone || defaultTimezone || Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai";
  const [name, setName] = useState(initial?.name || `${planName || planId} regression`);
  const [cadenceType, setCadenceType] = useState<"DAILY" | "INTERVAL_MINUTES">(initialType);
  const [dailyTime, setDailyTime] = useState(initial?.cadence.type === "DAILY" ? initial.cadence.time : "09:00");
  const [intervalMinutes, setIntervalMinutes] = useState(
    initial?.cadence.type === "INTERVAL_MINUTES"
      ? initial.cadence.intervalMinutes
      : initial?.cadence.type === "INTERVAL_HOURS"
        ? initial.cadence.intervalHours * 60
        : 5,
  );
  const [anchorAt, setAnchorAt] = useState(() => {
    const value = initial?.cadence.type === "INTERVAL_HOURS" || initial?.cadence.type === "INTERVAL_MINUTES"
      ? initial.cadence.anchorAt
      : new Date(Date.now() + 5 * 60 * 1000);
    try {
      return utcInstantToZonedLocalInput(value, initialTimezone);
    } catch {
      return new Date(value).toISOString().slice(0, 16);
    }
  });
  const [timezone, setTimezone] = useState(initialTimezone);
  const [misfirePolicy, setMisfirePolicy] = useState<"FIRE_ONCE" | "SKIP">(initial?.misfirePolicy === "SKIP" ? "SKIP" : "FIRE_ONCE");
  const [enabled, setEnabled] = useState(initial?.enabled ?? true);
  const [experimentConfig, setExperimentConfig] = useState<ExperimentConfigDraft>(() => initialExperimentConfig(initial, connections));
  const [error, setError] = useState<string | null>(null);

  const availableConnections = useMemo(() => {
    if (!initial?.agentConnectionId || connections.some((item) => item.agentConnectionId === initial.agentConnectionId)) return connections;
    return [{
      agentConnectionId: initial.agentConnectionId,
      name: initial.agentConnectionNameSnapshot || "已删除的 Agent",
      status: "OFFLINE",
      healthStatus: "MISSING",
    }, ...connections];
  }, [connections, initial]);

  const summary = useMemo(() => cadenceType === "DAILY"
    ? `每天 ${dailyTime}（${timezone}）`
    : `每 ${intervalMinutes} 分钟，从 ${anchorAt.replace("T", " ")} 起（${timezone}）`,
  [cadenceType, dailyTime, intervalMinutes, anchorAt, timezone]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!name.trim() || !timezone.trim()) {
      setError("请填写任务名称和 IANA 时区。");
      return;
    }
    if (cadenceType === "INTERVAL_MINUTES" && (!Number.isInteger(intervalMinutes) || intervalMinutes < 5)) {
      setError("间隔分钟数必须是大于等于 5 的整数。");
      return;
    }
    let normalizedTimezone: string;
    try {
      normalizedTimezone = normalizeIanaTimezone(timezone);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "IANA 时区无效。");
      return;
    }
    let execution;
    try {
      execution = executionInputFromDraft(experimentConfig, availableConnections);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "实验配置不合法。");
      return;
    }
    let cadence: CreateScheduleInput["cadence"];
    try {
      cadence = cadenceType === "DAILY"
        ? { type: "DAILY", time: dailyTime }
        : { type: "INTERVAL_MINUTES", intervalMinutes, anchorAt: zonedLocalDateTimeToUtc(anchorAt, normalizedTimezone) };
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "首次锚点无效。");
      return;
    }
    setError(null);
    await onSubmit({
      name: name.trim(),
      planId,
      cadence,
      timezone: normalizedTimezone,
      misfirePolicy,
      enabled,
      agentConnectionId: execution.agentConnectionId,
      versionMode: execution.versionMode,
      experimentVersion: execution.experimentVersion,
      experimentParameters: execution.experimentParameters,
      recordName: execution.recordName,
      contextSchemaVersion: 1,
    });
  }

  return (
    <form className="form-stack" id="schedule-form" onSubmit={submit}>
      <div className="readonly-context"><span>AgentLoop Plan</span><strong>{planName || "未命名 Plan"}</strong><code>{planId}</code></div>
      <Field label="任务名称" required><input value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：daily-regression" autoFocus disabled={busy} /></Field>

      <div className="form-section-title"><span>触发规则</span></div>
      <Field label="运行频率" required>
        <div className="segmented">
          <button type="button" className={cadenceType === "DAILY" ? "active" : ""} onClick={() => setCadenceType("DAILY")} disabled={busy}>每天固定时间</button>
          <button type="button" className={cadenceType === "INTERVAL_MINUTES" ? "active" : ""} onClick={() => setCadenceType("INTERVAL_MINUTES")} disabled={busy}>每 N 分钟</button>
        </div>
      </Field>
      {cadenceType === "DAILY" ? (
        <Field label="每天执行时间" required><input type="time" value={dailyTime} onChange={(event) => setDailyTime(event.target.value)} required disabled={busy} /></Field>
      ) : (
        <div className="form-grid two">
          <Field label="间隔分钟" hint="最小 5 分钟" required><input type="number" min="5" step="1" value={intervalMinutes} onChange={(event) => setIntervalMinutes(Number(event.target.value))} disabled={busy} /></Field>
          <Field label="首次锚点" required><input type="datetime-local" value={anchorAt} onChange={(event) => setAnchorAt(event.target.value)} required disabled={busy} /></Field>
        </div>
      )}
      <div className="form-grid two">
        <Field label="IANA 时区" hint="例如 Asia/Shanghai" required><input value={timezone} onChange={(event) => setTimezone(event.target.value)} placeholder="Asia/Shanghai" disabled={busy} /></Field>
        <Field label="错过触发策略" required>
          <select value={misfirePolicy} onChange={(event) => setMisfirePolicy(event.target.value as "FIRE_ONCE" | "SKIP")} disabled={busy}>
            <option value="FIRE_ONCE">恢复后补跑一次</option><option value="SKIP">跳过错过的 Slot</option>
          </select>
        </Field>
      </div>

      <div className="form-section-title"><span>每次实验的 Agent、版本与参数</span></div>
      <ExperimentConfigFields value={experimentConfig} onChange={setExperimentConfig} connections={availableConnections} planId={planId} disabled={busy} requireOnline={false} />

      <label className="switch-row">
        <span><strong>{initial ? "保持启用" : "创建后启用"}</strong><small>启用代表授权平台按此规则持续发起实验</small></span>
        <input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} disabled={busy} /><i />
      </label>
      <div className="schedule-summary"><span>规则预览</span><strong>{summary}</strong></div>
      {error && <p className="inline-error">{error}</p>}
      <div className="inline-form-actions"><button type="button" className="button secondary" onClick={onCancel} disabled={busy}>取消</button><button type="submit" className="button primary" disabled={busy || availableConnections.length === 0}>{busy ? "保存中…" : initial ? "保存修改" : "创建定时任务"}</button></div>
    </form>
  );
}
