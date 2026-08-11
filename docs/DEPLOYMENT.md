# AgentLoop 离线实验 Launcher 部署指南

本文面向后续客户侧部署与运维。默认推荐使用目标机器同 OS/CPU 架构构建的原生包；源码包主要用于审计、二次开发和在目标平台重新制包。

## 1. 部署边界

- Launcher 默认只监听 Loopback。公网访问必须经企业反向代理，并由代理提供 TLS、SSO/鉴权、访问控制和审计；受信私网或隔离容器网络可使用显式远程监听开关。
- 单个数据目录只能运行一个 Launcher 实例和一个 Scheduler，当前版本不支持多副本共享 SQLite。
- 云端 Experiment Plan、Dataset、Evaluator 由 AgentLoop 管理；客户侧 Runner 读取已有 `OFFLINE` Plan、调用本地 Agent，并创建唯一 Experiment Run。
- `ACCEPTED` 只表示已取得 `recordId`，`HANDED_OFF` 只表示本地执行与上报职责结束；两者都不等于评估通过。
- AK/SK 只能通过 Settings 写入加密 Secret Store，禁止写入代码包、镜像层、启动参数、日志或普通配置文件。

## 2. 生产前置条件

1. AgentLoop 已有可运行的 `OFFLINE` Plan，并绑定非空 Dataset 和至少一个变量映射完整的 Evaluator。
2. 部署身份具备 AgentSpace、Plan、Dataset、Experiment Run、结果上传，以及 AgentSpace 绑定 SLS Project 中 `agent-trajectory` 和 Dashboard 的最小权限。
3. 客户主机可出站访问 AgentLoop Endpoint 和对应 SLS Endpoint；客户 Agent 能连接 Launcher 的 WS/WSS 入口。
4. 客户 Agent 将五字段 experiment context 写入真实入口轨迹并 flush 到当前 AgentSpace 绑定的 `agent-trajectory` Logstore。
5. 准备专用非特权系统用户、权限为 `0700` 的持久数据目录，以及由 Secret Manager 生成并注入的 Fernet Master Key。

## 3. 构建与完整性校验

在与目标部署机相同的 OS/CPU 架构上安装 Python 3.12、uv、Node.js 和 npm，然后执行：

```bash
./scripts/build_native_release.sh
```

Linux AMD64 必须在 Linux AMD64 构建，不能复用 macOS 包。产物位于 `release/`。复制到部署机后先校验：

```bash
sha256sum -c agentloop-launcher-<version>-linux-x86_64.tar.xz.sha256
tar -xJf agentloop-launcher-<version>-linux-x86_64.tar.xz
cd agentloop-launcher-<version>-<platform>
./agentloop-launcher doctor
```

Linux 默认使用并行 xz 压缩，并在归档前将内容相同的原生库转换为硬链接。需要兼容旧交付流程时可使用
`--archive-format tar.gz`。正式发布默认执行 clean build；本地反复验证可使用 `--incremental` 复用
PyInstaller 工作目录。构建机访问 PyPI 较慢时，可配置：

```bash
AGENTLOOP_PYPI_MIRROR_BASE=https://mirrors.aliyun.com/pypi \
AGENTLOOP_BUILD_CACHE_DIR=/var/cache/agentloop-launcher-build \
./scripts/build_native_release.sh
```

源码交付包使用：

```bash
./scripts/build_source_release.sh
```

该脚本以白名单复制源码，并拒绝 AK、高熵凭据赋值、私钥、`.env`、Master Key、加密 Secret、SQLite、日志、缓存、虚拟环境、`node_modules`、构建产物和嵌套 `.git`。

## 4. 单机生产部署

建议目录：

```text
/opt/agentloop-launcher/<version>/   只读程序目录
/var/lib/agentloop-launcher/         持久数据，0700，归专用用户所有
```

首次启动前从客户 Secret Manager 注入 `AGENTLOOP_LAUNCHER_MASTER_KEY`。不要把真实值写入本文、源码包或工单。启动命令示例：

```bash
AGENTLOOP_LAUNCHER_ENVIRONMENT=production \
AGENTLOOP_LAUNCHER_DATA_DIR=/var/lib/agentloop-launcher \
AGENTLOOP_LAUNCHER_MASTER_KEY='<injected-at-runtime>' \
./agentloop-launcher --no-open-browser
```

程序固定使用真实 `agentloop` Gateway 并默认启用 Scheduler。数据目录已存在时必须由当前用户拥有且权限为 `0700`；禁止使用 `/`、Home、系统临时目录、符号链接或共享目录。

systemd 可将上述非敏感变量放入服务单元，并由客户现有 Secret Manager 在启动时注入 Master Key。服务应设置：专用 `User`、只读程序目录、可写数据目录、`Restart=on-failure`，以及 `ExecStart=.../agentloop-launcher --no-open-browser`。不要在单元文件中放 AK/SK。

## 5. 网络入口

Launcher 后端继续监听 `127.0.0.1:8090`。同机反向代理把企业 HTTPS 入口转发到该地址，并同时支持：

- 普通 HTTP 与前端静态资源；
- `/api/v1/agent-connections/{id}/connect` 的 WebSocket Upgrade；
- 企业 SSO/鉴权、来源限制、TLS 和访问日志脱敏。

外部 Agent 使用代理提供的 `wss://` 地址连接，一次性 Token 从 Secret Manager 读取，不进入命令行历史。管理 UI/API 应经过企业 SSO；Agent WebSocket 路径保留 Launcher 自身的 Bearer Token 校验，代理不得覆盖 `Authorization`，且必须正确转发 WebSocket Upgrade。

若反向代理位于另一容器/主机、Loopback 无法互通，可在受控私有网络启用直接监听：

```bash
./start-agentloop-remote
```

该脚本使用显式的单机本地密钥模式：首次在 Data 目录安全生成 `master.key`，后续自动复用，不需要设置环境变量。指定数据目录可使用 `./start-agentloop-remote --data-dir /absolute/path`。正式多环境部署仍应使用前述 Secret Manager 注入方式。

非 Loopback 地址只有在显式允许远程访问、Production 模式和可用 Master Key 同时满足时才会启动。该开关不提供认证或 TLS；必须用防火墙、安全组或容器网络阻止不可信来源直接访问 `8090`，不得将其暴露到公网。

## 6. 首次上线验收

1. `./agentloop-launcher doctor` 通过，`GET /api/v1/health` 返回健康。
2. Settings 保存 Region、AgentSpace、Endpoint、Console Base URL 和 AK/SK 后执行 Test Connection。
3. Agent Connection 显示 `CONNECTED/HEALTHY`。
4. 选择一个已批准的真实 `OFFLINE` Plan 发起一次受控实验。
5. Launch History 获得唯一 `recordId`，每题 Trace 关联达到 `REPORTED`，Runner 最终状态与预期一致。
6. 在 AgentLoop/SLS 独立回读同一 `recordId` 的逐题结果、Evaluator 输出和 Plan 大盘。仅本地 HTTP 200、Toast 或 `HANDED_OFF` 不构成云端验收。

## 7. 备份、升级与回滚

- 备份前停止服务，完整备份数据目录；若 Master Key 由外部 Secret Manager 托管，必须独立备份对应版本和恢复权限。只恢复 `secrets.enc` 而缺少原 Master Key 会导致启动失败。
- 升级前在测试环境执行后端测试、前端测试、原生包 `doctor` 和目标架构冒烟；生产升级前保存数据目录快照。
- 新版本部署到新的只读版本目录，复用原数据目录；启动后检查健康、Profile 连接、Schedule 和 Launch History，不自动补跑中断的旧 Slot。
- 数据库迁移不保证向后兼容。需要回滚时同时恢复升级前程序目录与数据目录快照，不能只替换二进制。

## 8. 常见故障定位

- 启动拒绝共享数据目录：修正专用目录的 owner 和 `0700` 权限，不要让程序自动放宽权限。
- `master.key is missing`：恢复原 Master Key；不得生成新 Key 覆盖已有 `secrets.enc`。
- Agent 离线：检查反向代理 WebSocket Upgrade、Token 是否轮换、客户 Agent 是否重连。
- Trace 超时或歧义：核对 AgentSpace 绑定 Project、`agent-trajectory`、五字段 context、索引完整性和 exporter flush；不得跳过唯一性验证强行写 detail。
- 进程中断：遗留 Run 会收口为 `UNKNOWN/INTERRUPTED`，不会自动 Resume 或重跑；先在 AgentLoop 核对远端状态，再决定是否以新的幂等键发起新 Run。
