// DC 操作日志 + 当前活动区测试（计划第 8 节 Web 测试清单 1-7）。
// 覆盖：仅 SSE 时的 running 行、finished 就地更新、空状态分档、refreshSession 对账、
// 会话切换/重复事件/旧回调隔离、无模型文本时的活动区走时、取消/超时/unknown/needs_attention 视觉。

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";

const getDcSession = vi.hoisted(() => vi.fn());
const resumeDcSession = vi.hoisted(() => vi.fn());
const listDcSessions = vi.hoisted(() => vi.fn());
const stopDcTurn = vi.hoisted(() => vi.fn());
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
  stopDcTurn,
}));

import { useDcConsole, selectOperationRecords } from "../../stores/dc-console";
import type { DcEvent, DcEventType, DcSessionView } from "../../api/dc-types";
import { DcOperationLog } from "./DcOperationLog";
import { DcActivityBar } from "./DcActivityBar";
import { DcChat } from "./DcChat";

let nextEventId = 1;

function event(
  type: DcEventType,
  payload: Record<string, unknown> = {},
  message = "",
  sessionId = "dc-test",
): DcEvent {
  return {
    event_id: nextEventId++,
    session_id: sessionId,
    type,
    timestamp: "2026-01-01T08:00:00.000Z",
    message: message || type,
    payload,
  };
}

function emptySession(overrides: Partial<DcSessionView> = {}): DcSessionView {
  return {
    session_id: "dc-test",
    device_id: "mock-device",
    tier: 1,
    status: "idle",
    created_at: "2026-01-01T08:00:00Z",
    turns: [],
    invocations: [],
    ...overrides,
  };
}

function records() {
  return selectOperationRecords(useDcConsole.getState() as never);
}

describe("DcOperationLog 实时账本", () => {
  beforeEach(() => {
    nextEventId = 1;
    getDcSession.mockReset();
    listDcSessions.mockReset();
    listDcSessions.mockResolvedValue([]);
    stopDcTurn.mockReset();
    stopDcTurn.mockResolvedValue({ session_id: "dc-test", status: "cancelling" });
    useDcConsole.getState().reset();
    useDcConsole.setState({ activeSessionId: "dc-test" });
  });

  it("只收到 tool_call_started 时立即显示 1 步且状态为 running", () => {
    const { container } = render(<DcOperationLog />);
    expect(container.textContent).toContain("0 步");

    act(() => {
      useDcConsole.getState().appendEvent(
        event("tool_call_started", {
          invocation_id: "inv-1",
          turn_id: "turn-1",
          tool: "screenshot",
          args: { source: "tool" },
          status: "running",
          phase: "snapshot_display",
          started_at: "2026-01-01T08:00:00.000Z",
          deadline_at: "2026-01-01T08:00:30.000Z",
          cancellable: true,
        }),
      );
    });

    expect(container.textContent).toContain("1 步");
    expect(container.querySelector(".dc-operation-row.running")).not.toBeNull();
    expect(container.querySelector(".dc-operation-row.running .dc-tool-spinner")).not.toBeNull();
    expect(screen.getByText("screenshot")).toBeInTheDocument();
    expect(screen.getByText("执行中")).toBeInTheDocument();
    // 空状态不再出现
    expect(container.textContent).not.toContain("尚无操作记录");
  });

  it("tool_call_finished 就地更新同一行，不新增重复行，计数不变", () => {
    const { container } = render(<DcOperationLog />);
    const { appendEvent } = useDcConsole.getState();

    act(() => {
      appendEvent(
        event("tool_call_started", {
          invocation_id: "inv-1",
          turn_id: "turn-1",
          tool: "screenshot",
          args: {},
          status: "running",
          phase: "snapshot_display",
          started_at: "2026-01-01T08:00:00.000Z",
        }),
      );
      appendEvent(
        event(
          "tool_call_finished",
          {
            invocation_id: "inv-1",
            turn_id: "turn-1",
            tool: "screenshot",
            success: true,
            status: "succeeded",
            duration_ms: 8713,
            result_summary: "Screenshot 1080x2232",
            effect_status: "none",
            ended_at: "2026-01-01T08:00:08.713Z",
          },
          "",
        ),
      );
    });

    expect(container.textContent).toContain("1 步");
    expect(container.querySelectorAll(".dc-operation-row")).toHaveLength(1);
    expect(container.querySelector(".dc-operation-row.running")).toBeNull();
    expect(container.querySelector(".dc-operation-row.ok")).not.toBeNull();
    expect(container.textContent).toContain("00:08");
    // 聊天工具卡与日志状态一致
    const toolMessages = useDcConsole.getState().messages.filter((m) => m.role === "tool");
    expect(toolMessages).toHaveLength(1);
    expect(toolMessages[0].toolStatus).toBe("success");
    expect(toolMessages[0].toolState).toBe("succeeded");
  });

  it("初始 session 快照为空、只有 SSE 事件时日志仍显示调用", () => {
    useDcConsole.setState({ session: emptySession() });
    const { container } = render(<DcOperationLog />);

    act(() => {
      useDcConsole.getState().appendEvent(
        event("tool_call_started", {
          invocation_id: "inv-sse",
          turn_id: "turn-1",
          tool: "dump_ui_hierarchy",
          args: {},
          status: "running",
          started_at: "2026-01-01T08:00:00.000Z",
        }),
      );
    });

    expect(container.textContent).toContain("1 步");
    expect(screen.getByText("dump_ui_hierarchy")).toBeInTheDocument();
  });

  it("tool_call_progress 更新阶段但不新增行，也不把 running 回退为终态", () => {
    const { appendEvent } = useDcConsole.getState();
    act(() => {
      appendEvent(
        event("tool_call_started", {
          invocation_id: "inv-1",
          turn_id: "turn-1",
          tool: "screenshot",
          status: "running",
          phase: "snapshot_display",
          started_at: "2026-01-01T08:00:00.000Z",
        }),
      );
      appendEvent(
        event("tool_call_progress", {
          invocation_id: "inv-1",
          turn_id: "turn-1",
          tool: "screenshot",
          status: "running",
          phase: "file_recv",
          elapsed_ms: 9000,
          remaining_ms: 21000,
          cancellable: true,
        }),
      );
    });

    const all = records();
    expect(all).toHaveLength(1);
    expect(all[0].status).toBe("running");
    expect(all[0].phase).toBe("file_recv");
    expect(all[0].ended_at).toBeNull();
  });

  it("refreshSession 在 running 期间不覆盖实时调用，完成后对账补齐字段", async () => {
    const { appendEvent } = useDcConsole.getState();
    act(() => {
      appendEvent(
        event("tool_call_started", {
          invocation_id: "inv-live",
          turn_id: "turn-1",
          tool: "screenshot",
          args: { source: "tool" },
          status: "running",
          phase: "snapshot_display",
          started_at: "2026-01-01T08:00:00.000Z",
        }),
      );
    });

    // 服务端快照仍为空（工具尚未落账）：对账不得清空实时行
    getDcSession.mockResolvedValue(emptySession({ status: "acting" }));
    await useDcConsole.getState().refreshSession(true);

    let all = records();
    expect(all).toHaveLength(1);
    expect(all[0].status).toBe("running");
    expect(all[0].tool).toBe("screenshot");

    // 服务端随后落账为终态：对账补齐 duration/result/effect，不新增行
    getDcSession.mockResolvedValue(
      emptySession({
        status: "idle",
        turns: [
          {
            turn_id: "turn-1",
            user_message: "截个图",
            status: "completed",
            agent_summary: "完成",
            started_at: "2026-01-01T08:00:00Z",
            ended_at: "2026-01-01T08:00:09Z",
            invocation_ids: ["inv-live"],
          },
        ],
        invocations: [
          {
            invocation_id: "inv-live",
            turn_id: "turn-1",
            tool: "screenshot",
            tier: 1,
            args: { source: "tool" },
            success: true,
            status: "succeeded",
            started_at: "2026-01-01T08:00:00Z",
            ended_at: "2026-01-01T08:00:08.713Z",
            duration_ms: 8713,
            result_summary: "Screenshot 1080x2232",
            effect_status: "none",
            phase: "finished",
          },
        ],
      }),
    );
    await useDcConsole.getState().refreshSession(true);

    all = records();
    expect(all).toHaveLength(1);
    expect(all[0].status).toBe("succeeded");
    expect(all[0].duration_ms).toBe(8713);
    expect(all[0].result_summary).toBe("Screenshot 1080x2232");
    expect(all[0].ended_at).toBe("2026-01-01T08:00:08.713Z");
  });

  it("服务端快照仍有旧 running 时，实时终态不会被回退", async () => {
    const { appendEvent } = useDcConsole.getState();
    act(() => {
      appendEvent(
        event("tool_call_started", {
          invocation_id: "inv-1",
          turn_id: "turn-1",
          tool: "click",
          status: "running",
          phase: "device",
          started_at: "2026-01-01T08:00:00.000Z",
        }),
      );
      appendEvent(
        event("tool_call_finished", {
          invocation_id: "inv-1",
          turn_id: "turn-1",
          tool: "click",
          status: "failed",
          error: "boom",
          error_code: "device_error",
          duration_ms: 120,
          effect_status: "unknown",
          ended_at: "2026-01-01T08:00:00.120Z",
        }),
      );
    });

    // 陈旧快照：同一条仍是 running
    getDcSession.mockResolvedValue(
      emptySession({
        status: "idle",
        invocations: [
          {
            invocation_id: "inv-1",
            turn_id: "turn-1",
            tool: "click",
            tier: 1,
            args: {},
            success: false,
            status: "running",
            started_at: "2026-01-01T08:00:00Z",
            ended_at: null,
            duration_ms: 0,
          },
        ],
      }),
    );
    await useDcConsole.getState().refreshSession(true);

    const all = records();
    expect(all).toHaveLength(1);
    expect(all[0].status).toBe("failed");
  });

  it("会话切换清理旧账本，旧会话迟到的回调不污染新会话", () => {
    const { appendEvent } = useDcConsole.getState();
    act(() => {
      appendEvent(
        event("tool_call_started", {
          invocation_id: "inv-old",
          turn_id: "turn-old",
          tool: "back",
          status: "running",
          started_at: "2026-01-01T08:00:00.000Z",
        }),
      );
    });
    expect(records()).toHaveLength(1);

    useDcConsole.setState({ sessions: [] });
    act(() => {
      useDcConsole.getState().selectSession("dc-other");
    });
    expect(records()).toHaveLength(0);

    // 旧会话的 SSE 回调迟到
    act(() => {
      appendEvent(
        event("tool_call_started", {
          invocation_id: "inv-late",
          turn_id: "turn-old",
          tool: "click",
          status: "running",
          started_at: "2026-01-01T08:00:01.000Z",
        }, "", "dc-test"),
      );
    });
    expect(records()).toHaveLength(0);
  });

  it("重复投递同一事件不产生重复行", () => {
    const started = event("tool_call_started", {
      invocation_id: "inv-dup",
      turn_id: "turn-1",
      tool: "wait",
      status: "running",
      started_at: "2026-01-01T08:00:00.000Z",
    });
    act(() => {
      useDcConsole.getState().appendEvent(started);
      useDcConsole.getState().appendEvent({ ...started, payload: { ...started.payload } });
    });
    expect(records()).toHaveLength(1);
  });

  it("空状态分档：未选择会话 / 正在同步 / 断线等待对账 / 尚无操作记录", () => {
    useDcConsole.setState({ activeSessionId: "", syncing: false, connection: "idle" });
    const first = render(<DcOperationLog />);
    expect(screen.getByText("未选择会话")).toBeInTheDocument();
    first.unmount();

    useDcConsole.setState({ activeSessionId: "dc-test", syncing: true });
    const second = render(<DcOperationLog />);
    expect(second.container.textContent).toContain("正在同步操作记录");
    expect(second.container.textContent).not.toContain("尚无操作记录");
    second.unmount();

    useDcConsole.setState({ syncing: false, connection: "reconnecting" });
    const third = render(<DcOperationLog />);
    expect(third.container.textContent).toContain("连接已断开，正在等待对账");
    third.unmount();

    useDcConsole.setState({ connection: "open" });
    render(<DcOperationLog />);
    expect(screen.getByText("尚无操作记录")).toBeInTheDocument();
  });

  it("终态视觉区分 succeeded / failed / timed_out / cancelled / unknown", () => {
    const { appendEvent } = useDcConsole.getState();
    const cases: Array<[string, string, string]> = [
      ["inv-a", "succeeded", "ok"],
      ["inv-b", "failed", "fail"],
      ["inv-c", "timed_out", "timeout"],
      ["inv-d", "cancelled", "cancelled"],
      ["inv-e", "unknown", "unknown"],
    ];
    act(() => {
      for (const [id, status] of cases) {
        appendEvent(
          event("tool_call_finished", {
            invocation_id: id,
            turn_id: "turn-1",
            tool: "click",
            status,
            duration_ms: 10,
            ended_at: "2026-01-01T08:00:00.010Z",
          }),
        );
      }
    });

    const { container } = render(<DcOperationLog />);
    for (const [, , tone] of cases) {
      expect(container.querySelectorAll(`.dc-operation-row.${tone}`)).toHaveLength(1);
    }
    expect(container.textContent).toContain("5 步");
    expect(container.textContent).toContain("结果未知");
    expect(container.textContent).toContain("超时");
  });

  it("兼容旧服务：只有 success 布尔时归一化为 succeeded/failed", () => {
    const { appendEvent } = useDcConsole.getState();
    act(() => {
      appendEvent(
        event("tool_call_finished", {
          invocation_id: "inv-old-ok",
          turn_id: "turn-1",
          tool: "screenshot",
          success: true,
          duration_ms: 100,
        }),
      );
      appendEvent(
        event("tool_call_finished", {
          invocation_id: "inv-old-bad",
          turn_id: "turn-1",
          tool: "back",
          success: false,
          duration_ms: 20,
          error: "boom",
        }),
      );
    });

    const all = records();
    expect(all.map((r) => r.status)).toEqual(["succeeded", "failed"]);
  });

  it("展开行显示参数与结果摘要，长文本不撑破布局（截断容器）", () => {
    const { appendEvent } = useDcConsole.getState();
    act(() => {
      appendEvent(
        event("tool_call_finished", {
          invocation_id: "inv-long",
          turn_id: "turn-1",
          tool: "input_text",
          args: { text: "x".repeat(400) },
          status: "succeeded",
          duration_ms: 30,
          result_summary: "y".repeat(400),
        }),
      );
    });

    const { container } = render(<DcOperationLog />);
    fireEvent.click(container.querySelector(".dc-op-head") as HTMLElement);

    expect(container.querySelector(".dc-op-detail")).not.toBeNull();
    expect(container.querySelector(".dc-op-detail .dc-op-block pre")?.textContent).toContain("x".repeat(20));
    expect(container.textContent).toContain("结果摘要");
  });
});

describe("DcActivityBar 当前活动区", () => {
  beforeEach(() => {
    nextEventId = 1;
    getDcSession.mockReset();
    getDcSession.mockResolvedValue(emptySession({ status: "acting" }));
    listDcSessions.mockReset();
    listDcSessions.mockResolvedValue([]);
    stopDcTurn.mockReset();
    stopDcTurn.mockResolvedValue({ session_id: "dc-test", status: "cancelling" });
    useDcConsole.getState().reset();
    useDcConsole.setState({ activeSessionId: "dc-test" });
  });

  it("长时间没有模型文本时仍显示阶段、调用 ID、已耗时与超时倒计时", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2026-01-01T08:00:00.000Z"));
      act(() => {
        useDcConsole.getState().appendEvent(
          event("model_call_started", {
            model_call_id: "model-call-1",
            turn_id: "turn-1",
            phase: "waiting_model",
            attempt: 1,
            deadline_at: "2026-01-01T08:01:30.000Z",
            remaining_budget_ms: 90000,
          }),
        );
      });

      const { container } = render(<DcActivityBar />);
      expect(screen.getByText("正在等待模型")).toBeInTheDocument();
      expect(screen.getByText("model-call-1")).toBeInTheDocument();
      // 没有任何模型消息
      expect(useDcConsole.getState().messages).toHaveLength(0);
      expect(container.querySelector(".dc-activity-elapsed")?.textContent).toBe("00:00");
      // 90 秒 deadline：倒计时读数明确
      expect(container.querySelector(".dc-activity-status")?.textContent).toContain("将在 90 秒时超时");
      expect(container.textContent).toContain("最后进度");
    } finally {
      vi.useRealTimers();
    }
  });

  it("阶段文案覆盖截图/UI 层级/设备/校验", () => {
    const { appendEvent } = useDcConsole.getState();
    const phases: Array<[string, string]> = [
      ["snapshot_display", "正在采集截图"],
      ["file_recv", "正在接收设备文件"],
      ["dump_ui_hierarchy", "正在读取 UI 层级"],
      ["waiting_device", "正在等待设备响应"],
      ["validating", "正在校验结果"],
    ];
    for (const [phase, label] of phases) {
      act(() => {
        appendEvent(
          event("tool_call_started", {
            invocation_id: `inv-${phase}`,
            turn_id: "turn-1",
            tool: "screenshot",
            status: "running",
            phase,
            started_at: "2026-01-01T08:00:00.000Z",
            cancellable: true,
          }),
        );
      });
      const view = render(<DcActivityBar />);
      expect(screen.getByText(label)).toBeInTheDocument();
      view.unmount();
      act(() => {
        appendEvent(
          event("tool_call_finished", {
            invocation_id: `inv-${phase}`,
            turn_id: "turn-1",
            tool: "screenshot",
            status: "succeeded",
            duration_ms: 10,
          }),
        );
      });
    }
  });

  it("本地 tick 让已耗时持续走时（无需新事件）", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2026-01-01T08:00:00.000Z"));
      act(() => {
        useDcConsole.getState().appendEvent(
          event("model_call_started", {
            model_call_id: "model-call-tick",
            turn_id: "turn-1",
            phase: "waiting_model",
            deadline_at: "2026-01-01T08:01:30.000Z",
          }),
        );
      });
      const { container } = render(<DcActivityBar />);
      expect(container.querySelector(".dc-activity-elapsed")?.textContent).toBe("00:00");

      act(() => {
        vi.advanceTimersByTime(5000);
      });
      expect(container.querySelector(".dc-activity-elapsed")?.textContent).toBe("00:05");
    } finally {
      vi.useRealTimers();
    }
  });

  it("deadline 已过时显示已超时而不是永久转圈", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2026-01-01T08:02:00.000Z"));
      act(() => {
        useDcConsole.getState().appendEvent(
          event("model_call_started", {
            model_call_id: "model-call-timeout",
            turn_id: "turn-1",
            phase: "waiting_model",
            deadline_at: "2026-01-01T08:01:30.000Z",
          }),
        );
      });
      const { container } = render(<DcActivityBar />);
      expect(container.querySelector(".dc-activity-status")?.textContent).toContain("已超时");
    } finally {
      vi.useRealTimers();
    }
  });

  it("needs_attention 显示原因与处置选项", () => {
    act(() => {
      useDcConsole.getState().appendEvent(
        event("needs_attention", {
          turn_id: "turn-1",
          invocation_id: "inv-1",
          reason: "上一步点击的副作用未知",
          options: ["重新观测", "确认已生效并继续", "终止"],
        }),
      );
    });

    const { container } = render(<DcActivityBar />);
    expect(screen.getByText("需要人工确认")).toBeInTheDocument();
    expect(container.querySelector(".dc-activity-status")?.textContent).toContain("禁止自动重放");
    expect(container.textContent).toContain("上一步点击的副作用未知");
    expect(container.textContent).toContain("重新观测");
  });

  it("取消后显示上下文连续性提示", () => {
    useDcConsole.setState({
      continuation: {
        previous_turn_id: "turn-1",
        previous_status: "cancelled",
        original_user_goal: "打开日历并新建 9/18 纪念事件",
        public_agent_summary: "",
        public_agent_steps: [],
        completed_operations: ["start_app"],
        active_or_unknown_operation: null,
        last_snapshot_path: null,
        last_page_path: null,
        last_foreground_app: null,
        last_progress_at: null,
        effect_status: "none",
        reconcile_required: false,
        context_version: 3,
        updated_at: "2026-01-01T08:00:00Z",
      },
    });

    const { container } = render(<DcActivityBar />);
    expect(container.textContent).toContain("上一轮已取消");
    expect(container.textContent).toContain("打开日历并新建 9/18 纪念事件");
  });

  it("空闲且无连续性提示时不占位", () => {
    const { container } = render(<DcActivityBar />);
    expect(container.querySelector(".dc-activity")).toBeNull();
  });
});

describe("停止按钮两阶段", () => {
  beforeEach(() => {
    nextEventId = 1;
    getDcSession.mockReset();
    getDcSession.mockResolvedValue(emptySession({ status: "acting" }));
    stopDcTurn.mockReset();
    stopDcTurn.mockResolvedValue({ session_id: "dc-test", status: "cancelling" });
    useDcConsole.getState().reset();
    useDcConsole.setState({ activeSessionId: "dc-test" });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("请求停止后按钮进入「正在确认设备状态」，收到终态后恢复发送", async () => {
    act(() => {
      useDcConsole.getState().appendEvent(event("turn_started", { turn_id: "turn-1" }));
    });

    const { container } = render(<DcChat />);
    const stop = screen.getByRole("button", { name: "停止执行" });
    fireEvent.click(stop);

    await act(async () => {
      await Promise.resolve();
    });

    expect(stopDcTurn).toHaveBeenCalledWith("dc-test");
    expect(useDcConsole.getState().cancelStatus).toBe("confirming");
    expect(screen.getByRole("button", { name: "正在确认设备状态" })).toBeDisabled();
    expect(container.textContent).toContain("正在确认设备状态");

    act(() => {
      useDcConsole.getState().appendEvent(event("turn_interrupted", { turn_id: "turn-1", reason: "user_cancelled" }));
    });

    expect(useDcConsole.getState().cancelStatus).toBe("settled");
    expect(screen.getByRole("button", { name: "发送消息" })).toBeInTheDocument();
  });

  it("turn_finished(status=cancelled) 也会结束确认阶段", () => {
    act(() => {
      useDcConsole.getState().appendEvent(event("turn_started", { turn_id: "turn-1" }));
      useDcConsole.setState({ cancelStatus: "confirming", cancelRequestedAt: new Date().toISOString() });
    });

    const { container } = render(<DcChat />);
    act(() => {
      useDcConsole
        .getState()
        .appendEvent(event("turn_finished", { turn_id: "turn-1", status: "cancelled" }));
    });

    expect(useDcConsole.getState().cancelStatus).toBe("settled");
    expect(container.querySelector(".dc-activity")?.textContent).toContain("已取消");
  });
});
