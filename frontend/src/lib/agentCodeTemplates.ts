export interface AgentCodeTemplate {
  id: string;
  name: string;
  description: string;
  source: string;
}

const SESSION_HTTP_SOURCE = `import httpx

from agentloop_sdk import SolutionOutput, Task


def build_request_body(task: Task) -> dict:
    """从 Dataset Task 中提取真实问题。"""
    task_input = task.input if isinstance(task.input, dict) else {}
    content = task_input.get("question", task_input.get("input", ""))
    return {"content": content}


async def solution_http(task: Task, *args, **kwargs) -> SolutionOutput:
    """每道题创建独立 Session，再把题目发送给本地 Agent。"""
    async with httpx.AsyncClient() as client:
        session_resp = await client.post(
            "http://127.0.0.1:18000/v1/sessions",
            json={"cwd": ".", "max_turns": 12, "max_budget_usd": 2},
            timeout=60.0,
        )
        session_resp.raise_for_status()
        session_id = session_resp.json()["id"]

        request_body = build_request_body(task)
        message_resp = await client.post(
            f"http://127.0.0.1:18000/v1/sessions/{session_id}/messages",
            json=request_body,
            timeout=240.0,
        )
        message_resp.raise_for_status()

        return SolutionOutput(
            success=True,
            output=message_resp.text,
            trajectory=[],
            meta={
                "experiment_input": request_body,
                "status_code": message_resp.status_code,
                "session_id": session_id,
            },
        )
`;

const SINGLE_HTTP_SOURCE = `import httpx

from agentloop_sdk import SolutionOutput, Task


async def solution_http(task: Task, *args, **kwargs) -> SolutionOutput:
    """把一道题直接 POST 到客户 Agent 的 HTTP 接口。"""
    task_input = task.input if isinstance(task.input, dict) else {}
    content = task_input.get("question", task_input.get("input", ""))

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "http://127.0.0.1:8000/invoke",  # 修改为客户 Agent 地址
            json={"content": content},
            timeout=240.0,
        )
        response.raise_for_status()

    return SolutionOutput(
        success=True,
        output=response.text,
        trajectory=[],
        meta={
            "experiment_input": {"content": content},
            "status_code": response.status_code,
        },
    )
`;

const ECHO_SOURCE = `from agentloop_sdk import SolutionOutput, Task


async def solution_http(task: Task, *args, **kwargs) -> SolutionOutput:
    """最小连通性示例：原样返回题目，不调用真实 Agent。"""
    task_input = task.input if isinstance(task.input, dict) else {}
    content = task_input.get("question", task_input.get("input", ""))
    return SolutionOutput(
        success=True,
        output=content,
        trajectory=[],
        meta={"agentName": "local-echo"},
    )
`;

export const AGENT_CODE_TEMPLATES: AgentCodeTemplate[] = [
  {
    id: "session-http",
    name: "Session HTTP Agent（推荐）",
    description: "参考当前接入方式：每题创建 Session，再向 messages 接口发送问题。",
    source: SESSION_HTTP_SOURCE,
  },
  {
    id: "single-http",
    name: "单 HTTP 接口",
    description: "适合已有 invoke/chat 接口的 Agent，只需修改 URL、请求体和结果解析。",
    source: SINGLE_HTTP_SOURCE,
  },
  {
    id: "echo",
    name: "最小回显示例",
    description: "仅验证 AgentLoop 到本地 Provider 的执行链路，不代表真实 Agent 效果。",
    source: ECHO_SOURCE,
  },
];

export function agentCodeTemplate(templateId: string): AgentCodeTemplate | undefined {
  return AGENT_CODE_TEMPLATES.find((template) => template.id === templateId);
}

export function matchingAgentCodeTemplate(source: string): string {
  return AGENT_CODE_TEMPLATES.find((template) => template.source === source)?.id ?? "custom";
}
