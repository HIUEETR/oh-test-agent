// 左栏当前运行卡：运行 ID、状态徽章与核心指标。

import { History } from "lucide-react";
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
