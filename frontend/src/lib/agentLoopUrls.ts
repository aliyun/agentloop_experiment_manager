function parseHttpAddress(value: string, label: string): URL {
  const trimmed = value.trim();
  const candidate = /^[a-z][a-z0-9+.-]*:\/\//i.test(trimmed)
    ? trimmed
    : `https://${trimmed}`;

  let parsed: URL;
  try {
    parsed = new URL(candidate);
  } catch {
    throw new Error(`${label}格式不正确，请输入域名或 HTTP(S) 地址。`);
  }
  if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname) {
    throw new Error(`${label}只支持 HTTP(S) 地址。`);
  }
  if (parsed.username || parsed.password) {
    throw new Error(`${label}不能包含用户名或密码。`);
  }
  return parsed;
}

/**
 * Tea/OpenAPI SDK 的 endpoint 字段需要域名（可带端口），而不是浏览器 URL。
 * 同时兼容用户粘贴 https:// 开头的地址。
 */
export function normalizeAgentLoopApiEndpoint(value: string | null | undefined): string | null {
  const trimmed = value?.trim() ?? '';
  if (!trimmed) return null;
  const parsed = parseHttpAddress(trimmed, 'AgentLoop API Endpoint');
  if ((parsed.pathname && parsed.pathname !== '/') || parsed.search || parsed.hash) {
    throw new Error('AgentLoop API Endpoint 只能填写域名和端口，不能包含路径、参数或片段。');
  }
  return parsed.host;
}

/**
 * 控制台字段保存可用于拼接深链的站点根地址。用户可以省略协议，也可以直接粘贴
 * 某个控制台详情页；后者会安全地收敛到 origin，避免重复拼接控制台路径。
 */
export function normalizeAgentLoopConsoleBaseUrl(value: string | null | undefined): string | null {
  const trimmed = value?.trim() ?? '';
  if (!trimmed) return null;
  const parsed = parseHttpAddress(trimmed, 'AgentLoop Console 地址');
  return parsed.origin;
}
