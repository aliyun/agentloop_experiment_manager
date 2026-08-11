# AgentLoop 离线实验 Launcher 二次开发指南

本文给维护者说明代码边界、扩展点、验证方法和重新打包流程。修改前先阅读根目录 `SPEC.md`、`SDK_CONTRACT.md` 和 `docs/TRACE_CORRELATION.md`。

## 1. 开发环境

- Python 3.12 与 uv；后端依赖以 `backend/uv.lock` 为准。
- Node.js 与 npm；前端依赖以 `frontend/package-lock.json` 为准。
- 可选 Go，仅用于 `sdk/agentloop-go-contract` 对第三方 SDK 快照做静态契约测试，不是生产运行依赖。

初始化和启动：

```bash
./agentloop-launcher
```

一键入口会在缺少前端静态产物时执行锁定依赖安装与构建，再启动同源 FastAPI 服务。前端热更新使用：

```bash
npm --prefix frontend run dev
```

默认 Gateway 为 `disabled`；`fake` 只能用于本地 UI 和测试。真实实验必须使用正式入口固定的 `agentloop` Gateway，且必须在云端独立回读结果。

## 2. 代码地图

| 路径 | 职责 |
|---|---|
| `backend/app/agentloop/cloud_control.py` | 公开 OpenAPI SDK 的 AgentSpace、Plan List/Get 只读控制面 |
| `backend/app/agentloop/offline_runner.py` | Dataset 遍历、唯一 Run 创建、本地 Agent 调用、Trace 屏障和结果上传 |
| `backend/app/agentloop/production.py` | 组合控制面与 Runner 的生产 Gateway |
| `backend/app/services/launches.py` | Launch 状态机、幂等、`recordId` 固化 |
| `backend/app/services/agent_connections.py` | Agent 注册、Token 生命周期和 WebSocket Hub |
| `backend/app/services/managed_code.py` | 可信 Solution Provider 的加密存储、独立进程和单题验证 |
| `backend/app/scheduler/` | 周期计算、Slot 去重与单实例 Worker |
| `backend/app/agentloop/trace_correlation.py` | `agent-trajectory` 唯一 Trace 查询与失败关闭 |
| `backend/app/dashboards/` | 每 Plan 唯一 SLS Dashboard 模板与发布回读 |
| `backend/app/storage/` | SQLite Schema、迁移和 Repository |
| `backend/app/api/` | FastAPI 路由与公开 Schema |
| `frontend/src/lib/api.ts` | 前端 API Client |
| `frontend/src/pages/` | 五个产品页面 |
| `backend/app/agent_connector.py` | 外部客户 Agent 可复用的 WebSocket Connector |

## 3. 不可破坏的系统不变量

1. Plan、Dataset、Evaluator 在本地只读；不要添加 Create/Update/Delete UI 或 API。
2. `AgentLoopOfflineRunner` 是唯一 Experiment Run/Record Owner。控制面不得在 Runner 前创建第二个 Run，`recordId` 不得本地生成。
3. AK/SK、Agent Token 和托管源码不得明文进入 SQLite、读取 API、日志、Trace、命令行或普通环境快照。
4. 每题必须先在 AgentSpace 实际绑定的 `agent-trajectory` 中证明唯一 Trace，再首次写 experiment detail；缺失、歧义、不完整或截断结果都必须 Fail Closed。
5. `ACCEPTED`、`HANDED_OFF`、`completed` 和“评估通过”是不同事实，UI、API 和日志不能混用。
6. 一个 Experiment Plan 只有一个稳定 Dashboard；Run、版本和题目是大盘内维度，不是资源键。
7. 本地不持久化题目、回答、Trace body、trajectory、分数或评估报告。
8. 受管代码进程只是故障隔离，不是恶意代码沙箱；不要扩大为任意不可信代码执行平台。

## 4. 常见扩展方式

### 接入新的客户 Agent

- Web 工作台扩展 `solution_http(Task) -> SolutionOutput` 样例，只处理单个 Dataset item。
- 外部进程复用 `AgentConnector` 并实现 `handle(payload)`；保持协议版本、外层 `taskId` 和错误语义。
- 不要在用户代码中遍历 Dataset、创建 Run、注入版本或调用 `run_experiment_parallel`。

### 增加 AgentLoop 读能力

先扩展 `backend/app/agentloop/gateway.py` 的稳定 Protocol，再在生产与 Fake 实现中同时落地，并通过 Service 暴露。优先使用锁定版本的公开 SDK；不要在生产路径 shell 调用 `aliyun`。任何写能力都必须重新核对产品非目标和最小权限。

### 修改 Trace 关联

保持固定时间窗、分页上限、`Complete + isAccurate + non-limited` 条件、四字段精确匹配和多策略去重。Agent 返回的 `traceId` 只能作为提示，不能绕过同一 Logstore 的验证。新增来源字段时同步更新存储枚举、API 脱敏、测试和 `TRACE_CORRELATION.md`。

### 修改数据库

在 `backend/app/storage/database.py` 增加单向、可审计的 Schema 版本迁移，并覆盖旧版本升级、外键检查、进程中断和重复执行。不要把原始业务载荷加入本地表。涉及生产升级时在部署文档中明确备份与回滚边界。

### 修改前端或 API

先更新后端 Schema 和测试，再更新 `frontend/src/lib/types.ts`、API Client 与页面。必须继续区分空数据、远端错误、权限错误和不完整结果；不要让 Toast 或本地状态替代云端回读。

## 5. 验证矩阵

每次提交至少执行：

```bash
uv run --project backend pytest
uv run --project backend ruff check backend/app backend/tests scripts
npm --prefix frontend test
npm --prefix frontend run typecheck
npm --prefix frontend run build
```

涉及 Go SDK 契约时再执行：

```bash
cd sdk/agentloop-go-contract && go test ./...
```

涉及真实生产链路时，先用 Mock/单元测试验证请求构造和失败边界，避免仅为代码验证创建新的云端实验。最终 E2E 必须使用批准的真实 Plan、在线 Agent 和受控幂等键，并在 AgentLoop/SLS 回读同一 `recordId`；保留 Plan/Run/Trace 的精确证据与时间窗口，但绝不把 AK/SK 写入证据文件。

## 6. 依赖与版本升级

- Python 依赖修改 `backend/pyproject.toml` 后更新并提交 `backend/uv.lock`。
- 前端依赖使用 npm 更新，并提交 `frontend/package-lock.json`。
- 发布版本必须同时更新 `backend/pyproject.toml`、`frontend/package.json` 和 `backend/app/__init__.py`；原生构建会拒绝版本不一致。
- 升级 `agentloop-sdk` 或公开 OpenAPI SDK 时，重点回归 Dataset 查询完整性、Runner-owned Record、上传异常、完成屏障、Trace ID 字段和错误脱敏。
- `third_party/agentloop-go-sdk` 是固定版本的契约快照；更新时同时记录上游 tag/commit，并运行 Go contract test。

## 7. 打包与交付

源码包：

```bash
./scripts/build_source_release.sh
```

原生包：

```bash
./scripts/build_native_release.sh
```

Linux 默认生成 `.tar.xz`。正式交付使用默认 clean build；开发期间可执行
`./scripts/build_native_release.sh --incremental` 复用 PyInstaller 和依赖缓存。公网构建机应通过
`AGENTLOOP_PYPI_MIRROR_BASE` 指定可访问的 PyPI 镜像，并通过
`AGENTLOOP_BUILD_CACHE_DIR` 将 Python、uv 和 PyInstaller 缓存放到持久目录。

源码包只复制白名单路径，并在生成归档前扫描敏感内容；原生包只复制可执行运行时和启动、部署、Agent 接入、Trace、二次开发文档。交付前必须校验 SHA-256、检查归档成员、在干净环境运行测试或 `doctor`，并再次确认归档内没有 `.env`、AK/SK、Master Key、Secret Store、SQLite、日志、缓存、源码映射或本地运行结果。
