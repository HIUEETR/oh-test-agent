// DcDistillButton 测试（Phase 5）：占位身份拦截、无录制时禁用、成功/失败反馈。

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { DcDistillButton } from "./DcDistillButton";
import { useDcConsole } from "../../stores/dc-console";
import type { DcDistillResult } from "../../api/dc-types";

const distillDcProfile = vi.fn();

vi.mock("../../api/dc-client", () => ({
  closeDcSession: vi.fn(),
  createDcSession: vi.fn(),
  dcArtifactUrl: vi.fn(),
  distillDcProfile: (...args: unknown[]) => distillDcProfile(...args),
  fetchDcScript: vi.fn(),
  generateDcScript: vi.fn(),
  getDcSession: vi.fn(),
  listDcSessions: vi.fn(),
  sendDcMessage: vi.fn(),
  setDcTier: vi.fn(),
  stopDcTurn: vi.fn(),
}));

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

/** 会话替身：只需 invocations.length。 */
function seedSession(invocationCount: number) {
  useDcConsole.setState({
    activeSessionId: "dc-1",
    session: { invocations: Array.from({ length: invocationCount }, () => ({})) } as never,
  });
}

describe("DcDistillButton", () => {
  beforeEach(() => {
    distillDcProfile.mockReset();
    useDcConsole.getState().reset();
  });

  it("无录制操作时禁用蒸馏", () => {
    seedSession(0);

    render(<DcDistillButton />);

    expect(screen.getByRole("button", { name: /蒸馏为 Profile/ })).toBeDisabled();
    expect(screen.getByText(/先录制至少 3 个页面/)).toBeInTheDocument();
  });

  it("默认占位身份（空值）在点击时给出明确原因且不调用后端", async () => {
    seedSession(5);
    render(<DcDistillButton />);

    fireEvent.click(screen.getByRole("button", { name: /蒸馏为 Profile/ }));

    await waitFor(() => {
      expect(screen.getByText(/不能是 com.example.app/)).toBeInTheDocument();
    });
    expect(distillDcProfile).not.toHaveBeenCalled();
  });

  it("Ability 仍为占位值 EntryAbility 时同样被拦截", async () => {
    seedSession(5);
    render(<DcDistillButton />);

    fireEvent.change(screen.getByLabelText("bundleName"), { target: { value: "com.example.notes" } });
    fireEvent.change(screen.getByLabelText("Main Ability"), { target: { value: "EntryAbility" } });
    fireEvent.click(screen.getByRole("button", { name: /蒸馏为 Profile/ }));

    await waitFor(() => {
      expect(screen.getByText(/不能是 EntryAbility/)).toBeInTheDocument();
    });
    expect(distillDcProfile).not.toHaveBeenCalled();
  });

  it("占位 bundleName 被本地拦截且不调用后端", async () => {
    seedSession(5);
    render(<DcDistillButton />);

    fireEvent.change(screen.getByLabelText("bundleName"), { target: { value: "com.example.app" } });
    fireEvent.change(screen.getByLabelText("Main Ability"), { target: { value: "MainAbility" } });
    fireEvent.click(screen.getByRole("button", { name: /蒸馏为 Profile/ }));

    await waitFor(() => {
      expect(screen.getByText(/不能是 com.example.app/)).toBeInTheDocument();
    });
    expect(distillDcProfile).not.toHaveBeenCalled();
  });

  it("合法身份时调用后端并展示蒸馏结果", async () => {
    seedSession(5);
    distillDcProfile.mockResolvedValue(distillResult());
    render(<DcDistillButton />);

    fireEvent.change(screen.getByLabelText("bundleName"), { target: { value: "com.example.notes" } });
    fireEvent.change(screen.getByLabelText("Main Ability"), { target: { value: "MainAbility" } });
    fireEvent.click(screen.getByRole("button", { name: /蒸馏为 Profile/ }));

    await waitFor(() => {
      expect(distillDcProfile).toHaveBeenCalledWith("dc-1", "com.example.notes", "MainAbility");
    });
    await waitFor(() => {
      expect(screen.getByText(/状态：verified/)).toBeInTheDocument();
    });
  });
});
