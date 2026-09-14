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
  DcChatMessage,
  DcEvent,
  DcScriptArtifact,
  DcSessionSummary,
  DcSessionView,
  DcToolTier,
} from "../api/dc-types";

/** 去重用的本地事件缓冲上限；后端 bus 自身 buffer 为 500。 */
const MAX_TRACKED_EVENTS = 500;

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
  setError: (message: string) => void;
  reset: () => void;
}

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
      }));
      await get().refreshSession();
      void get().loadSessions();
    } catch (cause) {
      set({ error: apiError("创建 DC 会话失败", cause) });
    }
  },

  selectSession: (sessionId: string) => {
    set({
      activeSessionId: sessionId,
      events: [],
      messages: [],
      session: null,
      script: null,
      latestScreenshotUrl: null,
      status: "idle",
      error: "",
    });
    if (!sessionId) return;
    const summary = get().sessions.find((item) => item.session_id === sessionId);
    if (summary && summary.active === false) {
      // 历史会话：先从磁盘快照恢复，再展示（恢复后可直接继续对话）
      void (async () => {
        try {
          const view = await resumeDcSession(sessionId);
          set((state) => ({
            session: view,
            status: view.status,
            script: view.script ?? null,
            messages: aggregateMessages(view),
            latestScreenshotUrl: view.latest_snapshot_path
              ? dcArtifactUrl(sessionId, view.latest_snapshot_path)
              : state.latestScreenshotUrl,
          }));
          await get().loadSessions();
        } catch (cause) {
          set({ error: apiError("恢复历史会话失败", cause) });
        }
      })();
      return;
    }
    void get().refreshSession();
  },

  refreshSession: async (quiet = false) => {
    const { activeSessionId } = get();
    if (!activeSessionId) return;
    try {
      const session = await getDcSession(activeSessionId);
      // 进行中轮次由增量事件驱动，全量重建只用于已完成轮次（避免双写冲突）
      const inFlight = session.status === "thinking" || session.status === "acting";
      const patch: Partial<DcState> = {
        session,
        status: session.status,
        script: session.script ?? null,
        latestScreenshotUrl: session.latest_snapshot_path
          ? dcArtifactUrl(activeSessionId, session.latest_snapshot_path)
          : get().latestScreenshotUrl,
        ...(quiet ? {} : { error: "" }),
      };
      if (!inFlight) patch.messages = aggregateMessages(session);
      set(patch);
    } catch (cause) {
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
    try {
      await stopDcTurn(activeSessionId);
    } catch (cause) {
      set({ error: apiError("停止轮次失败", cause) });
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
      set({
        activeSessionId: "",
        session: null,
        events: [],
        messages: [],
        script: null,
        latestScreenshotUrl: null,
        status: "idle",
      });
      // 会话仍在磁盘快照中：刷新列表使其以「历史会话」出现，可随时恢复
      await get().loadSessions();
    } catch (cause) {
      set({ error: apiError("关闭会话失败", cause) });
    }
  },

  appendEvent: (event: DcEvent) => {
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
        case "turn_started":
          patch.status = "thinking";
          patch.sending = true;
          break;
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
        case "tool_call_started":
          patch.messages = [
            ...state.messages,
            {
              id: p.invocation_id as string,
              role: "tool",
              content: `调用 ${p.tool as string}`,
              timestamp: event.timestamp,
              turnId: p.turn_id as string | undefined,
              toolName: p.tool as string,
              toolArgs: p.args as Record<string, unknown> | undefined,
              toolStatus: "running",
            },
          ];
          patch.status = "acting";
          break;
        case "tool_call_finished": {
          // 以 invocation_id 就地更新（running → success/failed），不新增消息
          const invocationId = p.invocation_id as string;
          const success = Boolean(p.success);
          const durationMs = p.duration_ms as number | undefined;
          const known = state.messages.some((m) => m.id === invocationId);
          if (known) {
            patch.messages = state.messages.map((message) =>
              message.id === invocationId
                ? {
                    ...message,
                    content: `${message.toolName ?? (p.tool as string)} ${success ? "✓" : "✗"} (${durationMs ?? 0}ms)`,
                    toolStatus: success ? "success" : "failed",
                    toolResult: (p.result_summary as string) ?? (p.error as string) ?? "",
                    durationMs,
                  }
                : message,
            );
          } else {
            // started 事件丢失时兜底建卡（幂等）
            patch.messages = [
              ...state.messages,
              {
                id: invocationId,
                role: "tool",
                content: `${p.tool as string} ${success ? "✓" : "✗"} (${durationMs ?? 0}ms)`,
                timestamp: event.timestamp,
                turnId: p.turn_id as string | undefined,
                toolName: p.tool as string,
                toolArgs: p.args as Record<string, unknown> | undefined,
                toolStatus: success ? "success" : "failed",
                toolResult: (p.result_summary as string) ?? (p.error as string) ?? "",
                durationMs,
              },
            ];
          }
          break;
        }
        case "assistant_message": {
          patch.status = "idle";
          patch.sending = false;
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
        case "turn_finished":
          patch.status = "idle";
          patch.sending = false;
          break;
        case "error":
          patch.error = event.message;
          patch.status = "idle";
          patch.sending = false;
          break;
        case "script_generated":
          void get().refreshSession(true);
          break;
        default:
          break;
      }

      return { ...state, ...patch };
    });
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
    }),
}));

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
      messages.push({
        id: inv.invocation_id,
        role: "tool",
        content: `${inv.tool} ${inv.success ? "✓" : "✗"} (${inv.duration_ms ?? 0}ms)`,
        timestamp: inv.started_at,
        turnId: turn.turn_id,
        toolName: inv.tool,
        toolArgs: inv.args,
        toolResult: inv.error ? `ERROR: ${inv.error}` : formatInvocationArgs(inv.args),
        toolStatus: inv.success ? "success" : "failed",
        durationMs: inv.duration_ms,
      });
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

/** 文本规范化：用于判断终态回答与叙述步骤是否重复（忽略首尾空白差异）。 */
function normalizeText(text: string): string {
  return (text ?? "").trim();
}
