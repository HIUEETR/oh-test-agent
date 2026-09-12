// 闭环流水线：把 采集→感知→规划→探索→脚本→回放→报告 的推进状态映射为阶段条。
// 纯函数推导，事件存在即完成；进行中的阶段高亮，不适用时跳过。

import { TERMINAL_STATES, type RunEvent, type RunTrace } from "../api/types";

export type PipelinePhaseState = "pending" | "active" | "done" | "skipped";

export type PipelinePhase = {
  key: string;
  label: string;
  state: PipelinePhaseState;
};

function hasEvent(events: RunEvent[], type: string): boolean {
  return events.some((event) => event.type === type);
}

/** 推导流水线各阶段状态（events 须按 event_id 升序）。 */
export function derivePipeline(events: RunEvent[], trace: RunTrace | null, reportReady: boolean): PipelinePhase[] {
  if (!trace && !events.length) {
    // 尚未关联运行：整条流水线待命。
    return PIPELINE_LABELS.map(([key, label]) => ({ key, label, state: "pending" }));
  }
  const terminal = Boolean(trace && TERMINAL_STATES.has(trace.state));
  const captured = hasEvent(events, "screen_captured") || Boolean(trace?.snapshots.length);
  const perceived = hasEvent(events, "elements_detected");
  const planned = hasEvent(events, "plan_created");
  const exploring = hasEvent(events, "discovery_started") && !hasEvent(events, "discovery_finished");
  const explored = hasEvent(events, "discovery_finished");
  const scripted = hasEvent(events, "script_generated") || Boolean(trace?.generated);
  const replaying = hasEvent(events, "execution_started") && !hasEvent(events, "execution_finished");
  const replayed = hasEvent(events, "execution_finished") || Boolean(trace?.replays.length);
  const reportDone = reportReady || (terminal && scripted);

  const skipReplay = terminal && !replaying && !replayed
    && (trace?.replay_status === "not_eligible" || trace?.replay_status === "not_requested");

  const states: PipelinePhaseState[] = [
    captured ? "done" : "active",
    perceived ? "done" : captured ? "active" : "pending",
    planned ? "done" : terminal ? "skipped" : "pending",
    explored ? "done" : exploring ? "active" : terminal ? "skipped" : "pending",
    scripted ? "done" : replaying || replayed ? "done" : terminal ? "skipped" : "pending",
    replayed ? "done" : replaying ? "active" : skipReplay ? "skipped" : "pending",
    reportDone ? "done" : terminal ? (scripted ? "active" : "pending") : "pending",
  ];

  return PIPELINE_LABELS.map(([key, label], index) => ({ key, label, state: states[index] }));
}

const PIPELINE_LABELS: Array<[string, string]> = [
  ["capture", "采集"],
  ["perceive", "感知"],
  ["plan", "规划"],
  ["explore", "探索"],
  ["script", "脚本"],
  ["replay", "回放"],
  ["report", "报告"],
];
