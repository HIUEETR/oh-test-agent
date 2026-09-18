// 深链管理：?run_id= 与 ?tab= 的读取与写回。
//
// 2026-09-17 重构：顶层 Tab 由 8 个收敛为 5 个
// （session / graph / script / profiles / runs）。旧深链值
// （live / advisor / dc → session，report → script）继续兼容，
// 避免用户收藏或历史记录里的链接失效。

export const TAB_KEYS = ["session", "graph", "script", "profiles", "runs"] as const;
export type TabKey = (typeof TAB_KEYS)[number];

/** 旧 tab 值 → 新 tab 值的兼容映射（只在读取深链时生效，不写回地址栏）。 */
export const LEGACY_TAB_ALIASES: Record<string, TabKey> = {
  live: "session",
  advisor: "session",
  dc: "session",
  report: "script",
};

/** 默认 Tab（无法识别或缺失时使用）。 */
export const DEFAULT_TAB: TabKey = "session";

/** 归一化深链 tab 值：合法新值原样返回，旧值映射，其余回退默认。 */
export function normalizeTab(requested: string | null): TabKey {
  if (!requested) return DEFAULT_TAB;
  if ((TAB_KEYS as readonly string[]).includes(requested)) return requested as TabKey;
  return LEGACY_TAB_ALIASES[requested] ?? DEFAULT_TAB;
}

/** 从当前地址读取深链参数。 */
export function readDeepLink(): { runId: string; tab: TabKey } {
  const params = new URLSearchParams(window.location.search);
  return { runId: params.get("run_id") ?? "", tab: normalizeTab(params.get("tab")) };
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
