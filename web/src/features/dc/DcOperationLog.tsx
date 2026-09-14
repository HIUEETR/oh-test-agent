// DC 模式操作日志：按时间序显示 DcToolInvocation 录制记录。

import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { useDcConsole } from "../../stores/dc-console";
import type { DcToolInvocation } from "../../api/dc-types";

export function DcOperationLog() {
  const invocations = useDcConsole((state) => state.session?.invocations ?? []);
  const [expanded, setExpanded] = useState(true);

  return (
    <section className="panel dc-operations">
      <button
        type="button"
        className="card-heading dc-operations-header"
        onClick={() => setExpanded(!expanded)}
        aria-expanded={expanded}
      >
        {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <span>操作日志</span>
        <small>{invocations.length} 步</small>
      </button>
      {expanded && (
        <div className="dc-operation-list">
          {invocations.length === 0 ? (
            <p className="dc-empty-hint">尚无操作记录</p>
          ) : (
            invocations.map((inv) => <OperationRow key={inv.invocation_id} inv={inv} />)
          )}
        </div>
      )}
    </section>
  );
}

function OperationRow({ inv }: { inv: DcToolInvocation }) {
  const argsSummary = Object.entries(inv.args)
    .filter(([, v]) => v != null)
    .map(([k, v]) => `${k}=${typeof v === "string" ? v.slice(0, 30) : JSON.stringify(v)}`)
    .join(", ");

  return (
    <div className={`dc-operation-row ${inv.success ? "ok" : "fail"}`}>
      <span className="dc-op-icon">{inv.success ? "✓" : "✗"}</span>
      <span className="dc-op-tool">{inv.tool}</span>
      <span className="dc-op-args" title={argsSummary}>{argsSummary.slice(0, 60)}</span>
      <span className="dc-op-duration">{inv.duration_ms}ms</span>
    </div>
  );
}
