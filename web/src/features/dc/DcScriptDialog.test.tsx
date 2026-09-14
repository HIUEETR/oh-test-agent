// DcScriptDialog 测试：弹窗必须 portal 到 body（否则被 .panel 的 backdrop-filter
// 关在面板层叠上下文里，被相邻面板遮挡）、Esc/遮罩关闭、复制下载可用。

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { DcScriptDialog } from "./DcScriptDialog";
import { useDcConsole } from "../../stores/dc-console";
import type { DcScriptArtifact } from "../../api/dc-types";

vi.mock("../../api/dc-client", () => ({
  closeDcSession: vi.fn(),
  createDcSession: vi.fn(),
  dcArtifactUrl: vi.fn((sessionId: string, path: string) => `/api/dc/sessions/${sessionId}/artifacts/${path}`),
  fetchDcScript: vi.fn(),
  generateDcScript: vi.fn(),
  getDcSession: vi.fn(),
  listDcSessions: vi.fn(),
  sendDcMessage: vi.fn(),
  setDcTier: vi.fn(),
  stopDcTurn: vi.fn(),
}));

function artifact(): DcScriptArtifact {
  return {
    python_path: "artifacts/runs/dc-1/generated/dc_test_dc_1.py",
    python_text: "from hypium import UiDriver\n\n\ndef main() -> int:\n    return 0\n",
    warnings: ["inv-1: swipe direction inferred as LEFT"],
    generated_at: "2026-01-01T08:00:00Z",
    included_operations: 5,
    omitted_operations: [{ invocation_id: "inv-2", tool: "screenshot", reason: "not replayable" }],
  };
}

function openPreview() {
  render(<DcScriptDialog />);
  fireEvent.click(screen.getByRole("button", { name: /预览/ }));
}

describe("DcScriptDialog", () => {
  beforeEach(() => {
    useDcConsole.getState().reset();
    useDcConsole.setState({ script: artifact() });
  });

  it("弹窗经 portal 渲染到 body，而不是留在面板内", () => {
    const { container } = render(<DcScriptDialog />);
    fireEvent.click(screen.getByRole("button", { name: /预览/ }));

    const overlay = document.body.querySelector(".dc-script-overlay");
    expect(overlay).not.toBeNull();
    // 面板内部不得包含遮罩（原先的层叠上下文 bug 就是因为它在这里）
    expect(container.querySelector(".dc-script-overlay")).toBeNull();
    expect(container.contains(overlay)).toBe(false);
    expect(screen.getByRole("dialog", { name: "生成的 Hypium 脚本" })).toBeInTheDocument();
  });

  it("展示脚本源码与统计信息", () => {
    openPreview();

    expect(document.body.querySelector(".dc-script-code")?.textContent).toContain("from hypium import UiDriver");
    expect(screen.getByText("可回放操作：5")).toBeInTheDocument();
    expect(screen.getByText("省略操作：1")).toBeInTheDocument();
    expect(screen.getByText("警告：1")).toBeInTheDocument();
  });

  it("Esc 关闭并恢复页面滚动", () => {
    openPreview();
    expect(document.body.style.overflow).toBe("hidden");

    fireEvent.keyDown(window, { key: "Escape" });

    expect(document.body.querySelector(".dc-script-overlay")).toBeNull();
    expect(document.body.style.overflow).toBe("");
  });

  it("点击遮罩关闭，点击弹窗内部不关闭", () => {
    openPreview();

    fireEvent.click(screen.getByRole("dialog", { name: "生成的 Hypium 脚本" }));
    expect(document.body.querySelector(".dc-script-overlay")).not.toBeNull();

    fireEvent.click(document.body.querySelector(".dc-script-overlay") as Element);
    expect(document.body.querySelector(".dc-script-overlay")).toBeNull();
  });
});
