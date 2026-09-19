// DcTierMenu 测试：默认 L2、展开 5 项、点击切换层级、Esc/外部点击关闭、执行中与无会话时禁用。

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { DcTierMenu } from "./DcTierMenu";
import { useDcConsole } from "../../stores/dc-console";

const setDcTier = vi.fn();

vi.mock("../../api/dc-client", () => ({
  closeDcSession: vi.fn(),
  createDcSession: vi.fn(),
  dcArtifactUrl: vi.fn(),
  distillDcProfile: vi.fn(),
  fetchDcScript: vi.fn(),
  generateDcScript: vi.fn(),
  getDcSession: vi.fn(),
  listDcSessions: vi.fn(),
  sendDcMessage: vi.fn(),
  setDcTier: (...args: unknown[]) => setDcTier(...args),
  stopDcTurn: vi.fn(),
}));

/** 会话替身：DcTierMenu 只读 tier / status。 */
function seedSession(options: { sessionStatus?: string; withSession?: boolean } = {}) {
  const { sessionStatus = "idle", withSession = true } = options;
  useDcConsole.setState({
    activeSessionId: withSession ? "dc-1" : "",
    session: withSession ? ({ tier: 2, status: sessionStatus, invocations: [] } as never) : null,
  });
}

describe("DcTierMenu", () => {
  beforeEach(() => {
    setDcTier.mockReset();
    useDcConsole.getState().reset();
  });

  it("默认显示 L2 与短标签，展开后列出 L1-L5 全量描述", () => {
    seedSession();
    render(<DcTierMenu />);

    const trigger = screen.getByRole("button", { name: "工具层级 L2" });
    expect(trigger).toHaveTextContent("L2 · 观测诊断");
    expect(trigger).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(trigger);

    expect(trigger).toHaveAttribute("aria-expanded", "true");
    const menu = screen.getByRole("menu", { name: "选择工具层级" });
    const items = within(menu).getAllByRole("menuitem");
    expect(items).toHaveLength(5);
    expect(within(menu).getByText(/L1 · UI 交互/)).toBeInTheDocument();
    expect(within(menu).getByText(/L5 · 受控 Shell/)).toBeInTheDocument();
    // 当前层级高亮
    expect(within(menu).getByRole("menuitem", { name: /L2 · 观测诊断/ })).toHaveAttribute("aria-checked", "true");
  });

  it("点击菜单项调用 changeTier 并关闭菜单", () => {
    seedSession();
    render(<DcTierMenu />);

    fireEvent.click(screen.getByRole("button", { name: "工具层级 L2" }));
    fireEvent.click(screen.getByRole("menuitem", { name: /L3 · 应用管理/ }));

    expect(setDcTier).toHaveBeenCalledWith("dc-1", 3);
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("Esc 关闭菜单且不切换层级", () => {
    seedSession();
    render(<DcTierMenu />);

    fireEvent.click(screen.getByRole("button", { name: "工具层级 L2" }));
    fireEvent.keyDown(window, { key: "Escape" });

    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(setDcTier).not.toHaveBeenCalled();
  });

  it("点击外部关闭，点击菜单内部不关闭", () => {
    seedSession();
    render(<DcTierMenu />);

    fireEvent.click(screen.getByRole("button", { name: "工具层级 L2" }));
    const menu = screen.getByRole("menu");

    fireEvent.mouseDown(menu);
    expect(screen.getByRole("menu")).toBeInTheDocument();

    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("Agent 执行中触发器禁用", () => {
    seedSession({ sessionStatus: "thinking" });
    render(<DcTierMenu />);

    const trigger = screen.getByRole("button", { name: "工具层级 L2" });
    expect(trigger).toBeDisabled();

    fireEvent.click(trigger);
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("无会话时显示 L2、禁用并提示创建会话后可调整", () => {
    seedSession({ withSession: false });
    render(<DcTierMenu />);

    const trigger = screen.getByRole("button", { name: "工具层级 L2" });
    expect(trigger).toBeDisabled();
    expect(trigger).toHaveAttribute("title", "创建会话后可调整");
  });
});
