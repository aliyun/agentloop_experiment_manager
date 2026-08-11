import { useEffect, useMemo, useRef, useState } from "react";
import { EditIcon, PlayIcon, RefreshIcon } from "./Icons";
import { ErrorState, Field, Modal, Notice, StatusBadge } from "./UI";
import { api, errorMessage } from "../lib/api";
import { AGENT_CODE_TEMPLATES, agentCodeTemplate, matchingAgentCodeTemplate } from "../lib/agentCodeTemplates";
import { formatDateTime, truncateMiddle } from "../lib/format";
import { isAgentConnectionOnline, type AgentCodeConfig, type AgentConnection, type AgentTestInput, type AgentTestResult } from "../lib/types";

type WorkbenchOperation = "save" | "save-and-test" | "test" | null;

interface AgentConnectionWorkbenchProps {
  connection: AgentConnection;
  onClose: () => void;
  onConnectionChanged: () => void | Promise<void>;
  onNotify: (tone: "success" | "danger" | "warning", title: string, message: string) => void;
}

function parseTestInput(message: string, parametersText: string, timeoutText: string): AgentTestInput {
  if (!message.trim()) throw new Error("请先填写要发送给 Agent 的测试消息。");

  let parameters: unknown;
  try {
    parameters = JSON.parse(parametersText || "{}");
  } catch {
    throw new Error("测试参数必须是合法的 JSON 对象。");
  }
  if (!parameters || typeof parameters !== "object" || Array.isArray(parameters)) {
    throw new Error("测试参数必须是 JSON 对象，不能是数组或标量。");
  }

  const timeoutSeconds = Number(timeoutText);
  if (!Number.isFinite(timeoutSeconds) || timeoutSeconds < 1 || timeoutSeconds > 120) {
    throw new Error("测试超时必须是 1–120 秒之间的数字。");
  }
  return { message: message.trim(), parameters: parameters as Record<string, unknown>, timeoutSeconds };
}

export function AgentConnectionWorkbench({ connection, onClose, onConnectionChanged, onNotify }: AgentConnectionWorkbenchProps) {
  const [config, setConfig] = useState<AgentCodeConfig | null>(null);
  const [draft, setDraft] = useState("");
  const [templateId, setTemplateId] = useState("session-http");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [operationError, setOperationError] = useState<string | null>(null);
  const [operation, setOperation] = useState<WorkbenchOperation>(null);
  const [message, setMessage] = useState("我的位置在哪里");
  const [parametersText, setParametersText] = useState("{}");
  const [timeoutText, setTimeoutText] = useState("30");
  const [testResult, setTestResult] = useState<AgentTestResult | null>(null);
  const testResultRef = useRef<HTMLElement | null>(null);

  const sourceBytes = useMemo(() => new TextEncoder().encode(draft).length, [draft]);
  const dirty = config !== null && draft !== config.source;
  const busy = operation !== null;
  const runtimeCanReceive = config?.runtimeState.toUpperCase() === "RUNNING" || isAgentConnectionOnline(connection);
  const canTest = Boolean(config?.configured && !dirty && runtimeCanReceive && !busy);

  async function load() {
    setLoading(true);
    setLoadError(null);
    setOperationError(null);
    try {
      const next = await api.agentConnections.getLocalCode(connection.agentConnectionId);
      setConfig(next);
      const nextSource = next.configured ? next.source : AGENT_CODE_TEMPLATES[0].source;
      setDraft(nextSource);
      setTemplateId(matchingAgentCodeTemplate(nextSource));
      setTestResult(null);
    } catch (caught) {
      setLoadError(errorMessage(caught));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    let active = true;
    setLoading(true);
    setLoadError(null);
    setOperationError(null);
    setConfig(null);
    setDraft("");
    setTestResult(null);
    void api.agentConnections.getLocalCode(connection.agentConnectionId).then((next) => {
      if (!active) return;
      setConfig(next);
      const nextSource = next.configured ? next.source : AGENT_CODE_TEMPLATES[0].source;
      setDraft(nextSource);
      setTemplateId(matchingAgentCodeTemplate(nextSource));
    }).catch((caught) => {
      if (active) setLoadError(errorMessage(caught));
    }).finally(() => {
      if (active) setLoading(false);
    });
    return () => { active = false; };
  }, [connection.agentConnectionId]);

  useEffect(() => {
    if (!testResult) return;
    const frame = window.requestAnimationFrame(() => {
      testResultRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [testResult]);

  function requestClose() {
    if (busy) return;
    if (dirty && !window.confirm("代码草稿尚未保存，确定关闭并放弃修改吗？")) return;
    onClose();
  }

  function validateSource() {
    if (!config) throw new Error("代码配置尚未加载完成。");
    if (!draft.trim()) throw new Error("单题执行代码不能为空。");
    if (sourceBytes > 64 * 1024) throw new Error("单题执行代码不能超过 64 KiB。");
  }

  function applyTemplate(nextTemplateId: string) {
    if (nextTemplateId === "custom") return;
    const template = agentCodeTemplate(nextTemplateId);
    if (!template) return;
    if (dirty && !window.confirm(`切换为“${template.name}”会覆盖当前草稿，是否继续？`)) return;
    setTemplateId(nextTemplateId);
    setDraft(template.source);
    setOperationError(null);
    setTestResult(null);
  }

  async function saveOnly() {
    setOperationError(null);
    setTestResult(null);
    try {
      validateSource();
      setOperation("save");
      const saved = await api.agentConnections.saveLocalCode(connection.agentConnectionId, {
        source: draft,
        expectedRevision: config!.revision,
        activate: false,
      });
      setConfig(saved);
      setDraft(saved.source);
      await onConnectionChanged();
      onNotify("success", "代码已保存", `已保存 revision ${saved.revision}；本次未请求启动或重启本地 Agent。`);
    } catch (caught) {
      const messageText = errorMessage(caught);
      setOperationError(messageText);
      onNotify("danger", "代码保存失败", messageText);
    } finally {
      setOperation(null);
    }
  }

  async function saveAndTest() {
    setOperationError(null);
    setTestResult(null);
    try {
      validateSource();
      const testInput = parseTestInput(message, parametersText, timeoutText);
      setOperation("save-and-test");
      const saved = await api.agentConnections.saveLocalCode(connection.agentConnectionId, {
        source: draft,
        expectedRevision: config!.revision,
        activate: true,
      });
      setConfig(saved);
      setDraft(saved.source);
      await onConnectionChanged();

      const result = await api.agentConnections.test(connection.agentConnectionId, testInput);
      setTestResult(result);
      const succeeded = result.status.toUpperCase() === "SUCCEEDED";
      onNotify(
        succeeded ? "success" : "warning",
        succeeded
          ? "已连接并收到 Agent 返回"
          : result.responseReceived
            ? "已连接，但 Agent 执行失败"
            : "未收到 Agent 返回",
        `测试 ${result.testId || "（无 ID）"} · ${result.status} · ${result.durationMs} ms`,
      );
    } catch (caught) {
      const messageText = errorMessage(caught);
      setOperationError(messageText);
      onNotify("danger", "启动或测试失败", messageText);
    } finally {
      setOperation(null);
    }
  }

  async function sendTest() {
    setOperationError(null);
    setTestResult(null);
    try {
      if (dirty) throw new Error("当前代码有未保存修改，请先保存后再测试。");
      const testInput = parseTestInput(message, parametersText, timeoutText);
      setOperation("test");
      const result = await api.agentConnections.test(connection.agentConnectionId, testInput);
      setTestResult(result);
      const succeeded = result.status.toUpperCase() === "SUCCEEDED";
      onNotify(
        succeeded ? "success" : "warning",
        succeeded
          ? "已连接并收到 Agent 返回"
          : result.responseReceived
            ? "已连接，但 Agent 执行失败"
            : "未收到 Agent 返回",
        `测试 ${result.testId || "（无 ID）"} · ${result.status} · ${result.durationMs} ms`,
      );
    } catch (caught) {
      const messageText = errorMessage(caught);
      setOperationError(messageText);
      onNotify("danger", "测试失败", messageText);
    } finally {
      setOperation(null);
    }
  }

  return (
    <Modal
      open
      title={`配置单题 Solution Provider · ${connection.name || "未命名 Agent"}`}
      description="选择一个模板并改成客户 Agent 的调用方式；保存后平台会立即启动并发送一道测试题。"
      onClose={requestClose}
      width="wide"
      footer={(
        <>
          <button className="button secondary" type="button" onClick={requestClose} disabled={busy}>关闭</button>
          <button className="button secondary" type="button" onClick={() => void saveOnly()} disabled={loading || busy || !dirty || sourceBytes > 64 * 1024}>
            <EditIcon /> {operation === "save" ? "保存中…" : "仅保存，不启动"}
          </button>
          <button className="button primary" type="button" onClick={() => void saveAndTest()} disabled={loading || busy || !config || sourceBytes > 64 * 1024}>
            <PlayIcon /> {operation === "save-and-test" ? "正在保存并验证…" : "保存代码并验证 Agent"}
          </button>
        </>
      )}
    >
      {loading ? (
        <div className="agent-workbench-loading"><span />正在读取加密保存的代码配置…</div>
      ) : loadError ? (
        <ErrorState message={loadError} onRetry={() => void load()} />
      ) : config && (
        <div className="agent-workbench">
          <Notice title="这是完成本地 Agent 配置的核心步骤">
            选择模板、修改 Agent 地址或 SDK 调用，然后点击底部“保存代码并验证 Agent”。一次操作会完成保存、启动和单题测试；验证成功后定时实验才能执行。
          </Notice>

          <div className="agent-code-metadata">
            <div><span>语言</span><strong>{config.language}</strong></div>
            <div><span>Revision</span><strong>{config.revision}</strong></div>
            <div><span>运行状态</span><StatusBadge value={config.runtimeState} compact /></div>
            <div><span>更新时间</span><strong>{formatDateTime(config.updatedAt)}</strong></div>
            <div className="agent-code-sha"><span>Source SHA-256</span><code title={config.sourceSha256 || undefined}>{config.sourceSha256 ? truncateMiddle(config.sourceSha256, 18) : "—"}</code></div>
          </div>

          <Field label="选择代码模板" hint="切换模板会替换编辑器中的当前草稿。">
            <select value={templateId} onChange={(event) => applyTemplate(event.target.value)} disabled={busy}>
              {AGENT_CODE_TEMPLATES.map((template) => <option key={template.id} value={template.id}>{template.name}</option>)}
              {templateId === "custom" && <option value="custom">自定义代码</option>}
            </select>
          </Field>
          <Notice title={agentCodeTemplate(templateId)?.name || "自定义代码"}>
            {agentCodeTemplate(templateId)?.description || "当前代码已在模板基础上自定义。"}
          </Notice>

          <Field label="单题执行代码（Python）" required hint="只需实现 solution_http(task)；Plan、Dataset 和实验上报由外层平台负责。">
            <textarea
              className="agent-code-editor"
              value={draft}
              onChange={(event) => { setDraft(event.target.value); setTemplateId(matchingAgentCodeTemplate(event.target.value)); }}
              spellCheck={false}
              rows={18}
              disabled={busy}
              aria-describedby="agent-code-size"
            />
          </Field>
          <div id="agent-code-size" className={`agent-code-size${sourceBytes > 64 * 1024 ? " danger" : ""}`}>
            <span>{dirty ? "有未保存修改" : "已与服务端 revision 同步"}</span>
            <span>{sourceBytes.toLocaleString()} / 65,536 bytes</span>
          </div>

          <section className="agent-test-section">
            <div className="agent-test-heading">
              <div><h3>单题试跑</h3><p>平台构造一个带 local-test 实验上下文的 Task，只执行一题并校验 SolutionOutput。</p></div>
              <StatusBadge value={connection.status} compact />
            </div>
            <Notice title="外层实验不会在这里执行">
              单题试跑不会创建云端 Experiment Run、读取正式 Dataset 或上传评估结果；正式实验仍从 Plans 发起。
            </Notice>
            <Field label="测试题目" required>
              <textarea value={message} onChange={(event) => setMessage(event.target.value)} rows={3} disabled={busy} />
            </Field>
            <div className="agent-test-grid">
              <Field label="参数（JSON 对象）">
                <textarea className="json-editor" value={parametersText} onChange={(event) => setParametersText(event.target.value)} rows={4} spellCheck={false} disabled={busy} />
              </Field>
              <Field label="超时（秒）" hint="允许 1–120 秒。">
                <input type="number" min={1} max={120} step={1} value={timeoutText} onChange={(event) => setTimeoutText(event.target.value)} disabled={busy} />
              </Field>
            </div>
            <div className="agent-test-actions">
              {!dirty && config.configured && !runtimeCanReceive && <small>Solution Provider 尚未运行；请点击底部“保存代码并验证 Agent”。</small>}
              {dirty && <small>草稿未保存，试跑按钮已禁用，避免测试旧 revision。</small>}
              <button className="button secondary" type="button" onClick={() => void sendTest()} disabled={!canTest}>
                <PlayIcon /> {operation === "test" ? "试跑中…" : "再次单题试跑"}
              </button>
            </div>
          </section>

          {operationError && <p className="agent-operation-error" role="alert">{operationError}</p>}

          {testResult && (
            <section ref={testResultRef} className={`agent-test-result ${testResult.status.toLowerCase()}`} aria-live="polite">
              <div className="agent-test-result-heading">
                <div>
                  <strong>
                    {testResult.status.toUpperCase() === "SUCCEEDED"
                      ? "连接已验证，已收到 Agent 返回"
                      : testResult.responseReceived
                        ? "已连接，但 Agent 执行失败"
                        : "未收到 Agent 响应"}
                  </strong>
                  <code>{testResult.testId || "无 testId"}</code>
                </div>
                <StatusBadge value={testResult.status} compact />
              </div>
              {testResult.errorMessage && (
                <p className="agent-operation-error">
                  {testResult.errorMessage}{testResult.errorCode ? `（${testResult.errorCode}）` : ""}
                </p>
              )}
              {testResult.sanitizedOutput ? (
                <div className="agent-test-output-label">
                  <span>Agent 返回内容（已脱敏）{testResult.sanitizedOutput.truncated ? "（已截断）" : ""}</span>
                  <pre>{testResult.sanitizedOutput.preview || "（Agent 返回了空字符串）"}</pre>
                </div>
              ) : (
                <Notice tone="warning" title="没有可展示的 Agent 输出">
                  {testResult.responseReceived
                    ? "平台收到了 Agent 的错误响应，但没有业务输出。请查看上方失败原因。"
                    : "平台没有收到 Agent 响应。请检查单题执行代码中的 Agent 地址、服务进程和网络连通性。"}
                </Notice>
              )}
              <dl>
                <div><dt>收到响应</dt><dd>{testResult.responseReceived ? "是" : "否"}</dd></div>
                <div><dt>耗时</dt><dd>{testResult.durationMs} ms</dd></div>
                <div><dt>代码 revision</dt><dd>{testResult.codeRevision ?? config.revision}</dd></div>
                <div><dt>输出类型</dt><dd>{testResult.sanitizedOutput?.type || "—"}</dd></div>
                <div><dt>Trace ID</dt><dd>{String(testResult.sanitizedMeta.traceId || "—")}</dd></div>
                <div><dt>脱敏项</dt><dd>{testResult.redactionCount}</dd></div>
                <div><dt>测试时间</dt><dd>{formatDateTime(testResult.testedAt)}</dd></div>
              </dl>
              {Object.keys(testResult.sanitizedMeta).length > 0 && (
                <div className="agent-test-output-label"><span>安全元信息</span><pre>{JSON.stringify(testResult.sanitizedMeta, null, 2)}</pre></div>
              )}
            </section>
          )}
        </div>
      )}
    </Modal>
  );
}
