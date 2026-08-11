import type { ReactNode } from "react";
import { CheckIcon, CloseIcon, InfoIcon, WarningIcon } from "./Icons";

export function StatusBadge({ value, compact = false }: { value?: string | null; compact?: boolean }) {
  const normalized = (value || "UNKNOWN").toLowerCase();
  let tone = "neutral";
  if (["accepted", "completed", "success", "active", "enabled", "handed_off", "online", "connected", "healthy", "resolved", "reported"].includes(normalized)) tone = "success";
  if (["running", "submitting", "pending", "polling", "evaluating", "created"].includes(normalized)) tone = "progress";
  if (["failed", "definitive_failure", "cancelled", "timeout", "timed_out", "ambiguous", "eval_cancelled", "unhealthy"].includes(normalized)) tone = "danger";
  if (["submission_unknown", "unknown", "skipped", "disabled", "paused", "offline", "degraded", "missing"].includes(normalized)) tone = "warning";
  return <span className={`status-badge ${tone}${compact ? " compact" : ""}`}><i />{value || "UNKNOWN"}</span>;
}

export function Notice({ tone = "info", title, children }: { tone?: "info" | "warning" | "success"; title: string; children: ReactNode }) {
  const Icon = tone === "warning" ? WarningIcon : tone === "success" ? CheckIcon : InfoIcon;
  return (
    <div className={`notice ${tone}`}>
      <Icon />
      <div><strong>{title}</strong><div>{children}</div></div>
    </div>
  );
}

export function PageHeader({ eyebrow, title, description, actions }: { eyebrow?: string; title: string; description: string; actions?: ReactNode }) {
  return (
    <header className="page-header">
      <div>
        {eyebrow && <span className="eyebrow">{eyebrow}</span>}
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {actions && <div className="header-actions">{actions}</div>}
    </header>
  );
}

export function EmptyState({ title, children, action }: { title: string; children: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty-state">
      <div className="empty-glyph"><span /><span /><span /></div>
      <h3>{title}</h3>
      <p>{children}</p>
      {action}
    </div>
  );
}

export function LoadingRows({ columns = 6 }: { columns?: number }) {
  return (
    <>
      {[0, 1, 2, 3].map((row) => (
        <tr className="loading-row" key={row}>
          {Array.from({ length: columns }).map((_, column) => <td key={column}><span /></td>)}
        </tr>
      ))}
    </>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="error-state" role="alert">
      <WarningIcon />
      <div><strong>数据加载失败</strong><p>{message}</p></div>
      <button className="button secondary" type="button" onClick={onRetry}>重试</button>
    </div>
  );
}

export function Pagination({ page, pageSize, total, onChange }: { page: number; pageSize: number; total: number; onChange: (page: number) => void }) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  if (total <= pageSize) return null;
  return (
    <div className="pagination">
      <span>共 {total} 条</span>
      <div>
        <button className="button ghost small" type="button" disabled={page <= 1} onClick={() => onChange(page - 1)}>上一页</button>
        <span>{page} / {pages}</span>
        <button className="button ghost small" type="button" disabled={page >= pages} onClick={() => onChange(page + 1)}>下一页</button>
      </div>
    </div>
  );
}

interface ModalProps {
  open: boolean;
  title: string;
  description?: string;
  children: ReactNode;
  footer: ReactNode;
  onClose: () => void;
  width?: "normal" | "wide";
}

export function Modal({ open, title, description, children, footer, onClose, width = "normal" }: ModalProps) {
  if (!open) return null;
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className={`modal ${width}`} role="dialog" aria-modal="true" aria-labelledby="modal-title">
        <header>
          <div><h2 id="modal-title">{title}</h2>{description && <p>{description}</p>}</div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="关闭"><CloseIcon /></button>
        </header>
        <div className="modal-body">{children}</div>
        <footer>{footer}</footer>
      </section>
    </div>
  );
}

export function Toast({ tone, title, children, onClose }: { tone: "success" | "danger" | "warning"; title: string; children?: ReactNode; onClose: () => void }) {
  const Icon = tone === "success" ? CheckIcon : WarningIcon;
  return (
    <div className={`toast ${tone}`} role="status">
      <Icon />
      <div><strong>{title}</strong>{children && <p>{children}</p>}</div>
      <button type="button" onClick={onClose} aria-label="关闭"><CloseIcon /></button>
    </div>
  );
}

export function Field({ label, hint, required, children }: { label: string; hint?: string; required?: boolean; children: ReactNode }) {
  return (
    <label className="field">
      <span>{label}{required && <b> *</b>}</span>
      {children}
      {hint && <small>{hint}</small>}
    </label>
  );
}
