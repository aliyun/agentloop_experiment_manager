# AgentLoop Local Experiment Launcher

部署在客户环境中的 AgentLoop 离线实验发起平台。AgentLoop 云端维护 Experiment Plan、Dataset、Evaluator 和实验结果；本项目位于客户网络内，负责把云端 Plan 交给本地 Agent 真实执行。

> **安全提示：** 平台可执行管理员在 Web 工作台保存的可信 Python Solution Provider，
> 且不内置生产用户认证。不要把 `8090` 端口直接暴露到公网；远程模式只能部署在受防火墙、
> VPN 或带 TLS/SSO 的反向代理保护的可信网络中。

产品边界很明确：

- 只读 List/Get 云端已有 Experiment Plan，不在本地创建或修改 Plan。
- 人工发起一次实验，或按每 N 分钟（最小 5 分钟）/每天的规则周期发起。
- 管理由本地 Agent 主动建立的 WebSocket Connection；也可在控制面板编辑单条 Dataset
  item 的可信 Python Solution Provider，启动受管进程并立即做单题测试。
- 为每次 Launch 固化 Agent、版本、JSON 参数和五字段 XML context。
- 保存本地 Launch History，并用 AgentLoop 返回的 `recordId` 生成结果深链。
- 每次有效 Launch 前按 `Experiment Plan ID` 幂等创建或更新一个云端 SLS 大盘；同一 Plan
  的人工、Run Now 和周期 Run 永远复用该大盘，Run/版本/题目只作为大盘内筛选与对比维度。
- 在每题上报前通过 `GetAgentSpace.slsProject` 从固定 `agent-trajectory` 解析并验证唯一
  `traceId`；本地 Launch History 展示已验证的 Trace ID、关联状态与计数，但不复制题目、回答、
  Trace body、trajectory、分数或报告；
  全部题目首次 detail 写入成功前，Experiment Run 不会标记为 `completed`。

## 最简单启动：原生发布包

macOS ARM64 发布包已把 Python 3.12、全部后端依赖和生产前端合并为一个目录。目标机器
不需要安装 Python、Node.js、npm 或 uv，也不需要分别启动前后端：

```bash
tar -xzf agentloop-launcher-0.1.0-darwin-arm64.tar.gz
cd agentloop-launcher-0.1.0-darwin-arm64
./agentloop-launcher
```

浏览器会自动打开 `http://127.0.0.1:8090`。首次启动自动创建稳定的用户数据目录和权限为
`0600` 的本地 Master Key；AK/SK、Region 和 AgentSpace 仍在网页 Settings 配置。

仅做完整性检查而不启动服务：

```bash
./agentloop-launcher doctor
```

按 `Ctrl+C` 优雅停止。发布包的完整说明见
[打包版启动说明](./docs/PACKAGED_START.md)，当前实包与验收证据见
[打包验收记录](./docs/PACKAGING_ACCEPTANCE_20260808.md)。

当前 `.tar.gz` 是制包机可运行的内部验收构建，未使用 Apple Developer ID 公证，不能直接
作为正式客户外发包。正式 macOS 外发需按启动说明使用企业证书与 notary profile 生成并
在干净 Mac 上复验；这不会影响源码模式或当前本机直接运行。

完整产品契约见 [SPEC.md](./SPEC.md)，SDK 与唯一 Record Owner 结论见 [SDK_CONTRACT.md](./SDK_CONTRACT.md)，
本地 Agent 接入见 [docs/LOCAL_AGENT_INTEGRATION.md](./docs/LOCAL_AGENT_INTEGRATION.md)，Trace
延迟关联与一次性上报见 [docs/TRACE_CORRELATION.md](./docs/TRACE_CORRELATION.md)。后续生产落地见
[部署指南](./docs/DEPLOYMENT.md)，代码扩展、验证与重新制包见
[二次开发指南](./docs/SECONDARY_DEVELOPMENT.md)。

## 已实现架构

```text
Browser
  ├─ Settings / Plans / Agent Connections / Schedules / Launch History
  └─ FastAPI API
       ├─ AgentLoopCloudControl
       │    └─ alibabacloud-agentloop20260520==2.3.1（Plan List/Get）
       ├─ AgentLoopOfflineRunner
       │    ├─ agentloop-sdk==1.0.3（Dataset 遍历、创建唯一 Run、结果上传）
       │    └─ GetAgentSpace.slsProject + aliyun-log-python-sdk
       │       （固定 agent-trajectory 查询、一次性 detail 上报屏障）
       ├─ AgentConnectionHub
       │    ├─ 客户 Agent 主动 WebSocket 连接
       │    └─ 控制面板代码启动的独立本地 Agent 子进程（复用同一 WebSocket 协议）
       ├─ Scheduler
       ├─ PlanDashboardPublisher
       │    └─ GetAgentSpace.slsProject + SLS Dashboard Get/Create/Update/Readback
       └─ SQLite（Launch + 无载荷 Trace/Plan 大盘同步状态）/ encrypted Secret Store
```

生产模式使用 `gateway_mode=agentloop`，已经接入公开 Python OpenAPI SDK 和真实离线 Runner，不再是“等待 Go Adapter”的 Fail Closed 占位。运行链路采用 **Runner-owned Record**：`agentloop-sdk` 创建且只创建一次 Experiment Run，并立即把权威 `recordId` 回传给 Launch Service；控制面绝不预先再调用一次 `CreateExperimentRun`。

`fake` 模式只用于离线 UI/单元测试，不能作为真实验收证据。

## 打包版前置条件

1. 操作系统与发布包文件名中的 OS/CPU 架构一致；当前提供 macOS ARM64 包。
2. AgentLoop 已存在可运行的 `OFFLINE` Experiment Plan，并绑定非空 Dataset 和 Evaluator。
3. 客户主机可以访问 AgentLoop Endpoint，本地 Agent 可以访问 Launcher 的 WebSocket 地址。
4. 有 AgentLoop AK/SK、Region、AgentSpace；AK/SK 通过 Settings 保存到加密 Secret Store。
   该身份还需具备 AgentSpace 绑定 Project 的 SLS Dashboard 读取、创建和更新权限；不需要删除权限。
5. 客户 Agent 已把 experiment context 写入真实入口 Span，并把轨迹导出到该 AgentSpace
   绑定 Project 的 `agent-trajectory` Logstore；随机生成一个 `traceId` 不满足要求。
   Launcher 先调用 GetAgentSpace 取得 `slsProject`，再只读取该 Project 的
   `agent-trajectory` Logstore。若 XML 未被索引，会在调用时间窗内以每页 100 条、最多 5 页扫描，
   仅在内存解析 `input`/`atif_content` 的 context；超过边界即失败，不会猜测关联。Agent 返回后
   默认等待轨迹可见 `180` 秒，可通过 `AGENTLOOP_LAUNCHER_TRACE_LOOKUP_TIMEOUT_SECONDS` 调整为
   `61`–`1800` 秒；修改环境变量后需重启 Launcher 才会生效。

## 源码模式一键启动（开发者）

源码开发环境仍需 Python 3.12、Node.js/npm 和 uv，但启动也已收敛为一个命令；首次运行会
自动构建前端和同步锁定依赖：

```bash
./agentloop-launcher
```

如需前端热更新，可使用原有双进程开发方式：

```bash
npm --prefix frontend run dev
```

热更新地址为 `http://127.0.0.1:4173`；正常一键启动地址为
`http://127.0.0.1:8090`。打开页面后按顺序：

1. 在 Settings 配置 Region、AgentSpace、AK/SK、Console Base URL，并执行 Test Connection。
2. 在 Agent Connections 点击“配置新 Agent”。默认选择“平台托管代码”，创建后直接进入
   代码工作台；只有独立部署并主动连接 Launcher 的外部 Agent 才需要生成和保存 Token。
3. 在工作台选择 Session HTTP、单接口 HTTP 或 Echo 样例，基于样例修改后点击
   “保存代码并验证 Agent”。平台会在一次操作中保存、启动并发送测试消息；验证成功后
   Connection 变为 `CONNECTED`。已启用的托管代码会在 Launcher 重启后自动恢复。
4. 在 Plans 选择可运行 Plan，指定 Agent、`MANUAL`/`AUTO` 版本和 JSON 参数后发起。
5. 在 Launch History 确认 `recordId`、Trace 关联达到 `REPORTED`、最终版本和时间；可分别
   跳转 Experiment Record 和当前 Plan 专属大盘查看 Trace、trajectory、跨 Run 趋势与逐题评分。

## 每个 Experiment Plan 一个大盘

大盘不是每次实验 Run 创建一个。资源键由 `AgentSpace + Plan ID` 稳定派生，Plan 改名只更新
显示名，不改变大盘名。人工 Launch、Schedule Run Now 和周期触发都在调用客户 Agent 前执行：

```text
GetAgentSpace → slsProject → GetDashboard
  ├─ 404: CreateDashboard
  └─ exists: UpdateDashboard
→ GetDashboard readback
→ 继续发起本次 Experiment Run
```

每条图表查询都固定包含目标 Plan ID 条件，不能在大盘中切换到其他 Plan。大盘保留 Run、实验
版本、题目 ID、Evaluator、Evaluator 版本、Rubric 和时间粒度筛选，用于在同一 Plan 内比较多次
运行。筛选器压缩在一行；逐题趋势按题目 ID 分类，每题一条线；逐题平均分使用低分优先的横向
柱状图，便于直接定位薄弱题目。失败记录不作为 0 分混入均值。同步失败只把本地
`plan_dashboards` 状态记为 `FAILED`，不会把客户 Agent 实验误判为失败；下一次触发该 Plan 会
自动重试并修复模板或被外部删除的大盘。

人工 Launch 与 Schedule 使用同一执行配置：

```json
{
  "agentConnectionId": "<local-connection-id>",
  "versionMode": "MANUAL",
  "experimentVersion": "1.2.3",
  "experimentParameters": {
    "temperature": 0.2
  },
  "contextSchemaVersion": 1
}
```

`AUTO` 模式必须省略 `experimentVersion`；平台为每次 Launch 生成一次版本并持久化，同一次 Launch 的所有 Dataset item 使用同一个版本。

## 在控制面板配置与测试本地 Agent

在 Agent Connections 的目标连接上点击“配置并验证代码”，可直接编辑该连接使用的
可信 Python。这里填写的不是完整实验脚本，而是**每个 Dataset item 的 Solution
Provider**：平台把当前 item 构造成 `Task` 后调用它，代码只负责调用一次客户 Agent 并返回
`SolutionOutput`。

代码必须定义约定的异步入口 `solution_http(Task) -> SolutionOutput`；例如：

```python
from agentloop_sdk import SolutionOutput, Task


async def solution_http(task: Task, *args, **kwargs) -> SolutionOutput:
    question = str(task.input.get("question") or task.input.get("input") or "")
    response = await call_customer_agent(question)
    return SolutionOutput(
        success=True,
        output=response.text,
        trajectory=response.trajectory,
        # 可回传真实 Trace ID 作为快速路径；Launcher 仍会到 Trace Store 验证。
        meta={"traceId": response.trace_id},
    )
```

Experiment Plan、Dataset 绑定、Dataset 遍历、Experiment Run 创建、并发、版本生成、XML
context 注入、Trace 验证/反查、结果上传和 Evaluator 执行都由外层平台负责。不要在工作台代码中创建
`AgentLoopConfig`，不要调用 `run_experiment_parallel`，也不要添加 `main` 或
`asyncio.run(...)` 之类的整场实验启动逻辑。Dataset 在云端 Plan 外层绑定，不由这段代码
指定。

工作台提供三种操作：

- “仅保存”会校验并加密保存新 revision；若旧 revision 的托管 Agent 正在运行，会先停止它，
  避免页面显示新 revision、实际仍执行旧代码，但不会启动新 revision。
- “保存代码并验证 Agent”会保存代码、启动或重启独立本地子进程，待其通过 WebSocket
  上线后，构造一个合成 `Task` 并调用一次 Solution Provider。
- 代码已保存且 Agent 在线时，可修改消息、JSON 参数及超时后再次点击“发送测试消息”。有
  未保存草稿时不能测试，避免误测旧 revision。

源码按 UTF-8 计不得超过 64 KiB；单次测试超时可设为 1–120 秒。源码保存在加密 Secret
Store 中，不写入 SQLite 明文。受管 Agent 虽在独立子进程中运行，但仍继承当前本地用户的
权限，**不是隔离恶意代码的安全沙箱**，只能运行已审查的可信代码。该子进程与正式离线
Runner 复用同一套 `AgentConnector → WebSocket → AgentConnectionHub` 链路；工作台返回的
只是本地代码执行与连通性结果，不是 AgentLoop 评估结果，也不能据此判断游戏质量。

外部客户 Agent 是另一种接入模式：由独立进程实现 `handle(payload)` 并主动连接
WebSocket，不使用上述 `solution_http(Task)` 的浏览器源码契约。完整协议、payload 和部署边界见
[本地 Agent 接入说明](./docs/LOCAL_AGENT_INTEGRATION.md)。

## Reference Calculator Agent

仓库提供一个确定性计算器 Agent，用于验证真实 WebSocket 链路。它不是 Mock：它作为独立
Agent 进程主动连接平台，并真实处理 `Compute <expression>` 题目；用于云端 Experiment 时
还必须配置真实 OTel exporter 并产生符合
[Trace 关联契约](./docs/TRACE_CORRELATION.md) 的入口 Trace。

只有选择“外部 Agent 主动连接”时才需要 Token。创建 Agent Connection 后，可直接使用页面
返回的打包版命令。它会在终端安全提示中读取
一次性 Token，Token 不进入命令行或 shell history：

```bash
./agentloop-launcher reference-agent \
  --url 'ws://127.0.0.1:8090/api/v1/agent-connections/<connection-id>/connect'
```

源码环境也可继续执行：

```bash
AGENT_CONNECTION_TOKEN="${AGENT_CONNECTION_TOKEN:?copy from create/rotate response}" \
uv run --project backend python examples/reference_calculator_agent.py \
  --url 'ws://127.0.0.1:8090/api/v1/agent-connections/<connection-id>/connect'
```

Dataset 的问题应形如 `Compute 2 + 3. Return only the number.`。替换成客户 Agent 时复用 `backend/app/agent_connector.py` 的 `AgentConnector`，只替换业务 `handler`；协议见 [本地 Agent 接入说明](./docs/LOCAL_AGENT_INTEGRATION.md)。

## 安全与部署边界

- 默认只监听 `127.0.0.1`。Development 模式拒绝非 Loopback 请求并校验写请求 Origin。
- 直接监听受信私有网络可运行 `./start-agentloop-remote`。脚本自动生成/复用 Data 目录中权限为 `0600` 的 `master.key`，不需要手工设置环境变量；也可以继续使用企业 Secret Manager 注入的 Master Key。
- 本项目不内置生产用户认证。直接远程监听只适合受防火墙保护的私网或隔离容器网络；公网入口必须使用客户现有 TLS、VPN/反向代理和 SSO，不能直接开放 `8090`。
- AK/SK 与 Agent Token 只进入 Secret Store。SQLite 只保存 Secret 引用/脱敏提示、本地
  Schedule/Launch/Agent Connection 元数据，以及无题目/回答/Trace body 的关联状态。
- Token 只在创建/轮换响应中显示一次；轮换会断开旧连接。
- 单部署只运行一个后端实例和一个 Scheduler。文件锁、Schedule revision、Slot 唯一键及 `Idempotency-Key` 防止本地重复触发。
- `ACCEPTED` 只表示获得并保存了 AgentLoop `recordId`；`HANDED_OFF` 只表示本地 Runner 完成执行/上报职责，都不代表评估通过。

## 验证

```bash
uv run --project backend pytest
uv run --project backend ruff check backend/app backend/tests

cd frontend
npm test
npm run typecheck
npm run build
```

构建当前操作系统/CPU 架构的自包含原生包：

```bash
./scripts/build_native_release.sh
```

构建用于代码审计和二次开发的脱敏源码包：

```bash
./scripts/build_source_release.sh
```

源码制包采用路径白名单并执行归档前敏感扫描，拒绝 AK、高熵凭据赋值、私钥、`.env`、
Master Key、SQLite、加密 Secret、日志、缓存、虚拟环境、`node_modules`、构建目录和嵌套 `.git`。

产物写入 `release/`。构建程序仅复制 PyInstaller 生成的可执行文件、`_internal` 运行目录和
三份启动/Trace 契约文档，并拒绝 `.env`、Master Key、SQLite、加密 Secret、source map 或 Python 缓存
进入发布包。原生包按平台构建，不跨 OS/CPU 复用；Linux AMD64 包必须在 Linux AMD64
制包机上生成并执行同一验收。

`--data-dir` 默认无需指定。显式指定时，最后一级应不存在并由程序创建，或必须是当前用户
所有且权限 `0700` 的专用目录；`/`、Home、系统 temp、symlink 和普通共享目录会被拒绝，
且程序不会擅自修改现有目录权限。

### 可重复的真实本地验收编排

[`scripts/real_e2e_acceptance.py`](./scripts/real_e2e_acceptance.py) 用于把已存在的
`OFFLINE` Plan 交给真实 Reference Agent 执行。脚本不会创建 Dataset、Evaluator 或
Plan；默认也不会发请求。它只在同时提供 `--execute` 和精确确认短语后，才会更新
本地 Profile、创建 Agent Connection 并创建一次云端 Experiment Run。

先对完整参数做预览：

```bash
uv run --project backend python scripts/real_e2e_acceptance.py \
  --launcher-url http://127.0.0.1:8090 \
  --region cn-hangzhou \
  --agent-space '<agent-space>' \
  --plan-id '<existing-offline-plan-id>' \
  --credentials-env-file ../.env \
  --version-mode MANUAL \
  --experiment-version acceptance-20260808-001 \
  --idempotency-key acceptance-20260808-001 \
  --output ./var/acceptance-20260808-001.json
```

确认预览中的 AgentSpace、Plan、版本、幂等键和成本边界后，使用完全相同的参数并追加：

```bash
  --execute --confirm RUN_REAL_EXPERIMENT
```

默认凭据文件支持 `access_id`/`access_key`，也支持标准 Alibaba Cloud 环境变量键名。
凭据和一次性 Agent Token 只在进程内存及子进程环境中传递，不进入命令行、控制台或
验收证据文件。如果运行中的 Launcher 已有可用的加密凭据，可改用
`--reuse-profile-credentials`。

脚本依次强校验：Launcher 为 `gatewayMode=agentloop`、Profile 连接成功、Plan 为绑定
Dataset 和 Evaluator 的可运行 `OFFLINE` Plan、Reference Agent 已真实连接、Launch 最终
为 `ACCEPTED/HANDED_OFF` 且存在稳定的权威 `recordId`。它会终止自己启动的 Agent
进程，但保留 Agent Connection 和 Launch History 以便审计。

`HANDED_OFF` 只证明客户侧 Runner 已执行并完成上传，不等于评估通过。完整云端验收仍需
用输出的 `recordId` 在 AgentLoop 回读 Experiment Run、评估任务和逐题结果。

真实云端验收不能由 Fake 测试替代。项目已于 2026-08-08 在
`cn-hangzhou / <agent-space>`
完成一次真实端到端验收，详见
[真实验收记录](./docs/REAL_E2E_ACCEPTANCE_20260808.md)。后续每次验收仍必须至少保留以下证据：

- 真实 `OFFLINE` Plan ID、Dataset ID、Evaluator 绑定；
- 本地 Agent Connection 在线；
- 单次 Launch 的本地 Launch ID、最终版本和唯一 `recordId`；
- AgentLoop 侧同一 `recordId` 的实验记录与结果页可访问；
- Dataset item 确实到达本地 Agent，结果和评估器自定义字段可在 AgentLoop 中回读；
- 没有第二个由控制面重复创建的 Experiment Run。

## 目录

```text
backend/                         FastAPI、真实 AgentLoop 适配、Runner、调度和 SQLite
frontend/                        React/Vite 管理界面
agentloop-launcher               源码模式一键入口
examples/reference_calculator_agent.py
                                 真实 Connector 参考 Agent
docs/LOCAL_AGENT_INTEGRATION.md  本地 Agent WebSocket 接入协议
SDK_CONTRACT.md                  SDK 版本与 Runner-owned Record 契约
SPEC.md                          产品、接口、数据与验收规格
third_party/agentloop-go-sdk/    历史 Go SDK 契约快照；不属于生产执行依赖
scripts/build_native_release.*   平台原生发布包构建器
```
