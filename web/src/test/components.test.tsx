// 组件冒烟测试：思考流与流水线状态条用样例事件渲染出预期内容。

import { describe, expect, it, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { ThoughtStream } from "../features/live/ThoughtStream";
import { PipelineStrip } from "../features/pipeline/PipelineStrip";
import { EventTimeline } from "../features/live/EventTimeline";
import { useConsole } from "../stores/console";
import type { RunEvent } from "../api/types";

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
    // pipeline-step 的状态 class 挂在标签文本的外层 span 上
    expect(screen.getByText("采集").parentElement?.className).toContain("done");
  });
});
