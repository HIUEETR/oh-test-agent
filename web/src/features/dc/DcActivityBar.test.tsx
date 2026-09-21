// ``DcActivityBar`` 的处置按钮测试（Phase 5.3）。
//
// 修的是一个既有死路：后端 ``POST /api/dc/sessions/{id}/resolve`` 早已实现，前端从未调用，
// ``needs_attention`` 触发后四个处置选项只是纯文本，用户无法解除阻塞。

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const resolveSession = vi.hoisted(() => vi.fn());

vi.mock("../../api/dc-client", () => ({
  closeDcSession: vi.fn(),
  createDcSession: vi.fn(),
  dcArtifactUrl: vi.fn((sessionId: string, path: string) => `/api/dc/sessions/${sessionId}/artifacts/${path}`),
  distillDcProfile: vi.fn(),
  fetchDcScript: vi.fn(),
  generateDcScript: vi.fn(),
  getDcSession: vi.fn(),
  listDcSessions: vi.fn(),
  resolveSession,
  resumeDcSession: vi.fn(),
  sendDcMessage: vi.fn(),
  setDcTier: vi.fn(),
  stopDcTurn: vi.fn(),
}));

import { useDcConsole } from "../../stores/dc-console";
import { DcActivityBar, RESOLVE_ACTIONS } from "./DcActivityBar";

function setAttentionState() {
  useDcConsole.setState({
    activeSessionId: "dc-test",
    attentionReason: "点击后设备副作用未确认",
    attentionOptions: ["reobserve", "confirm_effect", "retry", "terminate"],
    activity: {
      kind: "attention",
      phaseLabel: "需要人工确认",
      phase: "attention",
      operationId: "inv-1",
      turnId: "turn-1",
      tool: "click",
      epoch: 1,
      startedAt: null,
      elapsedMs: null,
      remainingMs: null,
      deadlineAt: null,
      lastProgressAt: null,
      cancellable: false,
      attentionReason: "点击后设备副作用未确认",
      attentionOptions: ["reobserve", "confirm_effect", "retry", "terminate"],
    },
  });
}

beforeEach(() => {
  resolveSession.mockReset();
  resolveSession.mockResolvedValue({
    session_id: "dc-test",
    device_id: "mock",
    tier: 1,
    status: "idle",
    created_at: "2026-01-01T00:00:00Z",
    turns: [],
    invocations: [],
  });
  useDcConsole.setState({
    activeSessionId: "",
    attentionReason: "",
    attentionOptions: [],
    error: "",
    activity: {
      kind: "idle",
      phaseLabel: "空闲",
      phase: "idle",
      operationId: "",
      turnId: null,
      tool: null,
      epoch: 0,
      startedAt: null,
      elapsedMs: null,
      remainingMs: null,
      deadlineAt: null,
      lastProgressAt: null,
      cancellable: false,
      attentionReason: null,
      attentionOptions: [],
    },
  });
});

describe("DcActivityBar resolve buttons", () => {
  it("only offers the four resolve actions while attention is required", () => {
    setAttentionState();
    render(<DcActivityBar />);

    for (const item of RESOLVE_ACTIONS) {
      expect(screen.getByRole("button", { name: item.label })).toBeInTheDocument();
    }
  });

  it("does not render resolve buttons when nothing needs attention", () => {
    render(<DcActivityBar />);

    expect(screen.queryByRole("group", { name: /处置未确认的设备副作用/ })).toBeNull();
  });

  it("calls resolveSession with the action of the clicked button", async () => {
    setAttentionState();
    render(<DcActivityBar />);

    fireEvent.click(screen.getByRole("button", { name: "确认已生效" }));

    await waitFor(() => expect(resolveSession).toHaveBeenCalledWith("dc-test", "confirm_effect"));
  });

  it("sends each of the four actions", async () => {
    setAttentionState();
    render(<DcActivityBar />);

    for (const item of RESOLVE_ACTIONS) {
      resolveSession.mockClear();
      fireEvent.click(screen.getByRole("button", { name: item.label }));
      await waitFor(() => expect(resolveSession).toHaveBeenCalledWith("dc-test", item.action));
    }
  });

  it("surfaces a resolve failure as an error instead of silently failing", async () => {
    setAttentionState();
    resolveSession.mockRejectedValue(new Error("device busy"));
    render(<DcActivityBar />);

    fireEvent.click(screen.getByRole("button", { name: "重新观测" }));

    await waitFor(() => expect(useDcConsole.getState().error).toContain("device busy"));
  });
});
