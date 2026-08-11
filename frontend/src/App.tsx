import { useCallback, useEffect, useState } from "react";
import { AgentIcon, SettingsIcon } from "./components/Icons";
import { Layout, type RouteKey } from "./components/Layout";
import { Modal } from "./components/UI";
import { AgentConnectionsPage } from "./pages/AgentConnectionsPage";
import { ExperimentsPage } from "./pages/ExperimentsPage";
import { PlansPage } from "./pages/PlansPage";
import { SchedulesPage } from "./pages/SchedulesPage";
import { SettingsPage } from "./pages/SettingsPage";
import { api, ApiError } from "./lib/api";
import { launchHistorySearch } from "./lib/launchNavigation";
import { missingSetupRequirements, type SetupRequirement } from "./lib/setupReadiness";

const paths: Record<RouteKey, string> = {
  agents: "/agent-connections",
  plans: "/plans",
  schedules: "/schedules",
  experiments: "/experiments",
  settings: "/settings",
};

function routeFromPath(pathname: string): RouteKey {
  const matched = (Object.entries(paths) as Array<[RouteKey, string]>).find(([, path]) => pathname === path || pathname.startsWith(`${path}/`));
  return matched?.[0] || "plans";
}

export default function App() {
  const [route, setRoute] = useState<RouteKey>(() => routeFromPath(window.location.pathname));
  const [missingSetup, setMissingSetup] = useState<SetupRequirement[]>([]);
  const [setupReminderOpen, setSetupReminderOpen] = useState(false);

  useEffect(() => {
    if (window.location.pathname === "/" || window.location.pathname === "") {
      window.history.replaceState({}, "", paths.plans);
    }
    const onPopState = () => setRoute(routeFromPath(window.location.pathname));
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  useEffect(() => {
    let cancelled = false;

    async function checkInitialSetup() {
      const [agentLoopResult, localAgentsResult] = await Promise.allSettled([
        api.settings.get(),
        api.agentConnections.list({ pageSize: 1 }),
      ]);

      let agentLoopConfigured: boolean | null = null;
      if (agentLoopResult.status === "fulfilled") {
        const settings = agentLoopResult.value;
        agentLoopConfigured = Boolean(
          settings.configured
          && settings.region
          && settings.agentSpace
          && settings.secretConfigured !== false,
        );
      } else if (
        agentLoopResult.reason instanceof ApiError
        && agentLoopResult.reason.code === "CONNECTION_NOT_CONFIGURED"
      ) {
        agentLoopConfigured = false;
      }

      const localAgentCount = localAgentsResult.status === "fulfilled"
        ? localAgentsResult.value.total
        : null;
      const missing = missingSetupRequirements({ agentLoopConfigured, localAgentCount });
      if (cancelled) return;
      setMissingSetup(missing);
      setSetupReminderOpen(missing.length > 0);
    }

    void checkInitialSetup();
    return () => { cancelled = true; };
  }, []);

  const navigate = useCallback((next: RouteKey, search = "") => {
    if (window.location.pathname === paths[next] && window.location.search === search) return;
    window.history.pushState({}, "", `${paths[next]}${search}`);
    setRoute(next);
  }, []);

  const openLaunchHistory = useCallback((launchId: string) => {
    navigate("experiments", launchHistorySearch(launchId));
  }, [navigate]);

  const openSetup = useCallback((target: "settings" | "agents") => {
    setSetupReminderOpen(false);
    navigate(target);
  }, [navigate]);

  return (
    <Layout current={route} onNavigate={navigate}>
      {route === "plans" && <PlansPage onOpenHistory={openLaunchHistory} />}
      {route === "agents" && <AgentConnectionsPage />}
      {route === "schedules" && <SchedulesPage />}
      {route === "experiments" && <ExperimentsPage />}
      {route === "settings" && <SettingsPage />}

      <Modal
        open={setupReminderOpen && missingSetup.length > 0}
        title="完成首次配置"
        description="运行本地实验前，需要同时配置 AgentLoop 和至少一个本地 Agent。"
        onClose={() => setSetupReminderOpen(false)}
        footer={<button className="button secondary" type="button" onClick={() => setSetupReminderOpen(false)}>稍后设置</button>}
      >
        <div className="setup-reminder-list">
          {missingSetup.includes("agentloop") && (
            <section className="setup-reminder-item">
              <span className="setup-reminder-icon"><SettingsIcon /></span>
              <div>
                <strong>配置 AgentLoop</strong>
                <p>设置 AK、Region 和 AgentSpace，供本地平台读取 Plan 并上报实验记录。</p>
              </div>
              <button className="button primary small" type="button" onClick={() => openSetup("settings")}>前往配置</button>
            </section>
          )}
          {missingSetup.includes("local-agent") && (
            <section className="setup-reminder-item">
              <span className="setup-reminder-icon"><AgentIcon /></span>
              <div>
                <strong>配置本地 Agent</strong>
                <p>创建至少一个 Agent Connection，并配置实验单题的本地执行方式。</p>
              </div>
              <button className="button primary small" type="button" onClick={() => openSetup("agents")}>前往配置</button>
            </section>
          )}
        </div>
      </Modal>
    </Layout>
  );
}
