// DcComposerActions 测试：任务完成门禁（无录制/执行中禁用）、脚本 pill 一键生成并自动预览、
// 蒸馏 pill 以「无身份参数」调用后端、成功后展示摘要、失败后给出「手动填写身份」兜底入口。

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { DcComposerActions } from "./DcComposerActions";
import { useDcConsole } from "../../stores/dc-console";
import type { DcDistillResult, DcScriptArtifact } from "../../api/dc-types";

const generateDcScript = vi.fn();
const distillDcProfile = vi.fn();

vi.mock("../../api/dc-client", () => ({
  closeDcSession: vi.fn(),
  createDcSession: vi.fn(),
  dcArtifactUrl: vi.fn(),
  distillDcProfile: (...args: unknown[]) => distillDcProfile(...args),
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
    python_text: "from hypium import UiDriver\n",
    warnings: [],
    generated_at: "2026-01-01T08:00:00Z",
    included_operations: 4,
    omitted_operations: [],
  };
}

function distillResult(): DcDistillResult {
  return {
    profile_id: "dc-dc-1",
    status: "verified",
    pages_covered: 3,
    stable_locators: 4,
    assertions: 2,
    replay_run_id: "dc-1:profile-attempt-1",
    replay_passed: true,
    warnings: [],
  };
}

/** 会话替身：DcComposerActions 只读 status / invocations.length。 */
function seed(options: { invocations?: number; status?: string; sessionStatus?: string } = {}) {
  const { invocations = 5, status = "idle", sessionStatus = "active" } = options;
  useDcConsole.setState({
    activeSessionId: "dc-1",
    status,
    session: {
      tier: 2,
      status: sessionStatus,
      invocations: Array.from({ length: invocations }, () => ({})),
    } as never,
  });
}

describe("DcComposerActions", () => {
  beforeEach(() => {
    generateDcScript.mockReset();
    distillDcProfile.mockReset();
    useDcConsole.getState().reset();
  });

  it("无录制记录时两个 pill 都禁用", () => {
    seed({ invocations: 0 });
    render(<DcComposerActions />);

    const scriptPill = screen.getByRole("button", { name: "Hypium 脚本" });
    const distillPill = screen.getByRole("button", { name: "蒸馏为 Profile" });
    expect(scriptPill).toBeDisabled();
    expect(distillPill).toBeDisabled();
    expect(distillPill).toHaveAttribute("title", "任务完成且有录制记录后可点击");
  });

  it("Agent 执行中两个 pill 都禁用", () => {
    seed({ status: "acting" });
    render(<DcComposerActions />);

    expect(screen.getByRole("button", { name: "Hypium 脚本" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "蒸馏为 Profile" })).toBeDisabled();
  });

  it("会话已关闭时两个 pill 都禁用", () => {
    seed({ sessionStatus: "closed" });
    render(<DcComposerActions />);

    expect(screen.getByRole("button", { name: "Hypium 脚本" })).toBeDisabled();
  });

  it("脚本 pill 调用生成接口并自动打开预览", async () => {
    seed();
    generateDcScript.mockResolvedValue(artifact());
    render(<DcComposerActions />);

    fireEvent.click(screen.getByRole("button", { name: "Hypium 脚本" }));

    await waitFor(() => {
      expect(generateDcScript).toHaveBeenCalledWith("dc-1", undefined, undefined);
    });
    await waitFor(() => {
      expect(screen.getByRole("dialog", { name: "生成的 Hypium 脚本" })).toBeInTheDocument();
    });
  });

  it("已有脚本时直接打开预览，不重复请求生成", () => {
    seed();
    useDcConsole.setState({ script: artifact() });
    render(<DcComposerActions />);

    fireEvent.click(screen.getByRole("button", { name: "Hypium 脚本" }));

    expect(screen.getByRole("dialog", { name: "生成的 Hypium 脚本" })).toBeInTheDocument();
    expect(generateDcScript).not.toHaveBeenCalled();
  });

  it("脚本落到占位身份时给出手动填写入口，并可显式身份重新生成", async () => {
    seed();
    generateDcScript
      .mockResolvedValueOnce({
        ...artifact(),
        replay_eligible: false,
        runnable_blockers: ["app identity is a placeholder (com.example.app/EntryAbility)"],
      })
      .mockResolvedValueOnce(artifact());
    render(<DcComposerActions />);

    fireEvent.click(screen.getByRole("button", { name: "Hypium 脚本" }));

    // 首次仍是无参调用（后端从录制推断），并把占位警告摆到 hint 行
    await waitFor(() => {
      expect(generateDcScript).toHaveBeenCalledWith("dc-1", undefined, undefined);
    });
    await waitFor(() => {
      expect(screen.getByText(/占位身份/)).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "手动填写身份" }));
    const dialog = screen.getByRole("dialog", { name: "手动填写应用身份" });
    expect(dialog).toBeInTheDocument();
    expect(dialog).toHaveTextContent(/重新生成/);

    fireEvent.change(screen.getByLabelText("bundleName"), { target: { value: "com.huawei.hmos.calendar" } });
    fireEvent.change(screen.getByLabelText("MainAbility"), { target: { value: "MainAbility" } });
    fireEvent.click(screen.getByRole("button", { name: /用该身份重试/ }));

    await waitFor(() => {
      expect(generateDcScript).toHaveBeenLastCalledWith("dc-1", "com.huawei.hmos.calendar", "MainAbility");
    });
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "手动填写应用身份" })).not.toBeInTheDocument();
    });
  });

  it("已有占位身份脚本时再次点击 pill 仍保留手动身份入口", () => {
    seed();
    useDcConsole.setState({
      script: {
        ...artifact(),
        replay_eligible: false,
        runnable_blockers: ["app identity is a placeholder (com.example.app/EntryAbility)"],
      },
    });
    render(<DcComposerActions />);

    fireEvent.click(screen.getByRole("button", { name: "Hypium 脚本" }));

    expect(screen.getByRole("dialog", { name: "生成的 Hypium 脚本" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "手动填写身份" })).toBeInTheDocument();
    expect(generateDcScript).not.toHaveBeenCalled();
  });

  it("蒸馏 pill 不传身份（由后端推断）并在成功后展示摘要", async () => {
    seed();
    distillDcProfile.mockResolvedValue(distillResult());
    render(<DcComposerActions />);

    fireEvent.click(screen.getByRole("button", { name: "蒸馏为 Profile" }));

    // 无身份参数：bundle_name / main_ability 均为 undefined，不随请求体发送
    await waitFor(() => {
      expect(distillDcProfile).toHaveBeenCalledWith("dc-1", undefined, undefined);
    });
    await waitFor(() => {
      expect(screen.getByText(/状态：verified/)).toBeInTheDocument();
    });
    expect(screen.getByText(/可在「Profile 资产」Tab 查看/)).toBeInTheDocument();
  });

  it("蒸馏失败展示错误文案，并通过「手动填写身份」以显式身份重试一次", async () => {
    seed();
    distillDcProfile
      .mockRejectedValueOnce(new Error("cannot infer application identity from session recordings; pass bundle_name/main_ability explicitly"))
      .mockResolvedValueOnce(distillResult());
    render(<DcComposerActions />);

    fireEvent.click(screen.getByRole("button", { name: "蒸馏为 Profile" }));

    await waitFor(() => {
      expect(screen.getByText(/cannot infer application identity/)).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("button", { name: "手动填写身份" }));

    const dialog = screen.getByRole("dialog", { name: "手动填写应用身份" });
    expect(dialog).toBeInTheDocument();
    // 空值不允许提交，避免把空身份当显式身份发给后端
    fireEvent.click(screen.getByRole("button", { name: /用该身份重试/ }));
    expect(distillDcProfile).toHaveBeenCalledTimes(1);

    fireEvent.change(screen.getByLabelText("bundleName"), { target: { value: "com.example.notes" } });
    fireEvent.change(screen.getByLabelText("MainAbility"), { target: { value: "MainAbility" } });
    fireEvent.click(screen.getByRole("button", { name: /用该身份重试/ }));

    await waitFor(() => {
      expect(distillDcProfile).toHaveBeenLastCalledWith("dc-1", "com.example.notes", "MainAbility");
    });
  });
});
