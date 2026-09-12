// 闭环流水线状态条：呼应比赛要求的 采集→感知→规划→探索→脚本→回放→报告 闭环可视化。

import { clsx } from "clsx";
import { useConsole } from "../../stores/console";
import { derivePipeline } from "../../utils/pipeline";
import type { PipelinePhaseState } from "../../utils/pipeline";

const STATE_CLASS: Record<PipelinePhaseState, string> = {
  done: "done",
  active: "active",
  pending: "",
  skipped: "skipped",
};

export function PipelineStrip() {
  const events = useConsole((state) => state.events);
  const trace = useConsole((state) => state.trace);
  const reportReady = useConsole((state) => state.reportReady);
  const phases = derivePipeline(events, trace, reportReady);

  return (
    <section className="panel pipeline-strip" aria-label="测试闭环进度">
      {phases.map((phase, index) => (
        <div className="pipeline-node" key={phase.key}>
          <span className={clsx("pipeline-step", STATE_CLASS[phase.state])} title={phase.label}>
            <i aria-hidden="true" />
            <span>{phase.label}{phase.state === "skipped" ? "（跳过）" : ""}</span>
          </span>
          {index < phases.length - 1 && null}
        </div>
      ))}
    </section>
  );
}
