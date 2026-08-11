import { useMemo } from "react";
import {
  isAgentConnectionOnline,
  type AgentConnection,
  type ExperimentExecutionInput,
  type ExperimentParameters,
  type VersionMode,
} from "../lib/types";
import { Field, Notice, StatusBadge } from "./UI";

export interface ExperimentConfigDraft {
  agentConnectionId: string;
  versionMode: VersionMode;
  experimentVersion: string;
  recordName: string;
  experimentParametersJson: string;
}

export function createExperimentConfigDraft(
  connections: AgentConnection[],
  initial?: Partial<ExperimentConfigDraft>,
): ExperimentConfigDraft {
  const preferred = connections.find(isAgentConnectionOnline) ?? connections[0];
  return {
    agentConnectionId: initial?.agentConnectionId ?? preferred?.agentConnectionId ?? "",
    versionMode: initial?.versionMode ?? "AUTO",
    experimentVersion: initial?.experimentVersion ?? "",
    recordName: initial?.recordName ?? "",
    experimentParametersJson: initial?.experimentParametersJson ?? "{}",
  };
}

export function parseExperimentParameters(value: string): ExperimentParameters {
  let parsed: unknown;
  try {
    parsed = JSON.parse(value || "{}");
  } catch {
    throw new Error("实验参数必须是合法的 JSON。");
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("实验参数必须是 JSON object，不能是数组、字符串或 null。");
  }
  return parsed as ExperimentParameters;
}

export function executionInputFromDraft(
  draft: ExperimentConfigDraft,
  connections: AgentConnection[],
  options: { requireOnline?: boolean } = {},
): ExperimentExecutionInput {
  const connection = connections.find((candidate) => candidate.agentConnectionId === draft.agentConnectionId);
  if (!connection) throw new Error("请选择一个本地 Agent Connection。");
  if (options.requireOnline && !isAgentConnectionOnline(connection)) {
    throw new Error("人工发起实验前，所选 Agent 必须在线。");
  }
  const version = draft.experimentVersion.trim();
  if (draft.versionMode === "MANUAL" && !version) throw new Error("手动版本模式必须填写实验版本号。");
  if (draft.versionMode === "MANUAL" && !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(version)) {
    throw new Error("版本号必须以字母或数字开头，且只能包含字母、数字、点、下划线和连字符。");
  }
  if (version.length > 128) throw new Error("版本号不能超过 128 个字符。");
  const recordName = draft.recordName.trim();
  if (recordName.length > 256) throw new Error("Record Name 不能超过 256 个字符。");
  return {
    agentConnectionId: draft.agentConnectionId,
    versionMode: draft.versionMode,
    experimentVersion: draft.versionMode === "MANUAL" ? version : null,
    experimentParameters: parseExperimentParameters(draft.experimentParametersJson),
    recordName: recordName || null,
    contextSchemaVersion: 1,
  };
}

interface ExperimentConfigFieldsProps {
  value: ExperimentConfigDraft;
  onChange: (value: ExperimentConfigDraft) => void;
  connections: AgentConnection[];
  planId: string;
  disabled?: boolean;
  requireOnline?: boolean;
  showContextPreview?: boolean;
}

export function ExperimentConfigFields({
  value,
  onChange,
  connections,
  planId,
  disabled,
  requireOnline = false,
  showContextPreview = true,
}: ExperimentConfigFieldsProps) {
  const selected = connections.find((connection) => connection.agentConnectionId === value.agentConnectionId);
  const versionPreview = value.versionMode === "MANUAL"
    ? value.experimentVersion.trim() || "请填写版本号"
    : "AUTO（每次 Launch 生成一次并固定）";
  const contextPreview = useMemo(() => [
    '<experiment_context schema_version="1">',
    `  <experiment_plan_id>${planId}</experiment_plan_id>`,
    "  <experiment_run_id>{AgentLoop 创建后写入}</experiment_run_id>",
    `  <experiment_version>${versionPreview}</experiment_version>`,
    "  <dataset_item_id>{当前题目 ID}</dataset_item_id>",
    "  <local_launch_id>{本地 Launch ID}</local_launch_id>",
    "</experiment_context>",
  ].join("\n"), [planId, versionPreview]);

  function set<K extends keyof ExperimentConfigDraft>(key: K, next: ExperimentConfigDraft[K]) {
    onChange({ ...value, [key]: next });
  }

  return (
    <div className="experiment-config-fields">
      <Field
        label="本地 Agent Connection"
        required
        hint={requireOnline ? "人工实验只能选择当前在线的 Agent。" : "定时任务触发时会检查该 Agent 是否在线。"}
      >
        <select
          value={value.agentConnectionId}
          onChange={(event) => set("agentConnectionId", event.target.value)}
          disabled={disabled || connections.length === 0}
        >
          <option value="">{connections.length ? "请选择 Agent" : "请先创建 Agent Connection"}</option>
          {connections.map((connection) => {
            const unavailable = requireOnline && !isAgentConnectionOnline(connection);
            return (
              <option key={connection.agentConnectionId} value={connection.agentConnectionId} disabled={unavailable}>
                {connection.name || connection.agentConnectionId} · {connection.status}{connection.healthStatus ? ` / ${connection.healthStatus}` : ""}
              </option>
            );
          })}
        </select>
      </Field>
      {selected && (
        <div className="selected-agent-summary">
          <span><StatusBadge value={selected.status} compact /></span>
          <strong>{selected.name}</strong>
          <code>{selected.agentConnectionId}</code>
        </div>
      )}

      <Field label="实验版本策略" required>
        <div className="segmented">
          <button
            type="button"
            className={value.versionMode === "AUTO" ? "active" : ""}
            onClick={() => set("versionMode", "AUTO")}
            disabled={disabled}
          >自动生成版本
          </button>
          <button
            type="button"
            className={value.versionMode === "MANUAL" ? "active" : ""}
            onClick={() => set("versionMode", "MANUAL")}
            disabled={disabled}
          >人工指定版本
          </button>
        </div>
      </Field>
      {value.versionMode === "MANUAL" && (
        <Field label="实验版本号" required hint="该值会作为本次 Launch 的不可变快照，并注入每道题的 context。">
          <input
            value={value.experimentVersion}
            onChange={(event) => set("experimentVersion", event.target.value)}
            placeholder="例如 1.2.3 或 build-20260808"
            disabled={disabled}
          />
        </Field>
      )}

      <Field label="Record Name（可选）" hint="用于标识本次人工 Launch，或作为定时任务每次 Launch 的 Record 名称。">
        <input
          value={value.recordName}
          onChange={(event) => set("recordName", event.target.value)}
          placeholder="例如 game-agent-regression"
          maxLength={256}
          disabled={disabled}
        />
      </Field>

      <Field label="Experiment Parameters (JSON object)" hint="参数随 Launch/Schedule 保存，并由本地 Connector 传给 Agent。">
        <textarea
          className="json-editor"
          value={value.experimentParametersJson}
          onChange={(event) => set("experimentParametersJson", event.target.value)}
          spellCheck={false}
          rows={5}
          disabled={disabled}
          placeholder={'{\n  "temperature": 0.2\n}'}
        />
      </Field>

      {showContextPreview && (
        <Notice title="最终 context 注入说明">
          原始题目保持不变；平台会将下列 XML 元信息追加到每道题的 context。AUTO 版本在一次 Launch 内固定，所有 Dataset item 使用同一值。
          <pre className="context-preview">{contextPreview}</pre>
        </Notice>
      )}
    </div>
  );
}
