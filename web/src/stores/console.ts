// 控制台全局状态（zustand）：
// 表单/运行/事件/脚本/Profile/报告 全部集中在单一 store，
// 组件通过选择器订阅所需切片；异步动作沿用旧控制台的并发锁与轮询语义。

import { create } from "zustand";
import { apiError, apiJson, apiUrl, locks, responseDetail } from "../api/client";
import type {
  DiscoveryPolicy,
  DiscoveryStatus,
  ExecuteResult,
  Health,
  ProfileSummary,
  RunEvent,
  RunSummary,
  RunTrace,
  ScriptResult,
  TargetCandidate,
} from "../api/types";
import { readDeepLink, writeDeepLink, type TabKey } from "../app/deep-links";

export type TargetKind = "app_name" | "bundle_name";
export type Operation = "idle" | "generating" | "executing";
export type ProfileAction = "verify" | "lock" | "rollback" | "invalidate";

export const DEFAULT_POLICY: DiscoveryPolicy = {
  enabled: true, allow_login: false, allow_permission: false, allow_submit: false,
  allow_publish: false, allow_download: false, max_pages: 20,
  max_actions_per_page: 8, max_duration_seconds: 900, temporary_test: false,
};

interface ConsoleState {
  /* 环境健康 */
  health: Health | null;
  healthUnavailable: boolean;
  healthLoading: boolean;

  /* 启动器表单 */
  targetKind: TargetKind;
  targetValue: string;
  task: string;
  mode: string;
  policy: DiscoveryPolicy;
  attemptCount: 1 | 3;

  /* 导航 */
  tab: TabKey;

  /* 当前运行 */
  runId: string;
  runBusy: boolean;
  operation: Operation;
  trace: RunTrace | null;
  discovery: DiscoveryStatus | null;
  events: RunEvent[];
  candidates: TargetCandidate[];
  selectedCandidate: string;
  script: ScriptResult | null;

  /* 资产与报告 */
  profiles: ProfileSummary[];
  runs: RunSummary[];
  reportReady: boolean;
  reportLoading: boolean;
  reportRevision: number;

  error: string;

  /* 动作 */
  patchForm: (patch: Partial<Pick<ConsoleState, "targetKind" | "targetValue" | "task" | "mode" | "policy" | "attemptCount" | "selectedCandidate">>) => void;
  setTab: (tab: TabKey) => void;
  setError: (message: string) => void;
  loadHealth: () => Promise<void>;
  loadProfiles: (quiet?: boolean) => Promise<void>;
  loadRuns: () => Promise<void>;
  resolveTarget: () => Promise<void>;
  startRun: () => Promise<void>;
  stopRun: () => Promise<void>;
  submitCandidate: () => Promise<void>;
  selectRun: (runId: string) => void;
  openRun: (runId: string) => void;
  appendEvent: (event: RunEvent) => void;
  refreshTrace: (quiet?: boolean) => Promise<RunTrace | null>;
  refreshDiscovery: (quiet?: boolean) => Promise<DiscoveryStatus | null>;
  loadScript: (quiet?: boolean) => Promise<void>;
  generate: () => Promise<void>;
  execute: () => Promise<void>;
  updateProfile: (profile: ProfileSummary, action: ProfileAction) => Promise<void>;
  probeReport: () => Promise<void>;
  resetRunView: () => void;
}

const initialDeepLink = readDeepLink();

export const useConsole = create<ConsoleState>()((set, get) => ({
  health: null,
  healthUnavailable: false,
  healthLoading: false,

  targetKind: "app_name",
  targetValue: "",
  task: "",
  mode: "regression",
  policy: DEFAULT_POLICY,
  attemptCount: 3,

  tab: initialDeepLink.tab,

  runId: initialDeepLink.runId,
  runBusy: false,
  operation: "idle",
  trace: null,
  discovery: null,
  events: [],
  candidates: [],
  selectedCandidate: "",
  script: null,

  profiles: [],
  runs: [],
  reportReady: false,
  reportLoading: false,
  reportRevision: 0,

  error: "",

  patchForm: (patch) => set(patch),

  setTab: (tab) => {
    set({ tab });
    writeDeepLink(tab, get().runId);
  },

  setError: (message) => set({ error: message }),

  loadHealth: async () => {
    if (!locks.acquire("health")) return;
    set({ healthLoading: true });
    try {
      const health = await apiJson<Health>("/api/health");
      set({ health, healthUnavailable: false, error: "" });
    } catch (cause) {
      set({ health: null, healthUnavailable: true, error: apiError("API 不可用", cause) });
    } finally {
      set({ healthLoading: false });
      locks.release("health");
    }
  },

  loadProfiles: async (quiet = false) => {
    try {
      const profiles = await apiJson<ProfileSummary[]>("/api/profiles");
      set({ profiles });
    } catch (cause) {
      if (!quiet) set({ error: apiError("读取 Profile 失败", cause) });
    }
  },

  loadRuns: async () => {
    try {
      const runs = await apiJson<RunSummary[]>("/api/runs?limit=50");
      set({ runs });
    } catch {
      // 历史列表失败不弹全局错误，避免干扰当前运行视图；再次进入页面时重试。
      set({ runs: [] });
    }
  },

  resolveTarget: async () => {
    const { targetKind, targetValue } = get();
    if (!targetValue.trim() || !locks.acquire("resolve")) return;
    set({ error: "" });
    try {
      const result = await apiJson<{ target?: TargetCandidate; candidates?: TargetCandidate[] }>(
        "/api/targets/resolve",
        { method: "POST", body: { target: { [targetKind]: targetValue.trim() } } },
      );
      set({ candidates: result.candidates ?? [], selectedCandidate: "" });
      if (result.target?.bundle_name && targetKind === "bundle_name") {
        set({ targetValue: result.target.bundle_name });
      }
    } catch (cause) {
      set({ error: apiError("解析目标失败", cause) });
    } finally {
      locks.release("resolve");
    }
  },

  startRun: async () => {
    const { targetKind, targetValue, task, mode, policy } = get();
    if (!targetValue.trim() || !locks.acquire("start")) return;
    set({ runBusy: true, error: "" });
    get().resetRunView();
    try {
      const result = await apiJson<{ run_id: string }>("/api/runs", {
        method: "POST",
        body: {
          target: { [targetKind]: targetValue.trim() },
          task: task.trim() || undefined,
          mode,
          max_steps: 20,
          auto_generate: true,
          discovery: policy,
        },
      });
      get().openRun(result.run_id);
    } catch (cause) {
      set({ error: apiError("启动 Agent 失败", cause), runBusy: false });
    } finally {
      locks.release("start");
    }
  },

  stopRun: async () => {
    const { runId } = get();
    if (!runId || !locks.acquire("stop")) return;
    try {
      await apiJson(`/api/runs/${encodeURIComponent(runId)}/stop`, { method: "POST" });
    } catch (cause) {
      set({ error: apiError("停止运行失败", cause) });
    } finally {
      set({ runBusy: false });
      locks.release("stop");
    }
  },

  submitCandidate: async () => {
    const { runId, candidates, selectedCandidate } = get();
    if (!runId || !selectedCandidate || !locks.acquire("candidate")) return;
    const candidate = candidates.find((item) => (item.candidate_id ?? item.bundle_name) === selectedCandidate);
    if (!candidate) {
      locks.release("candidate");
      return;
    }
    set({ error: "" });
    try {
      await apiJson(`/api/runs/${encodeURIComponent(runId)}/target-selection`, {
        method: "POST",
        body: { candidate_id: candidate.candidate_id, bundle_name: candidate.bundle_name },
      });
      set({ candidates: [], selectedCandidate: "" });
    } catch (cause) {
      set({ error: apiError("确认目标失败", cause) });
    } finally {
      locks.release("candidate");
    }
  },

  /** 选中运行但立即进入实时视图（新启动的运行）。 */
  openRun: (runId) => {
    set({ runId, tab: "live" });
    writeDeepLink("live", runId);
  },

  /** 切换到历史运行：清空当前运行视图后由轮询/SSE 重新拉取。 */
  selectRun: (runId) => {
    if (runId === get().runId) return;
    get().resetRunView();
    set({ runId, runBusy: false, tab: "live" });
    writeDeepLink("live", runId);
  },

  appendEvent: (event) => {
    const current = get().events;
    if (current.some((item) => item.event_id === event.event_id)) return;
    set({ events: [...current, event] });
    if (event.type === "target_candidates_found" && Array.isArray(event.payload.candidates)) {
      set({ candidates: event.payload.candidates as TargetCandidate[] });
    }
  },

  refreshTrace: async (quiet = false) => {
    const { runId } = get();
    if (!runId) return null;
    try {
      const next = await apiJson<RunTrace>(`/api/runs/${encodeURIComponent(runId)}`);
      set({ trace: next });
      if (next.target_candidates?.length) set({ candidates: next.target_candidates });
      return next;
    } catch (cause) {
      if (!quiet) set({ error: apiError("无法刷新运行状态", cause) });
      return null;
    }
  },

  refreshDiscovery: async (quiet = false) => {
    const { runId } = get();
    if (!runId) return null;
    try {
      const next = await apiJson<DiscoveryStatus>(`/api/runs/${encodeURIComponent(runId)}/discovery`);
      set({ discovery: next });
      if (next.target_candidates?.length) set({ candidates: next.target_candidates });
      return next;
    } catch (cause) {
      if (!quiet) set({ error: apiError("无法刷新目标发现状态", cause) });
      return null;
    }
  },

  loadScript: async (quiet = false) => {
    const { runId } = get();
    if (!runId) return;
    try {
      const response = await fetch(apiUrl(`/api/runs/${encodeURIComponent(runId)}/script`));
      if (response.status === 404) {
        set({ script: null });
        return;
      }
      if (!response.ok) throw new Error(await responseDetail(response));
      set({ script: await response.json() as ScriptResult });
    } catch (cause) {
      if (!quiet) set({ error: apiError("读取 Hypium 脚本失败", cause) });
    }
  },

  generate: async () => {
    const { runId } = get();
    if (!runId || !locks.acquire("generate")) return;
    set({ operation: "generating", error: "" });
    try {
      await apiJson(`/api/runs/${encodeURIComponent(runId)}/generate`, { method: "POST" });
      await get().loadScript(true);
      await get().refreshTrace(true);
      set({ reportRevision: get().reportRevision + 1 });
      get().setTab("script");
    } catch (cause) {
      set({ error: apiError("生成 Hypium 脚本失败", cause) });
    } finally {
      set({ operation: "idle" });
      locks.release("generate");
    }
  },

  execute: async () => {
    const { runId, attemptCount } = get();
    if (!runId || !locks.acquire("execute")) return;
    set({ operation: "executing", error: "" });
    try {
      const result = await apiJson<ExecuteResult>(
        `/api/runs/${encodeURIComponent(runId)}/execute?attempts=${attemptCount}`,
        { method: "POST" },
      );
      // 回放是后台任务：轮询 trace 直到 replay_status 离开 pending（上限约 7.5 分钟防呆）。
      let next = await get().refreshTrace(true);
      let ticks = 0;
      while (next?.replay_status === "pending" && ticks < 500) {
        await new Promise((resolve) => window.setTimeout(resolve, 900));
        next = await get().refreshTrace(true);
        ticks += 1;
      }
      if (result.replays) {
        const trace = get().trace;
        if (trace) set({ trace: { ...trace, replays: result.replays } });
      }
      set({ reportRevision: get().reportRevision + 1 });
    } catch (cause) {
      set({ error: apiError("Hypium 验收回放失败", cause) });
    } finally {
      set({ operation: "idle" });
      locks.release("execute");
    }
  },

  updateProfile: async (profile, action) => {
    const key = `profile-${profile.profile_id}-${action}`;
    if (!locks.acquire(key)) return;
    set({ error: "" });
    const profileId = encodeURIComponent(profile.target_app_id ?? profile.profile_id);
    const body = action === "lock" ? { locked: !profile.locked }
      : action === "rollback" ? { backup_name: profile.history?.at(-1)?.backup_name }
      : action === "invalidate" ? { reason: "控制台手动失效" }
      : { status: profile.status };
    try {
      const result = await apiJson<{ run_id?: string }>(`/api/profiles/${profileId}/${action}`, {
        method: "POST",
        body,
      });
      if (action === "verify" && result.run_id) {
        get().resetRunView();
        set({ runId: result.run_id, runBusy: true, tab: "live" });
        writeDeepLink("live", result.run_id);
      }
      await get().loadProfiles(true);
    } catch (cause) {
      set({ error: apiError("更新 Profile 失败", cause) });
    } finally {
      locks.release(key);
    }
  },

  probeReport: async () => {
    const { runId, trace } = get();
    if (!runId || !locks.acquire("report")) return;
    const traceRevision = trace?.revision ?? trace?.updated_at ?? `${trace?.state ?? "none"}`;
    set({ reportLoading: true, reportReady: false });
    try {
      const response = await fetch(
        apiUrl(`/api/runs/${encodeURIComponent(runId)}/report?revision=${encodeURIComponent(String(traceRevision))}`),
      );
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      await response.text();
      set({ reportReady: true });
    } catch (cause) {
      set({ error: apiError("报告暂不可用", cause) });
    } finally {
      set({ reportLoading: false });
      locks.release("report");
    }
  },

  /** 清空运行相关视图状态（启动/切换运行时调用）。 */
  resetRunView: () => {
    set({ trace: null, discovery: null, events: [], script: null, candidates: [], selectedCandidate: "", reportReady: false });
  },
}));
