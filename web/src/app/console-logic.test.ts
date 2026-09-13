// 深链与流水线推导的单元测试。

import { describe, expect, it } from "vitest";
import { readDeepLink, writeDeepLink, TAB_KEYS } from "./deep-links";
import { derivePipeline } from "../utils/pipeline";
import type { RunEvent, RunTrace } from "../api/types";

describe("deep-links", () => {
  it("读取合法 tab 与 run_id", () => {
    window.history.replaceState(null, "", "/?run_id=run-9&tab=graph");
    expect(readDeepLink()).toEqual({ runId: "run-9", tab: "graph" });
  });

  it("非法 tab 回退为 live；缺失 run_id 为空串", () => {
    window.history.replaceState(null, "", "/?tab=hacker");
    const { runId, tab } = readDeepLink();
    expect(tab).toBe("live");
    expect(runId).toBe("");
  });

  it("全部旧版 tab 值保持兼容", () => {
    for (const legacy of ["live", "graph", "script", "profiles", "report"] as const) {
      window.history.replaceState(null, "", `/?tab=${legacy}`);
      expect(readDeepLink().tab).toBe(legacy);
    }
    expect(TAB_KEYS).toContain("advisor");
  });

  it("writeDeepLink 把状态同步回地址栏", () => {
    window.history.replaceState(null, "", "/");
    writeDeepLink("advisor", "run-3");
    expect(window.location.search).toContain("tab=advisor");
    expect(window.location.search).toContain("run_id=run-3");
  });
});

describe("derivePipeline", () => {
  function event(type: string, id = 1): RunEvent {
    return { event_id: id, run_id: "r", type, timestamp: "", message: type, payload: {} };
  }
  function trace(overrides: Partial<RunTrace>): RunTrace {
    return {
      run_id: "r", target_app_id: "t", task: "", state: "discovering", mode: "exploration",
      model_used: "m", model_mock: false, plan: [], snapshots: [], actions: [], assertions: [],
      graph: { nodes: [], edges: [] }, replays: [], ...overrides,
    };
  }

  it("无运行时全部待命", () => {
    const phases = derivePipeline([], null, false);
    expect(phases.map((phase) => phase.state)).toEqual(Array(7).fill("pending"));
  });

  it("探索进行中：采集/感知完成，探索高亮", () => {
    const events = [event("screen_captured", 1), event("elements_detected", 2), event("discovery_started", 3)];
    const phases = derivePipeline(events, trace({}), false);
    const byKey = Object.fromEntries(phases.map((phase) => [phase.key, phase.state]));
    expect(byKey.capture).toBe("done");
    expect(byKey.perceive).toBe("done");
    expect(byKey.explore).toBe("active");
    expect(byKey.script).toBe("pending");
  });

  it("脚本与回放完成后报告就绪", () => {
    const events = [
      event("screen_captured", 1), event("script_generated", 2), event("execution_finished", 3),
    ];
    const phases = derivePipeline(events, trace({ state: "completed" }), true);
    const byKey = Object.fromEntries(phases.map((phase) => [phase.key, phase.state]));
    expect(byKey.script).toBe("done");
    expect(byKey.replay).toBe("done");
    expect(byKey.report).toBe("done");
  });
});
