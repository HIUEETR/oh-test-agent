// 深链与流水线推导的单元测试。

import { describe, expect, it } from "vitest";
import { readDeepLink, writeDeepLink, LEGACY_TAB_ALIASES, TAB_KEYS } from "./deep-links";
import { derivePipeline } from "../utils/pipeline";
import type { RunEvent, RunTrace } from "../api/types";

describe("deep-links", () => {
  it("读取合法 tab 与 run_id", () => {
    window.history.replaceState(null, "", "/?run_id=run-9&tab=graph");
    expect(readDeepLink()).toEqual({ runId: "run-9", tab: "graph" });
  });

  it("非法 tab 回退为 session；缺失 run_id 为空串", () => {
    window.history.replaceState(null, "", "/?tab=hacker");
    const { runId, tab } = readDeepLink();
    expect(tab).toBe("session");
    expect(runId).toBe("");
  });

  it("顶层 Tab 为 6 个（2026-09-17 收敛为 5 个 + Phase 5 新增「缺陷」）", () => {
    expect([...TAB_KEYS]).toEqual(["session", "graph", "script", "profiles", "runs", "defects"]);
  });

  it("全部旧版 tab 值映射到当前 Tab 集合（深链不失效）", () => {
    for (const [legacy, expected] of Object.entries(LEGACY_TAB_ALIASES)) {
      window.history.replaceState(null, "", `/?tab=${legacy}`);
      expect(readDeepLink().tab).toBe(expected);
    }
    for (const current of TAB_KEYS) {
      window.history.replaceState(null, "", `/?tab=${current}`);
      expect(readDeepLink().tab).toBe(current);
    }
  });

  it("缺陷相关的历史别名收敛到 defects", () => {
    for (const legacy of ["defect", "anomaly"]) {
      window.history.replaceState(null, "", `/?tab=${legacy}`);
      expect(readDeepLink().tab).toBe("defects");
    }
  });

  it("writeDeepLink 把状态同步回地址栏", () => {
    window.history.replaceState(null, "", "/");
    writeDeepLink("session", "run-3");
    expect(window.location.search).toContain("tab=session");
    expect(window.location.search).toContain("run_id=run-3");
  });
});

describe("derivePipeline", () => {
  function event(type: string, id = 1): RunEvent {
    return { event_id: id, run_id: "r", type, timestamp: "", message: type, payload: {} };
  }
  function trace(overrides: Partial<RunTrace>): RunTrace {
    return {
      run_id: "r", target_app_id: "t", task: "", state: "discovering", mode: "regression",
      model_used: "m", model_mock: false, plan: [], snapshots: [], actions: [], assertions: [],
      graph: { nodes: [], edges: [] }, replays: [], ...overrides,
    };
  }

  it("无运行时全部待命", () => {
    const phases = derivePipeline([], null, false);
    expect(phases.map((phase) => phase.state)).toEqual(Array(8).fill("pending"));
  });

  it("探索进行中：采集/感知完成，探索高亮", () => {
    const events = [event("screen_captured", 1), event("elements_detected", 2), event("discovery_started", 3)];
    const phases = derivePipeline(events, trace({}), false);
    const byKey = Object.fromEntries(phases.map((phase) => [phase.key, phase.state]));
    expect(byKey.capture).toBe("done");
    expect(byKey.perceive).toBe("done");
    expect(byKey.explore).toBe("active");
    expect(byKey.verify).toBe("pending");
    expect(byKey.script).toBe("pending");
  });

  it("探索完成后验证阶段点亮，脚本与回放随后推进", () => {
    const events = [
      event("discovery_started", 1), event("discovery_finished", 2), event("profile_draft_saved", 3),
      event("profile_verification_started", 4), event("profile_verification_round_started", 5),
      event("profile_verification_round_finished", 6), event("script_generated", 7),
      event("hypium_replay_started", 8), event("hypium_replay_finished", 9),
    ];
    // 3 次门禁（HYPIUM_REPLAY_ATTEMPTS=3）：只完成 1 次时回放阶段仍为 active。
    const phases = derivePipeline(events, trace({ replay_total: 3 }), false);
    const byKey = Object.fromEntries(phases.map((phase) => [phase.key, phase.state]));
    expect(byKey.explore).toBe("done");
    expect(byKey.verify).toBe("done");
    expect(byKey.script).toBe("done");
    // bootstrap 准入回放按 hypium_replay_* 事件逐次点亮
    expect(byKey.replay).toBe("active");
  });

  it("单次内联准入回放完成即回放阶段完成（默认门禁 HYPIUM_REPLAY_ATTEMPTS=1）", () => {
    const events = [
      event("script_generated", 1), event("hypium_replay_started", 2), event("hypium_replay_finished", 3),
    ];
    const phases = derivePipeline(events, trace({}), false);
    const byKey = Object.fromEntries(phases.map((phase) => [phase.key, phase.state]));
    expect(byKey.replay).toBe("done");
  });

  it("三次回放门禁按 trace.replay_total 判定完成（不再硬编码 3）", () => {
    const events = [
      event("script_generated", 1), event("hypium_replay_started", 2), event("hypium_replay_finished", 3),
    ];
    const phases = derivePipeline(events, trace({ replay_total: 3 }), false);
    const byKey = Object.fromEntries(phases.map((phase) => [phase.key, phase.state]));
    expect(byKey.replay).toBe("active");
  });

  it("脚本与回放完成后报告就绪", () => {
    const events = [
      event("screen_captured", 1), event("script_generated", 2), event("execution_finished", 3),
    ];
    const phases = derivePipeline(events, trace({ state: "completed" }), true);
    const byKey = Object.fromEntries(phases.map((phase) => [phase.key, phase.state]));
    expect(byKey.verify).toBe("done");
    expect(byKey.script).toBe("done");
    expect(byKey.replay).toBe("done");
    expect(byKey.report).toBe("done");
  });
});
