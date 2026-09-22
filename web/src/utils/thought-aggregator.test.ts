// 思考流聚合器的单元测试：事件 → 思考块的核心映射规则。

import { describe, expect, it } from "vitest";
import { aggregateThoughts } from "./thought-aggregator";
import type { RunEvent } from "../api/types";

let nextId = 1;

function makeEvent(type: string, payload: Record<string, unknown> = {}, message = ""): RunEvent {
  return {
    event_id: nextId++,
    run_id: "run-test",
    type,
    timestamp: "2026-01-01T00:00:00Z",
    message: message || type,
    payload,
  };
}

describe("aggregateThoughts", () => {
  it("把 plan_created 聚合为计划块并列出步骤", () => {
    const blocks = aggregateThoughts([
      makeEvent("plan_created", {
        steps: [{ step_id: "s1", instruction: "打开设置", tool: "open_app" }],
        mock: false,
      }, "已生成 1 个原子步骤"),
    ]);
    expect(blocks).toHaveLength(1);
    const block = blocks[0];
    expect(block.kind).toBe("plan");
    expect(block.phase).toBe("think");
    if (block.kind === "plan") {
      expect(block.steps).toHaveLength(1);
      expect(block.steps[0].instruction).toBe("打开设置");
      expect(block.mock).toBe(false);
    }
  });

  it("elements_detected 的元素数回填到对应感知块", () => {
    const blocks = aggregateThoughts([
      makeEvent("screen_captured", { snapshot_id: "s-1", image_path: "/x.png", summary: "这是一个设置页" }),
      makeEvent("elements_detected", { snapshot_id: "s-1", count: 12 }),
    ]);
    expect(blocks).toHaveLength(1);
    if (blocks[0].kind === "perception") {
      expect(blocks[0].elementCount).toBe(12);
      expect(blocks[0].summary).toBe("这是一个设置页");
    } else {
      expect.unreachable("应为 perception 块");
    }
  });

  it("合并视觉请求：摘要随后的 elements_detected 回填，不产生重复感知块", () => {
    // 后端在合并模式下先推送画面（summary 为空），观测完成后补发带摘要的 elements_detected。
    const blocks = aggregateThoughts([
      makeEvent("screen_captured", { snapshot_id: "s-2", image_path: "/y.png", summary: "" }),
      makeEvent("elements_detected", { snapshot_id: "s-2", count: 8, summary: "日历月视图" }),
    ]);
    expect(blocks).toHaveLength(1);
    if (blocks[0].kind === "perception") {
      expect(blocks[0].elementCount).toBe(8);
      expect(blocks[0].summary).toBe("日历月视图");
    } else {
      expect.unreachable("应为 perception 块");
    }
  });

  it("action_finished 的结果合并进对应步骤块并切换相位", () => {
    const blocks = aggregateThoughts([
      makeEvent("action_started", { step_id: "s1", decision: { tool: "click", target: "设置" } }, "打开设置"),
      makeEvent("action_finished", { step_id: "s1", tool: "click", success: false, error: "超时" }, "步骤失败"),
    ]);
    expect(blocks).toHaveLength(1);
    if (blocks[0].kind === "step") {
      expect(blocks[0].decision?.tool).toBe("click");
      expect(blocks[0].result?.success).toBe(false);
      expect(blocks[0].phase).toBe("fail");
    } else {
      expect.unreachable("应为 step 块");
    }
  });

  it("discovery_progress(stage=advisor) 聚合为顾问块并携带候选摘要", () => {
    const candidates = [{ index: 0, kind: "click", label: "设置", coordinate: null }];
    const blocks = aggregateThoughts([
      makeEvent("discovery_progress", {
        stage: "advisor", page_id: "page-1", source: "model",
        verdict: { page_summary: "应用首页", recommended: [0], avoid: [], reason: "结构入口优先" },
        candidates,
      }),
    ]);
    expect(blocks).toHaveLength(1);
    if (blocks[0].kind === "advisor") {
      expect(blocks[0].verdict?.page_summary).toBe("应用首页");
      expect(blocks[0].candidates[0].label).toBe("设置");
    } else {
      expect.unreachable("应为 advisor 块");
    }
  });

  it("advisor_turn 留痕事件不进入思考流（由顾问对话视图呈现）", () => {
    const blocks = aggregateThoughts([
      makeEvent("discovery_progress", { stage: "advisor_turn", page_id: "page-1", turn: { turn: 1 }, candidates: [] }),
    ]);
    expect(blocks).toHaveLength(0);
  });

  it("断言事件映射为断言块且相位区分通过/失败", () => {
    const blocks = aggregateThoughts([
      makeEvent("assertion_passed", { kind: "foreground", target: "设置", message: "前台应用正确" }),
      makeEvent("assertion_failed", { kind: "text", target: "标题", message: "文本不匹配" }),
    ]);
    expect(blocks.map((block) => block.phase)).toEqual(["gate", "fail"]);
  });

  it("page_discovered 聚合为页面块", () => {
    const blocks = aggregateThoughts([
      makeEvent("page_discovered", { page_path: "/page/0", element_count: 8, discovered_order: 1, image_path: "/p.png" }, "发现新页面状态"),
    ]);
    if (blocks[0].kind === "page") {
      expect(blocks[0].pagePath).toBe("/page/0");
      expect(blocks[0].elementCount).toBe(8);
    } else {
      expect.unreachable("应为 page 块");
    }
  });

  it("run_failed 与未知事件分别映射为失败与兜底通知块", () => {
    const blocks = aggregateThoughts([
      makeEvent("run_failed", { error: "设备断开" }),
      makeEvent("totally_new_event", {}),
    ]);
    expect(blocks[0].phase).toBe("fail");
    expect(blocks[1].kind).toBe("notice");
  });
});

describe("运行中异常块（Phase 2/3）", () => {
  it("把 anomaly_detected 聚合为醒目的异常块", () => {
    const blocks = aggregateThoughts([
      makeEvent(
        "anomaly_detected",
        { kind: "cppcrash", severity: "critical", page_path: "pages/Feed", action_id: "step-2" },
        "hilog 中出现 C++ 崩溃",
      ),
    ]);

    expect(blocks).toHaveLength(1);
    const block = blocks[0];
    expect(block.kind).toBe("anomaly");
    // critical 走 fail 相位（红色）
    expect(block.phase).toBe("fail");
    if (block.kind === "anomaly") {
      expect(block.severity).toBe("critical");
      expect(block.kindLabel).toBe("C++ 崩溃");
      expect(block.detail).toContain("pages/Feed");
    }
  });

  it("warning 级异常走 gate 相位而不是 fail", () => {
    const blocks = aggregateThoughts([
      makeEvent("anomaly_detected", { kind: "page_unresponsive", severity: "warning" }, "疑似无响应控件"),
    ]);

    expect(blocks[0].kind).toBe("anomaly");
    expect(blocks[0].phase).toBe("gate");
  });

  it("defect_recorded 同样进入思考流（不只是普通通知）", () => {
    const blocks = aggregateThoughts([
      makeEvent("defect_recorded", { kind: "appfreeze", severity: "critical" }, "疑似应用缺陷：应用冻屏"),
    ]);

    expect(blocks[0].kind).toBe("anomaly");
    if (blocks[0].kind === "anomaly") expect(blocks[0].kindLabel).toBe("应用冻屏");
  });

  it("未知类别回退到事件类型而不是空标签", () => {
    const blocks = aggregateThoughts([makeEvent("anomaly_detected", { kind: "brand_new_kind" }, "新异常")]);

    if (blocks[0].kind === "anomaly") expect(blocks[0].kindLabel).toBe("brand_new_kind");
  });
});
