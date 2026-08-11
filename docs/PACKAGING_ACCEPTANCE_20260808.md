# AgentLoop Launcher 原生包验收记录

## 1. 结论

`agentloop-launcher 0.1.0` 的 macOS ARM64 内部验收包已通过本地打包、解压、启动、页面/API、
本地 Agent 握手、重启持久化和发布包卫生检查。目标机器不需要另行安装 Python、Node.js、
npm 或 uv，解压后只需执行：

```bash
./agentloop-launcher
```

本轮仅验证客户侧 Launcher 的打包与本地运行链路，没有创建或运行任何云端 AgentLoop 实验。

当前产物是 ad-hoc 签名且未经过 Apple notarization 的内部验收包，**不满足正式客户外发验收**。
正式外发必须在持有公司 Developer ID 与 notary profile 的制包机重新构建，并在一台带
quarantine、开启 Gatekeeper 的干净 Mac 上复验。

## 2. 产物身份

| 项目 | 值 |
|---|---|
| 文件 | `release/agentloop-launcher-0.1.0-darwin-arm64.tar.gz` |
| 校验文件 | `release/agentloop-launcher-0.1.0-darwin-arm64.tar.gz.sha256` |
| SHA-256 | `6b8296605e760441f6504fa91937f3c54a472f46a06161f71527976a539b66c7` |
| 大小 | 51,631,502 bytes |
| 制包平台 | macOS 15.1 / arm64 |
| 内置 Python | 3.12.9 |
| 签名状态 | `Signature=adhoc`，`TeamIdentifier=not set` |

## 3. 最终实包黑盒验收

验收对象是从上述 `.tar.gz` 解压到全新 `/tmp` 目录的副本，不依赖源码目录或开发虚拟环境。

| 验收项 | 结果 | 证据摘要 |
|---|---|---|
| 干净环境完整性检查 | 通过 | `env -i PATH=/usr/bin:/bin ./agentloop-launcher doctor` 返回 0，且指定的不存在数据目录没有被创建 |
| 单命令启动 | 通过 | `./agentloop-launcher --no-open-browser --data-dir <fresh-dir>` 启动单一 FastAPI 进程 |
| 默认生产开关 | 通过 | `/api/v1/health` 返回 `gatewayMode=agentloop`、`schedulerEnabled=true` |
| 前端路由 | 通过 | `/`、`/plans`、`/agent-connections`、`/schedules`、`/experiments`、`/settings` 全部返回 200 |
| 404 边界 | 通过 | 不存在的 `/api/v1/*` 与 `/assets/*` 均返回 404，没有被 SPA fallback 吞掉 |
| 数据权限 | 通过 | 新数据目录为 `0700`；Master Key、SQLite、WAL、SHM、Lock 和加密 Secret 均为 `0600` |
| 危险目录保护 | 通过 | 现有 `0755` 目录与 symlink 数据目录均被拒绝；原目录权限不被修改，也不生成 Master Key |
| 单实例保护 | 通过 | 同一数据目录的第二实例返回 exit 3，且不输出 traceback |
| Agent 接入命令 | 通过 | 页面返回最终包中可执行文件的绝对路径；命令不包含一次性 Token |
| 本地 Agent 握手 | 通过 | 随包 Reference Agent 从解压目录运行后，健康接口返回 `CONNECTED`、`healthy=true` |
| 重启持久化 | 通过 | 重启后 Profile 与 Agent Connection 仍存在，GET 不返回 Token，Master Key 的时间、大小与权限不变 |
| 优雅停止 | 通过 | Reference Agent 与空闲 Launcher 均可由 `Ctrl+C` 正常退出；服务完成 shutdown 生命周期 |
| 归档卫生 | 通过 | 1,333 个归档条目；顶层只有 executable、`_internal` 和两份文档；未发现 `.env`、Key、Secret、DB、source map、`.pyc` 或本轮 dummy credential |

停止时存在运行中实验的取消与 `UNKNOWN`/远端 failed 收敛逻辑由单元测试覆盖；本轮没有为了
打包验收而创建云端 Run，因此不把它声明为云端实测结果。

## 4. 自动化回归

| 测试 | 结果 |
|---|---|
| Backend pytest | 104 passed |
| Backend Ruff | passed |
| Backend compileall | passed |
| Frontend tests（默认时区） | 8 passed |
| Frontend tests（`Pacific/Honolulu`） | 8 passed |
| Frontend TypeScript typecheck | passed |
| Frontend production build | passed |
| Go SDK contract | passed |
| 最终源码静态资源与实包内静态资源逐文件比较 | identical |

## 5. 签名与正式外发边界

对最终实包执行 `codesign --verify --deep --strict` 已通过，但这只证明 ad-hoc 签名内部一致。
权威 Gatekeeper 检查结果为：

```text
spctl --assess --type execute: exit 3 / rejected
Signature=adhoc
TeamIdentifier=not set
```

因此当前包的状态是“本机制包与内部验收通过”，不是“正式外发通过”。正式制包命令已经预留：

```bash
./scripts/build_native_release.sh \
  --codesign-identity 'Developer ID Application: <company>' \
  --notary-profile '<notarytool-keychain-profile>'
```

完成签名、公证、staple 与干净 Mac Gatekeeper 验收后，必须重新记录正式产物的文件名、
SHA-256、Team Identifier 和 notarization 结果，不能沿用本记录中的内部包结论。
