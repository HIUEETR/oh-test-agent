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
  DcToolInvocation,
  DcToolTier,
} from "../api/dc-types";

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
          turn_count: 0,
          invocation_count: 0,
        }],
        events: [],
        messages: [],
        session: null,
        script: null,
        latestScreenshotUrl: null,
        status: "idle",
      }));
      await get().refreshSession();
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
    void get().refreshSession();
  },

  refreshSession: async (quiet = false) => {
    const { activeSessionId } = get();
    if (!activeSessionId) return;
    try {
      const session = await getDcSession(activeSessionId);
      // 从 turns + invocations 聚合消息
      const messages = aggregateMessages(session);
      set({
        session,
        messages,
        status: session.status,
        script: session.script ?? null,
        latestScreenshotUrl: session.latest_snapshot_path
          ? dcArtifactUrl(activeSessionId, session.latest_snapshot_path)
          : null,
        ...(quiet ? {} : { error: "" }),
      });
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
      set((state) => ({
        sessions: state.sessions.filter((s) => s.session_id !== activeSessionId),
        activeSessionId: "",
        session: null,
        events: [],
        messages: [],
        script: null,
        latestScreenshotUrl: null,
        status: "idle",
      }));
    } catch (cause) {
      set({ error: apiError("关闭会话失败", cause) });
    }
  },

  appendEvent: (event: DcEvent) => {
    set((state) => {
      // 去重
      if (state.events.some((e) => e.event_id === event.event_id)) return state;
      const events = [...state.events, event];

      // 根据事件类型更新状态
      let patch: Partial<DcState> = { events };

      if (event.type === "screenshot_captured") {
        const snapshotPath = event.payload.snapshot_path as string | undefined;
        if (snapshotPath && state.activeSessionId) {
          patch.latestScreenshotUrl = dcArtifactUrl(state.activeSessionId, snapshotPath);
        }
      }

      if (event.type === "turn_started") {
        patch.status = "thinking";
        patch.sending = true;
      }

      if (event.type === "turn_finished" || event.type === "assistant_message") {
        patch.status = "idle";
        patch.sending = false;
        // 追加助手消息
        if (event.type === "assistant_message") {
          const summary = (event.payload.summary as string) ?? event.message;
          const assistantMessage: DcChatMessage = {
            id: `evt-${event.event_id}`,
            role: "assistant",
            content: summary,
            timestamp: event.timestamp,
            turnId: event.payload.turn_id as string | undefined,
          };
          patch.messages = [...state.messages, assistantMessage];
        }
      }

      if (event.type === "tool_call_finished") {
        // 追加工具调用消息
        const toolName = event.payload.tool as string;
        const success = event.payload.success as boolean;
        const durationMs = event.payload.duration_ms as number;
        const toolMessage: DcChatMessage = {
          id: `tool-${event.event_id}`,
          role: "tool",
          content: `${toolName} ${success ? "✓" : "✗"} (${durationMs}ms)`,
          timestamp: event.timestamp,
        };
        patch.messages = [...(patch.messages ?? state.messages), toolMessage];
      }

      if (event.type === "error") {
        patch.error = event.message;
        patch.status = "idle";
        patch.sending = false;
      }

      if (event.type === "script_generated") {
        void get().refreshSession(true);
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
    // 该轮的工具调用
    const turnInvocations = session.invocations.filter((inv) => inv.turn_id === turn.turn_id);
    for (const inv of turnInvocations) {
      messages.push({
        id: inv.invocation_id,
        role: "tool",
        content: formatInvocation(inv),
        timestamp: inv.started_at,
        turnId: turn.turn_id,
        invocations: [inv],
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

function formatInvocation(inv: DcToolInvocation): string {
  const status = inv.success ? "✓" : "✗";
  const args = Object.entries(inv.args)
    .filter(([, v]) => v != null)
    .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
    .join(", ");
  const duration = inv.duration_ms ? ` (${inv.duration_ms}ms)` : "";
  return `${inv.tool} ${status}${duration}${args ? ` · ${args}` : ""}${inv.error ? ` · ${inv.error}` : ""}`;
}
