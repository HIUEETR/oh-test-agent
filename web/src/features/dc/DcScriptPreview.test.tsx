// DcScriptPreview 测试：受控弹窗必须 portal 到 body（否则被 .panel 的 backdrop-filter
// 关在面板层叠上下文里，被相邻面板遮挡）、Esc/遮罩关闭、复制下载与「重新生成」可用。

import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { DcScriptPreview } from "./DcScriptPreview";
import { useDcConsole } from "../../stores/dc-console";
import type { DcScriptArtifact } from "../../api/dc-types";

const generateDcScript = vi.fn();

vi.mock("../../api/dc-client", () => ({
  closeDcSession: vi.fn(),
  createDcSession: vi.fn(),
  dcArtifactUrl: vi.fn((sessionId: string, path: string) => `/api/dc/sessions/${sessionId}/artifacts/${path}`),
  distillDcProfile: vi.fn(),
  fetchDcScript: vi.fn(),
  generateDcScript: (...args: unknown[]) => generateDcScript(...args),
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

/** 受控弹窗的宿主：把 open 状态接起来，验证 onClose 真的能关掉弹窗。 */
function Harness() {
  const [open, setOpen] = useState(true);
  return <DcScriptPreview open={open} onClose={() => setOpen(false)} />;
}

describe("DcScriptPreview", () => {
  beforeEach(() => {
    generateDcScript.mockReset();
    useDcConsole.getState().reset();
    useDcConsole.setState({ activeSessionId: "dc-1", script: artifact() });
  });

  it("弹窗经 portal 渲染到 body，而不是留在宿主容器内", () => {
    const { container } = render(<Harness />);

    const overlay = document.body.querySelector(".dc-script-overlay");
    expect(overlay).not.toBeNull();
    // 宿主内部不得包含遮罩（原先的层叠上下文 bug 就是因为它在这里）
    expect(container.querySelector(".dc-script-overlay")).toBeNull();
    expect(container.contains(overlay)).toBe(false);
    expect(screen.getByRole("dialog", { name: "生成的 Hypium 脚本" })).toBeInTheDocument();
  });

  it("展示脚本源码与统计信息", () => {
    render(<Harness />);

    expect(document.body.querySelector(".dc-script-code")?.textContent).toContain("from hypium import UiDriver");
    expect(screen.getByText("可回放操作：5")).toBeInTheDocument();
    expect(screen.getByText("省略操作：1")).toBeInTheDocument();
    expect(screen.getByText("警告：1")).toBeInTheDocument();
  });

  it("open=false 时不渲染任何弹窗", () => {
    render(<DcScriptPreview open={false} onClose={vi.fn()} />);

    expect(document.body.querySelector(".dc-script-overlay")).toBeNull();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("Esc 关闭并恢复页面滚动", () => {
    render(<Harness />);
    expect(document.body.style.overflow).toBe("hidden");

    fireEvent.keyDown(window, { key: "Escape" });

    expect(document.body.querySelector(".dc-script-overlay")).toBeNull();
    expect(document.body.style.overflow).toBe("");
  });

  it("点击遮罩关闭，点击弹窗内部不关闭", () => {
    render(<Harness />);

    fireEvent.click(screen.getByRole("dialog", { name: "生成的 Hypium 脚本" }));
    expect(document.body.querySelector(".dc-script-overlay")).not.toBeNull();

    fireEvent.click(document.body.querySelector(".dc-script-overlay") as Element);
    expect(document.body.querySelector(".dc-script-overlay")).toBeNull();
  });

  it("无断言但可执行时提示「无检查点，建议补充断言」", () => {
    useDcConsole.setState({ script: { ...artifact(), replay_eligible: true, explicit_assertions: 0 } });

    render(<Harness />);

    expect(screen.getByText("可立即执行 · 断言 0 条（无检查点，建议补充断言）")).toBeInTheDocument();
  });

  it("有断言且可执行时显示「可立即执行」并给出断言数", () => {
    useDcConsole.setState({ script: { ...artifact(), replay_eligible: true, explicit_assertions: 3 } });

    render(<Harness />);

    expect(screen.getByText("可立即执行 · 断言 3 条")).toBeInTheDocument();
  });

  it("不可执行（占位身份/无可回放动作）时显示原因", () => {
    useDcConsole.setState({ script: { ...artifact(), replay_eligible: false } });

    render(<Harness />);

    expect(screen.getByText("不可执行 · 缺少可回放动作或身份为占位值")).toBeInTheDocument();
  });

  it("footer 的「重新生成」重新调用生成接口", async () => {
    generateDcScript.mockResolvedValue(artifact());
    render(<Harness />);

    fireEvent.click(screen.getByRole("button", { name: /重新生成/ }));

    await vi.waitFor(() => {
      expect(generateDcScript).toHaveBeenCalledWith("dc-1", undefined, undefined);
    });
  });
});
