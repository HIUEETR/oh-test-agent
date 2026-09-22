// DC 模式当前活动区：独立于模型文本，始终显示阶段、调用 ID、已耗时、最后进度与超时状态。
// 没有任何模型文本时也必须显示并走时（本地每秒 tick）。

import { useEffect, useState } from "react";
import { AlertTriangle, Loader2 } from "lucide-react";
import { useDcConsole } from "../../stores/dc-console";
import type { ResolveAction } from "../../api/dc-client";
import type { DcActivityState, DcContinuationContext } from "../../api/dc-types";

/** 本地每秒 tick：让「已耗时」「剩余时间」在没有 SSE 事件时也持续走时。 */
function useNowTicker(active: boolean, key: string): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    setNow(Date.now());
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active, key]);
  return now;
}

const pad = (value: number) => String(value).padStart(2, "0");

/** 毫秒 → mm:ss（超过 1 小时显示 h:mm:ss）；无效值显示 --:--。 */
export function formatClockDuration(milliseconds: number | null | undefined): string {
  if (milliseconds == null || !Number.isFinite(milliseconds)) return "--:--";
  const total = Math.max(0, Math.floor(milliseconds / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor(total / 60) % 60;
  const seconds = total % 60;
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(seconds)}` : `${pad(minutes)}:${pad(seconds)}`;
}

/** 活动已耗时：优先按 started_at 实时推导（比事件里的快照更新）。 */
export function activityElapsedMs(activity: DcActivityState, now: number): number | null {
  if (activity.startedAt) {
    const diff = now - Date.parse(activity.startedAt);
    if (Number.isFinite(diff)) return Math.max(0, diff);
  }
  if (activity.kind === "idle") return null;
  return activity.elapsedMs;
}

/** 超过该阈值显示「仍在执行」提示（计划第 6 节建议的 UI 告警阈值）。 */
const ACTIVITY_WARN_MS = 5000;

/** 超时状态文案：倒计时 / 已超时 / 需要人工确认 / 仍在执行。 */
export function activityStatusText(activity: DcActivityState, now: number): string {
  if (activity.kind === "attention") return "需要人工确认：副作用未确认，禁止自动重放";
  if (activity.kind === "cancelling") return "正在确认设备状态";
  if (activity.kind === "idle") return activity.phaseLabel;
  if (activity.deadlineAt) {
    const remaining = Date.parse(activity.deadlineAt) - now;
    if (Number.isFinite(remaining)) {
      if (remaining <= 0) return "已超时，等待处置结果";
      if (remaining <= 90_000) return `将在 ${Math.max(1, Math.ceil(remaining / 1000))} 秒时超时`;
      return `预算剩余 ${formatClockDuration(remaining)}`;
    }
  }
  if (activity.remainingMs != null) {
    if (activity.remainingMs <= 0) return "已超时，等待处置结果";
    return `预算剩余 ${formatClockDuration(activity.remainingMs)}`;
  }
  const elapsed = activityElapsedMs(activity, now);
  if (elapsed != null && elapsed >= ACTIVITY_WARN_MS) {
    return `仍在执行，已耗时 ${formatClockDuration(elapsed)}`;
  }
  return activity.cancellable ? "仍在执行，可请求停止" : "仍在执行";
}

function lastProgressText(activity: DcActivityState): string {
  if (!activity.lastProgressAt) return "—";
  const date = new Date(activity.lastProgressAt);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/** 取消/中断后的上下文连续性提示。 */
export function continuationHint(continuation: DcContinuationContext | null): string {
  if (!continuation) return "";
  const status = continuation.previous_status;
  const goal = (continuation.original_user_goal || "").trim();
  const prefix =
    status === "cancelled" ? "上一轮已取消"
      : status === "interrupted" ? "上一轮被中断"
        : status === "needs_attention" ? "上一轮需要人工确认"
          : status === "failed" ? "上一轮失败"
            : "";
  if (!prefix) return "";
  return goal ? `${prefix}，原始目标已保留：${goal}` : `${prefix}，上下文已保留，可继续`;
}

/** ``needs_attention`` 四个处置动作的展示标签与说明（与后端 ResolveAttentionRequest 一致）。 */
export const RESOLVE_ACTIONS: Array<{ action: ResolveAction; label: string; hint: string }> = [
  { action: "reobserve", label: "重新观测", hint: "重新采集截图与 UI 层级确认设备状态" },
  { action: "confirm_effect", label: "确认已生效", hint: "人工确认副作用已经发生，可继续" },
  { action: "retry", label: "重试", hint: "副作用未发生，重放该动作" },
  { action: "terminate", label: "终止轮次", hint: "停止本轮并保留现场" },
];

export function DcActivityBar() {
  const activity = useDcConsole((state) => state.activity);
  const cancelStatus = useDcConsole((state) => state.cancelStatus);
  const busy = useDcConsole((state) => state.status === "thinking" || state.status === "acting");
  const attentionReason = useDcConsole((state) => state.attentionReason);
  const attentionOptions = useDcConsole((state) => state.attentionOptions);
  const continuation = useDcConsole((state) => state.continuation);
  const resolveAttention = useDcConsole((state) => state.resolveAttention);
  const activeSessionId = useDcConsole((state) => state.activeSessionId);
  const [resolving, setResolving] = useState<string>("");

  const active = activity.kind !== "idle" || cancelStatus !== "idle" || busy;
  const now = useNowTicker(active, `${activity.epoch}|${activity.kind}|${activity.operationId}`);

  const hint = continuationHint(continuation);
  // 没有任何活动时，只保留结果型提示（取消/中断/需人工确认），否则不占位。
  if (!active && !hint) return null;

  const elapsed = activityElapsedMs(activity, now);
  const status = activityStatusText(activity, now);
  const label = activity.phaseLabel || (busy ? "正在等待模型" : "已结束");
  const attention = activity.kind === "attention";
  const canResolve = attention && Boolean(activeSessionId) && Boolean(resolveAttention);

  /** 处置按钮：真正发请求（历史实现只把这四个选项渲染成纯文本，无法解除阻塞）。 */
  const onResolve = async (action: ResolveAction) => {
    if (!canResolve || resolving) return;
    setResolving(action);
    try {
      await resolveAttention(action);
    } finally {
      setResolving("");
    }
  };

  return (
    <div
      className={`dc-activity dc-activity-${activity.kind}${cancelStatus === "confirming" ? " cancelling" : ""}`}
      role="status"
      aria-live="polite"
    >
      <div className="dc-activity-main">
        <span className="dc-activity-spinner" aria-hidden="true">
          {attention
            ? <AlertTriangle size={13} />
            : active && activity.kind !== "idle"
              ? <Loader2 size={13} className="dc-activity-spin" />
              : null}
        </span>
        <span className="dc-activity-phase">{label}</span>
        {activity.operationId && (
          <span className="dc-activity-id dc-op-mono" title={activity.operationId}>
            {activity.operationId}
          </span>
        )}
        <span className="dc-activity-elapsed" title="已耗时">{formatClockDuration(elapsed)}</span>
        <span className="dc-activity-status">{status}</span>
      </div>
      <div className="dc-activity-foot">
        <span>最后进度 {lastProgressText(activity)}</span>
        {cancelStatus === "confirming" && <span className="dc-activity-cancel">正在确认设备状态</span>}
      </div>
      {attention && (attentionReason || attentionOptions.length > 0) && (
        <div className="dc-activity-attention">
          {attentionReason && <span>{attentionReason}</span>}
          {attentionOptions.length > 0 && (
            <span className="dc-activity-options">{attentionOptions.join(" / ")}</span>
          )}
        </div>
      )}
      {attention && (
        <div className="dc-activity-resolve" role="group" aria-label="处置未确认的设备副作用">
          {RESOLVE_ACTIONS.map((item) => (
            <button
              key={item.action}
              type="button"
              className="dc-resolve-btn"
              title={item.hint}
              disabled={!canResolve || Boolean(resolving)}
              onClick={() => void onResolve(item.action)}
            >
              {resolving === item.action ? "处理中…" : item.label}
            </button>
          ))}
        </div>
      )}
      {!active && hint && <div className="dc-activity-continuation">{hint}</div>}
    </div>
  );
}
