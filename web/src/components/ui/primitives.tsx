// 通用 UI 原语：状态点、徽章、空状态、可重试图片、JSON 查看器、Markdown、指标。
// 这些组件不含业务语义，供各功能区组合。

import { useEffect, useState, type ReactNode } from "react";
import { MonitorSmartphone, RefreshCw } from "lucide-react";
import clsx from "clsx";
import { renderMarkdown } from "../../utils/markdown";

/** 顶栏/面板用的状态圆点。 */
export function StatusDot({ ok, label, mock = false }: { ok: boolean; label: string; mock?: boolean }) {
  return (
    <span className="status-dot">
      <i className={clsx({ on: ok, mock: !ok && mock })} />
      {label}
    </span>
  );
}

/** 语义徽章：tone 决定配色。 */
export function Badge({ tone = "neutral", children }: { tone?: "ok" | "warn" | "danger" | "brand" | "neutral"; children: ReactNode }) {
  return <span className={clsx("badge", tone !== "neutral" && `badge-${tone}`)}>{children}</span>;
}

/** 空状态占位。 */
export function EmptyState({ title, hint, icon }: { title: string; hint?: string; icon?: ReactNode }) {
  return (
    <div className="empty-state">
      {icon ?? <MonitorSmartphone size={38} />}
      <strong>{title}</strong>
      {hint && <span>{hint}</span>}
    </div>
  );
}

/** 指标数值。 */
export function Metric({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="metric">
      <strong>{typeof value === "number" ? String(value).padStart(2, "0") : value}</strong>
      <small>{label}</small>
    </div>
  );
}

/** 加载失败可手动重试的图片（附 revision 缓存穿透）。 */
export function RetryImage({ src, alt }: { src: string; alt: string }) {
  const [revision, setRevision] = useState(0);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    setFailed(false);
    setRevision(0);
  }, [src]);
  if (!src || failed) {
    return (
      <div className="image-retry" role="alert">
        <span>图片加载失败</span>
        <button type="button" className="secondary compact" onClick={() => { setFailed(false); setRevision((value) => value + 1); }}>
          <RefreshCw size={14} />重试图片
        </button>
      </div>
    );
  }
  return (
    <img
      key={`${src}-${revision}`}
      src={`${src}${src.includes("?") ? "&" : "?"}revision=${revision}`}
      alt={alt}
      onError={() => setFailed(true)}
    />
  );
}

/** 原始 JSON 查看器（事件 payload、顾问原始输出等）。 */
export function JsonViewer({ value }: { value: unknown }) {
  let text: string;
  try {
    text = JSON.stringify(value, null, 2);
  } catch {
    text = String(value);
  }
  return <pre className="json-viewer">{text}</pre>;
}

/** 安全的 Markdown 渲染。 */
export function Markdown({ text }: { text: string }) {
  // marked + DOMPurify 在模块加载时已就绪；渲染结果仅含白名单标签。
  return <div className="markdown" dangerouslySetInnerHTML={{ __html: renderMarkdown(text) }} />;
}
