import { useCallback, useEffect, useMemo, useState } from "react";
import { ExternalIcon, RefreshIcon, SearchIcon } from "../components/Icons";
import { EmptyState, ErrorState, LoadingRows, Notice, PageHeader, Pagination, StatusBadge } from "../components/UI";
import { api, errorMessage } from "../lib/api";
import { formatDateTime, safeExternalUrl, skipReasonDescription, triggerLabel, truncateMiddle } from "../lib/format";
import type { Launch, PageResult, TraceCorrelationSummary } from "../lib/types";
import { focusedLaunchIdFromSearch } from "../lib/launchNavigation";
import { isTraceCorrelationTerminal } from "../lib/traceCorrelation";

export function ExperimentsPage() {
  const focusedLaunchId = useMemo(() => focusedLaunchIdFromSearch(window.location.search), []);
  const [page, setPage] = useState(1);
  const [planInput, setPlanInput] = useState("");
  const [planId, setPlanId] = useState("");
  const [launches, setLaunches] = useState<PageResult<Launch>>({ items: [], total: 0, page: 1, pageSize: 20 });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setLaunches(await api.launches.list({ planId, page, pageSize: 20 }));
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setLoading(false);
    }
  }, [page, planId]);

  useEffect(() => { void load(); }, [load]);

  const focusedLaunch = launches.items.find((launch) => launch.launchId === focusedLaunchId);
  const focusedTraceState = focusedLaunch?.traceCorrelation?.state;

  useEffect(() => {
    if (!focusedLaunchId || isTraceCorrelationTerminal(focusedTraceState)) return;
    let cancelled = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const latest = await api.launches.get(focusedLaunchId);
        if (!cancelled) {
          setLaunches((current) => {
            const exists = current.items.some((item) => item.launchId === latest.launchId);
            const items = exists
              ? current.items.map((item) => item.launchId === latest.launchId ? latest : item)
              : [latest, ...current.items].slice(0, current.pageSize);
            return { ...current, items, total: exists ? current.total : Math.max(current.total, items.length) };
          });
        }
      } catch {
        // Keep the current row visible and retry while the target remains non-terminal.
      } finally {
        if (!cancelled) timer = window.setTimeout(() => void poll(), 3000);
      }
    };
    timer = window.setTimeout(() => void poll(), 3000);
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [focusedLaunchId, focusedTraceState]);

  return (
    <div className="page">
      <PageHeader
        eyebrow="Local execution ledger"
        title="Launch History"
        description="查看每次人工或定时发起实验的不可变快照、Trace 关联进度，以及该 Experiment Plan 复用的结果大盘。"
        actions={<button className="button secondary" type="button" onClick={() => void load()} disabled={loading}><RefreshIcon /> 刷新</button>}
      />

      <Notice tone="warning" title="本地仅展示关联与入口">
        本地平台显示已解析的 Trace ID 与上报进度；同一 Plan 的历次运行共用一个 Plan 大盘，具体评估分数和逐样本结果请在大盘中查看。
      </Notice>

      {focusedLaunchId && <Notice title="已定位刚发起的 Launch">目标 Launch 会每 3 秒后台刷新，直到 Trace ID 解析成功或关联进入终态。</Notice>}

      <section className="panel table-panel">
        <div className="toolbar">
          <form className="search-box wide" onSubmit={(event) => { event.preventDefault(); setPage(1); setPlanId(planInput.trim()); }}>
            <SearchIcon />
            <input value={planInput} onChange={(event) => setPlanInput(event.target.value)} placeholder="按 Plan ID 精确过滤" aria-label="按 Plan ID 精确过滤" />
            <button type="submit">筛选</button>
          </form>
          {planId && <button className="button ghost small" type="button" onClick={() => { setPlanInput(""); setPlanId(""); setPage(1); }}>清除 Plan ID</button>}
          <span className="result-count">{loading ? "正在读取…" : `${launches.total} 条 Launch`}</span>
        </div>

        {error ? <ErrorState message={error} onRetry={() => void load()} /> : (
          <div className="table-scroll">
            <table>
              <thead><tr><th>Launch / Plan</th><th>Agent</th><th>最终版本</th><th>实验参数</th><th>触发来源</th><th>状态</th><th>Trace 关联</th><th>时间</th><th>Record / 结果</th><th>Plan 大盘</th><th>错误 / 跳过原因</th></tr></thead>
              <tbody>
                {loading ? <LoadingRows columns={11} /> : launches.items.map((launch) => <LaunchRow key={launch.launchId} launch={launch} focused={launch.launchId === focusedLaunchId} />)}
              </tbody>
            </table>
            {!loading && launches.items.length === 0 && <EmptyState title="暂无 Launch History">从 Plans 页面人工发起，或等待本地 Schedule 触发后，此处会记录每次真实启动尝试。</EmptyState>}
          </div>
        )}
        <Pagination page={launches.page || page} pageSize={launches.pageSize || 20} total={launches.total} onChange={setPage} />
      </section>
    </div>
  );
}

function LaunchRow({ launch, focused }: { launch: Launch; focused: boolean }) {
  const consoleUrl = safeExternalUrl(launch.agentLoopUrl);
  const parameters = JSON.stringify(launch.experimentParameters || {});
  return (
    <tr className={focused ? "launch-row-focused" : undefined} aria-current={focused ? "true" : undefined}>
      <td className="primary-cell"><strong>{launch.planNameSnapshot || "未命名 Plan"}</strong><code title={launch.launchId}>Launch {truncateMiddle(launch.launchId, 8, 5)}</code><code title={launch.planId}>Plan {truncateMiddle(launch.planId, 8, 5)}</code></td>
      <td><div className="stack-tight"><strong>{launch.agentConnectionNameSnapshot || "未命名 Agent"}</strong>{launch.agentConnectionId && <code title={launch.agentConnectionId}>{truncateMiddle(launch.agentConnectionId, 7, 5)}</code>}</div></td>
      <td><div className="stack-tight"><code title={launch.experimentVersion || undefined}>{launch.experimentVersion || "等待生成"}</code><small>{launch.versionMode || "—"}</small></div></td>
      <td><code className="parameter-code" title={parameters}>{parameters.length > 56 ? `${parameters.slice(0, 53)}…` : parameters}</code></td>
      <td>{triggerLabel(launch.triggerSource)}</td>
      <td><div className="stack-tight"><StatusBadge value={launch.submissionState} compact /><StatusBadge value={launch.runnerState} compact /></div></td>
      <td><TraceCorrelationCell summary={launch.traceCorrelation} /></td>
      <td><dl className="mini-dl"><div><dt>计划</dt><dd>{formatDateTime(launch.scheduledFor)}</dd></div><div><dt>请求</dt><dd>{formatDateTime(launch.requestedAt)}</dd></div><div><dt>完成</dt><dd>{formatDateTime(launch.workerFinishedAt)}</dd></div></dl></td>
      <td><div className="stack-tight">{launch.recordId ? <code title={launch.recordId}>{truncateMiddle(launch.recordId)}</code> : <span>—</span>}{consoleUrl ? <a className="button secondary small" href={consoleUrl} target="_blank" rel="noopener noreferrer"><ExternalIcon /> AgentLoop</a> : <small>链接尚不可用</small>}</div></td>
      <td><PlanDashboardCell launch={launch} /></td>
      <td>
        {launch.skipReason ? (
          <span className="skip-copy" title={`${launch.skipReason} · ${skipReasonDescription(launch.skipReason)}`}>
            <code>{launch.skipReason}</code>
            {skipReasonDescription(launch.skipReason)}
          </span>
        ) : launch.errorMessage ? (
          <span className="error-copy" title={launch.errorMessage}>{launch.errorCode && <code>{launch.errorCode}</code>}{launch.errorMessage}</span>
        ) : "—"}
      </td>
    </tr>
  );
}

function PlanDashboardCell({ launch }: { launch: Launch }) {
  const dashboard = launch.planDashboard;
  const dashboardUrl = safeExternalUrl(dashboard?.url);
  if (!dashboard) {
    return <span className="trace-correlation-unavailable">尚未初始化</span>;
  }
  return (
    <div className="stack-tight">
      <StatusBadge value={dashboard.state} compact />
      <small title={dashboard.dashboardName || undefined}>{dashboard.displayName || "当前 Plan 大盘"}</small>
      {dashboardUrl ? (
        <a className="button secondary small" href={dashboardUrl} target="_blank" rel="noopener noreferrer">
          <ExternalIcon /> 打开 Plan 大盘
        </a>
      ) : <small>大盘链接尚不可用</small>}
      <small title={dashboard.lastSyncedAt || undefined}>按 Plan 复用 · {formatDateTime(dashboard.lastSyncedAt)}</small>
      {dashboard.lastErrorCode && <code className="disabled-reason">{dashboard.lastErrorCode}</code>}
    </div>
  );
}

function TraceCorrelationCell({ summary }: { summary?: TraceCorrelationSummary }) {
  if (!summary) {
    return <span className="trace-correlation-unavailable">尚无关联摘要</span>;
  }

  const total = summary.totalTasks;
  const progressMaximum = Math.max(total, summary.reportedTasks, 1);
  const reported = Math.min(summary.reportedTasks, progressMaximum);
  const progress = total > 0 ? Math.min(100, Math.round((reported / total) * 100)) : summary.reportedTasks > 0 ? 100 : 0;
  const elapsed = formatTraceElapsed(summary.startedAt, summary.finishedAt);
  return (
    <div className="trace-correlation-cell">
      <div className="trace-correlation-heading">
        <StatusBadge value={summary.state} compact />
        <strong>{summary.reportedTasks}/{total}</strong><span>已上报</span>
      </div>
      <div
        className="trace-correlation-progress"
        role="progressbar"
        aria-label="Trace 上报进度"
        aria-valuemin={0}
        aria-valuemax={progressMaximum}
        aria-valuenow={reported}
      >
        <span style={{ width: `${progress}%` }} />
      </div>
      <div className="trace-correlation-counts">
        <span>已解析 <b>{summary.resolvedTasks}</b></span>
        <span>待处理 <b>{summary.pendingTasks}</b></span>
        <span className={summary.failedTasks > 0 ? "danger" : undefined}>失败 <b>{summary.failedTasks}</b></span>
        <span>轮询 <b>{summary.totalAttempts}</b> 次</span>
      </div>
      <div className="trace-id-list">
        <span>Trace ID</span>
        {summary.traceIds.length > 0
          ? summary.traceIds.map((traceId) => <code key={traceId} title={traceId}>{traceId}</code>)
          : <small>{isTraceCorrelationTerminal(summary.state) ? "未解析到 Trace ID" : "等待 Trace ID"}</small>}
      </div>
      <dl className="trace-correlation-time">
        <div><dt>耗时</dt><dd>{elapsed}</dd></div>
        <div><dt>完成</dt><dd title={summary.finishedAt || undefined}>{formatDateTime(summary.finishedAt)}</dd></div>
      </dl>
      {(summary.lastErrorCode || summary.lastErrorMessage) && (
        <span className="trace-correlation-error" title={summary.lastErrorMessage || summary.lastErrorCode || undefined}>
          {summary.lastErrorCode && <code>{summary.lastErrorCode}</code>}
          {summary.lastErrorMessage || "Trace 关联失败"}
        </span>
      )}
    </div>
  );
}

function formatTraceElapsed(startedAt?: string | null, finishedAt?: string | null): string {
  if (!startedAt) return "—";
  const started = new Date(startedAt).getTime();
  const finished = finishedAt ? new Date(finishedAt).getTime() : Date.now();
  if (!Number.isFinite(started) || !Number.isFinite(finished) || finished < started) return "—";
  const seconds = Math.floor((finished - started) / 1000);
  if (seconds < 1) return "<1 秒";
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分 ${seconds % 60} 秒`;
  const hours = Math.floor(minutes / 60);
  return `${hours} 小时 ${minutes % 60} 分`;
}
