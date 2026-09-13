// 组件冒烟测试：思考流与流水线状态条用样例事件渲染出预期内容。

import { describe, expect, it, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { ThoughtStream } from "../features/live/ThoughtStream";
import { PipelineStrip } from "../features/pipeline/PipelineStrip";
import { EventTimeline } from "../features/live/EventTimeline";
import { AdvisorPanel } from "../features/advisor/AdvisorPanel";
import { aggregateThoughts } from "../utils/thought-aggregator";
import { useConsole } from "../stores/console";
import type { AdvisorTurnRecord, RunEvent } from "../api/types";

let nextId = 1;
function event(type: string, payload: Record<string, unknown> = {}, message = ""): RunEvent {
  return {
    event_id: nextId++,
    run_id: "run-ui",
    type,
    timestamp: "2026-01-01T08:00:00Z",
    message: message || type,
    payload,
  };
}

function advisorTurn(turn: number): AdvisorTurnRecord {
  return {
    turn,
    page_path: `/page-${turn}`,
    snapshot_path: null,
    input: `候选列表第 ${turn} 轮`,
    output: { page_summary: `页面理解 ${turn}`, recommended: [0], avoid: [], reason: "结构优先" },
    source: "model",
    error: null,
    elapsed_ms: 100 + turn,
  };
}

describe("ThoughtStream", () => {
  beforeEach(() => {
    useConsole.setState({ runId: "run-ui", events: [] });
  });

  it("无事件时显示待命空态", () => {
    render(<ThoughtStream />);
    expect(screen.getByText("思考流待命")).toBeInTheDocument();
  });

  it("渲染计划步骤与顾问建议（含候选可读标签）", () => {
    useConsole.setState({
      runId: "run-ui",
      events: [
        event("plan_created", { steps: [{ step_id: "s1", instruction: "打开设置", tool: "open_app" }], mock: false }, "已生成 1 个原子步骤"),
        event("discovery_progress", {
          stage: "advisor", page_id: "page-1", source: "model",
          verdict: { page_summary: "应用首页", recommended: [0], avoid: [1], reason: "结构入口优先" },
          candidates: [
            { index: 0, kind: "click", label: "设置", coordinate: null },
            { index: 1, kind: "click", label: "信息流卡片", coordinate: null },
          ],
        }, "顾问建议"),
      ],
    });
    render(<ThoughtStream />);
    expect(screen.getByText("打开设置")).toBeInTheDocument();
    expect(screen.getByText("应用首页")).toBeInTheDocument();
    // 推荐动作以可读文本呈现而不是裸编号
    expect(screen.getByText(/推荐 · click「设置」/)).toBeInTheDocument();
    expect(screen.getByText(/回避 · click「信息流卡片」/)).toBeInTheDocument();
  });

  it("视觉摘要以感知块呈现", () => {
    useConsole.setState({
      runId: "run-ui",
      events: [event("screen_captured", { snapshot_id: "s1", image_path: "", summary: "这是设置页", width: 1, height: 1 }, "已采集本地截图")],
    });
    render(<ThoughtStream />);
    expect(screen.getByText("这是设置页")).toBeInTheDocument();
  });
});

describe("EventTimeline", () => {
  it("事件可展开查看 payload JSON", () => {
    useConsole.setState({
      runId: "run-ui",
      events: [event("screen_captured", { snapshot_id: "s1", sha256: "abc" }, "已采集本地截图")],
    });
    const { container } = render(<EventTimeline />);
    const row = container.querySelector("details.event-row");
    expect(row).not.toBeNull();
    // 展开后 payload 以 JSON 呈现
    row!.querySelector("summary")!.dispatchEvent(new Event("click", { bubbles: true }));
    expect(container.textContent).toContain('"sha256"');
  });
});

describe("PipelineStrip", () => {
  it("流水线随事件推进点亮各阶段", () => {
    useConsole.setState({
      events: [
        event("screen_captured", {}, "已采集本地截图"),
        event("elements_detected", {}, "识别到 5 个元素"),
        event("discovery_started", {}, "开始有界自动探索"),
      ],
      trace: null,
      reportReady: false,
    });
    render(<PipelineStrip />);
    expect(screen.getByText("探索")).toBeInTheDocument();
    expect(screen.getByText("验证")).toBeInTheDocument();
    // pipeline-step 的状态 class 挂在标签文本的外层 span 上
    expect(screen.getByText("采集").parentElement?.className).toContain("done");
  });
});

describe("AdvisorPanel", () => {
  beforeEach(() => {
    useConsole.setState({ runId: "run-ui", discovery: null, advisorTurns: [], health: null });
  });

  it("实时 advisor_turn 事件在探索结束前即可见（不再依赖 REST advisor_log）", () => {
    useConsole.setState({ advisorTurns: [advisorTurn(1)] });
    render(<AdvisorPanel />);
    expect(screen.getByText("页面理解 1")).toBeInTheDocument();
    expect(screen.getByText("1 轮调用")).toBeInTheDocument();
  });

  it("探索结束后的 REST advisor_log 与实时留痕按轮次合并去重", () => {
    useConsole.setState({
      discovery: { advisor_log: [advisorTurn(1), advisorTurn(2)] } as never,
      advisorTurns: [advisorTurn(2), advisorTurn(3)],
    });
    render(<AdvisorPanel />);
    expect(screen.getByText("3 轮调用")).toBeInTheDocument();
    expect(screen.getByText("页面理解 3")).toBeInTheDocument();
  });
});

describe("console store advisor turns", () => {
  beforeEach(() => {
    useConsole.setState({ events: [], advisorTurns: [] });
  });

  it("appendEvent 累积 advisor_turn 留痕并按轮去重排序", () => {
    const { appendEvent } = useConsole.getState();
    appendEvent(event("discovery_progress", { stage: "advisor_turn", turn: advisorTurn(2) }));
    appendEvent(event("discovery_progress", { stage: "advisor_turn", turn: advisorTurn(1) }));
    appendEvent(event("discovery_progress", { stage: "advisor_turn", turn: advisorTurn(2) }));
    expect(useConsole.getState().advisorTurns.map((turn) => turn.turn)).toEqual([1, 2]);
  });

  it("resetRunView 清空实时顾问留痕", () => {
    useConsole.setState({ advisorTurns: [advisorTurn(1)] });
    useConsole.getState().resetRunView();
    expect(useConsole.getState().advisorTurns).toEqual([]);
  });
});

describe("thought aggregator stop reasons", () => {
  it("探索早停原因以可读文案呈现并说明后续阶段", () => {
    const blocks = aggregateThoughts([event("discovery_finished", { stop_reason: "admission_metrics_reached" })]);
    const notice = blocks.find((block) => block.kind === "notice");
    expect(notice && "detail" in notice ? notice.detail : "").toContain("已达准入指标");
    expect(notice && "detail" in notice ? notice.detail : "").not.toContain("admission_metrics_reached");
  });

  it("验证与回放过程事件渲染为通知块", () => {
    const blocks = aggregateThoughts([
      event("profile_verification_started", {}),
      event("profile_verification_round_started", { round_number: 1 }),
      event("hypium_replay_started", { attempt: 1 }),
    ]);
    const titles = blocks.map((block) => block.title);
    expect(titles).toContain("profile_verification_started");
    expect(titles).toContain("profile_verification_round_started");
    expect(titles).toContain("hypium_replay_started");
  });
});
