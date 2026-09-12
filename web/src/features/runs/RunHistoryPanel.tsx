// 左栏运行卡片与历史运行列表：点击历史项即可回看该次运行的轨迹、图、脚本与报告。

import { useEffect } from "react";
import { History } from "lucide-react";
import clsx from "clsx";
import { Badge } from "../../components/ui/primitives";
import { stateTone } from "../../utils/format";
import { useConsole } from "../../stores/console";

/** 当前运行卡：运行 ID、状态徽章与核心指标。 */
export function CurrentRunCard() {
  const runId = useConsole((state) => state.runId);
  const trace = useConsole((state) => state.trace);
  if (!runId) return null;
  const state = trace?.state ?? "created";
  return (
    <section className="panel panel-pad run-card">
      <h3 className="section-title"><History size={17} />当前运行</h3>
      <code>{runId}</code>
      <div className="run-meta">
        <Badge tone={stateTone(state)}>{state}</Badge>
        {trace?.model_mock && <Badge tone="warn">Mock 模型</Badge>}
      </div>
      <div className="metrics">
        <Metric2 label="步骤" value={trace?.actions.length ?? 0} />
        <Metric2 label="页面" value={trace?.graph.nodes.length ?? 0} />
        <Metric2 label="断言" value={trace?.assertions.length ?? 0} />
      </div>
    </section>
  );
}

function Metric2({ label, value }: { label: string; value: number }) {
  return (
    <div className="metric">
      <strong>{String(value).padStart(2, "0")}</strong>
      <small>{label}</small>
    </div>
  );
}

/** 历史运行列表（GET /api/runs）。 */
export function RunHistoryPanel() {
  const runs = useConsole((state) => state.runs);
  const runId = useConsole((state) => state.runId);
  const loadRuns = useConsole((state) => state.loadRuns);
  const selectRun = useConsole((state) => state.selectRun);

  useEffect(() => { void loadRuns(); }, [loadRuns]);

  return (
    <section className="panel">
      <div className="card-heading"><span>历史运行</span><small>{runs.length} 条</small></div>
      <div className="run-list">
        {runs.map((run) => (
          <button
            type="button"
            key={run.run_id}
            className={clsx("run-item", run.run_id === runId && "active")}
            onClick={() => selectRun(run.run_id)}
          >
            <span className="run-item-top">
              <strong>{run.task || "（冒烟测试）"}</strong>
              <Badge tone={stateTone(run.state)}>{run.state}</Badge>
            </span>
            <small>{run.run_id}{run.updated_at ? ` · ${new Date(run.updated_at).toLocaleString()}` : ""}</small>
          </button>
        ))}
        {runs.length === 0 && <div className="empty-state"><strong>暂无历史运行</strong><span>启动 Agent 后运行会出现在这里</span></div>}
      </div>
    </section>
  );
}
