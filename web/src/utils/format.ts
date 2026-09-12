// 展示层格式化工具：耗时、状态文案、证据文件名。

/** 毫秒 → 「1.2 s」或「350 ms」。 */
export function formatDuration(milliseconds: number): string {
  return milliseconds >= 1000 ? `${(milliseconds / 1000).toFixed(1)} s` : `${Math.max(0, Math.round(milliseconds))} ms`;
}

/** 回放尝试的归一化状态（兼容旧字段 passed 布尔）。 */
export function replayStatus(attempt: { status?: string; passed: boolean }): string {
  if (attempt.status) return attempt.status.toLowerCase();
  return attempt.passed ? "passed" : "failed";
}

const STATUS_LABELS: Record<string, string> = {
  queued: "等待",
  pending: "等待",
  running: "执行中",
  passed: "通过",
  failed: "失败",
  timed_out: "超时",
  ineligible: "不合格",
  invalid_result: "结果无效",
};

/** 回放状态的中文文案。 */
export function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status;
}

/** 从证据路径中提取文件名用于链接文案。 */
export function evidenceLabel(path: string): string {
  const name = path.replaceAll("\\", "/").split("/").at(-1);
  return name || "证据";
}

/** 运行状态 → 徽章色阶。 */
export function stateTone(state: string): "ok" | "warn" | "danger" | "brand" | "neutral" {
  if (state === "completed") return "ok";
  if (state === "stopped_by_user") return "warn";
  if (state.startsWith("failed")) return "danger";
  if (state !== "idle" && state !== "created") return "brand";
  return "neutral";
}

/** 通用的时间展示（HH:MM:SS）。 */
export function timeLabel(timestamp: string): string {
  const date = new Date(timestamp);
  return Number.isNaN(date.getTime()) ? timestamp : date.toLocaleTimeString();
}
