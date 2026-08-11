import type { ReactNode } from "react";
import { AgentIcon, ExperimentIcon, PlansIcon, ScheduleIcon, SettingsIcon } from "./Icons";

export type RouteKey = "agents" | "plans" | "schedules" | "experiments" | "settings";

type NavItem = { key: RouteKey; label: string; description: string; icon: typeof PlansIcon };

const workflowNavItems: NavItem[] = [
  { key: "plans", label: "Plans", description: "实验计划", icon: PlansIcon },
  { key: "schedules", label: "Schedules", description: "定时任务", icon: ScheduleIcon },
  { key: "experiments", label: "Launch History", description: "本地账本", icon: ExperimentIcon },
];

const settingsNavItems: NavItem[] = [
  { key: "settings", label: "AgentLoop", description: "云端配置", icon: SettingsIcon },
  { key: "agents", label: "本地 Agent", description: "连接与执行", icon: AgentIcon },
];

interface LayoutProps {
  current: RouteKey;
  onNavigate: (route: RouteKey) => void;
  children: ReactNode;
}

export function Layout({ current, onNavigate, children }: LayoutProps) {
  const renderNavItem = (item: NavItem) => {
    const Icon = item.icon;
    return (
      <button
        key={item.key}
        type="button"
        className={current === item.key ? "nav-item active" : "nav-item"}
        onClick={() => onNavigate(item.key)}
        aria-current={current === item.key ? "page" : undefined}
      >
        <Icon className="nav-icon" />
        <span><strong>{item.label}</strong><small>{item.description}</small></span>
      </button>
    );
  };

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark"><span /></div>
          <div>
            <strong>AgentLoop</strong>
            <small>Experiment Launcher</small>
          </div>
        </div>

        <nav className="primary-nav" aria-label="主导航">
          {workflowNavItems.map(renderNavItem)}
          <span className="nav-section-label">Settings</span>
          {settingsNavItems.map(renderNavItem)}
        </nav>

        <div className="sidebar-foot">
          <div className="local-indicator"><span /> 客户侧运行</div>
          <p>本平台只发起实验。计划和实验结果的事实源均为 AgentLoop。</p>
        </div>
      </aside>
      <main className="main-content">{children}</main>
    </div>
  );
}
