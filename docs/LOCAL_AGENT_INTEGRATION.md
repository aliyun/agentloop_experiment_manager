# 本地 Agent 接入

Launcher 支持两种本地 Agent 接入方式，二者的用户代码契约不同：

1. **控制面板 Solution Provider**：用户在 Web 中只编写“执行一个 Dataset item”的
   `solution_http(Task) -> SolutionOutput`；Launcher 管理其受管子进程。
2. **外部 WebSocket Connector**：客户自己的独立 Agent 进程实现 `handle(payload)`，并主动
   连接 Launcher；Launcher 不主动访问客户 Agent 地址。

两种方式在运行时都复用 `backend/app/agent_connector.py` 的 WebSocket Connector，但不能把
两个入口契约混为一份完整实验脚本。`examples/reference_calculator_agent.py` 是外部进程接入
的最小真实示例。

## 1. 选择接入模式并创建 Connection

在 UI 点击“配置新 Agent”后有两种模式：

- **平台托管代码（推荐）**：Token 由平台内部管理。创建后直接进入代码工作台，选择样例、
  修改并点击“保存代码并验证 Agent”即可；不需要复制 Token，也不需要再寻找单独的测试入口。
- **外部 Agent 主动连接**：仅当客户 Agent 是独立进程时使用。该模式才展示一次性 Token、
  WebSocket URL 和接入命令。

也可以通过 API 创建用于外部 Agent 的 Connection：

```http
POST /api/v1/agent-connections
Content-Type: application/json

{"name":"game-agent-staging","description":"customer staging"}
```

创建响应当次返回 `agentConnectionId`、`token`、`connectUrl` 和参考 Agent 的
`connectCommand`。命令不包含 Token；执行后通过终端安全提示粘贴。Token 只展示一次，
列表和详情不会回显。遗失时轮换 Token，旧连接会被断开。

## 2. 在控制面板编写单题 Solution Provider

在 Agent Connections 列表中点击目标连接的“配置并验证代码”，即可在浏览器工作台
编辑该连接的 Python 源码，无需修改后端文件。源码必须定义规范异步入口
`solution_http(Task) -> SolutionOutput`：

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

`response.trace_id` 必须来自已经导出到当前 AgentSpace Trace Store 的真实 Trace，不得随机生成。
Launcher 会验证它；也可以省略，让 Launcher 按 context 反查。

这段代码的输入是外层平台构造的单题 `Task`：`task.input` 来自当前 Dataset row，并由平台
在问题字段末尾注入本次实验的 XML context；`ground_truth`、`tags` 和结构化 `metadata` 也由
平台按 Dataset 与 Launch 上下文提供。用户代码只负责调用一次客户 Agent，并把本题的
`success`、`output`、`trajectory` 和安全 `meta` 包装成 `SolutionOutput`。

以下职责属于外层 Launcher/AgentLoop，不属于 Web 中的 Solution Provider：

- Experiment Plan、Dataset 和 Evaluator 的创建、绑定与选择；
- Dataset 遍历与并发控制；
- Experiment Run 创建、版本解析/生成和 XML context 注入；
- `SolutionOutput` 汇总、上传以及 Evaluator 执行。

因此不要在工作台源码中创建 `AgentLoopConfig`，不要调用 `run_experiment_parallel`，也不要
添加 `main` 或 `asyncio.run(...)` 发起整场实验。Dataset 已由外层云端 Plan 绑定，不在这里
指定。工作台提供三类可直接修改的样例：

- **Session HTTP（推荐）**：先 `POST /v1/sessions` 创建会话，再向
  `/v1/sessions/{session_id}/messages` 发送当前题目；适合当前本地 Agent 接口。
- **单接口 HTTP**：把当前题目直接 POST 到一个固定 `/invoke` 接口。
- **Echo**：不访问外部服务，用于快速验证 Launcher 本身的执行链路。

工作台支持：

- “仅保存”：做语法及 Solution Provider 入口检查，生成新 revision 并加密保存；若旧
  revision 的托管 Agent 正在运行，会先停止它，但不会启动新 revision。
- “保存代码并验证 Agent”：保存后启动或重启独立子进程，等待它通过既有 WebSocket
  Connection 上线，再用测试消息、JSON 参数和合成实验 context 构造一个 `Task`，调用一次
  Solution Provider。它是配置托管 Agent 的主操作，不需要再点击第二个测试按钮。
- “发送测试消息”：对当前已保存且在线的 revision 再次测试；存在未保存草稿时该操作禁用。

源码按 UTF-8 字节数计算，上限 64 KiB；测试超时必须在 1–120 秒之间。页面只展示经过截断
和脱敏的测试输出及允许的安全元信息。源码由 Launcher 的加密 Secret Store 保存，不在
SQLite 中保存明文。

验证成功的托管代码会标记为自动启动；Launcher 重启时会自动恢复该 revision 并重新连接。
如果选择“仅保存，不启动”，该 revision 不会自动恢复，周期实验也不会把它视为在线 Agent。

受管 Agent 在独立的本地子进程中执行，并通过与正式离线 Runner 相同的
`AgentConnector → WebSocket → AgentConnectionHub` 链路收发任务。进程仍以启动 Launcher
的本地用户权限运行，因此这只是故障隔离边界，**不是抵御恶意代码的安全沙箱**；只能粘贴
经过审查的可信代码。工作台测试仅证明当前代码可执行且本地消息链路可收发，不是
AgentLoop 实验、Dataset 批量运行或 trajectory 评估结果，也不能作为游戏质量结论。

## 3. 使用外部 WebSocket Connector（`handle(payload)`）

外部客户 Agent 不粘贴到上述 Web 工作台。它作为独立进程实现传输层
`handle(payload)`，并用 `AgentConnector` 主动连接 Launcher：

```python
import asyncio

from app.agent_connector import AgentConnector, LocalAgentResponse


async def handle(payload: dict) -> LocalAgentResponse:
    # payload["input"] 是原始问题 + 五字段 XML context。
    # payload["originalInput"] 保留完整 Dataset row。
    # payload["context"] 是结构化实验上下文。
    # payload["parameters"] 是 Launch/Schedule JSON 参数。
    output = await call_customer_agent(
        prompt=payload["input"],
        parameters=payload["parameters"],
    )
    return LocalAgentResponse(
        success=True,
        output=output.text,
        trajectory=output.trajectory,
        meta={"traceId": output.trace_id},
    )


asyncio.run(
    AgentConnector(
        connect_url="ws://127.0.0.1:8090/api/v1/agent-connections/<id>/connect",
        token="<read from secret manager>",
        handler=handle,
    ).run()
)
```

生产中从 Secret Manager 读取 Token，不硬编码、不写日志。TLS 部署使用 `wss://`。

## 4. 协议 v1

连接请求：

```text
WS(S) /api/v1/agent-connections/{id}/connect
Authorization: Bearer <token>
```

握手：

```json
{"type":"registered","agentConnectionId":"...","protocolVersion":1}
{"type":"ready"}
```

服务端任务：

```json
{
  "type": "task",
  "taskId": "<transport-id>",
  "payload": {
    "type": "experiment.task",
    "requestId": "<dataset-task-id>",
    "input": "<question>\n<experiment_context ...>...</experiment_context>",
    "originalInput": {},
    "sdkTask": {
      "id": "<dataset-task-id>",
      "input": {},
      "groundTruth": null,
      "tags": [],
      "metadata": {}
    },
    "context": {
      "schema_version": 1,
      "experiment_plan_id": "...",
      "experiment_run_id": "...",
      "experiment_version": "...",
      "dataset_item_id": "...",
      "local_launch_id": "..."
    },
    "parameters": {}
  }
}
```

成功响应必须复用外层 `taskId`：

```json
{"type":"result","taskId":"<transport-id>","success":true,"output":"...","trajectory":[],"meta":{"traceId":"..."}}
```

失败响应：

```json
{"type":"error","taskId":"<transport-id>","error":"sanitized message"}
```

可发送 `{"type":"heartbeat"}`，服务端回复 `{"type":"heartbeat_ack"}`。未知消息类型、错误 Token、重复在线 Connection、超时或断开都会 Fail Closed。

## 5. Reference Calculator

打包版创建 Connection 后，直接复制页面命令：

```bash
./agentloop-launcher reference-agent \
  --url 'ws://127.0.0.1:8090/api/v1/agent-connections/<id>/connect'
```

命令启动后再在终端安全提示中粘贴一次性 Token。源码环境也可运行：

```bash
AGENT_CONNECTION_TOKEN="${AGENT_CONNECTION_TOKEN:?copy from create response}" \
uv run --project backend python examples/reference_calculator_agent.py \
  --url 'ws://127.0.0.1:8090/api/v1/agent-connections/<id>/connect'
```

它真实运行一个独立 Agent 进程，接受 `Compute <expression>`，适合验证 WebSocket、Dataset 遍历、唯一 Run、结果上传和 Evaluator。它不是客户业务 Agent，也不能把本地运行成功冒充云端验收；仍需在 AgentLoop 回读同一个 `recordId` 的结果。

## 6. 客户 Agent 验收清单

- Connection 页面显示 `CONNECTED/HEALTHY`。
- 每道 Dataset item 收到唯一任务，`originalInput`、`context` 和 `parameters` 完整。
- Agent 使用追加 XML 后的 `input`，或显式解析结构化 `context`。
- 返回 JSON 可序列化 `output`，错误信息已脱敏。
- 超时、断连、Agent error 不被包装成成功。
- 真实入口 Span 包含 `agentloop.experiment_run_id`、`agentloop.dataset_item_id`、
  `agentloop.local_launch_id`，并已 flush 到 AgentSpace 绑定的 Trace Store。
- `traceId` 如存在可回传并由 Launcher 验证，但不得携带 AK/SK、Token 或用户敏感数据；
  随机 UUID 不属于 Trace ID。
- AgentLoop 中能回读同一 `recordId` 的结果和 Evaluator 自定义字段。

Trace 查询与一次性 detail 上报规则见 [TRACE_CORRELATION.md](./TRACE_CORRELATION.md)。Launcher 先以
`GetAgentSpace` 解析当前 AgentSpace 的 `slsProject`，再通过 SLS SDK 读取该 Project 的固定
`agent-trajectory` Logstore；不使用 XTrace 或 `ListTracePreview`。关联以 XML 根
`experiment_context` 和 `experiment_plan_id`、`experiment_run_id`、`dataset_item_id`、
`local_launch_id` 四字段为准；`experiment_version` 仍会透传给评估器，但不参与唯一 Trace 匹配。
若字段未被索引，Launcher 会在固定调用时间窗中每页扫描 100 条、最多 5 页，仅在内存解析
`input`/`atif_content` 的 XML 并立即丢弃 payload；不能唯一证明时不会写 experiment detail。
