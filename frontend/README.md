# AgentLoop Local Experiment Launcher · Frontend

客户侧实验发起器的 React + TypeScript + Vite 前端。页面只调用根目录 `SPEC.md` 定义的本地 `/api/v1` 接口，不直接访问 AgentLoop，也不在浏览器中保存 AK/SK。

## 本地开发

```bash
cd frontend
npm install
npm run dev
```

开发服务器默认监听 `http://localhost:4173`，并将 `/api` 代理至 `http://127.0.0.1:8090`。可通过环境变量覆盖：

```bash
VITE_DEV_API_TARGET=http://127.0.0.1:8080 npm run dev
```

## 构建

```bash
npm run typecheck
npm run build
```

产物直接输出到 `backend/app/static/`，因此 wheel 和原生发布包都能由 FastAPI 同源托管，
运行时不需要 Node.js。后端的受限 history fallback 覆盖以下页面路径：

- `/plans`
- `/agent-connections`
- `/schedules`
- `/experiments`
- `/settings`

History fallback 只应处理这些前端页面的 `GET`/`HEAD` 深链（以及可选的 `/`），不得改写 `/api/v1/*`、静态资源或 Agent Connection 的 WebSocket 路径 `/api/v1/agent-connections/{id}/connect`。

如果前端与本地 API 不同源，可在构建时设置 `VITE_API_BASE_URL`，并由后端配置精确的 CORS allowlist。默认仅监听 loopback 的本地 MVP 不把 SSO 或 CSRF 作为 AgentLoop SDK 接入的 P0；当 API 暴露到非 loopback 地址、共享网络或反向代理入口时，部署方必须增加入口鉴权（例如企业 SSO）。只有采用 Cookie 会话认证时才需要同时落实 CSRF 防护。

## 产品边界

- Plan 只能在 AgentLoop 创建、编辑和删除。
- `ACCEPTED` 只表示已获得远端 Record ID，不表示实验、评估完成或效果达标。
- 本地仅显示 Record 摘要，完整结果通过服务端返回的安全链接跳转 AgentLoop 查看。
- Secret 保存后不回显；前端不会构造 AgentLoop 控制台 URL，也不会请求分数、Trace 或逐样本结果。
