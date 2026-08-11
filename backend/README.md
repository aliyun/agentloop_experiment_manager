# Backend

FastAPI 后端同时承担 AgentLoop 只读控制面、Runner-owned 离线实验执行、本地 Agent WebSocket Hub、Schedule 和 Launch History。

## 运行模式

`AGENTLOOP_LAUNCHER_GATEWAY_MODE` 支持：

- `agentloop`：生产链路。`alibabacloud-agentloop20260520==2.3.1` 读取 Plan，`agentloop-sdk==1.0.3` 遍历 Dataset、创建唯一 Experiment Run、调用本地 Agent 并上传结果。
- `fake`：只用于本地 UI 和自动化测试，不访问云端，不可用于验收。
- `disabled`：显式禁用外部操作，所有 AgentLoop 请求 Fail Closed。

`agentloop` 已是可执行实现，不依赖 CLI、私有 Python SDK 模块或待接入的 Go Adapter。生产业务代码不得通过 shell 调用 `aliyun`。

## 启动

在项目根目录：

```bash
./agentloop-launcher
```

该入口固定真实 `agentloop` Gateway、开启 Scheduler、托管构建后的 React 页面并消费
`host/port` 配置。首次本地单用户启动会在稳定应用数据目录生成权限为 `0600` 的 Master
Key；已有 `secrets.enc` 但缺少原 Key 时会拒绝生成新 Key。正式服务器仍应通过
`AGENTLOOP_LAUNCHER_MASTER_KEY` 注入 Secret Manager 管理的 Key。

管理页面：`http://127.0.0.1:8090`；Swagger UI：`http://127.0.0.1:8090/docs`。

主要环境变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AGENTLOOP_LAUNCHER_ENVIRONMENT` | `development` | `development/test/production` |
| `AGENTLOOP_LAUNCHER_HOST` | `127.0.0.1` | 一键入口实际监听地址 |
| `AGENTLOOP_LAUNCHER_PORT` | `8090` | 一键入口实际监听端口 |
| `AGENTLOOP_LAUNCHER_ALLOW_REMOTE_ACCESS` | `false` | 是否允许非 Loopback 监听；还必须使用 production 和外部 Master Key |
| `AGENTLOOP_LAUNCHER_DATA_DIR` | OS 用户应用数据目录 | SQLite、加密 Secret、锁和 Master Key |
| `AGENTLOOP_LAUNCHER_GATEWAY_MODE` | `disabled` | 真实运行必须是 `agentloop` |
| `AGENTLOOP_LAUNCHER_MASTER_KEY` | 无 | 非 Fake/Test 必填的 Fernet Key |
| `AGENTLOOP_LAUNCHER_SCHEDULER_ENABLED` | `false` | 是否启动本地 Scheduler |
| `AGENTLOOP_LAUNCHER_SCHEDULER_POLL_SECONDS` | `5` | 到期任务轮询间隔 |
| `AGENTLOOP_LAUNCHER_AGENT_TASK_TIMEOUT_SECONDS` | `300` | 单道题等待 Agent 的秒数 |
| `AGENTLOOP_LAUNCHER_TRACE_LOOKUP_TIMEOUT_SECONDS` | `180` | Agent 完成后等待轨迹可见的秒数；必须大于 `60` 且不超过 `1800` |
| `AGENTLOOP_LAUNCHER_CONSOLE_BASE_URL` | 无 | 可选全局值；实际深链使用 Profile 配置 |

服务器部署的 Master Key 必须来自客户 Secret Manager、Kubernetes Secret、systemd credential
或等价部署注入。本地单用户自动生成模式只为简化试用和驻场运行。不要把 Key 或 AK/SK
放进仓库、SQLite、启动参数、日志或示例文件。

## 生产执行链路

```text
POST /plans/{planId}/launch
  1. 用公开 Python OpenAPI SDK GetExperimentPlan
  2. 校验 OFFLINE + Dataset + Evaluator/变量映射 + Agent 在线
  3. 建立 LaunchIntent，解析并固化版本/参数/context template
  4. agentloop-sdk Runner 创建唯一 Experiment Run
  5. 回调权威 recordId，Launch 进入 ACCEPTED
  6. 遍历 Dataset，通过 WebSocket 逐题分发到指定 Agent
  7. 上传结果，Runner 状态进入 HANDED_OFF/FAILED/UNKNOWN
```

唯一 Record Owner 是 `AgentLoopOfflineRunner` 内的 `agentloop-sdk`。`AgentLoopCloudControl` 只负责 Plan List/Get；不得在 Runner 前调用 `CreateExperimentRun`。`recordId` 从 Runner 创建的 storage/Experiment Run 回调，不由本地生成。

## 本地 Agent

本地 Agent 必须主动连接：

```text
WS(S) /api/v1/agent-connections/{connectionId}/connect
Authorization: Bearer <token>
```

创建 Connection 的响应会且只会在当次返回 Token、`connectUrl` 和参考命令；List/Get 不回显 Token。完整协议见 [../docs/LOCAL_AGENT_INTEGRATION.md](../docs/LOCAL_AGENT_INTEGRATION.md)。

## API 范围

```text
GET    /api/v1/health

GET    /api/v1/settings/agentloop
PUT    /api/v1/settings/agentloop
POST   /api/v1/settings/agentloop/test

POST   /api/v1/agent-connections
GET    /api/v1/agent-connections
GET    /api/v1/agent-connections/{connectionId}
GET    /api/v1/agent-connections/{connectionId}/health
POST   /api/v1/agent-connections/{connectionId}/rotate-token
DELETE /api/v1/agent-connections/{connectionId}
WS     /api/v1/agent-connections/{connectionId}/connect

GET    /api/v1/plans
GET    /api/v1/plans/{planId}
POST   /api/v1/plans/{planId}/launch

GET    /api/v1/launches
GET    /api/v1/launches/{launchId}

GET    /api/v1/schedules
POST   /api/v1/schedules
GET    /api/v1/schedules/{scheduleId}
PATCH  /api/v1/schedules/{scheduleId}?expectedRevision=N
DELETE /api/v1/schedules/{scheduleId}?expectedRevision=N
POST   /api/v1/schedules/{scheduleId}/run-now
POST   /api/v1/schedules/{scheduleId}/enable?expectedRevision=N
POST   /api/v1/schedules/{scheduleId}/disable?expectedRevision=N
```

本地产品不提供 Plan/Dataset/Evaluator 写接口，也不提供 Record/结果展示接口。`recordId` 只作为 Launch History 中的云端关联键和 AgentLoop 深链参数。

人工 Launch 请求必须带稳定的 `Idempotency-Key`，并包含 Agent、版本策略和 JSON 参数：

```json
{
  "agentConnectionId": "<connection-id>",
  "versionMode": "AUTO",
  "experimentParameters": {},
  "contextSchemaVersion": 1,
  "recordName": null
}
```

`MANUAL` 模式必须提供符合 `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` 的 `experimentVersion`；`AUTO` 模式必须省略它。

## 数据与 Secret

SQLite 只承载本地控制元数据：

- `schedules`：Plan ID、Agent Connection 引用、版本策略、参数、调度规则；
- `launch_intents`：每次启动的不可变快照、时间、状态、`recordId`；
- `agent_connections`：本地 Agent 注册信息和 Token 的 Secret 引用；
- `agentloop_profile`：部署级 Region/AgentSpace/Endpoint、脱敏 AK 提示和 Secret 引用；
- `schema_version`：可审计迁移版本。

SQLite 不保存 Plan 正文、Dataset 行、Evaluator、Agent input/output、Trace、分数或结果。AK/SK 和 Agent Token 存储在加密 Secret Store；数据库只保留引用和脱敏提示。SQLite 启用 WAL、Foreign Keys、5 秒 Busy Timeout，数据目录/文件分别使用 `0700/0600` 权限。

## 版本与 Context

`AUTO` 版本格式为：

```text
auto-<UTC YYYYMMDDTHHMMSSffffffZ>-<launch-id 前 12 个字母数字>
```

版本在创建 Launch 时生成一次并落库，不会按 Dataset item 重新生成。每道题的原始问题后追加以下 XML，值通过 XML 库转义：

```xml
<experiment_context schema_version="1">
  <experiment_plan_id>...</experiment_plan_id>
  <experiment_run_id>...</experiment_run_id>
  <experiment_version>...</experiment_version>
  <dataset_item_id>...</dataset_item_id>
  <local_launch_id>...</local_launch_id>
</experiment_context>
```

Runner 同时把结构化 `context`、原始 Dataset 行 `originalInput` 和 `parameters` 传给 Agent，并把 `experiment_input`/`experiment_context` 写入 `SolutionOutput.meta`，供 AgentLoop 评估器按变量映射读取。禁止依赖字符串拼接构造未转义 XML。

## 调度与恢复

- 支持 `INTERVAL_MINUTES`（最小 5 分钟）、兼容既有 `INTERVAL_HOURS`，以及带 IANA 时区的 `DAILY`。
- Schedule 绑定不可变 Plan ID、Agent Connection、`MANUAL/AUTO` 版本策略和 JSON 参数。
- 每次触发前重新 Get Plan、检查 Agent 在线，并为 AUTO 生成本次 Launch 的新版本。
- Slot 唯一键、Schedule revision 和全局并发限制防止本地重复/重叠。
- `SKIP` 跳过历史错过的 Slot，但仍执行在两次 poll 容差内观测到的当前 Slot；
  `FIRE_ONCE` 最多补跑 Grace Window 内最近一个 Slot。失败 Slot 也会推进
  `next_fire_at`，不会每个 poll 重复触发。
- 进程重启把遗留 `RUNNING` 标记为 `UNKNOWN/INTERRUPTED`；不自动 Resume，不重跑旧 Slot。

## 安全边界

Development 模式只允许 Loopback 客户端，并拒绝不可信 Origin 的写请求。直接监听私有网络必须同时显式配置非 Loopback `host`、`allow_remote_access=true` 和 `production`，并选择外部 Master Key 或显式的 `--use-local-master-key` 单机模式；缺少任一条件都会拒绝启动。项目根目录的 `start-agentloop-remote` 已封装这些参数，本机密钥会在 Data 目录首次生成并自动复用。

`production` 模式不会自动提供用户认证、SSO、TLS 或 CSRF 会话层。即使开启远程监听，也只能将端口放在受信网络或严格防火墙后；面向公网时应继续监听 `127.0.0.1`，由具备 TLS、VPN/SSO 和访问控制的反向代理提供入口。单实例文件锁会拒绝同一数据目录上的第二个后端实例。

## 测试

```bash
uv run --project backend pytest
uv run --project backend ruff check backend/app backend/tests
```

这些测试证明本地契约和故障边界，不证明真实 AgentLoop 云端已经验收。端到端验收必须使用 `gateway_mode=agentloop`、真实 Plan/Dataset/Evaluator、真实在线 Agent，并在 AgentLoop 回读同一个 `recordId` 的结果。
