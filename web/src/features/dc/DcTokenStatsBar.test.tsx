// DcTokenStatsBar 测试：输入框下方的本会话 token 用量与缓存命中率。
// 覆盖：数值渲染、命中率分母语义、空态、预算进度、上下文占用率仅在单请求轮次显示。

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

const getDcSession = vi.hoisted(() => vi.fn());
const resumeDcSession = vi.hoisted(() => vi.fn());
const listDcSessions = vi.hoisted(() => vi.fn());
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
  stopDcTurn: vi.fn(),
}));

import { useDcConsole, selectTokenStats } from "../../stores/dc-console";
import type { DcTokenUsage } from "../../api/dc-types";
import { DcTokenStatsBar } from "./DcTokenStatsBar";

function usage(overrides: Partial<DcTokenUsage> = {}): DcTokenUsage {
  return {
    requests: 2,
    tool_calls: 1,
    input_tokens: 1721,
    output_tokens: 16,
    cache_read_tokens: 1536,
    cache_write_tokens: 0,
    details: { reasoning_tokens: 16 },
    ...overrides,
  };
}

/** 直接注入 store 状态（组件只读，不需要真实会话）。 */
function setUsage(
  session: DcTokenUsage | null,
  extra: { contextWindow?: number; requestLimit?: number; lastTurnRequests?: number } = {},
) {
  useDcConsole.setState({ tokenUsage: session, contextWindow: 0, requestLimit: 0, lastTurnRequests: 0, ...extra });
}

describe("DcTokenStatsBar", () => {
  beforeEach(() => {
    useDcConsole.getState().reset();
    useDcConsole.setState({ activeSessionId: "dc-test" });
  });

  it("渲染本会话 token 总量、缓存命中率与明细", () => {
    setUsage(usage());

    render(<DcTokenStatsBar />);

    // 总量 = 输入 + 输出 = 1737
    expect(screen.getByText("1,737")).toBeInTheDocument();
    // 命中率分母是 input_tokens（1721），不是 input + cached（3257）
    expect(screen.getByText("89.3%")).toBeInTheDocument();
    expect(screen.queryByText("47.2%")).not.toBeInTheDocument();
    expect(screen.getByText(/输入 1,721/)).toBeInTheDocument();
    expect(screen.getByText(/输出 16/)).toBeInTheDocument();
    expect(screen.getByText(/缓存读 1,536/)).toBeInTheDocument();
  });

  it("无数据时显示空态而不是 0% 或 NaN", () => {
    setUsage(null);

    render(<DcTokenStatsBar />);

    expect(screen.getByText("尚无用量数据")).toBeInTheDocument();
    expect(screen.queryByText(/NaN/)).not.toBeInTheDocument();
    expect(screen.queryByText(/缓存命中/)).not.toBeInTheDocument();
  });

  it("全 0 用量也视为无数据（Mock 模式）", () => {
    setUsage(usage({ requests: 0, input_tokens: 0, output_tokens: 0, cache_read_tokens: 0 }));

    render(<DcTokenStatsBar />);

    expect(screen.getByText("尚无用量数据")).toBeInTheDocument();
  });

  it("已知请求上限时显示 已用/上限", () => {
    setUsage(usage(), { requestLimit: 120 });

    render(<DcTokenStatsBar />);

    expect(screen.getByText("请求 2/120")).toBeInTheDocument();
  });

  it("上下文占用率仅在「本轮恰好一次请求」时显示", () => {
    setUsage(usage({ requests: 1, input_tokens: 2500 }), { contextWindow: 10000, lastTurnRequests: 1 });

    const withSingle = render(<DcTokenStatsBar />);
    expect(screen.getByText("上下文 25.0%")).toBeInTheDocument();
    withSingle.unmount();

    // 一轮内多次请求：input_tokens 是多次之和，不能当作单次上下文占用
    useDcConsole.setState({ lastTurnRequests: 3 });
    render(<DcTokenStatsBar />);
    expect(screen.queryByText(/上下文/)).not.toBeInTheDocument();
  });

  it("selectTokenStats：无输入 token 时命中率为 null（不做除零）", () => {
    useDcConsole.setState({ tokenUsage: usage({ input_tokens: 0, cache_read_tokens: 100 }), contextWindow: 0 });

    const stats = selectTokenStats(useDcConsole.getState());

    expect(stats.hasData).toBe(true);
    expect(stats.cacheHitRate).toBeNull();
  });
});
