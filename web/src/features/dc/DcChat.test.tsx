// DcChat 组件测试：五种角色的渲染、思考块折叠、工具卡状态与 Markdown 最终回复。

import { beforeEach, describe, expect, it } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { DcChat } from "./DcChat";
import { useDcConsole } from "../../stores/dc-console";
import type { DcChatMessage } from "../../api/dc-types";

function message(partial: Partial<DcChatMessage> & Pick<DcChatMessage, "id" | "role" | "content">): DcChatMessage {
  return { timestamp: "2026-01-01T08:00:00Z", ...partial };
}

describe("DcChat", () => {
  beforeEach(() => {
    useDcConsole.getState().reset();
  });

  it("按角色渲染用户、思考、叙述、工具与最终回复", () => {
    useDcConsole.setState({
      status: "thinking",
      messages: [
        message({ id: "u1", role: "user", content: "截个图看看" }),
        message({ id: "t1", role: "thinking", content: "先看看界面元素" }),
        message({ id: "n1", role: "narration", content: "我来截图" }),
        message({
          id: "inv-1",
          role: "tool",
          content: "screenshot ✓ (480ms)",
          toolName: "screenshot",
          toolArgs: { source: "tool" },
          toolResult: "Screenshot 1080x2232",
          toolStatus: "success",
          durationMs: 480,
        }),
        message({ id: "a1", role: "assistant", content: "**任务完成**" }),
      ],
    });

    const { container } = render(<DcChat />);

    expect(screen.getByText("截个图看看")).toBeInTheDocument();
    expect(screen.getByText("💭 思考")).toBeInTheDocument();
    expect(screen.getByText("先看看界面元素")).toBeInTheDocument();
    expect(screen.getByText("我来截图")).toBeInTheDocument();
    expect(screen.getByText("screenshot ✓ (480ms)")).toBeInTheDocument();
    // 最终回复走 Markdown 渲染
    expect(container.querySelector(".dc-message-assistant strong")?.textContent).toBe("任务完成");
  });

  it("思考块可折叠，展开后可见工具卡参数与结果", () => {
    useDcConsole.setState({
      status: "thinking",
      messages: [
        message({ id: "t1", role: "thinking", content: "推理内容" }),
        message({
          id: "inv-1",
          role: "tool",
          content: "click ✓ (10ms)",
          toolName: "click",
          toolArgs: { x: 10, y: 20 },
          toolResult: "ok: clicked",
          toolStatus: "success",
        }),
      ],
    });

    const { container } = render(<DcChat />);
    expect(screen.getByText("推理内容")).toBeInTheDocument();

    fireEvent.click(screen.getByText("💭 思考"));
    expect(screen.queryByText("推理内容")).not.toBeInTheDocument();

    fireEvent.click(screen.getByText("click ✓ (10ms)"));
    expect(container.textContent).toContain('"x": 10');
    expect(container.textContent).toContain("ok: clicked");
  });

  it("工具执行中显示 spinner，失败卡片标记 failed", () => {
    useDcConsole.setState({
      status: "acting",
      messages: [
        message({ id: "inv-1", role: "tool", content: "调用 start_app", toolName: "start_app", toolStatus: "running" }),
        message({
          id: "inv-2",
          role: "tool",
          content: "back ✗ (5ms)",
          toolName: "back",
          toolStatus: "failed",
          toolResult: "ERROR: boom",
        }),
      ],
    });

    const { container } = render(<DcChat />);
    expect(container.querySelector(".dc-tool-spinner")).not.toBeNull();
    expect(container.querySelector(".dc-message-tool.failed")).not.toBeNull();
    expect(screen.getByText("Agent 正在执行工具...")).toBeInTheDocument();
  });

  it("轮次结束后思考块自动折叠", () => {
    const thinking = message({ id: "t1", role: "thinking", content: "推理内容" });
    act(() => {
      useDcConsole.setState({ status: "thinking", messages: [thinking] });
    });
    const { rerender } = render(<DcChat />);
    expect(screen.getByText("推理内容")).toBeInTheDocument();

    act(() => {
      useDcConsole.setState({ status: "idle" });
    });
    rerender(<DcChat />);
    expect(screen.queryByText("推理内容")).not.toBeInTheDocument();
  });
});
