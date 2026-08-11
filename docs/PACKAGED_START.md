# AgentLoop Launcher 打包版启动说明

## 启动

在解压目录中运行：

```bash
./agentloop-launcher
```

浏览器会自动打开 `http://127.0.0.1:8090`。首次启动会创建稳定的用户数据目录和
`master.key`；页面中的 AK/SK 会加密保存，重新启动不需要重复配置。

仅检查包是否完整、不启动服务：

```bash
./agentloop-launcher --doctor
```

不自动打开浏览器：

```bash
./agentloop-launcher --no-open-browser
```

按 `Ctrl+C` 会优雅停止服务。周期实验只会在服务持续运行时触发。
如果停止时仍有实验执行，Launcher 最多等待当前后台任务 5 秒，随后取消本地执行并尽力把
已创建的 AgentLoop Run 收敛为 `failed`；本地 Launch 会记为 `UNKNOWN`。此类 Run 不会
自动 Resume/Reconcile，必须到 AgentLoop 复核后再决定是否新建一次实验。

## 首次配置

1. 打开 Settings，填写 Region、AgentSpace、AK/SK，然后 Test Connection。
2. 点击“配置新 Agent”，默认选择“平台托管代码”。创建后直接进入代码工作台，不需要处理
   Token。
3. 选择 Session HTTP、单接口 HTTP 或 Echo 样例，基于样例修改
   `async def solution_http(task: Task, ...) -> SolutionOutput`，再点击“保存代码并验证 Agent”。
   平台会一次完成保存、启动和测试，且已启用代码会在 Launcher 重启后自动恢复。
4. 只有使用独立进程实现 `handle(payload)` 的外部客户 Agent 时，才选择“外部 Agent 主动连接”
   并保存一次性 Token、WebSocket URL；接入方式见 `LOCAL_AGENT_INTEGRATION.md`。
5. 在 Plans 选择 AgentLoop 已创建的 OFFLINE Plan 并发起实验。

每题 Agent 返回后，Launcher 会先以当前 AgentSpace 调用 `GetAgentSpace` 取得 `slsProject`，再用
SLS SDK 查询该 Project 固定的 `agent-trajectory` Logstore，验证唯一 Trace 后才首次写入实验 detail；
所有题目 detail 成功后才把 Run 标记为 `completed` 并触发云端评估。若 XML 未建立索引，Launcher
会在该调用时间窗内每页读取 100 条、最多 5 页，只在内存解析 `input`/`atif_content` 的 XML context
后丢弃 payload；页不完整、超限、歧义或查询错误均会失败关闭，不会补写无 `traceId` 的 detail。
本路径不使用 XTrace 或 `ListTracePreview`。完整规则见解压目录中的
[TRACE_CORRELATION.md](./TRACE_CORRELATION.md)。

页面代码会加密保存并在独立本地子进程运行，但该进程仍具有当前用户权限，不是恶意代码
沙箱。Web 页面代码只是每个 Dataset item 的 Solution Provider；Plan/Dataset/Run、并发、
版本 XML context、结果上传和评估由外层平台负责。不要把 `AgentLoopConfig`、
`run_experiment_parallel` 或 `main` 粘贴到工作台。单题测试只验证本地执行与 WebSocket
连通性，不代表完整 Dataset 实验、AgentLoop 评估或游戏质量。

默认只监听本机回环地址，不会暴露到局域网。

如需在受信私网或隔离容器网络直接监听所有网卡，直接运行一键脚本：

```bash
./start-agentloop-remote
```

脚本会自动使用稳定的 Data 目录，并忽略调用终端中残留的 `AGENTLOOP_LAUNCHER_MASTER_KEY`：首次启动原子生成权限为 `0600` 的 `master.key`，以后启动自动读取同一密钥，不需要设置环境变量。如果 `secrets.enc` 已存在但 `master.key` 丢失，脚本会拒绝启动，不会生成新密钥破坏已有 AK/SK。需要固定数据位置时直接追加参数：

```bash
./start-agentloop-remote --data-dir /absolute/path/agentloop-launcher-data
```

然后从另一台机器访问 `http://<服务器私网 IP>:8090`。需要限制监听网卡时可追加 `--host <私网 IP>`；启动日志会明确打印实际监听地址、Data 目录、密钥文件路径和安全警告。

这个脚本只简化单机私网部署，不会增加登录鉴权、TLS 或 CSRF 会话保护。请用主机防火墙/安全组只允许可信网段访问；禁止把 `8090` 直接开放到公网。正式企业部署仍应由客户现有 TLS、VPN、SSO 或反向代理保护，并优先通过 Secret Manager 注入外部管理的 Master Key。

## 数据位置

数据目录使用操作系统稳定的用户应用数据目录，可通过下面的参数显式指定：

```bash
./agentloop-launcher --data-dir /absolute/path/to/data
```

推荐让 Launcher 创建最后一级目录。若目录已存在，必须是当前用户所有、权限 `0700` 的
专用目录；不能传 `/`、用户 Home、系统临时目录、符号链接或普通 `0755` 共享目录。例如：

```bash
mkdir -m 700 /absolute/path/to/agentloop-launcher-data
./agentloop-launcher --data-dir /absolute/path/to/agentloop-launcher-data
```

请同时备份数据目录和 Master Key；丢失 Master Key 后无法恢复已保存的 AK/SK 与 Agent Token。

当前原生包支持 macOS 和 Linux；每个发布包只适用于文件名标明的操作系统与 CPU 架构。

## macOS 签名边界

仓库当前生成的 `.tar.gz` 是内部验收构建，仅有 PyInstaller 的 ad-hoc 签名，没有 Apple
Developer ID 公证。它已在制包机做过真实解压启动验收，但被浏览器下载并加上 quarantine
后，Gatekeeper 可能阻止运行。对客户正式分发时不要要求用户绕过 Gatekeeper；应由发布
流水线使用公司的 Developer ID 与 notary profile 构建：

```bash
./scripts/build_native_release.sh \
  --codesign-identity 'Developer ID Application: <company>' \
  --notary-profile '<notarytool-keychain-profile>'
```

该模式生成经签名并提交 Apple notarization 的 `.zip`。正式外发前还必须在一台干净、开启
Gatekeeper 的 Mac 上重新验证签名、公证和启动。Linux 包应由 Linux 对应架构制包机生成。
