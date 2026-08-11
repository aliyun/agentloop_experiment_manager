import { useCallback, useEffect, useState } from "react";
import { api, ApiError, errorMessage } from "../lib/api";
import { createIdempotencyKey, formatDateTime, safeExternalUrl, truncateMiddle } from "../lib/format";
import { shouldCompleteStableRequest, shouldDiscardStableRequestAfterApiError } from "../lib/stableRequestKeys";
import { isAgentConnectionOnline, type AgentConnection, type CreateScheduleInput, type LaunchResult, type PageResult, type Plan } from "../lib/types";
import { ExternalIcon, PlayIcon, PlusIcon, RefreshIcon, ScheduleIcon, SearchIcon } from "../components/Icons";
import { EmptyState, ErrorState, LoadingRows, Modal, Notice, PageHeader, Pagination, StatusBadge, Toast } from "../components/UI";
import { ScheduleForm } from "../components/ScheduleForm";
import {
  createExperimentConfigDraft,
  executionInputFromDraft,
  ExperimentConfigFields,
  type ExperimentConfigDraft,
} from "../components/ExperimentConfigFields";
import { shouldAutoOpenHistory } from "../lib/launchNavigation";

const emptyPage: PageResult<Plan> = { items: [], total: 0, page: 1, pageSize: 20 };

export function PlansPage({ onOpenHistory }: { onOpenHistory: (launchId: string) => void }) {
  const [plans, setPlans] = useState<PageResult<Plan>>(emptyPage);
  const [connections, setConnections] = useState<AgentConnection[]>([]);
  const [defaultTimezone, setDefaultTimezone] = useState("UTC");
  const [page, setPage] = useState(1);
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [launchPlan, setLaunchPlan] = useState<Plan | null>(null);
  const [schedulePlan, setSchedulePlan] = useState<Plan | null>(null);
  const [launching, setLaunching] = useState(false);
  const [savingSchedule, setSavingSchedule] = useState(false);
  const [launchKey, setLaunchKey] = useState("");
  const [launchConfig, setLaunchConfig] = useState<ExperimentConfigDraft>(() => createExperimentConfigDraft([]));
  const [launchError, setLaunchError] = useState<string | null>(null);
  const [launchSubmissionUnknown, setLaunchSubmissionUnknown] = useState(false);
  const [launchResult, setLaunchResult] = useState<LaunchResult | null>(null);
  const [toast, setToast] = useState<{ tone: "success" | "danger" | "warning"; title: string; message: string } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [planPage, connectionPage, settings] = await Promise.all([
        api.plans.list({ search, status, page, pageSize: 20 }),
        api.agentConnections.list({ pageSize: 100 }),
        api.settings.get(),
      ]);
      setPlans(planPage);
      setConnections(connectionPage.items);
      setDefaultTimezone(settings.defaultTimezone);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setLoading(false);
    }
  }, [page, search, status]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (!launchResult || !shouldAutoOpenHistory(launchResult.submissionState)) return;
    const timer = window.setTimeout(() => onOpenHistory(launchResult.launchId), 1800);
    return () => window.clearTimeout(timer);
  }, [launchResult, onOpenHistory]);

  function finishAcceptedLaunch() {
    if (launchResult && shouldAutoOpenHistory(launchResult.submissionState)) {
      onOpenHistory(launchResult.launchId);
      return;
    }
    setLaunchPlan(null);
  }

  function openLaunch(plan: Plan) {
    setLaunchPlan(plan);
    setLaunchResult(null);
    setLaunchError(null);
    setLaunchSubmissionUnknown(false);
    setLaunchConfig(createExperimentConfigDraft(connections));
    setLaunchKey(createIdempotencyKey());
  }

  async function confirmLaunch() {
    if (!launchPlan) return;
    setLaunchError(null);
    let input;
    try {
      input = executionInputFromDraft(launchConfig, connections, { requireOnline: true });
    } catch (caught) {
      setLaunchError(errorMessage(caught));
      return;
    }
    setLaunching(true);
    try {
      const result = await api.plans.launch(launchPlan.planId, launchKey, input);
      if (shouldCompleteStableRequest(result.submissionState)) {
        setLaunchSubmissionUnknown(false);
        setLaunchResult(result);
      } else {
        setLaunchSubmissionUnknown(true);
        setLaunchResult(null);
      }
    } catch (caught) {
      const definitiveFailure = caught instanceof ApiError && shouldDiscardStableRequestAfterApiError(caught);
      if (definitiveFailure) {
        setLaunchKey(createIdempotencyKey());
        setLaunchSubmissionUnknown(false);
      } else {
        setLaunchSubmissionUnknown(true);
      }
      setToast({
        tone: definitiveFailure ? "danger" : "warning",
        title: definitiveFailure ? "启动请求失败" : "启动响应无法判定",
        message: definitiveFailure
          ? `${errorMessage(caught)}。再次发起将使用新的请求标识。`
          : `${errorMessage(caught)}。平台已保留原请求标识和配置，请使用“安全重试”继续确认。`,
      });
    } finally {
      setLaunching(false);
    }
  }

  async function createSchedule(input: CreateScheduleInput) {
    setSavingSchedule(true);
    try {
      await api.schedules.create(input);
      setSchedulePlan(null);
      setToast({ tone: "success", title: "定时任务已创建", message: "平台将按设定 Slot 和版本策略发起实验。" });
    } catch (caught) {
      setToast({ tone: "danger", title: "创建失败", message: errorMessage(caught) });
    } finally {
      setSavingSchedule(false);
    }
  }

  const launchDashboardUrl = safeExternalUrl(launchResult?.planDashboard?.url);

  return (
    <div className="page">
      <PageHeader
        eyebrow="AgentLoop control plane"
        title="Experiment Plans"
        description="读取 AgentSpace 中已有的 Plan，并选择本地 Agent、版本策略和实验参数发起真实离线实验。"
        actions={<button className="button secondary" type="button" onClick={() => void load()} disabled={loading}><RefreshIcon /> 刷新</button>}
      />

      <Notice title="Plan 只能在 AgentLoop 创建">
        本地平台不会新建、编辑或删除 Plan、Dataset 与 Evaluator。发起前需同时具备可运行的 OFFLINE Plan 和在线 Agent Connection。
      </Notice>

      <section className="panel table-panel">
        <div className="toolbar">
          <form className="search-box" onSubmit={(event) => { event.preventDefault(); setPage(1); setSearch(searchInput.trim()); }}>
            <SearchIcon />
            <input value={searchInput} onChange={(event) => setSearchInput(event.target.value)} placeholder="按 Plan 名称搜索" aria-label="按 Plan 名称搜索" />
            <button type="submit">搜索</button>
          </form>
          <select value={status} onChange={(event) => { setPage(1); setStatus(event.target.value); }} aria-label="按云端返回状态过滤">
            <option value="">全部状态</option>
            <option value="pending">Pending</option>
            <option value="stopped">Stopped</option>
          </select>
          <span className="result-count">{loading ? "正在同步…" : `${plans.total} 个 Plan · ${connections.filter(isAgentConnectionOnline).length} 个 Agent 在线`}</span>
        </div>

        {error ? <ErrorState message={error} onRetry={() => void load()} /> : (
          <div className="table-scroll">
            <table>
              <thead><tr><th>Plan</th><th>类型 / 云端状态（仅展示）</th><th>Dataset</th><th>配置规模</th><th>更新时间</th><th className="align-right">操作</th></tr></thead>
              <tbody>
                {loading ? <LoadingRows columns={6} /> : plans.items.map((plan) => (
                  <tr key={plan.planId}>
                    <td className="primary-cell"><strong>{plan.planName || "未命名 Plan"}</strong><code title={plan.planId}>{truncateMiddle(plan.planId)}</code></td>
                    <td><div className="stack-tight"><span className="type-label">{plan.experimentType || "unknown"}</span><StatusBadge value={plan.status} compact /></div></td>
                    <td><div className="stack-tight"><span>{plan.datasetName || "—"}</span>{plan.datasetId && <code title={plan.datasetId}>{truncateMiddle(plan.datasetId, 8, 5)}</code>}</div></td>
                    <td><span className="metric-pair"><b>{plan.experimentCount ?? 0}</b> experiments</span><span className="metric-pair"><b>{plan.evaluatorCount ?? 0}</b> evaluators</span></td>
                    <td>{formatDateTime(plan.updatedAt)}</td>
                    <td>
                      <div className="row-actions">
                        {safeExternalUrl(plan.consoleUrl) && <a className="icon-button" href={safeExternalUrl(plan.consoleUrl)!} target="_blank" rel="noopener noreferrer" title="在 AgentLoop 查看"><ExternalIcon /></a>}
                        <button className="button secondary small" type="button" disabled={!plan.canLaunchLocally} title={plan.cannotLaunchReason || undefined} onClick={() => setSchedulePlan(plan)}><ScheduleIcon /> 定时</button>
                        <button className="button primary small" type="button" disabled={!plan.canLaunchLocally} title={plan.cannotLaunchReason || undefined} onClick={() => openLaunch(plan)}><PlayIcon /> 发起</button>
                      </div>
                      {!plan.canLaunchLocally && <small className="disabled-reason">{plan.cannotLaunchReason || "当前 Plan 不可在本地运行"}</small>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!loading && plans.items.length === 0 && <EmptyState title="尚未找到实验计划" action={<button className="button secondary" type="button" onClick={() => void load()}><RefreshIcon /> 刷新 Plan</button>}>请先前往 AgentLoop 创建 Experiment Plan，然后返回此页面刷新。</EmptyState>}
          </div>
        )}
        <Pagination page={plans.page || page} pageSize={plans.pageSize || 20} total={plans.total} onChange={setPage} />
      </section>

      <Modal
        open={Boolean(launchPlan)}
        title={launchResult ? "实验 Launch 已创建" : "发起离线实验"}
        description={launchResult ? "本地账本已保存本次启动信息。" : "执行将在客户网络调用所选 Agent，并由 Runner 将结果写入 AgentLoop。"}
        onClose={() => { if (!launching) finishAcceptedLaunch(); }}
        width="wide"
        footer={launchResult ? (
          <>
            <button className="button secondary" type="button" onClick={finishAcceptedLaunch}>{shouldAutoOpenHistory(launchResult.submissionState) ? "立即进入 Launch History" : "关闭"}</button>
            {safeExternalUrl(launchResult.agentLoopUrl) && <a className="button secondary" href={safeExternalUrl(launchResult.agentLoopUrl)!} target="_blank" rel="noopener noreferrer"><ExternalIcon /> 查看实验记录</a>}
            {launchDashboardUrl && <a className="button primary" href={launchDashboardUrl} target="_blank" rel="noopener noreferrer"><ExternalIcon /> 打开 Plan 大盘</a>}
          </>
        ) : (
          <><button className="button secondary" type="button" onClick={() => setLaunchPlan(null)} disabled={launching}>取消</button><button className="button primary" type="button" onClick={() => void confirmLaunch()} disabled={launching || connections.length === 0}><PlayIcon /> {launching ? "正在提交…" : launchSubmissionUnknown ? "使用原请求标识安全重试" : "确认发起"}</button></>
        )}
      >
        {launchResult ? (
          <div className="launch-accepted">
            <div className="success-orbit"><span><PlusIcon /></span></div>
            <StatusBadge value={launchResult.submissionState} />
            <dl>
              <div><dt>Record ID</dt><dd><code>{launchResult.recordId || "等待 Runner 创建"}</code></dd></div>
              <div><dt>Launch ID</dt><dd><code>{launchResult.launchId}</code></dd></div>
              <div><dt>Agent</dt><dd>{launchResult.agentConnectionNameSnapshot || launchResult.agentConnectionId || "—"}</dd></div>
              <div><dt>最终版本</dt><dd><code>{launchResult.experimentVersion || "等待生成"}</code></dd></div>
              <div><dt>Plan 大盘</dt><dd><StatusBadge value={launchResult.planDashboard?.state || "PENDING"} compact /> {launchResult.planDashboard?.displayName || "等待初始化"}</dd></div>
              <div><dt>大盘同步</dt><dd>{formatDateTime(launchResult.planDashboard?.lastSyncedAt)}{launchResult.planDashboard?.lastErrorCode && <> · <code>{launchResult.planDashboard.lastErrorCode}</code></>}</dd></div>
              <div><dt>请求时间</dt><dd>{formatDateTime(launchResult.requestedAt)}</dd></div>
            </dl>
            <Notice tone="warning" title="Launch 已创建不等于实验完成">{shouldAutoOpenHistory(launchResult.submissionState) ? "即将自动进入 Launch History；页面会持续刷新 Trace 关联状态。当前 Plan 的所有运行会复用同一个大盘。" : "请到 Launch History 查看本地执行状态；当前 Plan 的所有运行会复用同一个大盘。"}</Notice>
          </div>
        ) : launchPlan && (
          <div className="form-stack">
            <div className="readonly-context"><span>AgentLoop Plan</span><strong>{launchPlan.planName}</strong><code>{launchPlan.planId}</code></div>
            <Notice title="一个 Experiment Plan 对应一个大盘">
              发起时会创建或更新该 Plan 的固定大盘；以后人工或定时触发同一 Plan 时继续复用，不会为每次 Launch 新建大盘。
            </Notice>
            <ExperimentConfigFields value={launchConfig} onChange={setLaunchConfig} connections={connections} planId={launchPlan.planId} disabled={launching || launchSubmissionUnknown} requireOnline />
            {launchSubmissionUnknown && (
              <Notice tone="warning" title="提交状态未知，尚未确认实验是否创建">
                平台会保留本次 Idempotency-Key 和原始配置。请使用下方“安全重试”按钮继续确认；如需修改配置，请取消后重新发起一次新的逻辑请求。
              </Notice>
            )}
            {launchError && <p className="inline-error">{launchError}</p>}
          </div>
        )}
      </Modal>

      <Modal open={Boolean(schedulePlan)} title="创建定时任务" description="定时规则、Agent 绑定和版本策略保存在本地；Plan 定义仍由 AgentLoop 管理。" onClose={() => !savingSchedule && setSchedulePlan(null)} footer={<></>} width="wide">
        {schedulePlan && <ScheduleForm planId={schedulePlan.planId} planName={schedulePlan.planName} connections={connections} defaultTimezone={defaultTimezone} busy={savingSchedule} onCancel={() => setSchedulePlan(null)} onSubmit={createSchedule} />}
      </Modal>

      {toast && <Toast tone={toast.tone} title={toast.title} onClose={() => setToast(null)}>{toast.message}</Toast>}
    </div>
  );
}
