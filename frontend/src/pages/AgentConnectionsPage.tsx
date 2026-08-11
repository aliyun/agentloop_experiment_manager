import { useCallback, useEffect, useState, type FormEvent } from "react";
import { AgentConnectionWorkbench } from "../components/AgentConnectionWorkbench";
import { CopyIcon, EditIcon, KeyIcon, PlusIcon, RefreshIcon, TrashIcon } from "../components/Icons";
import { EmptyState, ErrorState, Field, LoadingRows, Modal, Notice, PageHeader, StatusBadge, Toast } from "../components/UI";
import { api, errorMessage } from "../lib/api";
import { formatDateTime, truncateMiddle } from "../lib/format";
import { isAgentConnectionOnline, type AgentConnection, type AgentConnectionSecret } from "../lib/types";

type ToastState = { tone: "success" | "danger" | "warning"; title: string; message: string };
type AgentSetupMode = "managed" | "external";

async function copyText(value: string): Promise<void> {
  if (!navigator.clipboard?.writeText) throw new Error("当前浏览器不支持安全剪贴板 API，请手动复制。");
  await navigator.clipboard.writeText(value);
}

export function AgentConnectionsPage() {
  const [connections, setConnections] = useState<AgentConnection[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [setupMode, setSetupMode] = useState<AgentSetupMode>("managed");
  const [busy, setBusy] = useState(false);
  const [secret, setSecret] = useState<AgentConnectionSecret | null>(null);
  const [rotating, setRotating] = useState<AgentConnection | null>(null);
  const [deleting, setDeleting] = useState<AgentConnection | null>(null);
  const [workbenchConnection, setWorkbenchConnection] = useState<AgentConnection | null>(null);
  const [toast, setToast] = useState<ToastState | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await api.agentConnections.list({ pageSize: 100 });
      setConnections(result.items);
      setWorkbenchConnection((current) => current
        ? result.items.find((item) => item.agentConnectionId === current.agentConnectionId) ?? current
        : null);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  function openCreate() {
    setName("");
    setDescription("");
    setSetupMode("managed");
    setCreateOpen(true);
  }

  async function create(event: FormEvent) {
    event.preventDefault();
    if (!name.trim()) {
      setToast({ tone: "danger", title: "缺少名称", message: "请为 Agent Connection 填写一个可识别的名称。" });
      return;
    }
    setBusy(true);
    try {
      const created = await api.agentConnections.create({ name: name.trim(), description: description.trim() || null });
      setCreateOpen(false);
      await load();
      if (setupMode === "managed") {
        setWorkbenchConnection(created);
        setToast({ tone: "success", title: "Agent 已创建", message: "请选择代码模板，修改后点击“保存代码并验证 Agent”。" });
      } else {
        setSecret(created);
      }
    } catch (caught) {
      setToast({ tone: "danger", title: "创建失败", message: errorMessage(caught) });
    } finally {
      setBusy(false);
    }
  }

  async function rotateToken() {
    if (!rotating) return;
    setBusy(true);
    try {
      const rotated = await api.agentConnections.rotateToken(rotating.agentConnectionId);
      setRotating(null);
      setSecret(rotated);
      await load();
    } catch (caught) {
      setToast({ tone: "danger", title: "Token 轮换失败", message: errorMessage(caught) });
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!deleting) return;
    setBusy(true);
    try {
      await api.agentConnections.remove(deleting.agentConnectionId);
      setDeleting(null);
      setToast({ tone: "success", title: "连接已删除", message: "该 Agent 无法再接收新的实验任务；历史 Launch 未被删除。" });
      await load();
    } catch (caught) {
      setToast({ tone: "danger", title: "删除失败", message: errorMessage(caught) });
    } finally {
      setBusy(false);
    }
  }

  async function copy(label: string, value?: string | null) {
    if (!value) return;
    try {
      await copyText(value);
      setToast({ tone: "success", title: `${label} 已复制`, message: "敏感内容只保留在当前页面和系统剪贴板中。" });
    } catch (caught) {
      setToast({ tone: "warning", title: "复制失败", message: errorMessage(caught) });
    }
  }

  return (
    <div className="page">
      <PageHeader
        eyebrow="Local Agent"
        title="Settings"
        description="配置实验如何调用客户 Agent。默认在 Web 选择模板并修改单题执行代码。"
        actions={(
          <>
            <button className="button secondary" type="button" onClick={() => void load()} disabled={loading}><RefreshIcon /> 刷新</button>
            <button className="button primary" type="button" onClick={openCreate}><PlusIcon /> 配置新 Agent</button>
          </>
        )}
      />

      <Notice title="大多数用户只需配置单题代码">
        平台会管理内部 Connection 和 Token。只有 Agent 作为独立进程部署、需要主动连接本平台时，才使用“外部接入 / Token”能力。
      </Notice>

      <section className="panel table-panel">
        <div className="section-heading">
          <div><h2>已注册 Agent</h2><p>{connections.length} 个连接 · 状态来自本地连接管理器</p></div>
          <span className="result-count">{loading ? "正在读取…" : `${connections.filter(isAgentConnectionOnline).length} 个在线`}</span>
        </div>
        {error ? <ErrorState message={error} onRetry={() => void load()} /> : (
          <div className="table-scroll">
            <table>
              <thead><tr><th>Agent</th><th>连接状态</th><th>健康状态</th><th>最近心跳</th><th>创建时间</th><th className="align-right">操作</th></tr></thead>
              <tbody>
                {loading ? <LoadingRows columns={6} /> : connections.map((connection) => (
                  <tr key={connection.agentConnectionId}>
                    <td className="primary-cell">
                      <strong>{connection.name || "未命名 Agent"}</strong>
                      {connection.description && <span>{connection.description}</span>}
                      <code title={connection.agentConnectionId}>{truncateMiddle(connection.agentConnectionId)}</code>
                    </td>
                    <td><StatusBadge value={connection.status} compact /></td>
                    <td><StatusBadge value={connection.healthStatus || "UNKNOWN"} compact /></td>
                    <td><div className="stack-tight"><span>{formatDateTime(connection.lastSeenAt)}</span>{connection.connectedAt && <small>上线 {formatDateTime(connection.connectedAt)}</small>}</div></td>
                    <td>{formatDateTime(connection.createdAt)}</td>
                    <td><div className="row-actions">
                      <button className="button primary small" type="button" onClick={() => setWorkbenchConnection(connection)} disabled={busy}><EditIcon /> 配置并验证代码</button>
                      <button className="button secondary small" type="button" onClick={() => setRotating(connection)} disabled={busy}><KeyIcon /> 外部接入 / Token</button>
                      <button className="icon-button danger" type="button" title="删除连接" onClick={() => setDeleting(connection)} disabled={busy}><TrashIcon /></button>
                    </div></td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!loading && connections.length === 0 && (
              <EmptyState title="还没有配置本地 Agent" action={<button className="button primary" type="button" onClick={openCreate}><PlusIcon /> 配置新 Agent</button>}>
                创建后选择一个代码模板，修改 Agent 地址并完成测试，才能从 Plan 发起真实实验。
              </EmptyState>
            )}
          </div>
        )}
      </section>

      <Modal open={createOpen} title="配置新 Agent" description="先选择执行方式。推荐由平台托管单题执行代码。" onClose={() => !busy && setCreateOpen(false)} footer={<></>}>
        <form className="form-stack" onSubmit={create}>
          <Field label="执行方式" required>
            <div className="segmented">
              <button type="button" className={setupMode === "managed" ? "active" : ""} onClick={() => setSetupMode("managed")} disabled={busy}>平台托管代码（推荐）</button>
              <button type="button" className={setupMode === "external" ? "active" : ""} onClick={() => setSetupMode("external")} disabled={busy}>外部 Agent 主动连接</button>
            </div>
          </Field>
          <Notice title={setupMode === "managed" ? "创建后直接编写并验证代码" : "适用于独立部署的 Agent 进程"}>
            {setupMode === "managed"
              ? "平台内部管理 Token；下一步直接选择代码模板，不需要复制连接凭据。"
              : "创建后只展示一次 Token 和 WebSocket 地址，外部 Agent 使用它们主动连接本平台。"}
          </Notice>
          <Field label="连接名称" required><input value={name} onChange={(event) => setName(event.target.value)} placeholder="例如 game-agent-staging" autoFocus disabled={busy} /></Field>
          <Field label="说明"><textarea value={description} onChange={(event) => setDescription(event.target.value)} rows={3} placeholder="部署环境、负责人或用途" disabled={busy} /></Field>
          <div className="inline-form-actions"><button className="button secondary" type="button" onClick={() => setCreateOpen(false)} disabled={busy}>取消</button><button className="button primary" type="submit" disabled={busy}>{busy ? "创建中…" : setupMode === "managed" ? "创建并配置代码" : "创建并获取 Token"}</button></div>
        </form>
      </Modal>

      <Modal
        open={Boolean(secret)}
        title="外部 Agent 接入信息"
        description="这是 Token 与参考 Agent 命令唯一一次可见的页面。"
        onClose={() => setSecret(null)}
        footer={<button className="button primary" type="button" onClick={() => setSecret(null)}>我已安全保存</button>}
        width="wide"
      >
        {secret && <div className="secret-delivery">
          <Notice tone="warning" title="关闭后不可再次读取">
            请勿将 Token 放入代码仓库、聊天消息或日志。若遗失，请执行 Token 轮换；旧 Token 会立即失效。
          </Notice>
          <div className="secret-row"><span>Connection ID</span><code>{secret.agentConnectionId}</code><button className="button secondary small" type="button" onClick={() => void copy("Connection ID", secret.agentConnectionId)}><CopyIcon /> 复制</button></div>
          {secret.token && <div className="secret-row"><span>一次性 Token</span><code className="secret-value">{secret.token}</code><button className="button secondary small" type="button" onClick={() => void copy("Token", secret.token)}><CopyIcon /> 复制</button></div>}
          {secret.connectUrl && <div className="secret-row"><span>连接地址</span><code>{secret.connectUrl}</code><button className="button secondary small" type="button" onClick={() => void copy("连接地址", secret.connectUrl)}><CopyIcon /> 复制</button></div>}
          {secret.connectCommand ? (
            <div className="command-block"><span>参考 Agent 连通性测试</span><pre>{secret.connectCommand}</pre><button className="button secondary small" type="button" onClick={() => void copy("参考 Agent 命令", secret.connectCommand)}><CopyIcon /> 复制命令</button><small>运行后会在终端安全提示中粘贴 Token，命令行和 shell history 不包含 Token。</small></div>
          ) : <Notice tone="warning" title="服务端未返回接入命令">请检查后端 Connector 配置。前端不会自行拼接可能错误的程序路径或参数。</Notice>}
        </div>}
      </Modal>

      <Modal
        open={Boolean(rotating)}
        title="轮换 Agent Token？"
        description="旧 Token 会失效，在线 Agent 可能需要使用新 Token 重新连接。"
        onClose={() => !busy && setRotating(null)}
        footer={<><button className="button secondary" type="button" onClick={() => setRotating(null)} disabled={busy}>取消</button><button className="button danger" type="button" onClick={() => void rotateToken()} disabled={busy}><KeyIcon /> {busy ? "轮换中…" : "确认轮换"}</button></>}
      >
        <div className="confirm-summary"><dl><div><dt>Agent</dt><dd>{rotating?.name}</dd></div><div><dt>Connection ID</dt><dd><code>{rotating?.agentConnectionId}</code></dd></div></dl></div>
      </Modal>

      <Modal
        open={Boolean(deleting)}
        title="删除 Agent Connection？"
        description="删除后该连接不能再接收实验任务。"
        onClose={() => !busy && setDeleting(null)}
        footer={<><button className="button secondary" type="button" onClick={() => setDeleting(null)} disabled={busy}>取消</button><button className="button danger" type="button" onClick={() => void remove()} disabled={busy}><TrashIcon /> {busy ? "删除中…" : "确认删除"}</button></>}
      >
        <div className="confirm-summary"><dl><div><dt>Agent</dt><dd>{deleting?.name}</dd></div><div><dt>Connection ID</dt><dd><code>{deleting?.agentConnectionId}</code></dd></div></dl><Notice tone="warning" title="历史 Launch 保留">删除连接不会删除已经创建的 Launch 或 AgentLoop Experiment Record。</Notice></div>
      </Modal>

      {workbenchConnection && (
        <AgentConnectionWorkbench
          connection={workbenchConnection}
          onClose={() => setWorkbenchConnection(null)}
          onConnectionChanged={load}
          onNotify={(tone, title, message) => setToast({ tone, title, message })}
        />
      )}

      {toast && <Toast tone={toast.tone} title={toast.title} onClose={() => setToast(null)}>{toast.message}</Toast>}
    </div>
  );
}
