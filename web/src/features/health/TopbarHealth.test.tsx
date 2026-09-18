// TopbarHealth 测试：环境芯片组必须如实反映 /api/health 语义
// （三个芯片文本 + 状态圆点 + 刷新按钮），health 缺失时降级为 unknown/not found。

import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { TopbarHealth } from "./TopbarHealth";
import { useConsole } from "../../stores/console";

function seedHealth() {
  useConsole.setState({
    health: {
      status: "ok",
      device: { connected: true, id: "127.0.0.1:5555" },
      model: { configured: true, vision_configured: true, provider: "auto" },
      hypium: { importable: true, version: "6.1.0.210" },
    },
  });
}

describe("TopbarHealth", () => {
  beforeEach(() => {
    useConsole.setState({ health: null, healthLoading: false });
  });

  it("展示设备 / Provider / Hypium 三个芯片，圆点反映健康语义", () => {
    seedHealth();
    const { container } = render(<TopbarHealth />);

    expect(screen.getByText("127.0.0.1:5555")).toBeInTheDocument();
    expect(screen.getByText("auto")).toBeInTheDocument();
    expect(screen.getByText("6.1.0.210")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "刷新环境状态" })).toBeInTheDocument();

    const dots = [...container.querySelectorAll(".env-dot")];
    expect(dots).toHaveLength(3);
    expect(dots.every((dot) => dot.classList.contains("ok"))).toBe(true);
  });

  it("health 缺失时降级为 unknown / not found，且圆点不置为 ok", () => {
    const { container } = render(<TopbarHealth />);

    expect(screen.getAllByText("unknown")).toHaveLength(2);
    expect(screen.getByText("not found")).toBeInTheDocument();

    const dots = [...container.querySelectorAll(".env-dot")];
    expect(dots).toHaveLength(3);
    expect(dots.some((dot) => dot.classList.contains("ok"))).toBe(false);
  });

  it("点击刷新按钮触发 loadHealth，并在加载中禁用", async () => {
    const loadHealth = vi.fn().mockResolvedValue(undefined);
    useConsole.setState({ loadHealth });
    render(<TopbarHealth />);

    fireEvent.click(screen.getByRole("button", { name: "刷新环境状态" }));
    expect(loadHealth).toHaveBeenCalledTimes(1);

    act(() => {
      useConsole.setState({ healthLoading: true });
    });
    expect(screen.getByRole("button", { name: "刷新环境状态" })).toBeDisabled();
  });
});
