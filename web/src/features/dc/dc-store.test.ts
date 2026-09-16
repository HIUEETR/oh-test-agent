// DC store 单测：事件驱动的增量消息流、工具卡就地更新、截图 URL、去重与刷新对账。

import { beforeEach, describe, expect, it, vi } from "vitest";

const getDcSession = vi.hoisted(() => vi.fn());
const resumeDcSession = vi.hoisted(() => vi.fn());
const listDcSessions = vi.hoisted(() => vi.fn());
const dcArtifactUrl = vi.hoisted(() =>
  vi.fn((sessionId: string, path: string) => `/api/dc/sessions/${sessionId}/artifacts/${path}`),
);

vi.mock("../../api/dc-client", () => ({
  closeDcSession: vi.fn(),
  createDcSession: vi.fn(),
  dcArtifactUrl,
  fetchDcScript: vi.fn(),
  generateDcScript: vi.fn(),
  getDcSession,
  listDcSessions,
  resumeDcSession,
  sendDcMessage: vi.fn(),
  setDcTier: vi.fn(),
  stopDcTurn: vi.fn(),
}));

import { useDcConsole } from "../../stores/dc-console";
import type { DcEvent, DcEventType, DcSessionSummary, DcSessionView } from "../../api/dc-types";

let nextEventId = 1;

function event(type: DcEventType, payload: Record<string, unknown> = {}, message = ""): DcEvent {
  return {
    event_id: nextEventId++,
    session_id: "dc-test",
    type,
    timestamp: "2026-01-01T08:00:00Z",
    message: message || type,
    payload,
  };
}

function visitSession(): DcSessionView {
  return {
    session_id: "dc-test",
    device_id: "mock-device",
    tier: 1,
    status: "idle",
    created_at: "2026-01-01T08:00:00Z",
    turns: [
      {
        turn_id: "turn-1",
        user_message: "截个图看看",
        status: "completed",
        agent_summary: "任务完成",
        started_at: "2026-01-01T08:00:00Z",
        ended_at: "2026-01-01T08:00:05Z",
        invocation_ids: ["inv-1"],
        steps: [
          { step: 1, kind: "thinking", text: "先看看界面" },
          { step: 2, kind: "agent_text", text: "我来截图" },
        ],
      },
    ],
    invocations: [
      {
        invocation_id: "inv-1",
        turn_id: "turn-1",
        tool: "screenshot",
        tier: 1,
        args: { source: "tool" },
        success: true,
        started_at: "2026-01-01T08:00:01Z",
        ended_at: "2026-01-01T08:00:02Z",
        duration_ms: 480,
      },
    ],
    latest_snapshot_path: "screens/dc_test.jpeg",
    token_usage: {
      requests: 5,
      tool_calls: 3,
      input_tokens: 4000,
      output_tokens: 200,
      cache_read_tokens: 1000,
      cache_write_tokens: 0,
      details: {},
    },
  };
}

describe("dc-console store appendEvent", () => {
  beforeEach(() => {
    nextEventId = 1;
    getDcSession.mockReset();
    dcArtifactUrl.mockClear();
    useDcConsole.getState().reset();
    useDcConsole.setState({ activeSessionId: "dc-test" });
  });

  it("截图事件用相对路径拼 artifact URL", () => {
    useDcConsole.getState().appendEvent(event("screenshot_captured", { snapshot_path: "screens/dc_1.jpeg" }));

    expect(useDcConsole.getState().latestScreenshotUrl).toBe(
      "/api/dc/sessions/dc-test/artifacts/screens/dc_1.jpeg",
    );
  });

  it("截图失败（snapshot_path 为 null）时保留上一帧", () => {
    useDcConsole.getState().appendEvent(event("screenshot_captured", { snapshot_path: "screens/dc_1.jpeg" }));
    useDcConsole
      .getState()
      .appendEvent(event("screenshot_captured", { snapshot_path: null, error: "RuntimeError: offline" }));

    expect(useDcConsole.getState().latestScreenshotUrl).toBe(
      "/api/dc/sessions/dc-test/artifacts/screens/dc_1.jpeg",
    );
  });

  it("thinking / agent_text 渲染为思考块与叙述块，并带上 turn_id", () => {
    useDcConsole.getState().appendEvent(event("thinking", { text: "先看界面", step: 1, turn_id: "turn-1" }));
    useDcConsole.getState().appendEvent(event("agent_text", { text: "我来截图", step: 2, turn_id: "turn-1" }));

    const messages = useDcConsole.getState().messages;
    expect(messages.map((m) => m.role)).toEqual(["thinking", "narration"]);
    expect(messages[0].content).toBe("先看界面");
    expect(messages[1].content).toBe("我来截图");
    expect(messages[0].turnId).toBe("turn-1");
  });

  it("工具卡由 running 就地变 success（同一 id，不新增消息）", () => {
    const { appendEvent } = useDcConsole.getState();
    appendEvent(event("tool_call_started", { invocation_id: "inv-7", tool: "click", args: { x: 1, y: 2 } }));
    expect(useDcConsole.getState().messages).toHaveLength(1);
    expect(useDcConsole.getState().messages[0].toolStatus).toBe("running");
    expect(useDcConsole.getState().status).toBe("acting");

    appendEvent(
      event("tool_call_finished", {
        invocation_id: "inv-7",
        tool: "click",
        success: true,
        duration_ms: 120,
        result_summary: "ok: clicked",
      }),
    );

    const messages = useDcConsole.getState().messages;
    expect(messages).toHaveLength(1);
    expect(messages[0].id).toBe("inv-7");
    expect(messages[0].toolStatus).toBe("success");
    expect(messages[0].toolResult).toBe("ok: clicked");
    expect(messages[0].durationMs).toBe(120);
    expect(messages[0].content).toContain("✓");
  });

  it("tool_call_finished 缺少 started 时兜底建卡", () => {
    useDcConsole.getState().appendEvent(
      event("tool_call_finished", { invocation_id: "inv-9", tool: "back", success: false, error: "boom" }),
    );

    const messages = useDcConsole.getState().messages;
    expect(messages).toHaveLength(1);
    expect(messages[0].toolStatus).toBe("failed");
    expect(messages[0].toolResult).toBe("boom");
  });

  it("assistant_message 追加最终回复并结束 busy", () => {
    useDcConsole.getState().appendEvent(event("turn_started", { turn_id: "turn-1" }));
    expect(useDcConsole.getState().status).toBe("thinking");

    useDcConsole
      .getState()
      .appendEvent(event("assistant_message", { summary: "任务完成", turn_id: "turn-1" }, "任务完成"));

    const state = useDcConsole.getState();
    expect(state.messages.map((m) => m.role)).toEqual(["assistant"]);
    expect(state.messages[0].content).toBe("任务完成");
    expect(state.status).toBe("idle");
    expect(state.sending).toBe(false);
  });

  it("同一 event_id 重复投递不产生重复消息", () => {
    const screenshot = event("thinking", { text: "重复", step: 1 });
    useDcConsole.getState().appendEvent(screenshot);
    useDcConsole.getState().appendEvent(screenshot);

    expect(useDcConsole.getState().messages).toHaveLength(1);
    expect(useDcConsole.getState().events).toHaveLength(1);
  });

  it("叙述与终态回答文本相同时就地升级，只保留一条消息", () => {
    const { appendEvent } = useDcConsole.getState();
    appendEvent(event("agent_text", { text: "任务完成", step: 1, turn_id: "turn-1" }));
    expect(useDcConsole.getState().messages.map((m) => m.role)).toEqual(["narration"]);

    appendEvent(event("assistant_message", { summary: "任务完成", turn_id: "turn-1" }, "任务完成"));

    const messages = useDcConsole.getState().messages;
    expect(messages).toHaveLength(1);
    expect(messages[0].role).toBe("assistant");
    expect(messages[0].content).toBe("任务完成");
  });

  it("不同轮的叙述不会被终态回答吞掉", () => {
    const { appendEvent } = useDcConsole.getState();
    appendEvent(event("agent_text", { text: "任务完成", step: 1, turn_id: "turn-1" }));
    appendEvent(event("assistant_message", { summary: "任务完成", turn_id: "turn-2" }, "任务完成"));

    const messages = useDcConsole.getState().messages;
    expect(messages.map((m) => m.role)).toEqual(["narration", "assistant"]);
  });

  it("模型请求预算耗尽时活动区显示专用阶段，而不是通用模型失败", () => {
    // 后端 UsageLimitExceeded → provider 发 model_call_failed(error_code=usage_limit)
    useDcConsole.getState().appendEvent(
      event("model_call_failed", {
        model_call_id: "model-call-9",
        turn_id: "turn-1",
        attempt: 31,
        error_code: "usage_limit",
        request_limit: 30,
        tool_calls_limit: 50,
        error: "The next request would exceed the request_limit of 30",
      }),
    );

    const activity = useDcConsole.getState().activity;
    expect(activity.phase).toBe("usage_limit");
    expect(activity.phaseLabel).toBe("本轮请求预算已用尽");
    expect(activity.operationId).toBe("model-call-9");
  });

  it("模型超时仍映射为 model_timeout，其他错误回落到 model_failed", () => {
    const { appendEvent } = useDcConsole.getState();
    appendEvent(event("model_call_failed", { model_call_id: "m1", error_code: "model_timeout" }));
    expect(useDcConsole.getState().activity.phase).toBe("model_timeout");

    appendEvent(event("model_call_failed", { model_call_id: "m2", error_code: "provider_error" }));
    expect(useDcConsole.getState().activity.phase).toBe("model_failed");
  });

  it("token_usage_updated 整值覆盖，重复投递不会把用量翻倍", () => {
    const payload = {
      session: {
        requests: 2,
        tool_calls: 1,
        input_tokens: 1721,
        output_tokens: 16,
        cache_read_tokens: 1536,
        cache_write_tokens: 0,
        details: {},
      },
      turn: { requests: 2, input_tokens: 1721, output_tokens: 16, cache_read_tokens: 1536 },
      context_window: 128000,
      request_limit: 120,
    };
    const same = event("token_usage_updated", payload);
    useDcConsole.getState().appendEvent(same);
    useDcConsole.getState().appendEvent({ ...same, event_id: same.event_id + 1 });

    const state = useDcConsole.getState();
    // 覆盖而非累加：仍是服务端权威值
    expect(state.tokenUsage?.input_tokens).toBe(1721);
    expect(state.tokenUsage?.requests).toBe(2);
    expect(state.contextWindow).toBe(128000);
    expect(state.requestLimit).toBe(120);
    expect(state.lastTurnRequests).toBe(2);
  });
});

describe("dc-console store refreshSession", () => {
  beforeEach(() => {
    nextEventId = 1;
    getDcSession.mockReset();
    useDcConsole.getState().reset();
    useDcConsole.setState({ activeSessionId: "dc-test" });
  });

  it("空闲时用 turns + invocations 全量重建（含步骤块与工具卡）", async () => {
    getDcSession.mockResolvedValue(visitSession());

    await useDcConsole.getState().refreshSession();
    const messages = useDcConsole.getState().messages;
    expect(messages.map((m) => m.role)).toEqual(["user", "thinking", "narration", "tool", "assistant"]);
    expect(messages[1].content).toBe("先看看界面");
    expect(messages[3].id).toBe("inv-1");
    expect(messages[3].toolStatus).toBe("success");
    expect(messages[3].toolArgs).toEqual({ source: "tool" });
    expect(useDcConsole.getState().latestScreenshotUrl).toBe(
      "/api/dc/sessions/dc-test/artifacts/screens/dc_test.jpeg",
    );
  });

  it("进行中轮次不覆盖增量消息（避免双写冲突）", async () => {
    useDcConsole.getState().appendEvent(event("thinking", { text: "增量思考", step: 1 }));
    const inFlight = { ...visitSession(), status: "acting" };
    getDcSession.mockResolvedValue(inFlight);

    await useDcConsole.getState().refreshSession(true);

    const messages = useDcConsole.getState().messages;
    expect(messages).toHaveLength(1);
    expect(messages[0].content).toBe("增量思考");
    expect(useDcConsole.getState().latestScreenshotUrl).toBe(
      "/api/dc/sessions/dc-test/artifacts/screens/dc_test.jpeg",
    );
  });

  it("重建时跳过与终态回答重复的叙述步骤（历史记录兼容）", async () => {
    const session = visitSession();
    session.turns[0].agent_summary = "我来截图";
    getDcSession.mockResolvedValue(session);

    await useDcConsole.getState().refreshSession();

    const roles = useDcConsole.getState().messages.map((m) => m.role);
    expect(roles).toEqual(["user", "thinking", "tool", "assistant"]);
    expect(useDcConsole.getState().messages.filter((m) => m.content === "我来截图")).toHaveLength(1);
  });

  it("用会话快照回填 token 用量；reset 后清空", async () => {
    getDcSession.mockResolvedValue(visitSession());

    await useDcConsole.getState().refreshSession();
    expect(useDcConsole.getState().tokenUsage?.input_tokens).toBe(4000);

    useDcConsole.getState().reset();
    expect(useDcConsole.getState().tokenUsage).toBeNull();
  });
});

describe("dc-console store 历史会话", () => {
  beforeEach(() => {
    nextEventId = 1;
    getDcSession.mockReset();
    resumeDcSession.mockReset();
    listDcSessions.mockReset();
    listDcSessions.mockResolvedValue([]);
    useDcConsole.getState().reset();
  });

  function historySummary(): DcSessionSummary {
    return {
      session_id: "dc-old",
      device_id: "mock-device",
      tier: 2,
      status: "closed",
      created_at: "2026-01-01T08:00:00Z",
      last_active_at: "2026-01-02T08:00:00Z",
      turn_count: 1,
      invocation_count: 1,
      active: false,
      script_available: false,
    };
  }

  it("选择历史会话时先 resume 再渲染消息与截图", async () => {
    const view: DcSessionView = { ...visitSession(), session_id: "dc-old", restored: true, restored_context: "text" };
    resumeDcSession.mockResolvedValue(view);
    useDcConsole.setState({ sessions: [historySummary()] });

    useDcConsole.getState().selectSession("dc-old");
    await vi.waitFor(() => expect(useDcConsole.getState().session).not.toBeNull());

    expect(resumeDcSession).toHaveBeenCalledWith("dc-old");
    expect(getDcSession).not.toHaveBeenCalled();
    const state = useDcConsole.getState();
    expect(state.session?.restored).toBe(true);
    expect(state.messages.map((m) => m.role)).toEqual([
      "user",
      "thinking",
      "narration",
      "tool",
      "assistant",
    ]);
    expect(state.latestScreenshotUrl).toBe("/api/dc/sessions/dc-old/artifacts/screens/dc_test.jpeg");
  });

  it("恢复失败时给出可读错误且不抛异常", async () => {
    resumeDcSession.mockRejectedValue(new Error("409 cannot resume session"));
    useDcConsole.setState({ sessions: [historySummary()] });

    useDcConsole.getState().selectSession("dc-old");
    await vi.waitFor(() => expect(useDcConsole.getState().error).not.toBe(""));

    expect(useDcConsole.getState().error).toContain("恢复历史会话失败");
    expect(useDcConsole.getState().session).toBeNull();
  });

  it("选择活跃会话走 refreshSession，不触发 resume", async () => {
    getDcSession.mockResolvedValue(visitSession());
    useDcConsole.setState({
      sessions: [{ ...historySummary(), session_id: "dc-test", active: true }],
    });

    useDcConsole.getState().selectSession("dc-test");
    await vi.waitFor(() => expect(getDcSession).toHaveBeenCalled());

    expect(resumeDcSession).not.toHaveBeenCalled();
    expect(useDcConsole.getState().session?.session_id).toBe("dc-test");
  });
});
