# Experiment Detail 与 Trajectory 一次性关联

## 目标与边界

客户 Agent 位于 AgentLoop 云端不可直接访问的网络中。每个 Dataset item 执行后，Launcher 必须先
在该 AgentSpace 实际绑定的 SLS Project 的 `agent-trajectory` Logstore 中证明存在唯一轨迹，才向
`experiment_detail` 首次写入包含顶层 `traceId` 的记录。

禁止先写无 `traceId` 的 detail，再追加一条“补写”记录；SLS detail 是追加写，重复 `task_id` 会
破坏题目明细唯一性。Launcher 不使用 XTrace Project、`logstore-tracing` 或
`ListTracePreview` 作为本功能的数据源。

## 执行顺序

```mermaid
sequenceDiagram
    participant R as Local Runner
    participant A as Customer Agent
    participant G as AgentLoop GetAgentSpace
    participant S as SLS agent-trajectory
    participant D as experiment_detail
    participant E as Experiment Run
    participant V as Bound Evaluator

    R->>A: Dataset item + experiment_context
    A-->>R: output + optional traceId hint
    R->>G: GetAgentSpace(agentSpace)
    G-->>R: slsProject
    loop bounded polling
        R->>S: GetLogs(slsProject, agent-trajectory, fixed window)
        S-->>R: 0, 1, or multiple candidate trajectory logs
    end
    alt exactly one verified trajectory
        R->>D: write detail once with traceId
        D-->>R: upload accepted
    else missing, ambiguous, rejected, or timed out
        R-->>E: fail Run; no detail is written for this item
    end
    R->>E: completed only after every item detail is reported
    E->>V: trigger the Plan-bound evaluation
```

Agent 返回的 `traceId` 只是快速路径提示，仍必须在同一 `agent-trajectory` Logstore 中验证；没有
返回时按 XML context 反查。`AgentLoopGeneralEvaluator` 只在单题 Solution 返回后调用
`save_solution_result`，因此 Runner 在单题函数内完成轮询，再把验证后的 ID 放入
`SolutionOutput.meta.traceId`。存储层会二次校验该 ID 已为唯一 `(task_id, attempt)` 登记，拒绝
无 Trace、重复 detail 或一个 Trace 对应多个题目的写入。

## 数据源解析与查询顺序

每个 Launcher 实例按当前 Profile 的 Region、AK/SK 和 AgentSpace 调用 `GetAgentSpace`，读取响应
的 `slsProject`；随后使用 `aliyun-log-python-sdk` 的 `LogClient` 访问该 Project 中固定名称为
`agent-trajectory` 的 Logstore。启动前会验证该 Logstore 存在。不得从名称推断 Project，也不得使用
另一个 XTrace Project。

唯一性证明使用一个 XML 根标记和四个精确值；`experiment_version` 只用于评估/审计，不参与匹配：

```text
experiment_context
experiment_plan_id
experiment_run_id
dataset_item_id
local_launch_id
```

每次轮询使用 Agent 调用开始到结束时间、前后各扩展 120 秒的固定窗口。SLS 查询顺序如下：

1. 如 Agent 返回合法 32 位十六进制 `traceId`，查询 `trace_id`，但仍在该条轨迹的 `input` 或
   `atif_content` 中验证完整 XML context。
2. 以 `input` 同时检索上述五项（根标记 + Plan/Run/Item/Launch），并逐条解析验证。
3. 以日志全文同时检索上述五项，兼容 XML 被封装在 `atif_content` 或 SLS 没有 `input` 专用索引的
   情况；仍逐条解析验证。
4. 若前三项无结果，在同一固定窗口以 `*` 扫描：每页 100 条、最多 5 页，使用 offset 翻页。
   仅在内存中解析 `input` 和 `atif_content` 中的 XML，精确比对四个字段后立即丢弃 payload。

即使 hint 或 `input` 查询已经得到一个候选，Launcher 仍会继续完成第 3 步全文查询，用其完整、
准确的结果排除只存在于其他轨迹字段中的第二个 Trace；若不同策略结果矛盾，则继续以第 4 步扫描
核对，无法一致时失败关闭。

每一页都必须满足 SLS `is_completed=true`、`meta.isAccurate=true` 且 `meta.limited` 不为真；否则不从
不完整结果中选择 Trace。查询响应、SLS payload、题目、回答和 `output` 都不写入 SQLite、日志或
Launcher API。`output` 被明确排除，避免 Agent 回显其他请求 context 造成误关联。

来源仅记录下列无载荷枚举值：

- `agent_trajectory_trace_id`
- `agent_trajectory_input_context`
- `agent_trajectory_line_context`
- `agent_trajectory_scan_context`

任一查询步骤的候选按不同 Trace ID 去重：0 个继续轮询；1 个才解析成功；2 个及以上为
`TRACE_AMBIGUOUS`，Fail Closed。Agent 调用结束后默认轮询 `180` 秒；可通过
`AGENTLOOP_LAUNCHER_TRACE_LOOKUP_TIMEOUT_SECONDS` 配置为 `61`–`1800` 秒，必须严格大于
`60` 秒。截止时间内仍没有唯一轨迹则为 `TRACE_LOOKUP_TIMED_OUT`。配置由 Launcher 启动时读取，
由 Offline Runner 构造每次实验使用的 `TracePollingPolicy`，修改后需重启进程。

## 错误与 Fail Closed

以下错误不会写 detail，也不会让 Run 进入 `completed`：

| 错误码 | 含义 |
| --- | --- |
| `TRACE_SOURCE_NOT_FOUND` | `GetAgentSpace.slsProject` 缺失，或 Project 中没有 `agent-trajectory` |
| `TRACE_SEARCH_PERMISSION_DENIED` | AK/SK 无权读取该 AgentSpace 绑定的 trajectory Logstore |
| `TRACE_SEARCH_UNAVAILABLE` | SLS/AgentLoop 暂时不可用，可在轮询窗口内重试 |
| `TRACE_SEARCH_REJECTED` | SLS 拒绝请求，不能安全继续 |
| `TRACE_SEARCH_DEADLINE_EXCEEDED` | 单次 SLS 请求已超过本轮轮询剩余时间 |
| `TRAJECTORY_QUERY_INCOMPLETE` | 查询未完成或 `isAccurate` 不为真，可重试 |
| `TRAJECTORY_QUERY_LIMITED` | SLS 返回 limited 结果，拒绝从截断候选中选择 |
| `TRAJECTORY_QUERY_INCONSISTENT` | 字段查询与固定窗口扫描结果不一致，可重试 |
| `TRAJECTORY_INVALID_RESPONSE` | SLS 响应结构不满足安全解析条件，可重试 |
| `TRAJECTORY_SCAN_LIMIT_EXCEEDED` | 一个策略达到 5 页扫描边界，拒绝从不完整窗口选择 |
| `TRAJECTORY_PAYLOAD_LIMIT_EXCEEDED` | 单条轨迹超过本地内存检查边界 |
| `TRAJECTORY_TRACE_ID_INVALID` | 匹配 XML 的轨迹没有合法 Trace ID |
| `TRACE_ID_INVALID` / `TRACE_HINT_MISMATCH` | Agent 提示 ID 非法，或与 SLS 中验证结果不一致 |

错误内容必须脱敏，不回显 AK/SK、请求体或 Trace payload。

## Agent 埋点契约

客户 Agent 必须把 Launcher 透传的结构化 `payload.context` 写入一条会导出到当前 AgentSpace
trajectory Logstore 的真实入口轨迹；可以写入 `input` 或 `atif_content`。常用属性示例：

```python
attributes = {
    "agentloop.experiment_plan_id": context["experiment_plan_id"],
    "agentloop.experiment_run_id": context["experiment_run_id"],
    "agentloop.experiment_version": context["experiment_version"],
    "agentloop.dataset_item_id": context["dataset_item_id"],
    "agentloop.local_launch_id": context["local_launch_id"],
    "gen_ai.span.kind": "ENTRY",
}
```

仅生成随机 UUID 并命名为 `traceId` 不符合契约；ID 必须能由上述 SLS 查询真实读到。短生命周期
Agent 应在返回前 flush exporter。Agent 可以返回真实 ID，也可以不返回并让 Launcher 反查。

## 本地状态、完成屏障与恢复

SQLite `trace_correlations` 以 `(launch_id, dataset_item_id)` 唯一，只保存 Run/item/Trace ID、来源、
轮询次数、状态、时间和脱敏错误。状态为：

```text
PENDING -> POLLING -> RESOLVED -> REPORTED
                     |
                     +-> AMBIGUOUS / TIMED_OUT / FAILED
```

`REPORTED` 只在包含唯一 Trace ID 的 detail 首次上传成功后记录。最终将 Experiment Run 标记为
`completed` 时会再次校验：预期 task 集合、已验证 Trace 集合、已成功上报 detail 集合必须完全一致，
并且没有失败任务；任一条件不满足即拒绝 `completed`。进程重启无法安全恢复 Agent 输出（本地不保存
input/output），因此遗留的 PENDING/POLLING/RESOLVED 会收口为 `FAILED / LOCAL_PROCESS_INTERRUPTED`，
不会自动补写或重复上报。

Launch API 返回汇总进度及已验证 Trace ID 列表；Launch History 展示 Trace ID、关联状态、
reported/total、轮询次数和脱敏错误，不返回 Trace body、题目或 Agent 输出。
题目、回答、轨迹详情、分数和评估结果仍只在 AgentLoop 查看。

## 验收条件

- `GetAgentSpace` 返回的 `slsProject` 与实际查询的 Project 一致，且读取固定 `agent-trajectory`；
- 延迟可见时，前几次查询为空、随后出现唯一轨迹，最终只产生一条 detail；
- Agent 返回 hint 时，必须经过相同 SLS Logstore 验证；没有 hint 时，五项查询键可反查唯一轨迹；
- 未建索引时，固定窗口最多 5 页 × 100 条扫描可从 `input`/`atif_content` 的 XML 反查，且不保留
  payload；超过边界、页不准确、limited 或响应异常必须失败关闭；
- 歧义、超时、权限、上传失败均不产生无 Trace detail，Run 不进入 `completed`；
- 只有所有 item 都是 `REPORTED`，Run 才完成并由云端触发绑定 Evaluator；
- 本地数据库、日志和 API 不包含 AK/SK、原始题目/回答、轨迹 body 或 output。
