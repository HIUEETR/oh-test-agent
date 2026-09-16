// DC 模式独立 zustand store。
// 完全不引用 stores/console.ts（隔离要求）；两者状态互不影响。

import { create } from "zustand";
import { apiError } from "../api/client";
import {
  closeDcSession,
  createDcSession,
  dcArtifactUrl,
  generateDcScript,
  getDcSession,
  listDcSessions,
  resumeDcSession,
  sendDcMessage,
  setDcTier,
  stopDcTurn,
} from "../api/dc-client";
import type {
  DcActivityKind,
  DcActivitySnapshot,
  DcActivityState,
  DcCancelStatus,
  DcChatMessage,
  DcContinuationContext,
  DcEffectStatus,
  DcEvent,
  DcLiveConnection,
  DcLiveToolRecord,
  DcScriptArtifact,
  DcSessionSummary,
  DcSessionView,
  DcToolInvocation,
  DcToolStatus,
  DcToolTier,
} from "../api/dc-types";

/** 去重用的本地事件缓冲上限；后端 bus 自身 buffer 为 500。 */
const MAX_TRACKED_EVENTS = 500;

/** 停止按钮：请求后多久仍未有终态，就给出「设备状态仍在确认」的可读提示。 */
const CANCEL_CONFIRM_DELAY_MS = 4000;
/** 停止按钮：超过此时间仍未收到终态，明确提示可能需要刷新。 */
const CANCEL_STALL_MS = 30000;

/** 活动状态默认值（空闲）。 */
export function idleActivity(): DcActivityState {
  return {
    kind: "idle",
    phaseLabel: "",
    phase: "",
    operationId: "",
    turnId: null,
    tool: null,
    startedAt: null,
    lastProgressAt: null,
    deadlineAt: null,
    elapsedMs: null,
    remainingMs: null,
    cancellable: false,
    attentionReason: null,
    attentionOptions: [],
    epoch: 0,
  };
}

/** 阶段标识 → 中文文案（后端 phase 可能来自工具内部拆分）。 */
const PHASE_LABELS: Record<string, string> = {
  waiting_model: "正在等待模型",
  model: "正在等待模型",
  snapshot_display: "正在采集截图",
  file_recv: "正在接收设备文件",
  file_send: "正在推送文件",
  decode: "正在解码截图",
  cleanup: "正在清理临时文件",
  dump_ui_hierarchy: "正在读取 UI 层级",
  ui_tree: "正在读取 UI 层级",
  inspect_screen: "正在解析界面元素",
  collect_logs: "正在收集日志",
  foreground_app: "正在读取前台应用",
  validating: "正在校验结果",
  validate: "正在校验结果",
  waiting_device: "正在等待设备响应",
  device: "正在等待设备响应",
  hdc: "正在等待设备响应",
  running: "正在等待设备响应",
  reconciling: "正在核对设备状态",
  cancelling: "正在停止执行",
  capturing_context: "正在采集上下文",
  context: "正在采集上下文",
  finished: "已结束",
};

interface DcState {
  /* 会话列表与选择 */
  sessions: DcSessionSummary[];
  activeSessionId: string;
  session: DcSessionView | null;

  /* 对话与事件 */
  events: DcEvent[];
  messages: DcChatMessage[];
  latestScreenshotUrl: string | null;
  uiTreeDigest: string;
  status: string;
  sending: boolean;

  /* 脚本 */
  script: DcScriptArtifact | null;

  /* 错误 */
  error: string;

  /* ---- 实时执行账本（Phase 1） ---- */
  liveInvocations: DcLiveToolRecord[];
  liveInvocationIndex: Record<string, DcLiveToolRecord>;
  activity: DcActivityState;
  cancelStatus: DcCancelStatus;
  cancelRequestedAt: string | null;
  connection: DcLiveConnection;
  syncing: boolean;
  lastSyncedAt: string | null;
  /** 最近一轮的公开连续性摘要（Phase 3） */
  continuation: DcContinuationContext | null;
  /** needs_attention 的处置选项 */
  attentionOptions: string[];
  attentionReason: string;

  /* 动作 */
  loadSessions: () => Promise<void>;
  createSession: (deviceId?: string) => Promise<void>;
  selectSession: (sessionId: string) => void;
  refreshSession: (quiet?: boolean) => Promise<void>;
  sendMessage: (text: string) => Promise<void>;
  stopTurn: () => Promise<void>;
  changeTier: (tier: DcToolTier) => Promise<void>;
  generateScript: (bundleName?: string, mainAbility?: string) => Promise<void>;
  closeSession: () => Promise<void>;
  appendEvent: (event: DcEvent) => void;
  setConnection: (connection: DcLiveConnection) => void;
  setError: (message: string) => void;
  reset: () => void;
}

/** 空账本引用：保持稳定，避免 zustand selector 每次产生新数组导致无限重渲染。 */
const NO_LIVE_INVOCATIONS: DcLiveToolRecord[] = [];
export const EMPTY_RECORDS: DcLiveToolRecord[] = NO_LIVE_INVOCATIONS;

export const useDcConsole = create<DcState>()((set, get) => ({
  sessions: [],
  activeSessionId: "",
  session: null,
  events: [],
  messages: [],
  latestScreenshotUrl: null,
  uiTreeDigest: "",
  status: "idle",
  sending: false,
  script: null,
  error: "",

  liveInvocations: NO_LIVE_INVOCATIONS,
  liveInvocationIndex: {},
  activity: idleActivity(),
  cancelStatus: "idle",
  cancelRequestedAt: null,
  connection: "idle",
  syncing: false,
  lastSyncedAt: null,
  continuation: null,
  attentionOptions: [],
  attentionReason: "",

  loadSessions: async () => {
    try {
      const sessions = await listDcSessions();
      set({ sessions });
    } catch (cause) {
      set({ error: apiError("加载 DC 会话列表失败", cause) });
    }
  },

  createSession: async (deviceId?: string) => {
    try {
      set({ error: "" });
      const result = await createDcSession({ device_id: deviceId });
      set((state) => ({
        activeSessionId: result.session_id,
        sessions: [...state.sessions, {
          session_id: result.session_id,
          device_id: result.device_id,
          tier: result.tier,
          status: result.status,
          created_at: new Date().toISOString(),
          last_active_at: new Date().toISOString(),
          turn_count: 0,
          invocation_count: 0,
          active: true,
          script_available: false,
        }],
        events: [],
        messages: [],
        session: null,
        script: null,
        latestScreenshotUrl: null,
        status: "idle",
        ...emptyLivePatch(),
      }));
      await get().refreshSession();
      void get().loadSessions();
    } catch (cause) {
      set({ error: apiError("创建 DC 会话失败", cause) });
    }
  },

  selectSession: (sessionId: string) => {
    // 会话切换：清理旧会话的实时账本，并让旧会话迟到的 SSE 回调失效
    sessionEpoch += 1;
    set({
      activeSessionId: sessionId,
      events: [],
      messages: [],
      session: null,
      script: null,
      latestScreenshotUrl: null,
      status: "idle",
      error: "",
      syncing: Boolean(sessionId),
      ...emptyLivePatch(),
    });
    if (!sessionId) return;
    const summary = get().sessions.find((item) => item.session_id === sessionId);
    if (summary && summary.active === false) {
      // 历史会话：先从磁盘快照恢复，再展示（恢复后可直接继续对话）
      void (async () => {
        try {
          const view = await resumeDcSession(sessionId);
          if (get().activeSessionId !== sessionId) return; // 期间已切换会话
          // 恢复结果本身就是一次全量投影，直接作为账本基线（不再多发一次 GET）
          const ledger = mergeLedger(NO_LIVE_INVOCATIONS, view.invocations);
          set((state) => ({
            session: view,
            status: view.status,
            script: view.script ?? null,
            messages: aggregateMessages(view),
            latestScreenshotUrl: view.latest_snapshot_path
              ? dcArtifactUrl(sessionId, view.latest_snapshot_path)
              : state.latestScreenshotUrl,
            syncing: false,
            lastSyncedAt: new Date().toISOString(),
            continuation: view.continuation ?? null,
            liveInvocations: ledger,
            liveInvocationIndex: buildLedgerIndex(ledger),
          }));
          await get().loadSessions();
        } catch (cause) {
          if (get().activeSessionId !== sessionId) return;
          set({ error: apiError("恢复历史会话失败", cause), syncing: false });
        }
      })();
      return;
    }
    void get().refreshSession();
  },

  refreshSession: async (quiet = false) => {
    const { activeSessionId } = get();
    if (!activeSessionId) return;
    // 会话切换后旧请求的回包必须丢弃，避免写入新会话状态
    const epoch = sessionEpoch;
    if (!quiet) set({ syncing: true });
    try {
      const session = await getDcSession(activeSessionId);
      if (get().activeSessionId !== activeSessionId || sessionEpoch !== epoch) return;
      // 进行中轮次由增量事件驱动，全量重建只用于已完成轮次（避免双写冲突）
      const inFlight = session.status === "thinking" || session.status === "acting";
      const ledger = mergeLedger(get().liveInvocations, session.invocations);
      const patch: Partial<DcState> = {
        session,
        status: session.status,
        script: session.script ?? null,
        latestScreenshotUrl: session.latest_snapshot_path
          ? dcArtifactUrl(activeSessionId, session.latest_snapshot_path)
          : get().latestScreenshotUrl,
        // 对账：实时账本只补缺失字段，running 行不会被快照清空或回退
        liveInvocations: ledger,
        liveInvocationIndex: buildLedgerIndex(ledger),
        continuation: session.continuation ?? get().continuation,
        syncing: false,
        lastSyncedAt: new Date().toISOString(),
        ...(quiet ? {} : { error: "" }),
      };
      if (!inFlight) patch.messages = aggregateMessages(session);
      set(patch);
    } catch (cause) {
      if (get().activeSessionId !== activeSessionId || sessionEpoch !== epoch) return;
      set({ syncing: false });
      if (!quiet) set({ error: apiError("刷新 DC 会话失败", cause) });
    }
  },

  sendMessage: async (text: string) => {
    const { activeSessionId } = get();
    if (!activeSessionId || !text.trim()) return;
    set({ sending: true, error: "" });
    // 立即追加用户消息到本地列表（乐观更新）
    const userMessage: DcChatMessage = {
      id: `local-${Date.now()}`,
      role: "user",
      content: text,
      timestamp: new Date().toISOString(),
    };
    set((state) => ({ messages: [...state.messages, userMessage] }));
    try {
      await sendDcMessage(activeSessionId, text);
      // 不立即 refreshSession；SSE 事件会推送更新
    } catch (cause) {
      set({ error: apiError("发送消息失败", cause) });
    } finally {
      set({ sending: false });
    }
  },

  stopTurn: async () => {
    const { activeSessionId } = get();
    if (!activeSessionId) return;
    // 阶段一：请求停止。按钮立即进入「正在确认设备状态」，不永久转圈。
    const requestedAt = new Date().toISOString();
    set((state) => ({
      cancelStatus: "confirming",
      cancelRequestedAt: requestedAt,
      activity: {
        ...state.activity,
        kind: "cancelling",
        phaseLabel: "正在确认设备状态",
        lastProgressAt: requestedAt,
        epoch: state.activity.epoch + 1,
      },
    }));
    try {
      await stopDcTurn(activeSessionId);
    } catch (cause) {
      set({
        error: apiError("停止轮次失败", cause),
        cancelStatus: "settled",
      });
    }
  },

  changeTier: async (tier: DcToolTier) => {
    const { activeSessionId } = get();
    if (!activeSessionId) return;
    try {
      await setDcTier(activeSessionId, tier);
      await get().refreshSession(true);
    } catch (cause) {
      set({ error: apiError("变更工具层级失败", cause) });
    }
  },

  generateScript: async (bundleName?: string, mainAbility?: string) => {
    const { activeSessionId } = get();
    if (!activeSessionId) return;
    try {
      const script = await generateDcScript(activeSessionId, bundleName, mainAbility);
      set({ script });
    } catch (cause) {
      set({ error: apiError("生成脚本失败", cause) });
    }
  },

  closeSession: async () => {
    const { activeSessionId } = get();
    if (!activeSessionId) return;
    try {
      await closeDcSession(activeSessionId);
      sessionEpoch += 1;
      set({
        activeSessionId: "",
        session: null,
        events: [],
        messages: [],
        script: null,
        latestScreenshotUrl: null,
        status: "idle",
        ...emptyLivePatch(),
      });
      // 会话仍在磁盘快照中：刷新列表使其以「历史会话」出现，可随时恢复
      await get().loadSessions();
    } catch (cause) {
      set({ error: apiError("关闭会话失败", cause) });
    }
  },

  setConnection: (connection: DcLiveConnection) => set({ connection }),

  appendEvent: (event: DcEvent) => {
    // 旧会话迟到的回调：直接丢弃，避免污染新会话状态
    const currentSessionId = get().activeSessionId;
    if (event.session_id && currentSessionId && event.session_id !== currentSessionId) return;
    if (!currentSessionId) return;
    let reconcile = false;
    set((state) => {
      // 去重（SSE 重连 replay 可能重复投递）
      if (state.events.some((e) => e.event_id === event.event_id)) return state;
      // 只保留最近 500 条用于去重，避免长会话内存无限增长
      const events = [...state.events, event].slice(-MAX_TRACKED_EVENTS);
      const patch: Partial<DcState> = { events };
      const p = event.payload as Record<string, any>;

      switch (event.type) {
        case "screenshot_captured": {
          // 后端给的是会话相对 POSIX 路径；失败帧 snapshot_path 为 null，保持上一帧
          const rel = p.snapshot_path as string | null | undefined;
          if (rel && state.activeSessionId) {
            patch.latestScreenshotUrl = dcArtifactUrl(state.activeSessionId, rel);
          }
          break;
        }
        case "turn_started": {
          patch.status = "thinking";
          patch.sending = true;
          patch.cancelStatus = "idle";
          patch.cancelRequestedAt = null;
          patch.attentionOptions = [];
          patch.attentionReason = "";
          const turnId = (p.turn_id as string) ?? null;
          patch.activity = nextActivity(state.activity, {
            kind: "capturing_context",
            phase: "capturing_context",
            turnId,
          });
          break;
        }
        case "context_capture_started": {
          patch.status = "thinking";
          patch.activity = nextActivity(state.activity, {
            kind: "capturing_context",
            phase: (p.phase as string) || "capturing_context",
            turnId: (p.turn_id as string) ?? null,
            startedAt: event.timestamp,
          });
          break;
        }
        case "context_capture_finished":
          patch.activity = nextActivity(state.activity, {
            kind: "capturing_context",
            phase: (p.phase as string) || "context_capture_finished",
          });
          break;
        case "model_call_started": {
          patch.status = "thinking";
          patch.activity = nextActivity(state.activity, {
            kind: "model",
            phase: (p.phase as string) || "waiting_model",
            operationId: (p.model_call_id as string) ?? "",
            turnId: (p.turn_id as string) ?? null,
            startedAt: (p.started_at as string) ?? event.timestamp,
            lastProgressAt: event.timestamp,
            deadlineAt: (p.deadline_at as string) ?? null,
            remainingMs: numberOrNull(p.remaining_budget_ms),
            cancellable: true,
          });
          break;
        }
        case "model_call_progress": {
          patch.activity = nextActivity(state.activity, {
            kind: "model",
            phase: (p.phase as string) || "waiting_model",
            operationId: (p.model_call_id as string) ?? state.activity.operationId,
            turnId: (p.turn_id as string) ?? null,
            startedAt: state.activity.startedAt ?? event.timestamp,
            lastProgressAt: (p.last_progress_at as string) ?? event.timestamp,
            elapsedMs: numberOrNull(p.elapsed_ms),
            remainingMs: numberOrNull(p.remaining_ms),
            cancellable: true,
          });
          break;
        }
        case "model_call_finished": {
          patch.activity = nextActivity(state.activity, {
            kind: "model",
            phase: "validating",
            operationId: (p.model_call_id as string) ?? state.activity.operationId,
            lastProgressAt: event.timestamp,
            elapsedMs: numberOrNull(p.duration_ms),
          });
          break;
        }
        case "model_call_failed": {
          patch.activity = nextActivity(state.activity, {
            kind: "model",
            phase: (p.error_code as string) === "model_timeout" ? "model_timeout" : "model_failed",
            operationId: (p.model_call_id as string) ?? state.activity.operationId,
            lastProgressAt: event.timestamp,
            elapsedMs: numberOrNull(p.duration_ms),
          });
          break;
        }
        case "thinking":
          patch.messages = [
            ...state.messages,
            {
              id: `think-${event.event_id}`,
              role: "thinking",
              content: (p.text as string) ?? event.message,
              timestamp: event.timestamp,
              turnId: p.turn_id as string | undefined,
              step: p.step as number | undefined,
            },
          ];
          break;
        case "agent_text":
          patch.messages = [
            ...state.messages,
            {
              id: `text-${event.event_id}`,
              role: "narration",
              content: (p.text as string) ?? event.message,
              timestamp: event.timestamp,
              turnId: p.turn_id as string | undefined,
              step: p.step as number | undefined,
            },
          ];
          break;
        case "tool_call_started": {
          const record = recordFromStarted(p, event.timestamp);
          const ledger = upsertRecord(state.liveInvocations, record);
          patch.liveInvocations = ledger;
          patch.liveInvocationIndex = buildLedgerIndex(ledger);
          patch.messages = upsertToolMessage(state.messages, record, event.timestamp);
          patch.status = "acting";
          patch.activity = nextActivity(state.activity, {
            kind: "tool",
            phase: record.phase,
            operationId: record.invocation_id,
            turnId: record.turn_id,
            tool: record.tool,
            startedAt: record.started_at,
            lastProgressAt: record.last_progress_at ?? record.started_at,
            deadlineAt: record.deadline_at,
            cancellable: record.cancellable,
          });
          break;
        }
        case "tool_call_progress": {
          const existing = state.liveInvocationIndex[p.invocation_id as string];
          const record: DcLiveToolRecord = {
            ...(existing ?? recordFromProgressSeed(p)),
            phase: (p.phase as string) ?? existing?.phase ?? "",
            last_progress_at: (p.last_progress_at as string) ?? event.timestamp,
            cancellable: p.cancellable == null ? (existing?.cancellable ?? true) : Boolean(p.cancellable),
            status: "running",
            live: true,
            // running 行不能被进度事件回退为终态
            ended_at: null,
          };
          const ledger = upsertRecord(state.liveInvocations, record);
          patch.liveInvocations = ledger;
          patch.liveInvocationIndex = buildLedgerIndex(ledger);
          patch.messages = upsertToolMessage(state.messages, record, event.timestamp);
          patch.activity = nextActivity(state.activity, {
            kind: "tool",
            phase: record.phase,
            operationId: record.invocation_id,
            turnId: record.turn_id,
            tool: record.tool,
            startedAt: existing?.started_at ?? state.activity.startedAt ?? event.timestamp,
            lastProgressAt: record.last_progress_at,
            deadlineAt: existing?.deadline_at ?? null,
            elapsedMs: numberOrNull(p.elapsed_ms),
            remainingMs: numberOrNull(p.remaining_ms),
            cancellable: record.cancellable,
          });
          break;
        }
        case "tool_call_finished": {
          const existing = state.liveInvocationIndex[p.invocation_id as string];
          const record: DcLiveToolRecord = {
            ...(existing ?? recordFromFinished(p, event.timestamp)),
            status: normalizeToolStatus(p.status, p.success),
            ended_at: (p.ended_at as string) ?? event.timestamp,
            duration_ms: numberOrNull(p.duration_ms) ?? existing?.duration_ms ?? null,
            result_summary: (p.result_summary as string) ?? existing?.result_summary ?? "",
            error: (p.error as string) ?? null,
            error_code: (p.error_code as string) ?? null,
            effect_status: normalizeEffect(p.effect_status, existing?.effect_status),
            phase: "finished",
            live: true,
            cancellable: false,
          };
          const ledger = upsertRecord(state.liveInvocations, record);
          patch.liveInvocations = ledger;
          patch.liveInvocationIndex = buildLedgerIndex(ledger);
          patch.messages = upsertToolMessage(state.messages, record, event.timestamp);
          patch.activity = settleToolActivity(state.activity, record);
          break;
        }
        case "turn_cancel_requested": {
          patch.cancelStatus = "confirming";
          patch.cancelRequestedAt = (p.requested_at as string) ?? event.timestamp;
          patch.activity = nextActivity(state.activity, {
            kind: "cancelling",
            phaseLabelOverride: "正在确认设备状态",
            lastProgressAt: event.timestamp,
          });
          break;
        }
        case "turn_interrupted": {
          patch.cancelStatus = "settled";
          patch.status = "idle";
          patch.sending = false;
          patch.activity = nextActivity(state.activity, {
            kind: "idle",
            phase: (p.reason as string) || "interrupted",
            phaseLabelOverride: `已中断：${(p.reason as string) || "执行环境中断"}`,
          });
          void get().refreshSession(true);
          break;
        }
        case "context_checkpoint": {
          // 只记录版本号触发对账，不重建消息
          patch.lastSyncedAt = state.lastSyncedAt;
          break;
        }
        case "needs_attention": {
          patch.attentionReason = (p.reason as string) ?? event.message;
          patch.attentionOptions = Array.isArray(p.options) ? (p.options as string[]) : [];
          patch.cancelStatus = "settled";
          patch.status = "idle";
          patch.sending = false;
          patch.activity = nextActivity(state.activity, {
            kind: "attention",
            phaseLabelOverride: "需要人工确认",
            operationId: (p.invocation_id as string) ?? state.activity.operationId,
            attentionReason: (p.reason as string) ?? event.message,
            attentionOptions: Array.isArray(p.options) ? (p.options as string[]) : [],
          });
          break;
        }
        case "assistant_message": {
          patch.status = "idle";
          patch.sending = false;
          patch.cancelStatus = "settled";
          patch.activity = nextActivity(state.activity, { kind: "idle", phaseLabelOverride: "" });
          const summary = (p.summary as string) ?? event.message;
          // 终态回答可能已作为 narration（agent_text）渲染过（历史记录或 Mock 场景）：
          // 同一轮中文本相同的叙述就地升级为助手消息，避免出现两条「任务完成」。
          const last = state.messages[state.messages.length - 1];
          const sameTurn = last && (p.turn_id == null || last.turnId === p.turn_id);
          if (last && sameTurn && last.role === "narration" && normalizeText(last.content) === normalizeText(summary)) {
            patch.messages = [
              ...state.messages.slice(0, -1),
              { ...last, id: `final-${event.event_id}`, role: "assistant", content: summary, timestamp: event.timestamp },
            ];
          } else {
            patch.messages = [
              ...state.messages,
              {
                id: `final-${event.event_id}`,
                role: "assistant",
                content: summary,
                timestamp: event.timestamp,
                turnId: p.turn_id as string | undefined,
              },
            ];
          }
          break;
        }
        case "turn_finished": {
          const status = (p.status as string) ?? "";
          patch.status = "idle";
          patch.sending = false;
          patch.cancelStatus = "settled";
          patch.activity = nextActivity(state.activity, {
            kind: "idle",
            phaseLabelOverride: turnFinishedLabel(status),
          });
          // 终态：做一次 GET 对账，补齐 running 行缺失的字段（不回退实时状态）
          reconcile = true;
          break;
        }
        case "error":
          patch.error = event.message;
          patch.status = "idle";
          patch.sending = false;
          patch.cancelStatus = "settled";
          patch.activity = nextActivity(state.activity, { kind: "idle", phaseLabelOverride: "" });
          reconcile = true;
          break;
        case "script_generated":
          reconcile = true;
          break;
        default:
          break;
      }

      return { ...state, ...patch };
    });
    // 在 set 之外触发对账，避免嵌套 set 与重复请求；同一 tick 内只对账一次
    if (reconcile) scheduleReconcile();
  },

  setError: (message: string) => set({ error: message }),

  reset: () =>
    set({
      sessions: [],
      activeSessionId: "",
      session: null,
      events: [],
      messages: [],
      latestScreenshotUrl: null,
      uiTreeDigest: "",
      status: "idle",
      sending: false,
      script: null,
      error: "",
      ...emptyLivePatch(),
    }),
}));

// ---------------------------------------------------------------------------
// 会话代次：用于丢弃旧会话迟到的 SSE 回调与 GET 回包
// ---------------------------------------------------------------------------

let sessionEpoch = 0;

/** 同一 tick 内最多对账一次：终态事件密集时不会打出多次 GET。 */
let reconcileScheduled = false;

function scheduleReconcile(): void {
  if (reconcileScheduled) return;
  reconcileScheduled = true;
  queueMicrotask(() => {
    reconcileScheduled = false;
    void useDcConsole.getState().refreshSession(true);
  });
}

/** 清空实时账本相关的状态（会话切换/关闭时使用）。 */
function emptyLivePatch(): Partial<DcState> {
  return {
    liveInvocations: NO_LIVE_INVOCATIONS,
    liveInvocationIndex: {},
    activity: idleActivity(),
    cancelStatus: "idle",
    cancelRequestedAt: null,
    connection: "idle",
    syncing: false,
    lastSyncedAt: null,
    continuation: null,
    attentionOptions: [],
    attentionReason: "",
  };
}

// ---------------------------------------------------------------------------
// 实时操作账本：snapshot（GET）是权威事实，SSE 是增量补丁，按 invocation_id 合并
// ---------------------------------------------------------------------------

const KNOWN_STATUSES: DcToolStatus[] = ["running", "succeeded", "failed", "timed_out", "cancelled", "unknown"];
const ACTIVE_STATUSES: DcToolStatus[] = ["running"];

/** 状态归一化：旧服务只给 `success: boolean` 时按契约推导。 */
export function normalizeToolStatus(raw: unknown, success?: unknown): DcToolStatus {
  if (typeof raw === "string" && (KNOWN_STATUSES as string[]).includes(raw)) return raw as DcToolStatus;
  if (typeof raw === "string" && raw) return "unknown";
  if (typeof success === "boolean") return success ? "succeeded" : "failed";
  return "unknown";
}

/** 是否处于活动（未结束）状态。 */
export function isActiveToolStatus(status: DcToolStatus): boolean {
  return ACTIVE_STATUSES.includes(status);
}

/** 把快照中的一条 invocation 归一化为账本行。 */
function liveFromInvocation(inv: DcToolInvocation): DcLiveToolRecord {
  const status = normalizeToolStatus(inv.status, inv.success);
  return {
    invocation_id: inv.invocation_id,
    turn_id: inv.turn_id,
    tool: inv.tool,
    args: inv.args ?? {},
    status,
    phase: inv.phase ?? "",
    started_at: inv.started_at,
    ended_at: inv.ended_at ?? null,
    duration_ms: inv.duration_ms ?? null,
    last_progress_at: inv.last_progress_at ?? null,
    deadline_at: inv.deadline_at ?? null,
    effect_status: (inv.effect_status as DcEffectStatus) ?? "none",
    command_id: inv.command_id ?? null,
    result_summary: inv.result_summary ?? "",
    error: inv.error ?? null,
    error_code: inv.error_code ?? null,
    cancellable: inv.cancellable ?? isActiveToolStatus(status),
    live: false,
  };
}

/** 历史快照合并进实时账本：已存在的行只补缺失字段，实时状态绝不回退。 */
export function mergeLedger(
  ledger: DcLiveToolRecord[],
  snapshot: readonly DcToolInvocation[] | null | undefined,
): DcLiveToolRecord[] {
  const order: string[] = ledger.map((record) => record.invocation_id);
  const byId = new Map<string, DcLiveToolRecord>();
  for (const record of ledger) byId.set(record.invocation_id, record);

  for (const inv of snapshot ?? []) {
    const live = byId.get(inv.invocation_id);
    if (!live) {
      // 历史记录：直接补进账本
      order.push(inv.invocation_id);
      byId.set(inv.invocation_id, liveFromInvocation(inv));
      continue;
    }
    const snap = liveFromInvocation(inv);
    const merged: DcLiveToolRecord = { ...live };
    // 1) 实时行处于活动状态时，快照不得清空或回退
    if (!isActiveToolStatus(live.status)) merged.status = live.status;
    // 2) 只补缺失信息
    if (!merged.phase && snap.phase) merged.phase = snap.phase;
    if (merged.ended_at == null && snap.ended_at) merged.ended_at = snap.ended_at;
    if (merged.duration_ms == null) merged.duration_ms = snap.duration_ms;
    if (merged.last_progress_at == null) merged.last_progress_at = snap.last_progress_at;
    if (merged.deadline_at == null) merged.deadline_at = snap.deadline_at;
    if (merged.command_id == null) merged.command_id = snap.command_id;
    if (merged.error_code == null) merged.error_code = snap.error_code;
    if (!merged.result_summary) merged.result_summary = snap.result_summary;
    if (merged.error == null) merged.error = snap.error;
    if (!merged.args || Object.keys(merged.args).length === 0) merged.args = snap.args;
    if (merged.effect_status === "none" && snap.effect_status !== "none") merged.effect_status = snap.effect_status;
    // 3) 快照已给出终态而实时行还没有终态时，采纳快照终态
    if (isActiveToolStatus(merged.status) && !isActiveToolStatus(snap.status)) {
      merged.status = snap.status;
      merged.cancellable = false;
    }
    byId.set(inv.invocation_id, merged);
  }

  return order.map((id) => byId.get(id)!).filter(Boolean);
}

function buildLedgerIndex(ledger: DcLiveToolRecord[]): Record<string, DcLiveToolRecord> {
  const index: Record<string, DcLiveToolRecord> = {};
  for (const record of ledger) index[record.invocation_id] = record;
  return index;
}

/** 按 invocation_id 就地更新；不存在时追加到末尾（不产生重复行）。 */
export function upsertRecord(ledger: DcLiveToolRecord[], record: DcLiveToolRecord): DcLiveToolRecord[] {
  const position = ledger.findIndex((item) => item.invocation_id === record.invocation_id);
  if (position < 0) return [...ledger, record];
  const next = ledger.slice();
  next[position] = record;
  return next;
}

/** tool_call_started 事件 → 账本行。 */
function recordFromStarted(p: Record<string, any>, timestamp: string): DcLiveToolRecord {
  return {
    invocation_id: String(p.invocation_id ?? ""),
    turn_id: String(p.turn_id ?? ""),
    tool: String(p.tool ?? ""),
    args: (p.args as Record<string, unknown>) ?? {},
    status: "running",
    phase: (p.phase as string) ?? "",
    started_at: (p.started_at as string) ?? timestamp,
    ended_at: null,
    duration_ms: null,
    last_progress_at: (p.last_progress_at as string) ?? (p.started_at as string) ?? timestamp,
    deadline_at: (p.deadline_at as string) ?? null,
    effect_status: normalizeEffect(p.effect_status, undefined),
    command_id: (p.command_id as string) ?? null,
    result_summary: "",
    error: null,
    error_code: null,
    cancellable: p.cancellable == null ? true : Boolean(p.cancellable),
    live: true,
  };
}

/** tool_call_progress 在缺少 started 事件时的兜底行。 */
function recordFromProgressSeed(p: Record<string, any>): DcLiveToolRecord {
  return {
    invocation_id: String(p.invocation_id ?? ""),
    turn_id: String(p.turn_id ?? ""),
    tool: String(p.tool ?? ""),
    args: {},
    status: "running",
    phase: "",
    started_at: new Date().toISOString(),
    ended_at: null,
    duration_ms: null,
    last_progress_at: null,
    deadline_at: null,
    effect_status: "none",
    command_id: null,
    result_summary: "",
    error: null,
    error_code: null,
    cancellable: true,
    live: true,
  };
}

/** tool_call_finished 在缺少 started 事件时的兜底行（幂等，不产生重复）。 */
function recordFromFinished(p: Record<string, any>, timestamp: string): DcLiveToolRecord {
  return {
    invocation_id: String(p.invocation_id ?? ""),
    turn_id: String(p.turn_id ?? ""),
    tool: String(p.tool ?? ""),
    args: (p.args as Record<string, unknown>) ?? {},
    status: normalizeToolStatus(p.status, p.success),
    phase: "finished",
    started_at: (p.started_at as string) ?? timestamp,
    ended_at: (p.ended_at as string) ?? timestamp,
    duration_ms: numberOrNull(p.duration_ms),
    last_progress_at: null,
    deadline_at: null,
    effect_status: normalizeEffect(p.effect_status, undefined),
    command_id: (p.command_id as string) ?? null,
    result_summary: (p.result_summary as string) ?? "",
    error: (p.error as string) ?? null,
    error_code: (p.error_code as string) ?? null,
    cancellable: false,
    live: true,
  };
}

function normalizeEffect(raw: unknown, fallback: DcEffectStatus | undefined): DcEffectStatus {
  if (raw === "none" || raw === "confirmed" || raw === "unknown") return raw;
  return fallback ?? "none";
}

/** 工具行 → 聊天工具卡状态。 */
export function cardStatusFor(status: DcToolStatus): "running" | "success" | "failed" {
  if (isActiveToolStatus(status)) return "running";
  return status === "succeeded" ? "success" : "failed";
}

function recordToMessage(record: DcLiveToolRecord, timestamp: string, previous?: DcChatMessage): DcChatMessage {
  const running = isActiveToolStatus(record.status);
  return {
    id: record.invocation_id,
    role: "tool",
    content: running ? `调用 ${record.tool}` : `${record.tool} ${toolMark(record.status)} (${record.duration_ms ?? 0}ms)`,
    timestamp: previous?.timestamp ?? timestamp,
    turnId: record.turn_id || previous?.turnId,
    toolName: record.tool,
    toolArgs: record.args,
    toolResult: running ? (previous?.toolResult ?? "") : record.result_summary || record.error || "",
    toolStatus: cardStatusFor(record.status),
    durationMs: record.duration_ms ?? undefined,
    toolState: record.status,
    toolPhase: record.phase,
    toolErrorCode: record.error_code,
    toolEffectStatus: record.effect_status,
  };
}

/** 工具卡按 invocation_id 就地更新；不存在时追加（幂等，不新增重复卡）。 */
function upsertToolMessage(
  messages: DcChatMessage[],
  record: DcLiveToolRecord,
  timestamp: string,
): DcChatMessage[] {
  const position = messages.findIndex((message) => message.id === record.invocation_id);
  if (position < 0) return [...messages, recordToMessage(record, timestamp)];
  const next = messages.slice();
  next[position] = recordToMessage(record, timestamp, messages[position]);
  return next;
}

function toolMark(status: DcToolStatus): string {
  switch (status) {
    case "succeeded": return "✓";
    case "failed": return "✗";
    case "timed_out": return "⏱";
    case "cancelled": return "⊘";
    default: return "?";
  }
}

// ---------------------------------------------------------------------------
// 当前活动：阶段文案 + 计时 + 超时倒计时
// ---------------------------------------------------------------------------

interface ActivityPatch {
  kind?: DcActivityKind;
  phase?: string;
  phaseLabelOverride?: string;
  operationId?: string;
  turnId?: string | null;
  tool?: string | null;
  startedAt?: string | null;
  lastProgressAt?: string | null;
  deadlineAt?: string | null;
  elapsedMs?: number | null;
  remainingMs?: number | null;
  cancellable?: boolean;
  attentionReason?: string | null;
  attentionOptions?: string[];
  resetElapsed?: boolean;
}

/** 阶段标识 → 中文文案。 */
export function phaseLabelFor(phase: string, tool?: string | null): string {
  if (!phase) return tool ? `正在执行 ${tool}` : "正在执行";
  const known = PHASE_LABELS[phase];
  if (known) return known;
  // 未知 phase：不伪造语义，保留原标识便于排查
  return tool ? `正在执行 ${tool}（${phase}）` : `正在执行（${phase}）`;
}

/** 由当前活动与新事件推导下一个活动；`epoch` 变化时组件重建计时器。 */
function nextActivity(current: DcActivityState, patch: ActivityPatch): DcActivityState {
  const now = Date.now();
  const kind = patch.kind ?? current.kind;
  if (kind === "idle" && patch.phaseLabelOverride === undefined && current.kind === "idle") {
    return current;
  }
  const tool = patch.tool !== undefined ? patch.tool : current.tool;
  const phase = patch.phase !== undefined ? patch.phase : current.phase;
  const startedAt = deriveStartedAt(patch, current, kind);
  let elapsed = patch.elapsedMs !== undefined ? patch.elapsedMs : current.elapsedMs;
  if (elapsed == null && startedAt) elapsed = Math.max(0, now - Date.parse(startedAt));
  if (typeof elapsed === "number" && !Number.isFinite(elapsed)) elapsed = null;
  const label = patch.phaseLabelOverride ?? phaseLabelFor(phase, tool);
  return {
    kind,
    phaseLabel: label,
    phase,
    operationId: patch.operationId !== undefined ? patch.operationId : current.operationId,
    turnId: patch.turnId !== undefined ? patch.turnId : current.turnId,
    tool: tool ?? null,
    startedAt,
    lastProgressAt:
      patch.lastProgressAt !== undefined ? patch.lastProgressAt : current.lastProgressAt,
    deadlineAt: patch.deadlineAt !== undefined ? patch.deadlineAt : current.deadlineAt,
    elapsedMs: elapsed,
    remainingMs: patch.remainingMs !== undefined ? patch.remainingMs : current.remainingMs,
    cancellable: patch.cancellable !== undefined ? patch.cancellable : current.cancellable,
    attentionReason:
      patch.attentionReason !== undefined ? patch.attentionReason : current.attentionReason,
    attentionOptions:
      patch.attentionOptions !== undefined ? patch.attentionOptions : current.attentionOptions,
    epoch: current.epoch + 1,
  };
}

/** 开始时间的推导与重置：新阶段总是重新计时，同一阶段内的进度事件不回退。 */
function deriveStartedAt(
  patch: ActivityPatch,
  current: DcActivityState,
  kind: DcActivityKind,
): string | null {
  if (patch.startedAt !== undefined && patch.startedAt !== null) return patch.startedAt;
  if (patch.kind !== undefined && patch.kind !== "idle" && patch.kind !== current.kind) {
    return patch.startedAt ?? null;
  }
  if (patch.resetElapsed) return patch.startedAt ?? null;
  return current.startedAt;
}

/** 工具结束后：仍在运行的工具继续显示，否则活动区回到空闲。 */
function settleToolActivity(current: DcActivityState, record: DcLiveToolRecord): DcActivityState {
  if (current.operationId && record.invocation_id !== current.operationId) return current;
  if (isActiveToolStatus(record.status)) return current;
  return nextActivity(current, { kind: "idle", phaseLabelOverride: "" });
}

function turnFinishedLabel(status: string): string {
  switch (status) {
    case "completed": return "轮次已完成";
    case "cancelled": return "已取消（上下文已保留，可继续）";
    case "interrupted": return "已中断（上下文已保留，可继续）";
    case "needs_attention": return "需要人工确认";
    case "failed": return "轮次失败";
    default: return status ? `轮次结束：${status}` : "轮次已结束";
  }
}

function numberOrNull(value: unknown): number | null {
  if (value == null) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

// ---------------------------------------------------------------------------
// 消息聚合：从 DcSessionView 的 turns + invocations 构建聊天消息列表
// ---------------------------------------------------------------------------

function aggregateMessages(session: DcSessionView): DcChatMessage[] {
  const messages: DcChatMessage[] = [];
  for (const turn of session.turns) {
    // 用户消息
    messages.push({
      id: turn.turn_id,
      role: "user",
      content: turn.user_message,
      timestamp: turn.started_at,
      turnId: turn.turn_id,
    });
    // 该轮模型侧的思考/叙述（后端 steps 与增量事件一一对应）
    (turn.steps ?? []).forEach((step, index) => {
      // 终态回答已由 agent_summary 渲染：跳过与之重复的叙述步骤（历史记录兼容）
      if (step.kind === "agent_text" && normalizeText(step.text) === normalizeText(turn.agent_summary)) return;
      messages.push({
        id: `${turn.turn_id}-step-${index}`,
        role: step.kind === "thinking" ? "thinking" : "narration",
        content: step.text,
        timestamp: turn.started_at,
        turnId: turn.turn_id,
        step: step.step,
      });
    });
    // 该轮的工具调用
    const turnInvocations = session.invocations.filter((inv) => inv.turn_id === turn.turn_id);
    for (const inv of turnInvocations) {
      messages.push(invocationMessage(inv));
    }
    // 助手总结
    if (turn.agent_summary) {
      messages.push({
        id: `${turn.turn_id}-summary`,
        role: "assistant",
        content: turn.agent_summary,
        timestamp: turn.ended_at ?? turn.started_at,
        turnId: turn.turn_id,
      });
    }
  }
  return messages;
}

function formatInvocationArgs(args: Record<string, unknown>): string {
  return Object.entries(args)
    .filter(([, v]) => v != null)
    .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
    .join(", ");
}

/** 快照 invocation → 工具卡（聚合历史消息时使用）。 */
function invocationMessage(inv: DcToolInvocation): DcChatMessage {
  const status = normalizeToolStatus(inv.status, inv.success);
  const running = isActiveToolStatus(status);
  const fallbackResult = running
    ? formatInvocationArgs(inv.args ?? {})
    : inv.error
      ? `ERROR: ${inv.error}`
      : inv.result_summary || formatInvocationArgs(inv.args ?? {});
  return {
    id: inv.invocation_id,
    role: "tool",
    content: running
      ? `调用 ${inv.tool}`
      : `${inv.tool} ${toolMark(status)} (${inv.duration_ms ?? 0}ms)`,
    timestamp: inv.started_at,
    turnId: inv.turn_id,
    toolName: inv.tool,
    toolArgs: inv.args,
    toolResult: fallbackResult,
    toolStatus: cardStatusFor(status),
    durationMs: inv.duration_ms ?? undefined,
    toolState: status,
    toolPhase: inv.phase,
    toolErrorCode: inv.error_code ?? null,
    toolEffectStatus: (inv.effect_status as DcEffectStatus) ?? "none",
  };
}

/** 文本规范化：用于判断终态回答与叙述步骤是否重复（忽略首尾空白差异）。 */
function normalizeText(text: string): string {
  return (text ?? "").trim();
}

// ---------------------------------------------------------------------------
// 选择器：返回稳定引用，避免 useSyncExternalStore 无限重渲染
// ---------------------------------------------------------------------------

const IDLE_ACTIVITY_SNAPSHOT: DcActivitySnapshot = {
  activity: idleActivity(),
  activityKey: "idle",
  busy: false,
  cancelStatus: "idle",
  cancelHint: "",
};

const OP_RECORDS_CACHE = new WeakMap<DcLiveToolRecord[], { snapshot?: readonly DcToolInvocation[]; merged: DcLiveToolRecord[] }>();

/**
 * 操作日志数据源：历史快照 + 实时账本合并后的稳定行列表。
 * 已覆盖的快照不重复合并，保证同一引用（避免每次读 store 都产生新数组）。
 */
export function selectOperationRecords(state: DcState): DcLiveToolRecord[] {
  const ledger = state.liveInvocations;
  const snapshot = state.session?.invocations;
  if (!snapshot || snapshot.length === 0) return ledger;
  const cached = OP_RECORDS_CACHE.get(ledger);
  if (cached && cached.snapshot === snapshot) return cached.merged;
  const covered = snapshot.every((inv) => ledger.some((r) => r.invocation_id === inv.invocation_id));
  if (covered) {
    OP_RECORDS_CACHE.set(ledger, { snapshot, merged: ledger });
    return ledger;
  }
  const merged = mergeLedger(ledger, snapshot);
  OP_RECORDS_CACHE.set(ledger, { snapshot, merged });
  return merged;
}

/**
 * 当前活动区数据源。
 * 返回值在活动未变化时保持同一引用（含 `activityKey`），组件据此决定是否重建计时器。
 */
export function selectActivitySnapshot(state: DcState): DcActivitySnapshot {
  const activity = state.activity;
  const sessionBusy = state.status === "thinking" || state.status === "acting";
  const cancelStatus = state.cancelStatus;
  const hint = cancelHintFor(cancelStatus, state.cancelRequestedAt);
  const active = activity.kind !== "idle" || cancelStatus === "confirming";
  if (!active && !sessionBusy) {
    if (
      IDLE_ACTIVITY_SNAPSHOT.cancelStatus === cancelStatus &&
      IDLE_ACTIVITY_SNAPSHOT.cancelHint === hint
    ) {
      return IDLE_ACTIVITY_SNAPSHOT;
    }
    return { activity, activityKey: "idle", busy: false, cancelStatus, cancelHint: hint };
  }
  const key = [
    activity.kind,
    activity.operationId,
    activity.startedAt ?? "",
    activity.epoch,
  ].join("|");
  return { activity, activityKey: key, busy: true, cancelStatus, cancelHint: hint };
}

/** 停止按钮两阶段文案：请求 → 确认设备状态；长时间无终态也要给出可读状态。 */
export function cancelHintFor(status: DcCancelStatus, requestedAt: string | null): string {
  if (status === "idle") return "";
  const elapsed = requestedAt ? Date.now() - Date.parse(requestedAt) : 0;
  if (Number.isFinite(elapsed) && elapsed >= CANCEL_STALL_MS) {
    return "仍未收到设备确认，可刷新会话查看最新状态";
  }
  if (Number.isFinite(elapsed) && elapsed >= CANCEL_CONFIRM_DELAY_MS) {
    return "设备仍在收尾：正在等待旧工作线程结束";
  }
  return "正在确认设备状态";
}

/** 是否存在运行中的工具调用。 */
export function hasRunningTool(records: DcLiveToolRecord[]): boolean {
  return records.some((record) => isActiveToolStatus(record.status));
}

/** 计数：running + 终态 + unknown 的总数（即账本行数）。 */
export function operationCount(records: DcLiveToolRecord[]): number {
  return records.length;
}

/** 当前正在运行的调用（按账本顺序取第一条）。 */
export function firstRunningRecord(records: DcLiveToolRecord[]): DcLiveToolRecord | null {
  return records.find((record) => isActiveToolStatus(record.status)) ?? null;
}
