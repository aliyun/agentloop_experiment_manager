# AgentLoop Local Experiment Launcher 真实端到端验收

> 历史证据边界：本记录验证的是 2026-08-08 当时版本。该版本接受 Reference Agent 随机
> 生成的 32 位 ID，尚未实现“先经 GetAgentSpace 解析 `slsProject`、再在固定
> `agent-trajectory` Logstore 验证唯一 Trace，最后一次性写 experiment_detail”的新屏障。
> 因此本记录不能作为当前 Trace 关联版本的云端验收结论；当前版本必须使用真实可导出的
> Agent Trace 重新验收，并读回实际 SLS source。

验收时间：2026-08-08（Asia/Singapore）
验收环境：`cn-hangzhou` / AgentSpace `<agent-space>`
访问身份：Aliyun CLI Profile `<profile>`

> 公开版本已移除实际云端资源 ID、Trace ID、本机 Profile 名和控制台深链；原始验收记录
> 仅保留在受控审计环境中。

## 结论

本项目已完成一次不使用 Mock 的真实端到端实验：独立 Reference Calculator Agent
进程主动通过 WebSocket 连接客户侧 Launcher；Launcher 读取 AgentLoop 云端 Dataset，创建
唯一 Experiment Run，把三道题逐题发送给本地 Agent，再把真实输出与 XML context 上传到
AgentLoop。云端实验记录最终为 `completed`，3/3 任务成功；自动评估实际调用 3 次，3/3
成功、3/3 得分为 1。完成权威回读后，持续评估任务已置为 `Terminated`，避免继续扫描或
产生额外评估调用。

Reference Calculator Agent 是一个真实、独立运行并通过正式连接协议接入的确定性 Agent，
不是测试替身；它仅用于验证平台链路，不代表客户最终业务 Agent 的实现。

## 云端资源

| 资源 | ID / 名称 | 验收状态 |
| --- | --- | --- |
| Dataset | `<dataset-id>` | Schema 与 3 行数据已通过 `ExecuteQuery` 回读，`progress=Complete`、`count=3` |
| Evaluator | `<evaluator-id>` / `1.0.0` | `AGENT`、`raw_prompt`、`direct_llm`，变量映射与自定义输出 Schema 已回读 |
| Experiment Plan | `<plan-id>` | `offline`，绑定上述 Dataset 与 Evaluator |
| Experiment Run | `<run-id>` | `completed`，`progress=100`，3/3 完成，0 失败 |
| Evaluation Task | `<evaluation-task-id>` | 完成 3 条评估后主动终止 |
| Evaluation Run | `<evaluation-run-id>` | `Terminated`，终止前累计 3/3 成功、0 失败 |

结果页已使用实际 `recordId` 与 `planId` 完成回读；公开版本不保留控制台深链。

## 本地发起证据

- Launch ID：`<launch-id>`
- Agent Connection ID：`<agent-connection-id>`
- 实验版本：`acceptance-20260808-002`
- 本地最终状态：`submissionState=ACCEPTED`、`runnerState=HANDED_OFF`
- 权威 `recordId`：`<run-id>`
- Worker 完成时间：`2026-08-08T05:42:46.406872Z`

`recordId` 由 Runner 创建并在第一道题发送前持久化。控制面未预创建第二个 Run。

## 逐题验证

| Dataset Item | 问题 | 期望 / 实际 | Trace ID | 评估 |
| --- | --- | --- | --- | --- |
| `<dataset-item-1>` | `2 + 3` | `5 / 5` | `<trace-id-1>` | success / 1 |
| `<dataset-item-2>` | `12 * 7` | `84 / 84` | `<trace-id-2>` | success / 1 |
| `<dataset-item-3>` | `100 - 58` | `42 / 42` | `<trace-id-3>` | success / 1 |

SLS `experiment_detail` 与 `evaluation_detail` 均使用有界时间窗回读，两个响应均为：

- `meta.isAccurate=true`
- `meta.progress=Complete`
- `meta.count=3`
- `meta.processedRows=3`

三条 `evaluation_detail.custom_outputs` 逐条满足：

- `status=success`
- `score=1`、`score_value=1.0`
- `answer_match=true`
- `context_parse_status=ok`
- `context_schema_version=1`
- `experiment_plan_id`、`experiment_run_id`、`experiment_version`、
  `dataset_item_id`、`local_launch_id` 均与本次 Launch 精确一致

## 验收中发现并修复的问题

第一次执行在云端 Run 创建前失败，错误为公开 SDK Client 与 `agentloop-sdk` vendored
Request Model 不兼容：`ExecuteQueryRequest` 缺少 `length`。AgentLoop 侧确认该次没有创建
Run。修复后，Runner 的 Dataset 查询、完成和失败收口统一使用 `agentloop-sdk` 自带的
vendored Client/Model；SLS Client 同时带上 Launcher User-Agent，完成更新异常不再被吞掉。

## 回归结果

- Backend：`89 passed`
- Ruff：通过
- Frontend 边界测试：`8/8 passed`，并在 `TZ=Pacific/Honolulu` 下再次 `8/8 passed`
- Frontend TypeScript：通过
- Frontend production build：通过
- Go SDK 快照：`go test ./...` 通过（无测试文件，完成编译校验）

真实云端验收完成后，发布前独立审计又补强了失败与调度分支：单题失败/上传失败不再先把
Run 写成 `completed`，`SKIP` 不再跳过正常当前 Slot；Plan 的云端返回状态只作展示，
不作为本地发起门禁，实际 Run 是否被接受以 `CreateExperimentRun` 结果为准；Run Now 重试
复用稳定请求键，IANA 时区转换和错误脱敏也增加了回归测试。上游 Storage 的本地题目/回答
文件写入也已关闭，只保留 AgentLoop SLS 上报。Dataset loader 现同时校验每页完成状态、
truncation、行数/列类型、ID 唯一性和总量上限；任一分页失败都会在创建 Run 前终止，不再用
部分数据继续实验。这些补强没有创建第二个云端 Run，也没有产生超过本次 3 条的额外模型评估调用。

云端 Dataset、Evaluator、Plan、Experiment Run 与评估明细均保留，便于后续审计；仅把本次
自动生成的持续 Evaluation Task 终止，没有删除资源。
