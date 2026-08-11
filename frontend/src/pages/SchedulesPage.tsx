import { useCallback, useEffect, useRef, useState } from "react";
import { EditIcon, ExternalIcon, PlayIcon, PlusIcon, RefreshIcon, TrashIcon } from "../components/Icons";
import { ScheduleForm } from "../components/ScheduleForm";
import { EmptyState, ErrorState, LoadingRows, Modal, Notice, PageHeader, StatusBadge, Toast } from "../components/UI";
import { api, ApiError, errorMessage } from "../lib/api";
import { createIdempotencyKey, formatCadence, formatDateTime, safeExternalUrl, skipReasonDescription, truncateMiddle } from "../lib/format";
import { shouldCompleteStableRequest, shouldDiscardStableRequestAfterApiError, StableRequestKeys } from "../lib/stableRequestKeys";
import type { AgentConnection, CreateScheduleInput, LaunchResult, Plan, Schedule } from "../lib/types";

const PLAN_PICKER_PAGE_SIZE = 100;
const PLAN_PICKER_MAX_PAGES = 50;

async function loadAllRunnablePlans(): Promise<Plan[]> {
  const plans = new Map<string, Plan>();
  for (let page = 1; page <= PLAN_PICKER_MAX_PAGES; page += 1) {
    const result = await api.plans.list({ page, pageSize: PLAN_PICKER_PAGE_SIZE });
    const sizeBefore = plans.size;
    result.items.forEach((plan) => plans.set(plan.planId, plan));
    if (result.total > PLAN_PICKER_PAGE_SIZE * PLAN_PICKER_MAX_PAGES) {
      throw new Error(`Plan 数量超过安全上限 ${PLAN_PICKER_PAGE_SIZE * PLAN_PICKER_MAX_PAGES}，请在 AgentLoop 缩小范围后重试。`);
    }
    if (result.items.length < PLAN_PICKER_PAGE_SIZE || plans.size >= result.total) {
      return [...plans.values()].filter((plan) => plan.canLaunchLocally);
    }
    if (plans.size === sizeBefore) {
      throw new Error("Plan 分页没有继续前进，已停止加载以避免无限请求。");
    }
  }
  throw new Error(`Plan 分页达到安全上限 ${PLAN_PICKER_MAX_PAGES} 页，未能确认已加载完整列表。`);
}

export function SchedulesPage() {
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<Schedule | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [createPlanId, setCreatePlanId] = useState("");
  const [availablePlans, setAvailablePlans] = useState<Plan[]>([]);
  const [connections, setConnections] = useState<AgentConnection[]>([]);
  const [plansLoading, setPlansLoading] = useState(false);
  const [deleting, setDeleting] = useState<Schedule | null>(null);
  const [saving, setSaving] = useState(false);
  const [actionId, setActionId] = useState<string | null>(null);
  const [runResult, setRunResult] = useState<LaunchResult | null>(null);
  const [schedulerEnabled, setSchedulerEnabled] = useState<boolean | null>(null);
  const [defaultTimezone, setDefaultTimezone] = useState("UTC");
  const [recentLaunchWarning, setRecentLaunchWarning] = useState<string | null>(null);
  const runNowKeys = useRef(new StableRequestKeys(createIdempotencyKey));
  const [toast, setToast] = useState<{ tone: "success" | "danger" | "warning"; title: string; message: string } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setSchedulerEnabled(null);
    setRecentLaunchWarning(null);
    try {
      const [schedulePage, connectionPage, health, settings] = await Promise.all([
        api.schedules.list({ pageSize: 100 }),
        api.agentConnections.list({ pageSize: 100 }),
        api.health.get(),
        api.settings.get(),
      ]);
      const latestLaunches = await Promise.allSettled(
        schedulePage.items.map(async (schedule) => {
          const launchPage = await api.launches.list({ scheduleId: schedule.scheduleId, pageSize: 1 });
          return { ...schedule, lastLaunch: launchPage.items[0] ?? null };
        }),
      );
      const failedLookups = latestLaunches.filter((result) => result.status === "rejected").length;
      setSchedules(latestLaunches.map((result, index) => (
        result.status === "fulfilled"
          ? result.value
          : { ...schedulePage.items[index], lastLaunch: null }
      )));
      setConnections(connectionPage.items);
      setSchedulerEnabled(health.schedulerEnabled);
      setDefaultTimezone(settings.defaultTimezone);
      if (failedLookups > 0) {
        setRecentLaunchWarning(`${failedLookups} 个 Schedule 的最近 Launch 未能读取；任务配置仍可正常管理，请刷新重试。`);
      }
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  async function updateSchedule(input: CreateScheduleInput) {
    if (!editing) return;
    setSaving(true);
    try {
      const { planId: _planId, contextSchemaVersion: _contextSchemaVersion, ...editableFields } = input;
      const { enabled: _enabled, ...scheduleFields } = editableFields;
      const updated = await api.schedules.update(editing.scheduleId, scheduleFields, editing.revision);
      if (input.enabled !== editing.enabled) {
        if (input.enabled) await api.schedules.enable(editing.scheduleId, updated.revision);
        else await api.schedules.disable(editing.scheduleId, updated.revision);
      }
      setEditing(null);
      setToast({ tone: "success", title: "定时任务已更新", message: "修改仅影响未来的时间 Slot。" });
      await load();
    } catch (caught) {
      setToast({ tone: "danger", title: "保存失败", message: errorMessage(caught) });
    } finally {
      setSaving(false);
    }
  }

  async function openCreate() {
    setCreateOpen(true);
    setCreatePlanId("");
    setAvailablePlans([]);
    setPlansLoading(true);
    try {
      const [runnable, connectionPage] = await Promise.all([
        loadAllRunnablePlans(),
        api.agentConnections.list({ pageSize: 100 }),
      ]);
      setConnections(connectionPage.items);
      setAvailablePlans(runnable);
      if (runnable.length === 1) setCreatePlanId(runnable[0].planId);
    } catch (caught) {
      setToast({ tone: "danger", title: "读取 Plan 失败", message: errorMessage(caught) });
    } finally {
      setPlansLoading(false);
    }
  }

  async function createSchedule(input: CreateScheduleInput) {
    setSaving(true);
    try {
      await api.schedules.create(input);
      setCreateOpen(false);
      setToast({
        tone: "success",
        title: "定时任务已创建",
        message: schedulerEnabled === false
          ? "任务已保存；Scheduler 当前未启用，暂不会自动触发。"
          : "平台将按未来时间 Slot 发起实验。",
      });
      await load();
    } catch (caught) {
      setToast({ tone: "danger", title: "创建失败", message: errorMessage(caught) });
    } finally {
      setSaving(false);
    }
  }

  async function toggle(schedule: Schedule) {
    setActionId(schedule.scheduleId);
    try {
      if (schedule.enabled) await api.schedules.disable(schedule.scheduleId, schedule.revision);
      else await api.schedules.enable(schedule.scheduleId, schedule.revision);
      await load();
    } catch (caught) {
      setToast({ tone: "danger", title: schedule.enabled ? "暂停失败" : "启用失败", message: errorMessage(caught) });
    } finally {
      setActionId(null);
    }
  }

  async function runNow(schedule: Schedule) {
    setActionId(schedule.scheduleId);
    const idempotencyKey = runNowKeys.current.keyFor(schedule.scheduleId);
    try {
      const result = await api.schedules.runNow(schedule.scheduleId, idempotencyKey);
      if (shouldCompleteStableRequest(result.submissionState)) {
        runNowKeys.current.complete(schedule.scheduleId, idempotencyKey);
        setRunResult(result);
      } else {
        setRunResult(null);
        setToast({
          tone: "warning",
          title: "提交状态未知",
          message: "服务端尚未确认本次实验是否已创建。再次点击“立即执行”会复用同一请求标识进行安全重试，不会主动创建新的逻辑请求。",
        });
      }
      await load();
    } catch (caught) {
      const definitiveFailure = caught instanceof ApiError && shouldDiscardStableRequestAfterApiError(caught);
      if (definitiveFailure) runNowKeys.current.discard(schedule.scheduleId);
      setToast({
        tone: "danger",
        title: "立即执行失败",
        message: definitiveFailure
          ? `${errorMessage(caught)}。服务端已明确拒绝或终止本次请求，再次执行会创建新的逻辑尝试。`
          : `${errorMessage(caught)}。响应结果无法判定，重试会复用同一请求标识以避免重复创建实验。`,
      });
    } finally {
      setActionId(null);
    }
  }

  async function confirmDelete() {
    if (!deleting) return;
    setActionId(deleting.scheduleId);
    try {
      await api.schedules.remove(deleting.scheduleId, deleting.revision);
      runNowKeys.current.discard(deleting.scheduleId);
      setDeleting(null);
      setToast({ tone: "success", title: "定时任务已删除", message: "历史 Launch 与 AgentLoop Record 均未删除。" });
      await load();
    } catch (caught) {
      setToast({ tone: "danger", title: "删除失败", message: errorMessage(caught) });
    } finally {
      setActionId(null);
    }
  }

  const runDashboardUrl = safeExternalUrl(runResult?.planDashboard?.url);

  return (
    <div className="page">
      <PageHeader
        eyebrow="Local scheduler"
        title="Schedules"
        description="管理客户侧周期触发规则、Agent 绑定、版本策略与实验参数。每次触发仍会重新读取 AgentLoop Plan。"
        actions={<><button className="button secondary" type="button" onClick={() => void load()} disabled={loading}><RefreshIcon /> 刷新</button><button className="button primary" type="button" onClick={() => void openCreate()}><PlusIcon /> 新建定时任务</button></>}
      />

      {schedulerEnabled === false && (
        <Notice tone="warning" title="Scheduler 当前未启用">
          Schedule 可以保存和启用，但不会按时间自动触发。开启客户侧 Scheduler 并重启本地服务后才会周期运行；“立即执行”仍可使用。
        </Notice>
      )}

      {recentLaunchWarning && <Notice tone="warning" title="最近 Launch 数据不完整">{recentLaunchWarning}</Notice>}

      <Notice title="定时任务只负责发起实验">
        启用 Schedule 表示授权本平台在未来时间 Slot 使用绑定的 Agent 和版本策略发起实验；暂停或删除不会取消已开始的 Launch。
      </Notice>

      <section className="panel table-panel">
        <div className="section-heading">
          <div><h2>本地定时任务</h2><p>{schedules.length} 个任务 · 单机 Scheduler</p></div>
          <span className="muted-help"><PlusIcon /> Schedule 始终绑定不可变的 Plan ID</span>
        </div>
        {error ? <ErrorState message={error} onRetry={() => void load()} /> : (
          <div className="table-scroll">
            <table>
              <thead><tr><th>任务 / Plan</th><th>Agent / 版本</th><th>频率</th><th>状态</th><th>上次 / 下次触发</th><th>最近 Launch</th><th className="align-right">操作</th></tr></thead>
              <tbody>
                {loading ? <LoadingRows columns={7} /> : schedules.map((schedule) => (
                  <tr key={schedule.scheduleId}>
                    <td className="primary-cell"><strong>{schedule.name}</strong><span>{schedule.planNameSnapshot || "未命名 Plan"}</span><code title={schedule.planId}>{truncateMiddle(schedule.planId)}</code></td>
                    <td><div className="stack-tight"><strong>{schedule.agentConnectionNameSnapshot || connections.find((item) => item.agentConnectionId === schedule.agentConnectionId)?.name || "未命名 Agent"}</strong><code title={schedule.agentConnectionId}>{truncateMiddle(schedule.agentConnectionId, 7, 5)}</code><small>{schedule.versionMode === "AUTO" ? "AUTO · 每次生成" : `MANUAL · ${schedule.experimentVersion || "—"}`}</small></div></td>
                    <td><div className="stack-tight"><strong>{formatCadence(schedule.cadence)}</strong><small>{schedule.timezone}</small><small>{schedule.misfirePolicy === "FIRE_ONCE" ? "错过后补跑一次" : "错过后跳过"}</small></div></td>
                    <td><StatusBadge value={schedule.enabled ? "ENABLED" : "PAUSED"} compact /></td>
                    <td><dl className="mini-dl"><div><dt>上次</dt><dd>{formatDateTime(schedule.lastScheduledAt)}</dd></div><div><dt>下次</dt><dd>{formatDateTime(schedule.nextScheduledAt)}</dd></div></dl></td>
                    <td>
                      <RecentLaunch schedule={schedule} />
                    </td>
                    <td><div className="row-actions">
                      <button className="icon-button" type="button" title="编辑" onClick={() => setEditing(schedule)} disabled={actionId === schedule.scheduleId}><EditIcon /></button>
                      <button className="button secondary small" type="button" onClick={() => void toggle(schedule)} disabled={actionId === schedule.scheduleId}>{schedule.enabled ? "暂停" : "启用"}</button>
                      <button className="button primary small" type="button" onClick={() => void runNow(schedule)} disabled={actionId === schedule.scheduleId}><PlayIcon /> 立即执行</button>
                      <button className="icon-button danger" type="button" title="删除" onClick={() => setDeleting(schedule)} disabled={actionId === schedule.scheduleId}><TrashIcon /></button>
                    </div></td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!loading && schedules.length === 0 && <EmptyState title="还没有定时任务">前往 Plans 页面，选择一个可运行的 OFFLINE Plan 创建定时规则。</EmptyState>}
          </div>
        )}
      </section>

      <Modal open={createOpen} title="创建定时任务" description="从 AgentLoop 当前可见且可运行的 OFFLINE Plan 中选择。" onClose={() => !saving && setCreateOpen(false)} footer={<></>} width="wide">
        <div className="form-stack">
          <label className="field"><span>AgentLoop Plan <b>*</b></span>
            <select value={createPlanId} onChange={(event) => setCreatePlanId(event.target.value)} disabled={plansLoading || saving}>
              <option value="">{plansLoading ? "正在读取 Plan…" : "请选择 Plan"}</option>
              {availablePlans.map((plan) => <option value={plan.planId} key={plan.planId}>{plan.planName} · {plan.planId}</option>)}
            </select>
            {!plansLoading && availablePlans.length === 0 && <small>没有可运行的 Plan。请先在 AgentLoop 创建并配置 OFFLINE Plan。</small>}
          </label>
          {createPlanId && <ScheduleForm key={createPlanId} planId={createPlanId} planName={availablePlans.find((plan) => plan.planId === createPlanId)?.planName} connections={connections} defaultTimezone={defaultTimezone} busy={saving} onCancel={() => setCreateOpen(false)} onSubmit={createSchedule} />}
        </div>
      </Modal>

      <Modal open={Boolean(editing)} title="编辑定时任务" description="保存后 revision 将增加，修改只影响未来 Slot。" onClose={() => !saving && setEditing(null)} footer={<></>} width="wide">
        {editing && <ScheduleForm initial={editing} planId={editing.planId} planName={editing.planNameSnapshot} connections={connections} busy={saving} onCancel={() => setEditing(null)} onSubmit={updateSchedule} />}
      </Modal>

      <Modal
        open={Boolean(deleting)}
        title="删除定时任务？"
        description="删除后不会再创建未来的时间 Slot。"
        onClose={() => !actionId && setDeleting(null)}
        footer={<><button className="button secondary" type="button" onClick={() => setDeleting(null)} disabled={Boolean(actionId)}>取消</button><button className="button danger" type="button" onClick={() => void confirmDelete()} disabled={Boolean(actionId)}><TrashIcon /> {actionId ? "删除中…" : "确认删除"}</button></>}
      >
        <div className="confirm-summary"><dl><div><dt>任务名称</dt><dd>{deleting?.name}</dd></div><div><dt>Plan ID</dt><dd><code>{deleting?.planId}</code></dd></div></dl><Notice tone="warning" title="历史数据不会被删除">历史 Launch 以及 AgentLoop 上已存在的 Experiment Record 将继续保留。</Notice></div>
      </Modal>

      <Modal
        open={Boolean(runResult)}
        title="立即执行已被接受"
        description="本次执行来源已记录为 schedule_run_now，不会改变原定的下次触发时间，也不会为本次 Launch 单独创建大盘。"
        onClose={() => setRunResult(null)}
        footer={<>
          <button className="button secondary" type="button" onClick={() => setRunResult(null)}>关闭</button>
          {safeExternalUrl(runResult?.agentLoopUrl) && <a className="button secondary" href={safeExternalUrl(runResult?.agentLoopUrl)!} target="_blank" rel="noopener noreferrer"><ExternalIcon /> 查看实验记录</a>}
          {runDashboardUrl && <a className="button primary" href={runDashboardUrl} target="_blank" rel="noopener noreferrer"><ExternalIcon /> 打开 Plan 大盘</a>}
        </>}
      >
        <div className="confirm-summary"><dl><div><dt>Submission</dt><dd><StatusBadge value={runResult?.submissionState} /></dd></div><div><dt>Record ID</dt><dd><code>{runResult?.recordId || "—"}</code></dd></div><div><dt>Plan 大盘</dt><dd><StatusBadge value={runResult?.planDashboard?.state || "PENDING"} compact /> {runResult?.planDashboard?.displayName || "等待初始化"}</dd></div><div><dt>大盘同步</dt><dd>{formatDateTime(runResult?.planDashboard?.lastSyncedAt)}{runResult?.planDashboard?.lastErrorCode && <> · <code>{runResult.planDashboard.lastErrorCode}</code></>}</dd></div><div><dt>请求时间</dt><dd>{formatDateTime(runResult?.requestedAt)}</dd></div></dl><Notice tone="warning" title="ACCEPTED 不等于实验完成">请到 AgentLoop 查看远端执行和评估结果；这个大盘由 Experiment Plan 持有，定时任务后续触发仍会更新并复用它。</Notice></div>
      </Modal>

      {toast && <Toast tone={toast.tone} title={toast.title} onClose={() => setToast(null)}>{toast.message}</Toast>}
    </div>
  );
}

function RecentLaunch({ schedule }: { schedule: Schedule }) {
  const launch = schedule.lastLaunch;
  const dashboardUrl = safeExternalUrl(launch?.planDashboard?.url);
  return (
    <>
      {launch ? (
        <div className="stack-tight">
          <StatusBadge value={launch.submissionState} compact />
          <StatusBadge value={launch.runnerState} compact />
          <small>{formatDateTime(launch.requestedAt)}</small>
          {launch.recordId && <code title={launch.recordId}>{truncateMiddle(launch.recordId, 8, 5)}</code>}
          {launch.planDashboard && <StatusBadge value={launch.planDashboard.state} compact />}
          {dashboardUrl && <a className="button secondary small" href={dashboardUrl} target="_blank" rel="noopener noreferrer"><ExternalIcon /> Plan 大盘</a>}
          {launch.skipReason && (
            <small className="state-clarifier" title={`${launch.skipReason} · ${skipReasonDescription(launch.skipReason)}`}>
              {launch.skipReason} · {skipReasonDescription(launch.skipReason)}
            </small>
          )}
        </div>
      ) : <span>—</span>}
      {schedule.lastErrorMessage && <small className="disabled-reason">{schedule.lastErrorMessage}</small>}
    </>
  );
}
