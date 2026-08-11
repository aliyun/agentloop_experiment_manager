# AgentLoop SDK 与执行契约

> 核对日期：2026-08-08
> API 版本：`2026-05-20`
> 生产控制面：`alibabacloud-agentloop20260520==2.3.1`
> 离线 Runner：`agentloop-sdk==1.0.3`

## 1. 最终结论

本项目采用两套职责互斥的 Python SDK：

| 组件 | 版本 | 本项目职责 | 明确不做 |
|---|---:|---|---|
| `alibabacloud-agentloop20260520` | `2.3.1` | Test Connection、Experiment Plan List/Get 与必要的只读回读 | 不在 Launch 前创建或更新 Experiment Run，不遍历 Dataset，不调用客户 Agent |
| `agentloop-sdk` | `1.0.3` | 解析 Plan、遍历 Dataset、创建唯一 Experiment Run、逐样本调用 Solution、上传结果并完成/失败收口 | 不管理本地 Schedule、Agent Connection、Launch 幂等和 Web UI |

生产 Gateway 已在 `backend/app/agentloop/` 实现，不再等待 Go SDK Adapter，也不通过 `aliyun` CLI 启动实验。

唯一 Record Owner 已冻结为 **Runner-owned**：离线 Runner 内部的 `agentloop-sdk` 创建 Experiment Run；本地控制面绝不在它之前调用 `CreateExperimentRun`。因此一次 Launch 只会创建一个云端 Record。

## 2. 依赖锁定证据

`backend/pyproject.toml` 精确锁定：

```toml
dependencies = [
  "agentloop-sdk==1.0.3",
  "alibabacloud-agentloop20260520==2.3.1",
  # ...
]
```

`agentloop-sdk==1.0.3` 内置了一份生成 SDK。Runner 的 Client 与 Request Model 必须都来自
该 vendored 包，不能把公开 `2.3.1` Client 与 vendored Request（或反向）混在同一次调用。
真实验收曾由此触发 `ExecuteQueryRequest` 缺少 `length`；当前实现已统一 Runner 的查询、
完成和失败收口类型，并用回归测试冻结该边界。控制面的 Plan 读操作继续独立使用公开 SDK。

关键实现：

- `backend/app/agentloop/cloud_control.py`：公开生成式 Python OpenAPI SDK 的只读控制面适配。
- `backend/app/agentloop/offline_runner.py`：Runner-owned Dataset 执行和结果上传。
- `backend/app/agentloop/production.py`：把两者组合为业务 Gateway。
- `backend/app/agentloop/gateway.py`：业务层稳定 Protocol。
- `backend/app/services/launches.py`：唯一 Launch 状态机和 `recordId` 落库回调。

仓库中的 `third_party/agentloop-go-sdk/` 和 `sdk/agentloop-go-contract/` 是早期 SDK 契约快照，可证明 API `2026-05-20` 的 `CreateExperimentRun` 响应公开包含 `recordId`。它们不属于当前 Python 服务的生产运行依赖，也不意味着仍需实现 Python-Go Adapter。

## 3. 控制面契约

### 3.1 Test Connection

`AgentLoopCloudControl.test_connection` 使用同一组 Region、AgentSpace、Endpoint 和 AK/SK 调用 `GetAgentSpace`。响应中的 AgentSpace 必须与配置一致。

Test Connection 成功只是身份和 AgentSpace 可达性证据。每次 Plan 读取和 Launch 仍显式携带同一 Profile 投影，不依赖 CLI Profile 或进程全局默认凭据。

### 3.2 Plan List/Get

Plan 是云端事实源：

- `ListExperimentPlans` 处理 `nextToken`，服务端设 10,000 条安全上限。
- 本地 offset/limit 只作用于已经完成远端遍历和过滤的集合。
- 可见页中的每个 Plan 会继续执行 `GetExperimentPlan`，避免仅凭列表摘要误判可运行性。
- `canLaunchLocally=true` 至少要求：
  - `experimentType=OFFLINE`；
  - 存在 `datasetId`；
  - 至少绑定一个 Evaluator；
  - 每个 Evaluator 的必需变量都有 `variableMapping`。
- Plan 的 `status` 仅按 AgentLoop 响应透传展示，不参与本地可运行性判断；是否接受一次执行以 `CreateExperimentRun` 的响应为准。

本地没有 Plan Create/Update/Delete；Plan 名称只是展示快照，所有关联使用 `planId`。

### 3.3 Record/结果边界

本地产品不提供 Record 状态、分数、Trace 或逐样本结果页面。`recordId` 的用途只有：

1. 证明 AgentLoop 已创建权威 Experiment Run；
2. 作为 Launch History 的云端关联键；
3. 构造 AgentLoop Console 深链。

结果查看、评估状态和报告均留在 AgentLoop。即使 OpenAPI SDK具备 Get/List Run 能力，也不能据此扩张本地产品范围。

## 4. Runner-owned Record 契约

### 4.1 为什么不能先 `CreateExperimentRun`

`agentloop-sdk==1.0.3` 的 `AgentLoopEvaluatorStorage` 初始化时会创建 Experiment Run，并把 ID 暴露为 `storage.experiment_id`。如果控制面先调用一次 OpenAPI `CreateExperimentRun`，再启动该 Runner，会得到两个 Record：一个空 Record 和一个真实执行 Record。

因此规范顺序是：

```text
LaunchService
  → GetExperimentPlan（只读）
  → 建立本地 LaunchIntent / RUNNING
  → AgentLoopOfflineRunner.start
      → resolve Plan / Dataset
      → AgentLoopEvaluatorStorage 创建唯一 Run
      → on_record_created(storage.experiment_id)
  → LaunchIntent ACCEPTED + recordId
      → 逐 Dataset item 调用本地 Agent
      → 上传每条结果
      → Runner HANDED_OFF / FAILED / UNKNOWN
```

强制不变量：

- `recordId` 不由本地生成。
- `on_record_created` 在第一道客户 Agent 任务前执行并持久化 ID。
- Control Gateway 没有 `create_experiment_run` 业务方法。
- 不传递或依赖远端 `clientToken`。
- 浏览器 `Idempotency-Key` 和 Schedule Slot Key 只在本地防重复，不映射为远端 Token。

### 4.2 Handle 语义

Runner 返回的 Handle 分离两个事实：

```python
class ExperimentHandle(Protocol):
    @property
    def record_id(self) -> str: ...

    async def wait_local_handoff(self) -> LocalHandoffOutcome: ...
```

- 获得 `record_id`：本地 `submission_state=ACCEPTED`。
- `wait_local_handoff` 成功：本地 `runner_state=HANDED_OFF`，表示客户侧 Dataset 执行与上传职责结束。
- 两者都不表示 AgentLoop 评估完成或分数达标。

### 4.3 失败语义

- 创建 Run 前的确定性校验失败：`DEFINITIVE_FAILURE`。
- 远端可能已接受但本地无法判断：`SUBMISSION_UNKNOWN`，相同本地 Key 不自动重试。
- AgentSpace/SLS Project 解析在创建边界之前显式完成；本地构造错误和 OpenAPI 4xx 属于
  确定性失败，只有 `CreateExperimentRun` 请求已进入远端但未取得权威 `recordId` 时才进入
  `SUBMISSION_UNKNOWN`。
- Run 已创建后客户 Agent/上传失败：保留 `recordId`，Runner 标记 `FAILED`，并尽力将云端 Run 更新为 `failed`。
- `agentloop-sdk` 聚合结束时的自动完成钩子只记录“聚合已保存”；Runner 在核对
  `failed_tasks==0`、预期 task 集合与已验证 Trace/已写 detail 集合严格一致、且所有 SLS detail
  首次上传成功后才写最终 `completed`。否则直接写 `failed`，避免先触发基于不完整数据的自动评估。
- 进程中断：遗留本地 `RUNNING` 在重启时转 `UNKNOWN/INTERRUPTED`；不自动 Resume，不重跑旧 Slot。

`agentloop-sdk==1.0.3` 的部分逐条上传路径可能只记录 warning。项目用 `_AuditedStorage` 捕获上传异常；存在任一上传失败时不得把本地交接标成成功。

项目不会沿用 SDK 的本地文件结果存储：题目、Agent input/output、Trace 和聚合结果只在
当前进程内存中参与计算并上报 AgentLoop，不能写入客户侧 `save_dir`。Run 启动、进度和
SLS 上传错误也只能记录固定错误码或计数，不能打印 SDK 原始异常。

## 5. Dataset 到本地 Agent 的映射

`_RunnerBenchmark` 覆盖 SDK 默认 Dataset loader，并执行以下完整性门槛：

- 每次 ExecuteQuery 都必须返回 `progress=Complete`、`truncated!=true`；
- `columns`、`columnTypes`、`rows` 和 `meta.count` 必须维度一致；
- Dataset item ID 必须非空且唯一，Plan 指定的 selected item 必须完整返回；
- 任一分页失败必须终止整个 Launch，禁止使用已读取的前缀继续；
- 默认全量读取用 `max_rows+1` 探测，超过安全上限时明确失败，禁止静默截断；
- 本地日志不得输出 Query、响应行、Dataset ID 或 SDK 原始错误。

Runner 从每条 Dataset row 依次选择首个非空字符串字段作为问题：

```text
question → input → query → prompt → JSON(row)
```

它保留完整 Dataset row 为 `originalInput`，并向选定 Agent Connection 发送：

```json
{
  "type": "experiment.task",
  "requestId": "<dataset task id>",
  "input": "<original question>\n<experiment_context ...>...</experiment_context>",
  "originalInput": {
    "id": "<dataset item id>",
    "question": "<original question>"
  },
  "context": {
    "schema_version": 1,
    "experiment_plan_id": "<plan id>",
    "experiment_run_id": "<record id>",
    "experiment_version": "<resolved version>",
    "dataset_item_id": "<item id>",
    "local_launch_id": "<launch id>"
  },
  "parameters": {}
}
```

Agent 返回 `output` 和可选 `meta`。Runner 生成 `SolutionOutput`，至少保留：

- `meta.experiment_input`：包含 XML 的最终输入，可供 Evaluator 解析；
- `meta.experiment_context`：同一五字段结构化对象；
- `meta.agent_connection_id`；
- `meta.agent_metadata`；
- 经 AgentSpace 绑定的 trajectory Logstore 验证后的 `traceId`。Runner 先通过
  `GetAgentSpace.slsProject` 解析 SLS Project，再用 `aliyun-log-python-sdk` 读取该 Project 固定的
  `agent-trajectory`；不使用 XTrace 或 `ListTracePreview`。Agent 返回值只作为 `trace_id` 查询提示；
  未返回时，Runner 用 XML 根 `experiment_context` 和 experiment plan/run、Dataset item、local launch
  四个字段反查：依次为 hint、`input` 五项、日志全文五项，再在固定时间窗中以每页 100 条、最多 5 页
  的 `*` 扫描，仅在内存解析 `input`/`atif_content` XML。每页必须 Complete、`isAccurate=true` 且非
  limited；关联来源仅为 `agent_trajectory_trace_id`、`agent_trajectory_input_context`、
  `agent_trajectory_line_context`、`agent_trajectory_scan_context`。任一歧义、页不完整、扫描上限或错误
  均拒绝写 detail。得到唯一 ID 后才允许 `save_solution_result`。完整契约见
  `docs/TRACE_CORRELATION.md`。

这样云端 Evaluator 可以按 Plan 的变量映射读取 `input`/`output`，并从 `experiment_input` 或 context 中提取 `experiment_version` 等自定义信息。本地平台不创建、编辑或重新计算 Evaluator。

## 6. XML Context 契约

每道题只追加一个根元素，恰好有五个子字段：

```xml
<experiment_context schema_version="1">
  <experiment_plan_id>...</experiment_plan_id>
  <experiment_run_id>...</experiment_run_id>
  <experiment_version>...</experiment_version>
  <dataset_item_id>...</dataset_item_id>
  <local_launch_id>...</local_launch_id>
</experiment_context>
```

约束：

- 原始问题保持在前，XML 通过一个换行追加。
- 所有值由 `xml.etree.ElementTree` 序列化并正确转义。
- Run ID 和 Dataset item ID 在云端 Run 创建/遍历到行后填入。
- 缺字段、多字段、占位符被篡改或 XML 解析失败时，Runner Fail Closed。
- `schema_version` 是根属性，不计入五个子字段。

## 7. 版本与参数契约

### MANUAL

- 必填 `experimentVersion`。
- 格式：`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`。
- 该值进入 Launch 不可变快照和每道题 context。

### AUTO

- 请求必须省略 `experimentVersion`。
- 创建 Launch 时生成一次：

```text
auto-<UTC YYYYMMDDTHHMMSSffffffZ>-<launch-id 前 12 个字母数字>
```

- 生成值立即持久化；同一幂等 Launch 不重新生成。
- 每次新的定时触发是新的 Launch，因此得到新的 AUTO 版本。

### JSON 参数

- 顶层必须是 JSON object。
- 规范化时按 Key 排序、拒绝 NaN/Infinity 和非 JSON 值。
- 规范化参数、Agent Connection、版本模式、人工版本、Record Name 和 Context Schema 共同参与 `launch_config_hash`；相同 Idempotency-Key 携带不同配置会返回冲突。

## 8. SDK 升级 Gate

升级任一 SDK 前必须重新验证：

1. `ListExperimentPlans` 的分页字段和 Plan 模型没有破坏性变化。
2. `GetExperimentPlan` 仍返回 Dataset、Evaluators 和变量映射。
3. `AgentLoopEvaluatorStorage` 仍只创建一个 Run，并能在第一道题前取得权威 ID。
4. Dataset 行到 `Task.input/Task.id` 的映射没有变化。
5. ExecuteQuery 的 progress、truncation、count、columnTypes 与分页错误仍可 Fail Closed。
6. `SolutionOutput.meta.experiment_input` 能被上传并被 Evaluator 读取。
7. 逐条上传失败不会被误报为 `HANDED_OFF`，且本地磁盘不产生题目/回答文件。
8. 自定义 Endpoint、User-Agent、AK/SK、Region、AgentSpace 均作用于同一个目标环境。
9. Reference Calculator 的真实 Runner-owned E2E 重新通过。

## 9. 当前云端验收状态

代码和本地契约验证不能替代真实云端拨测。项目已于 2026-08-08 在
`cn-hangzhou / <agent-space>` 完成一次真实端到端验收，详见
[真实验收记录](./docs/REAL_E2E_ACCEPTANCE_20260808.md)。后续 SDK、环境或目标 Agent 变化后，
仍必须用同一次 Launch 的以下证据重新闭环：

- 云端 OFFLINE Plan、Dataset 和 Evaluator ID；
- 本地 Agent 确实收到 Dataset item；
- Launch History 中的最终版本和唯一 `recordId`；
- AgentLoop 中同一 `recordId` 的结果与评估输出；
- 自定义 Evaluator 能从注入 context 读取版本等字段；
- 控制面没有额外创建第二个 Record。
