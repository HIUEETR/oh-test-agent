// DC 流式输出（message_delta）单测：草稿逐字增长、全量事件就地收口（id 不变）、
// 终态 assistant_message 收口、role 改判 assistant→narration、残留草稿清理。
//
// 放在 src/stores/ 下：既有 appendEvent 用例在 src/features/dc/dc-store.test.ts，
// 本次改动不触碰该文件（同目录其他 DC 组件由并行任务负责）。

import { createElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const getDcSession = vi.hoisted(() => vi.fn());
const resumeDcSession = vi.hoisted(() => vi.fn());
const listDcSessions = vi.hoisted(() => vi.fn());
const dcArtifactUrl = vi.hoisted(() =>
  vi.fn((sessionId: string, path: string) => `/api/dc/sessions/${sessionId}/artifacts/${path}`),
);

vi.mock("../api/dc-client", () => ({
  closeDcSession: vi.fn(),
  createDcSession: vi.fn(),
  dcArtifactUrl,
  distillDcProfile: vi.fn(),
  fetchDcScript: vi.fn(),
  generateDcScript: vi.fn(),
  getDcSession,
  listDcSessions,
  resumeDcSession,
  sendDcMessage: vi.fn(),
  setDcTier: vi.fn(),
  stopDcTurn: vi.fn(),
}));

import { act, render } from "@testing-library/react";
import { useDcConsole } from "./dc-console";
import { DcChat } from "../features/dc/DcChat";
import type { DcChatMessage, DcEvent, DcEventType } from "../api/dc-types";

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

/** 一条 message_delta 事件（payload 契约见 dc-types.ts::DcMessageDeltaPayload）。 */
function delta(
  streamKey: string,
  role: "thinking" | "narration" | "assistant",
  text: string,
  turnId = "turn-1",
): DcEvent {
  return event("message_delta", {
    turn_id: turnId,
    stream_key: streamKey,
    role,
    delta: text,
    message_index: Number(streamKey.split(":m")[1]?.split(":")[0] ?? 0),
  });
}

describe("dc-console store 流式草稿（message_delta）", () => {
  beforeEach(() => {
    nextEventId = 1;
    getDcSession.mockReset();
    useDcConsole.getState().reset();
    useDcConsole.setState({ activeSessionId: "dc-test" });
  });

  it("同一 stream_key 的增量逐字增长为一条草稿，id 稳定", () => {
    const { appendEvent } = useDcConsole.getState();
    appendEvent(delta("turn-1:m0:text", "assistant", "任务"));

    const first = useDcConsole.getState().messages;
    expect(first).toHaveLength(1);
    expect(first[0].id).toBe("stream-turn-1:m0:text");
    expect(first[0].role).toBe("assistant");
    expect(first[0].content).toBe("任务");
    expect(first[0].streaming).toBe(true);
    expect(first[0].streamKey).toBe("turn-1:m0:text");
    expect(first[0].turnId).toBe("turn-1");

    appendEvent(delta("turn-1:m0:text", "assistant", "完成"));
    appendEvent(delta("turn-1:m0:text", "assistant", "。"));

    const messages = useDcConsole.getState().messages;
    expect(messages).toHaveLength(1);
    expect(messages[0].id).toBe(first[0].id);
    expect(messages[0].content).toBe("任务完成。");
    expect(messages[0].streaming).toBe(true);
  });

  it("同一条模型消息的思考与文本各一条草稿，互不干扰", () => {
    const { appendEvent } = useDcConsole.getState();
    appendEvent(delta("turn-1:m0:thinking", "thinking", "先看界面"));
    appendEvent(delta("turn-1:m0:text", "assistant", "我来截图"));

    const messages = useDcConsole.getState().messages;
    expect(messages.map((m) => m.role)).toEqual(["thinking", "assistant"]);
    expect(messages.map((m) => m.content)).toEqual(["先看界面", "我来截图"]);
    expect(messages.map((m) => m.streamKey)).toEqual(["turn-1:m0:thinking", "turn-1:m0:text"]);
  });

  it("空 delta 或缺失 stream_key 不产生空草稿", () => {
    const { appendEvent } = useDcConsole.getState();
    appendEvent(delta("turn-1:m0:text", "assistant", ""));
    appendEvent(event("message_delta", { turn_id: "turn-1", role: "assistant", delta: "无 key" }));

    expect(useDcConsole.getState().messages).toHaveLength(0);
  });

  it("thinking 全量事件按 stream_key 收口草稿：id 与时间戳不变，streaming 置 false", () => {
    const { appendEvent } = useDcConsole.getState();
    appendEvent(delta("turn-1:m0:thinking", "thinking", "先看"));
    const draft = useDcConsole.getState().messages[0];

    appendEvent(
      event("thinking", {
        text: "先看界面元素，再决定点哪",
        step: 1,
        turn_id: "turn-1",
        stream_key: "turn-1:m0:thinking",
      }),
    );

    const messages = useDcConsole.getState().messages;
    expect(messages).toHaveLength(1);
    expect(messages[0].id).toBe(draft.id);
    expect(messages[0].timestamp).toBe(draft.timestamp);
    expect(messages[0].content).toBe("先看界面元素，再决定点哪");
    expect(messages[0].streaming).toBe(false);
  });

  it("agent_text 全量事件收口叙述草稿（可能是已改判的 assistant 草稿）", () => {
    const { appendEvent } = useDcConsole.getState();
    const key = "turn-1:m0:text";
    appendEvent(delta(key, "assistant", "我来"));
    appendEvent(delta(key, "narration", "截图"));
    const draft = useDcConsole.getState().messages[0];
    expect(draft.role).toBe("narration");

    appendEvent(event("agent_text", { text: "我来截图看看", step: 2, turn_id: "turn-1", stream_key: key }));

    const messages = useDcConsole.getState().messages;
    expect(messages).toHaveLength(1);
    expect(messages[0].id).toBe(draft.id);
    expect(messages[0].role).toBe("narration");
    expect(messages[0].content).toBe("我来截图看看");
    expect(messages[0].streaming).toBe(false);
  });

  it("无 stream_key 的全量事件仍走追加路径（Mock/历史兼容）", () => {
    const { appendEvent } = useDcConsole.getState();
    appendEvent(event("thinking", { text: "历史思考", step: 1, turn_id: "turn-1" }));
    appendEvent(event("agent_text", { text: "历史叙述", step: 2, turn_id: "turn-1" }));

    const messages = useDcConsole.getState().messages;
    expect(messages.map((m) => m.role)).toEqual(["thinking", "narration"]);
    expect(messages.every((m) => m.streaming !== true)).toBe(true);
  });

  it("文本 key 的 role 由 assistant 改判为 narration，且不回退", () => {
    const { appendEvent } = useDcConsole.getState();
    const key = "turn-1:m1:text";
    appendEvent(delta(key, "assistant", "先点击"));
    expect(useDcConsole.getState().messages[0].role).toBe("assistant");

    appendEvent(delta(key, "narration", "目标按钮"));
    expect(useDcConsole.getState().messages[0].role).toBe("narration");

    // tool-call part 之后仍可能收到带 assistant 的增量：不得把叙述降回助手气泡
    appendEvent(delta(key, "assistant", "。"));
    const messages = useDcConsole.getState().messages;
    expect(messages).toHaveLength(1);
    expect(messages[0].role).toBe("narration");
    expect(messages[0].content).toBe("先点击目标按钮。");
  });

  it("assistant_message 优先收口同轮流式草稿，不追加重复的终态消息", () => {
    const { appendEvent } = useDcConsole.getState();
    appendEvent(delta("turn-1:m1:text", "assistant", "任务完"));
    const draft = useDcConsole.getState().messages[0];

    appendEvent(event("assistant_message", { summary: "任务完成", turn_id: "turn-1" }, "任务完成"));

    const messages = useDcConsole.getState().messages;
    expect(messages).toHaveLength(1);
    expect(messages[0].id).toBe(draft.id);
    expect(messages[0].role).toBe("assistant");
    expect(messages[0].content).toBe("任务完成");
    expect(messages[0].streaming).toBe(false);
    expect(useDcConsole.getState().status).toBe("idle");
  });

  it("assistant_message 不会跨轮收口其他轮次的流式草稿", () => {
    const { appendEvent } = useDcConsole.getState();
    appendEvent(delta("turn-2:m0:text", "assistant", "另一轮", "turn-2"));

    appendEvent(event("assistant_message", { summary: "本轮完成", turn_id: "turn-1" }, "本轮完成"));

    const messages = useDcConsole.getState().messages;
    expect(messages.map((m) => m.role)).toEqual(["assistant", "assistant"]);
    expect(messages[0].content).toBe("另一轮");
    expect(messages[0].streaming).toBe(true);
    expect(messages[1].content).toBe("本轮完成");
  });

  it("turn_finished 清理残留 streaming 标记但保留草稿内容", () => {
    const { appendEvent } = useDcConsole.getState();
    appendEvent(delta("turn-1:m0:thinking", "thinking", "半截思考"));
    appendEvent(delta("turn-1:m1:text", "assistant", "半截回答"));

    appendEvent(event("turn_finished", { turn_id: "turn-1", status: "completed" }));

    const messages = useDcConsole.getState().messages;
    expect(messages).toHaveLength(2);
    expect(messages.every((m) => m.streaming === false)).toBe(true);
    expect(messages.map((m) => m.content)).toEqual(["半截思考", "半截回答"]);
  });

  it("turn_interrupted 与 error 同样清理流式标记", () => {
    const { appendEvent } = useDcConsole.getState();
    appendEvent(delta("turn-1:m0:text", "assistant", "中断前"));
    appendEvent(event("turn_interrupted", { turn_id: "turn-1", reason: "device offline" }));
    expect(useDcConsole.getState().messages[0].streaming).toBe(false);

    appendEvent(delta("turn-1:m1:text", "assistant", "报错前"));
    appendEvent(event("error", { turn_id: "turn-1" }, "模型调用失败"));
    const messages = useDcConsole.getState().messages;
    expect(messages.every((m) => m.streaming === false)).toBe(true);
    expect(messages.map((m) => m.content)).toEqual(["中断前", "报错前"]);
  });

  it("已收口的草稿忽略迟到的增量（重连回放不重复写入文本）", () => {
    const { appendEvent } = useDcConsole.getState();
    appendEvent(delta("turn-1:m1:text", "assistant", "任务完"));
    appendEvent(delta("turn-1:m1:text", "assistant", "成"));
    // 全量事件收口：气泡已是权威文本
    appendEvent(
      event("agent_text", { text: "任务完成", step: 1, turn_id: "turn-1", stream_key: "turn-1:m1:text" }),
    );

    // 重连后回放的增量不得再追加到已定稿气泡上
    appendEvent(delta("turn-1:m1:text", "assistant", "任务完"));
    appendEvent(delta("turn-1:m1:text", "assistant", "成"));

    const messages = useDcConsole.getState().messages;
    expect(messages).toHaveLength(1);
    expect(messages[0].content).toBe("任务完成");
    expect(messages[0].streaming).toBe(false);
  });

  it("轮次结束后到达的增量不会复活已定稿气泡", () => {
    const { appendEvent } = useDcConsole.getState();
    appendEvent(delta("turn-1:m0:text", "assistant", "半截回答"));
    appendEvent(event("turn_finished", { turn_id: "turn-1", status: "completed" }));

    appendEvent(delta("turn-1:m0:text", "assistant", "追加"));

    const messages = useDcConsole.getState().messages;
    expect(messages).toHaveLength(1);
    expect(messages[0].content).toBe("半截回答");
    expect(messages[0].streaming).toBe(false);
  });
});

describe("DcChat 流式渲染", () => {
  // 说明：DcChat 现有测试在 src/features/dc/DcChat.test.tsx，本次改动不允许触碰该目录，
  // 故渲染断言暂放本文件（如需归位，可整体搬到 features/dc/DcChat.test.tsx）。
  beforeEach(() => {
    useDcConsole.getState().reset();
    useDcConsole.setState({ activeSessionId: "dc-test", status: "acting" });
  });

  function draft(partial: Partial<DcChatMessage>): DcChatMessage {
    return {
      id: "stream-turn-1:m1:text",
      role: "assistant",
      content: "",
      timestamp: "2026-01-01T08:00:00Z",
      turnId: "turn-1",
      streaming: true,
      streamKey: "turn-1:m1:text",
      ...partial,
    };
  }

  it("流式草稿用纯文本 + 光标渲染，半截 Markdown 不被解析", () => {
    useDcConsole.setState({ messages: [draft({ content: "**任务" })] });
    const { container } = render(createElement(DcChat));

    const plain = container.querySelector(".dc-message-assistant .dc-stream-plain");
    expect(plain?.textContent).toBe("**任务");
    expect(container.querySelector(".dc-stream-caret")).not.toBeNull();
    expect(container.querySelector(".dc-message-assistant strong")).toBeNull();
    expect(container.querySelector(".dc-message-assistant .markdown")).toBeNull();
  });

  it("定稿后切回 Markdown 且光标消失", () => {
    useDcConsole.setState({ messages: [draft({ content: "**任务完成**", streaming: false })] });
    const { container } = render(createElement(DcChat));

    expect(container.querySelector(".dc-stream-plain")).toBeNull();
    expect(container.querySelector(".dc-stream-caret")).toBeNull();
    expect(container.querySelector(".dc-message-assistant strong")?.textContent).toBe("任务完成");
  });

  it("流式思考草稿在思考块内显示光标，定稿后消失", () => {
    useDcConsole.setState({
      messages: [
        draft({
          id: "stream-turn-1:m0:thinking",
          role: "thinking",
          content: "先看",
          streamKey: "turn-1:m0:thinking",
        }),
      ],
    });
    const { container } = render(createElement(DcChat));
    expect(container.querySelector(".dc-thinking-body .dc-stream-caret")).not.toBeNull();

    act(() => {
      useDcConsole.setState({
        messages: [
          draft({
            id: "stream-turn-1:m0:thinking",
            role: "thinking",
            content: "先看界面",
            streaming: false,
          }),
        ],
      });
    });
    expect(container.querySelector(".dc-thinking-body .dc-stream-caret")).toBeNull();
    expect(container.querySelector(".dc-thinking-body")?.textContent).toBe("先看界面");
  });
});
