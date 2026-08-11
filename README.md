# AgentLoop Experiment Manager

AgentLoop Experiment Manager（下文简称 Launcher）运行在客户网络中，用来连接云端无法
直接访问的本地 Agent，执行 AgentLoop 中已经配置好的离线实验计划，并把运行记录、Trace
关联和评估数据回传云端。

AgentLoop 负责维护 Experiment Plan、Dataset、Evaluator 和最终结果；本平台负责本地 Agent
的连接、人工或周期执行，以及实验版本和上下文的传递。

> **安全提示：** 平台可以执行管理员在 Web 工作台保存的 Python Solution Provider，且不
> 内置生产用户认证。只运行经过审查的可信代码，不要把 `8090` 端口直接暴露到公网。

## 核心能力

- 读取 AgentLoop 中已有的 `OFFLINE` Experiment Plan，本地不重复维护实验定义。
- 支持在 Web 中编写单题调用代码，也支持独立 Agent 进程通过 WebSocket 主动连接。
- 人工发起实验，或按每 N 分钟、每天固定时间周期运行。
- 为每次实验固定或自动生成版本号，并向每道题注入实验上下文。
- 从 AgentSpace 绑定的 `agent-trajectory` 验证真实 Trace，再上报实验明细。
- 在 Launch History 查看 `recordId`、Trace ID 和运行状态，并跳转 AgentLoop 结果页。
- 为每个 Experiment Plan 维护一个结果大盘，用于比较多次 Run、版本和题目得分。

## 工作方式

```text
AgentLoop Cloud
  Plan + Dataset + Evaluator
              │
              ▼
Customer Network
  Experiment Manager ──► Local Agent
         │                    │
         │                    └─► agent-trajectory
         │
         └─ Trace correlation + experiment detail
              │
              ▼
AgentLoop Experiment Record + Evaluation + Plan Dashboard
```

本地平台只读取云端 Plan，不创建、编辑或删除 Plan、Dataset 和 Evaluator。完整评估结果仍以
AgentLoop 为准。

## 开始之前

准备以下资源：

1. AgentLoop 中已经创建一个 `OFFLINE` Experiment Plan。
2. Plan 已绑定非空 Dataset，以及至少一个变量映射完整的 Evaluator。
3. 准备可访问该 AgentSpace 的 AK/SK、Region 和 AgentSpace ID。
4. 本地 Agent 可以被 Launcher 调用，或能够主动连接 Launcher 的 WebSocket 地址。
5. Agent 会把包含实验上下文的真实轨迹写入当前 AgentSpace 绑定 Project 的
   `agent-trajectory` Logstore。

AK/SK 对应身份还需要具备 Plan、Dataset、Experiment Run、结果上报、轨迹读取和 SLS
Dashboard 读写所需的最小权限。

## 快速部署

### 从源码启动

这是当前公开仓库可以直接使用的方式。运行需要 Python 3.12 和
[uv](https://docs.astral.sh/uv/)；修改或重新构建前端时还需要 Node.js 和 npm。

```bash
git clone https://github.com/aliyun/agentloop_experiment_manager.git
cd agentloop_experiment_manager

./agentloop-launcher doctor
./agentloop-launcher
```

首次启动会同步锁定的后端依赖，并在缺少前端静态文件时自动安装和构建前端。浏览器随后
打开 `http://127.0.0.1:8090`。不希望自动打开浏览器时运行：

```bash
./agentloop-launcher --no-open-browser
```

### 使用原生发布包

当前公开仓库不附带原生二进制，请优先使用源码启动。如果从内部交付渠道或后续的
GitHub Releases 获得了与目标机器 OS/CPU 匹配的原生包，可以直接运行，不需要安装
Python、Node.js、npm 或 uv：

```bash
# .tar.gz 或 .tar.xz
tar -xf agentloop-launcher-<version>-<platform>.<archive>

# 经公证的 macOS .zip
unzip agentloop-launcher-<version>-<platform>.zip

cd agentloop-launcher-<version>-<platform>

./agentloop-launcher doctor
./agentloop-launcher
```

发布包不能跨操作系统或 CPU 架构复用。完整说明见
[打包版启动说明](./docs/PACKAGED_START.md)。

### 受信私网访问

在受防火墙保护的私网或隔离容器网络中，可以监听非 Loopback 地址：

```bash
mkdir -m 700 /absolute/path/agentloop-launcher-data
./start-agentloop-remote \
  --data-dir /absolute/path/agentloop-launcher-data
```

也可以让最后一级目录不存在，由 Launcher 安全创建。已有目录必须归当前用户所有且权限为
`0700`，不能使用 Home、系统临时目录或符号链接。脚本会生成并复用权限为 `0600` 的
`master.key`，不需要手工配置环境变量。

正式生产环境建议继续监听 `127.0.0.1:8090`，由企业反向代理提供 TLS、SSO、访问控制和
WebSocket Upgrade，并由 Secret Manager 注入 Master Key。`start-agentloop-remote` 不会增加
登录认证或 TLS，不能直接用于公网入口。生产目录、反向代理、备份、升级和回滚要求见
[部署指南](./docs/DEPLOYMENT.md)。

## 完成第一次实验

1. 先在 AgentLoop 云端创建并配置好 `OFFLINE` Experiment Plan。
2. 打开 **Settings → AgentLoop**，填写 Region、AgentSpace 和 AK/SK。先保存，再测试已保存
   的连接。
3. 打开 **Settings → 本地 Agent**，点击“配置新 Agent”。
4. 推荐选择“平台托管代码”，基于 Session HTTP 或单接口 HTTP 模板修改，然后点击
   “保存代码并验证 Agent”。验证成功后，Agent 会显示为在线。
5. 打开 **Plans** 并刷新，选择 Plan、本地 Agent、`MANUAL`/`AUTO` 版本策略和 JSON 参数，
   然后发起实验。
6. 实验成功受理并进入 `ACCEPTED` 后，页面会自动进入 **Launch History**。在这里查看权威
   `recordId`、Trace 关联进度和最终状态，并跳转 AgentLoop 实验结果或当前 Plan 的结果大盘。
7. 需要持续回归时，可以在 Plans 为同一 Plan 创建周期任务，并在 **Schedules** 中启停或
   Run Now。

“保存代码并验证 Agent”只执行一次合成的单题测试，用于确认本地代码和消息链路可用；它
不会遍历正式 Dataset、创建 Experiment Run 或执行云端评估。

## 接入本地 Agent

| 模式 | 适用场景 | 用户实现 |
|---|---|---|
| 平台托管代码（推荐） | 本地 Agent 已提供 HTTP 接口，希望直接在 Web 中配置 | `solution_http(Task) -> SolutionOutput` |
| 外部 WebSocket Connector | Agent 是独立服务或需要自行管理进程、依赖和生命周期 | `handle(payload)` 并主动连接 Launcher |

平台托管代码只负责执行一个 Dataset item。最小结构如下；把 `call_customer_agent` 替换成
客户 Agent 的真实调用即可：

```python
from agentloop_sdk import SolutionOutput, Task


async def solution_http(task: Task, *args, **kwargs) -> SolutionOutput:
    question = str(task.input.get("question") or task.input.get("input") or "")
    response = await call_customer_agent(question)
    return SolutionOutput(
        success=True,
        output=response.text,
        trajectory=response.trajectory,
        meta={"traceId": response.trace_id},
    )
```

这段代码只负责调用一次 Agent；实验编排、上下文注入、Trace 验证和结果上报都由平台完成。
不要在其中发起整场实验，也不要随机生成 Trace ID。

两种接入方式、HTTP 模板、WebSocket 协议和参考 Agent 见
[本地 Agent 接入指南](./docs/LOCAL_AGENT_INTEGRATION.md)。

## 版本、定时任务与结果

- `MANUAL`：人工指定可识别的 Agent/模型版本，例如 `1.2.3` 或 `prompt-v5`。
- `AUTO`：平台为每次 Launch 生成唯一版本；同一次实验的所有题目使用同一版本。
- 实验参数以 JSON 保存，并同时用于人工实验、Run Now 和周期实验。
- Schedule 支持每 N 分钟（最小 5 分钟）或每天固定时间运行；只有 Launcher 持续运行时才会触发。
- 本地只保留 Launch、Schedule、Agent Connection、Trace 关联和大盘同步等控制元数据。
- 分数、逐题评估和完整轨迹在 AgentLoop/SLS 中查看，不复制到本地数据库。

`ACCEPTED` 只表示已经取得 AgentLoop `recordId`，`HANDED_OFF` 只表示本地执行和上报职责
结束；二者都不代表评估通过。

## 二次开发

### 开发环境

- Python 3.12 与 uv：后端和 Launcher。
- Node.js 与 npm：React 前端开发和生产构建。
- Go（可选）：只用于 AgentLoop Go SDK 契约测试。

普通源码运行仍使用：

```bash
./agentloop-launcher
```

前端热更新需要同时运行后端和 Vite：

```bash
# 终端 1
./agentloop-launcher --no-open-browser

# 终端 2
npm --prefix frontend run dev
```

访问 `http://127.0.0.1:4173`，Vite 会把 `/api` 请求代理到 `127.0.0.1:8090`。

### 代码地图

| 路径 | 主要职责 |
|---|---|
| `backend/app/agentloop/` | AgentLoop Plan 读取、离线 Runner 和 Trace 关联 |
| `backend/app/services/` | Launch、Schedule、Agent Connection 和大盘业务逻辑 |
| `backend/app/storage/` | SQLite Schema、迁移和 Repository |
| `backend/app/api/` | FastAPI 路由和 API Schema |
| `frontend/src/` | React 管理界面和 API Client |
| `examples/` | 外部 Agent 参考实现 |
| `scripts/` | 源码包、原生包和受控 E2E 工具 |

### 验证

```bash
uv run --project backend pytest
uv run --project backend ruff check backend/app backend/tests scripts
npm --prefix frontend test
npm --prefix frontend run typecheck
npm --prefix frontend run build
```

涉及 Go SDK 契约时再执行：

```bash
cd sdk/agentloop-go-contract
go test ./...
```

构建脱敏源码包和当前 OS/CPU 的原生包：

```bash
./scripts/build_source_release.sh
./scripts/build_native_release.sh
```

修改前请先阅读[二次开发指南](./docs/SECONDARY_DEVELOPMENT.md)。其中包含完整代码地图、
扩展方式、不可破坏的系统不变量、依赖升级和交付检查清单。

## 安全与运行边界

- 默认只监听 `127.0.0.1`；非 Loopback 部署必须有可信网络或企业入口保护。
- AK/SK、Agent Token 和托管源码保存在加密 Secret Store，不进入 SQLite 明文字段。
- 本地不持久化 Dataset 题目、Agent 回答、Trace body、trajectory、分数或评估报告。
- `master.key` 与 Data 目录必须一起备份；一个 Data 目录只能运行一个 Launcher 实例。
- 托管 Python 进程仍具有启动用户的本机权限，它是故障隔离边界，不是恶意代码沙箱。

## 常见问题

**Plans 页面看不到实验计划**

确认 Settings 指向 Plan 所在的 Region 和 AgentSpace，AK/SK 有读取权限，并清空 Plans 页的
名称和状态筛选后刷新。Plan 即使可见，也只有在类型为 `OFFLINE` 且 Dataset/Evaluator 配置
完整时才能发起。

**本地 Agent 测试失败**

先确认 Agent HTTP 地址可以从 Launcher 所在机器访问，并检查 Session 创建、消息请求和
返回值解析是否与模板一致。外部 Agent 还需要检查 WebSocket Upgrade 和 Token 是否有效。

**Agent 已返回，但 History 一直等待 Trace**

检查入口轨迹是否包含本次实验上下文、是否已经 flush 到当前 AgentSpace 绑定的
`agent-trajectory`，以及 Launcher 身份是否有查询权限。详细规则见
[Trace 关联说明](./docs/TRACE_CORRELATION.md)。

## 文档导航

| 文档 | 适合读者 |
|---|---|
| [打包版启动说明](./docs/PACKAGED_START.md) | 获得原生包后快速启动和配置 |
| [部署指南](./docs/DEPLOYMENT.md) | 生产部署、反向代理、备份、升级和回滚 |
| [本地 Agent 接入](./docs/LOCAL_AGENT_INTEGRATION.md) | 接入 HTTP Agent 或外部 WebSocket Agent |
| [Trace 关联](./docs/TRACE_CORRELATION.md) | 排查 Trace 查询、唯一性验证和上报问题 |
| [二次开发指南](./docs/SECONDARY_DEVELOPMENT.md) | 扩展代码、运行测试和重新打包 |
| [产品与技术规格](./SPEC.md) | API、数据模型、状态机和验收契约 |
| [SDK 契约](./SDK_CONTRACT.md) | SDK 版本、能力边界和升级 Gate |
