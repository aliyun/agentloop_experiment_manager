import { useCallback, useEffect, useState, type FormEvent } from "react";
import { CheckIcon, RefreshIcon } from "../components/Icons";
import { ErrorState, Field, Notice, PageHeader, StatusBadge, Toast } from "../components/UI";
import { normalizeAgentLoopApiEndpoint, normalizeAgentLoopConsoleBaseUrl } from "../lib/agentLoopUrls";
import { api, ApiError, errorMessage } from "../lib/api";
import { formatDateTime } from "../lib/format";
import { normalizeIanaTimezone } from "../lib/timezone";
import type { AgentLoopSettings, ConnectionTestResult, UpdateAgentLoopSettingsInput } from "../lib/types";

const emptySettings: AgentLoopSettings = {
  configured: false,
  region: "",
  agentSpace: "",
  defaultTimezone: "UTC",
  maxConcurrency: 1,
};

function emptyForm(): UpdateAgentLoopSettingsInput {
  return {
    displayName: "Default",
    region: "",
    agentSpace: "",
    apiEndpoint: "",
    consoleBaseUrl: "",
    defaultTimezone: "UTC",
    maxConcurrency: 1,
    accessKeyId: "",
    accessKeySecret: "",
  };
}

export function SettingsPage() {
  const [settings, setSettings] = useState<AgentLoopSettings>(emptySettings);
  const [form, setForm] = useState<UpdateAgentLoopSettingsInput>(emptyForm);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<ConnectionTestResult | null>(null);
  const [toast, setToast] = useState<{ tone: "success" | "danger" | "warning"; title: string; message: string } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const value = await api.settings.get();
      setSettings(value);
      setForm({
        displayName: value.displayName || "Default",
        region: value.region || "",
        agentSpace: value.agentSpace || "",
        apiEndpoint: value.apiEndpoint || "",
        consoleBaseUrl: value.consoleBaseUrl || "",
        defaultTimezone: value.defaultTimezone || "UTC",
        maxConcurrency: value.maxConcurrency || 1,
        accessKeyId: "",
        accessKeySecret: "",
      });
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === "CONNECTION_NOT_CONFIGURED") {
        setSettings(emptySettings);
        setForm(emptyForm());
      } else {
        setError(errorMessage(caught));
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  function set<K extends keyof UpdateAgentLoopSettingsInput>(key: K, value: UpdateAgentLoopSettingsInput[K]) {
    setForm((current) => ({ ...current, [key]: value }));
    setTestResult(null);
  }

  function valid(): { apiEndpoint: string | null; consoleBaseUrl: string | null } | null {
    if (!form.region.trim() || !form.agentSpace.trim()) {
      setToast({ tone: "danger", title: "配置不完整", message: "Region 和 AgentSpace 为必填项。" });
      return null;
    }
    if (!settings.secretConfigured && (!form.accessKeyId || !form.accessKeySecret)) {
      setToast({ tone: "danger", title: "缺少凭据", message: "首次配置必须同时填写 AccessKey ID 和 AccessKey Secret。" });
      return null;
    }
    if (Boolean(form.accessKeyId) !== Boolean(form.accessKeySecret)) {
      setToast({ tone: "danger", title: "凭据不完整", message: "轮换凭据时必须同时填写 AccessKey ID 和 AccessKey Secret。" });
      return null;
    }
    try {
      normalizeIanaTimezone(form.defaultTimezone);
    } catch (caught) {
      setToast({ tone: "danger", title: "默认时区无效", message: errorMessage(caught) });
      return null;
    }
    if (!Number.isInteger(form.maxConcurrency) || form.maxConcurrency < 1 || form.maxConcurrency > 32) {
      setToast({ tone: "danger", title: "并发配置无效", message: "本地最大并发数必须是 1 到 32 之间的整数。" });
      return null;
    }
    try {
      return {
        apiEndpoint: normalizeAgentLoopApiEndpoint(form.apiEndpoint),
        consoleBaseUrl: normalizeAgentLoopConsoleBaseUrl(form.consoleBaseUrl),
      };
    } catch (caught) {
      setToast({ tone: "danger", title: "AgentLoop 地址无效", message: errorMessage(caught) });
      return null;
    }
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    const addresses = valid();
    if (!addresses) return;
    setSaving(true);
    try {
      const value = await api.settings.update({
        ...form,
        displayName: form.displayName?.trim() || "Default",
        region: form.region.trim(),
        agentSpace: form.agentSpace.trim(),
        apiEndpoint: addresses.apiEndpoint,
        consoleBaseUrl: addresses.consoleBaseUrl,
        defaultTimezone: normalizeIanaTimezone(form.defaultTimezone),
        maxConcurrency: form.maxConcurrency,
        accessKeyId: form.accessKeyId || undefined,
        accessKeySecret: form.accessKeySecret || undefined,
      });
      setSettings(value);
      setForm((current) => ({
        ...current,
        apiEndpoint: addresses.apiEndpoint || "",
        consoleBaseUrl: addresses.consoleBaseUrl || "",
        accessKeyId: "",
        accessKeySecret: "",
      }));
      setToast({ tone: "success", title: "AgentLoop Profile 已保存", message: "Secret 已写入 Secret Store，不会通过读取接口返回。" });
    } catch (caught) {
      setToast({ tone: "danger", title: "保存失败", message: errorMessage(caught) });
    } finally {
      setSaving(false);
    }
  }

  async function testConnection() {
    if (!settings.configured) {
      setToast({ tone: "warning", title: "请先保存配置", message: "连接测试只使用已安全保存到服务端的 AgentLoop Profile。" });
      return;
    }
    setTesting(true);
    setTestResult(null);
    try {
      setTestResult(await api.settings.test());
    } catch (caught) {
      setTestResult({ success: false, message: errorMessage(caught), verifiedAt: new Date().toISOString() });
    } finally {
      setTesting(false);
    }
  }

  return (
    <div className="page">
      <PageHeader
        eyebrow="AgentLoop"
        title="Settings"
        description="配置 AgentLoop 云端访问身份，以及本地 Schedule 默认时区和实验并发上限。"
        actions={<button className="button secondary" type="button" onClick={() => void load()} disabled={loading}><RefreshIcon /> 重新读取</button>}
      />

      <Notice title="凭据只用于服务端访问 AgentLoop">
        AccessKey Secret 保存后不会再次显示，也不会进入 SQLite、前端读取响应、日志或 Trace。建议使用最小权限的专用 RAM 身份。
      </Notice>

      {error ? <section className="panel"><ErrorState message={error} onRetry={() => void load()} /></section> : (
        <div className="settings-layout">
          <form className="panel settings-form" onSubmit={save}>
            <div className="section-heading"><div><h2>AgentLoop Profile</h2><p>每个本地部署启用一个云端访问身份</p></div><StatusBadge value={settings.configured ? "CONFIGURED" : "NOT_CONFIGURED"} compact /></div>
            <Field label="Profile 名称"><input value={form.displayName || ""} onChange={(event) => set("displayName", event.target.value)} placeholder="Default" disabled={loading} /></Field>
            <div className="form-grid two">
              <Field label="Region" required hint="例如 cn-hangzhou"><input value={form.region} onChange={(event) => set("region", event.target.value)} placeholder="cn-hangzhou" disabled={loading} /></Field>
              <Field label="AgentSpace" required><input value={form.agentSpace} onChange={(event) => set("agentSpace", event.target.value)} placeholder="AgentSpace ID" disabled={loading} /></Field>
            </div>
            <Field label="AgentLoop API Endpoint" hint="可直接填写 SDK Endpoint 域名；留空使用对应 Region 的默认值"><input type="text" inputMode="url" autoCapitalize="none" spellCheck={false} value={form.apiEndpoint || ""} onChange={(event) => set("apiEndpoint", event.target.value)} placeholder="agentloop.cn-hangzhou.aliyuncs.com" disabled={loading} /></Field>
            <Field label="AgentLoop Console Base URL" hint="可省略 https://，也可粘贴任意 AgentLoop 控制台页面地址"><input type="text" inputMode="url" autoCapitalize="none" spellCheck={false} value={form.consoleBaseUrl || ""} onChange={(event) => set("consoleBaseUrl", event.target.value)} placeholder="agentloop.console.aliyun.com" disabled={loading} /></Field>

            <div className="form-divider"><span>本地运行默认值</span></div>
            <div className="form-grid two">
              <Field label="默认 IANA 时区" required hint="新建 Schedule 时使用，例如 Asia/Shanghai"><input value={form.defaultTimezone} onChange={(event) => set("defaultTimezone", event.target.value)} placeholder="UTC" disabled={loading} /></Field>
              <Field label="最大并发实验数" required hint="本地 Runner 并发上限，范围 1–32"><input type="number" min="1" max="32" step="1" value={form.maxConcurrency} onChange={(event) => set("maxConcurrency", Number(event.target.value))} disabled={loading} /></Field>
            </div>

            <div className="form-divider"><span>RAM 凭据</span></div>
            {settings.accessKeyIdMasked && <div className="stored-secret"><CheckIcon /><span>已保存 AccessKey</span><code>{settings.accessKeyIdMasked}</code><small>仅在需要轮换时填写下方字段</small></div>}
            <div className="form-grid two">
              <Field label={settings.configured ? "新 AccessKey ID" : "AccessKey ID"} required={!settings.configured}><input value={form.accessKeyId || ""} onChange={(event) => set("accessKeyId", event.target.value)} placeholder={settings.configured ? "留空表示不变" : "LTAI..."} autoComplete="off" disabled={loading} /></Field>
              <Field label={settings.configured ? "新 AccessKey Secret" : "AccessKey Secret"} required={!settings.secretConfigured}><input type="password" value={form.accessKeySecret || ""} onChange={(event) => set("accessKeySecret", event.target.value)} placeholder={settings.secretConfigured ? "留空表示不变" : "仅在保存时提交"} autoComplete="new-password" disabled={loading} /></Field>
            </div>

            {testResult && <div className={`connection-result ${testResult.success ? "success" : "danger"}`}><StatusBadge value={testResult.success ? "SUCCESS" : "FAILED"} /><div><strong>{testResult.success ? "连接验证通过" : "连接验证失败"}</strong><p>{testResult.message}</p>{testResult.verifiedAt && <small>{formatDateTime(testResult.verifiedAt)}</small>}</div></div>}

            <div className="form-actions"><button className="button secondary" type="button" onClick={() => void testConnection()} disabled={loading || testing || saving || !settings.configured}>{testing ? "正在验证…" : "测试已保存连接"}</button><button className="button primary" type="submit" disabled={loading || saving || testing}>{saving ? "保存中…" : "保存 Profile"}</button></div>
          </form>

          <aside className="settings-side">
            <section className="panel runtime-card">
              <span className="eyebrow">Cloud target</span><h2>当前访问范围</h2>
              <dl><div><dt>Region</dt><dd><code>{settings.region || "—"}</code></dd></div><div><dt>AgentSpace</dt><dd><code>{settings.agentSpace || "—"}</code></dd></div><div><dt>默认时区</dt><dd><code>{settings.defaultTimezone}</code></dd></div><div><dt>并发上限</dt><dd>{settings.maxConcurrency}</dd></div><div><dt>最近验证</dt><dd>{formatDateTime(settings.lastVerifiedAt)}</dd></div><div><dt>验证结果</dt><dd><StatusBadge value={settings.lastVerificationStatus || "UNKNOWN"} compact /></dd></div></dl>
              {settings.lastVerificationMessage && <p className="runtime-message">{settings.lastVerificationMessage}</p>}
            </section>
            <section className="boundary-card"><strong>Profile 职责</strong><ol><li>读取云端 Experiment Plan</li><li>创建并更新实验 Run 数据</li><li>上传 Runner 与 Evaluator 输出</li><li>生成 AgentLoop 结果深链</li></ol></section>
          </aside>
        </div>
      )}

      {toast && <Toast tone={toast.tone} title={toast.title} onClose={() => setToast(null)}>{toast.message}</Toast>}
    </div>
  );
}
