// DC 模式操作日志：历史快照 + 实时执行账本。
// 数据源为 store 的 liveInvocations（SSE 增量）与 session.invocations（GET 快照）合并结果，
// 按 invocation_id 去重：started 立即出现 running 行，finished 就地变终态，绝不重复。

import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import {
  hasRunningTool,
  isActiveToolStatus,
  selectOperationRecords,
  useDcConsole,
} from "../../stores/dc-console";
import type { DcLiveConnection, DcLiveToolRecord, DcToolStatus } from "../../api/dc-types";

/** 每秒本地 tick，用于「已耗时」在没有新事件时也持续走时。 */
function useNowTicker(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  return now;
}

/** 毫秒 → mm:ss（超过 1 小时显示 h:mm:ss）。 */
export function formatElapsed(milliseconds: number | null | undefined): string {
  if (milliseconds == null || !Number.isFinite(milliseconds)) return "--:--";
  const total = Math.max(0, Math.floor(milliseconds / 1000));
  const seconds = total % 60;
  const minutes = Math.floor(total / 60) % 60;
  const hours = Math.floor(total / 3600);
  const pad = (value: number) => String(value).padStart(2, "0");
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(seconds)}` : `${pad(minutes)}:${pad(seconds)}`;
}

/** 记录已耗时：终态用 duration_ms，运行中按 started_at 推导。 */
export function recordElapsed(record: DcLiveToolRecord, now: number): number | null {
  if (!isActiveToolStatus(record.status)) {
    if (record.duration_ms != null) return record.duration_ms;
    if (record.ended_at) {
      const diff = Date.parse(record.ended_at) - Date.parse(record.started_at);
      return Number.isFinite(diff) ? Math.max(0, diff) : null;
    }
    return null;
  }
  const diff = now - Date.parse(record.started_at);
  return Number.isFinite(diff) ? Math.max(0, diff) : null;
}

/** 状态 → 视觉分档 class 后缀。 */
export function statusTone(status: DcToolStatus): string {
  switch (status) {
    case "running": return "running";
    case "succeeded": return "ok";
    case "timed_out": return "timeout";
    case "cancelled": return "cancelled";
    case "failed": return "fail";
    default: return "unknown";
  }
}

/** 状态 → 中文短标签。 */
export function statusLabel(status: DcToolStatus): string {
  switch (status) {
    case "running": return "执行中";
    case "succeeded": return "成功";
    case "failed": return "失败";
    case "timed_out": return "超时";
    case "cancelled": return "已取消";
    default: return "结果未知";
  }
}

function formatClock(iso: string | null): string {
  if (!iso) return "--:--:--";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "--:--:--";
  return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function argsSummary(args: Record<string, unknown>): string {
  const entries = Object.entries(args ?? {}).filter(([, value]) => value != null);
  if (entries.length === 0) return "（无参数）";
  return entries
    .map(([key, value]) => `${key}=${typeof value === "string" ? value : JSON.stringify(value)}`)
    .join(", ");
}

export function DcOperationLog() {
  const records = useDcConsole(selectOperationRecords);
  const activeSessionId = useDcConsole((state) => state.activeSessionId);
  const syncing = useDcConsole((state) => state.syncing);
  const connection = useDcConsole((state) => state.connection);
  const running = hasRunningTool(records);
  const [expanded, setExpanded] = useState(true);
  const now = useNowTicker(running);

  return (
    <section className="panel dc-operations">
      <button
        type="button"
        className="card-heading dc-operations-header"
        onClick={() => setExpanded(!expanded)}
        aria-expanded={expanded}
      >
        {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <span>操作日志</span>
        <small>{records.length} 步</small>
        {running && <span className="dc-op-live-dot" aria-label="有操作正在执行" />}
      </button>
      {expanded && (
        <div className="dc-operation-list">
          {records.length === 0 ? (
            <OperationEmptyState
              hasSession={Boolean(activeSessionId)}
              syncing={syncing}
              connection={connection}
            />
          ) : (
            records.map((record) => (
              <OperationRow key={record.invocation_id} record={record} now={now} />
            ))
          )}
        </div>
      )}
    </section>
  );
}

/**
 * 空状态分档：未选择会话 / 正在同步 / 断线等待对账 / 已选会话尚无工具。
 * 加载中与同步中绝不显示「尚无操作记录」。
 */
export function OperationEmptyState({
  hasSession,
  syncing,
  connection,
}: {
  hasSession: boolean;
  syncing: boolean;
  connection: DcLiveConnection;
}) {
  if (!hasSession) {
    return <p className="dc-empty-hint">未选择会话</p>;
  }
  if (syncing) {
    return (
      <p className="dc-empty-hint">
        <span className="dc-tool-spinner" aria-hidden="true" /> 正在同步操作记录...
      </p>
    );
  }
  if (connection === "reconnecting" || connection === "connecting") {
    return <p className="dc-empty-hint">连接已断开，正在等待对账...</p>;
  }
  if (connection === "closed") {
    return <p className="dc-empty-hint">连接已断开，请刷新会话重新对账</p>;
  }
  return <p className="dc-empty-hint">尚无操作记录</p>;
}

function OperationRow({ record, now }: { record: DcLiveToolRecord; now: number }) {
  const [open, setOpen] = useState(false);
  const running = isActiveToolStatus(record.status);
  const elapsed = recordElapsed(record, now);
  const summary = argsSummary(record.args);

  return (
    <div className={`dc-operation-row ${statusTone(record.status)}${open ? " open" : ""}`}>
      <button
        type="button"
        className="dc-op-head"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
      >
        <span className="dc-op-icon" aria-hidden="true">
          {running ? <span className="dc-tool-spinner" /> : statusIcon(record.status)}
        </span>
        <span className="dc-op-tool" title={record.tool}>{record.tool}</span>
        <span className="dc-op-args" title={summary}>{summary}</span>
        <span className="dc-op-state">{statusLabel(record.status)}</span>
        <span className="dc-op-duration">{formatElapsed(elapsed)}</span>
        <span className="dc-op-toggle" aria-hidden="true">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div className="dc-op-detail">
          <dl>
            <dt>调用 ID</dt><dd className="dc-op-mono">{record.invocation_id}</dd>
            <dt>轮次 ID</dt><dd className="dc-op-mono">{record.turn_id || "—"}</dd>
            <dt>开始</dt><dd>{formatClock(record.started_at)}</dd>
            <dt>结束</dt><dd>{record.ended_at ? formatClock(record.ended_at) : "—"}</dd>
            <dt>阶段</dt><dd>{record.phase || "—"}</dd>
            <dt>副作用</dt><dd>{effectLabel(record.effect_status)}</dd>
            {record.error_code && (<><dt>错误码</dt><dd className="dc-op-mono">{record.error_code}</dd></>)}
          </dl>
          <div className="dc-op-block">
            <small>参数</small>
            <pre>{JSON.stringify(record.args ?? {}, null, 2)}</pre>
          </div>
          {(record.result_summary || running) && (
            <div className="dc-op-block">
              <small>结果摘要</small>
              <pre>{record.result_summary || "执行中，暂无结果"}</pre>
            </div>
          )}
          {record.error && (
            <div className="dc-op-block error">
              <small>错误</small>
              <pre>{record.error}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function statusIcon(status: DcToolStatus): string {
  switch (status) {
    case "succeeded": return "✓";
    case "failed": return "✗";
    case "timed_out": return "⏱";
    case "cancelled": return "⊘";
    default: return "?";
  }
}

function effectLabel(effect: string): string {
  if (effect === "confirmed") return "已确认生效";
  if (effect === "unknown") return "未确认（需重新观测）";
  return "无副作用";
}
