// 深链管理：?run_id= 与 ?tab= 的读取与写回。
// 旧版 tab 值（live/graph/script/profiles/report）全部兼容，新增 advisor 与 runs。

export const TAB_KEYS = ["live", "graph", "advisor", "script", "profiles", "report", "runs"] as const;
export type TabKey = (typeof TAB_KEYS)[number];

/** 从当前地址读取深链参数；非法 tab 回退为 live。 */
export function readDeepLink(): { runId: string; tab: TabKey } {
  const params = new URLSearchParams(window.location.search);
  const requested = params.get("tab");
  const tab = requested && (TAB_KEYS as readonly string[]).includes(requested) ? (requested as TabKey) : "live";
  return { runId: params.get("run_id") ?? "", tab };
}

/** 导航后把 tab 与 run_id 同步回地址栏（不产生历史堆栈噪音）。 */
export function writeDeepLink(tab: TabKey, runId: string): void {
  const params = new URLSearchParams(window.location.search);
  if (runId) params.set("run_id", runId);
  else params.delete("run_id");
  params.set("tab", tab);
  const next = `${window.location.pathname}?${params.toString()}`;
  window.history.replaceState(null, "", next);
}
