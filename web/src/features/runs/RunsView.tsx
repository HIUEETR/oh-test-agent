// 历史运行主视图：完整表格，可回看任意一次运行。

import { useEffect } from "react";
import clsx from "clsx";
import { History } from "lucide-react";
import { Badge, EmptyState } from "../../components/ui/primitives";
import { useConsole } from "../../stores/console";
import { stateTone } from "../../utils/format";

export function RunsView() {
  const runs = useConsole((state) => state.runs);
  const runId = useConsole((state) => state.runId);
  const loadRuns = useConsole((state) => state.loadRuns);
  const selectRun = useConsole((state) => state.selectRun);

  useEffect(() => { void loadRuns(); }, [loadRuns]);

  return (
    <section className="panel">
      <div className="card-heading"><span>历史运行</span><small>{runs.length} 条记录</small></div>
      {runs.length === 0 ? (
        <EmptyState title="暂无历史运行" hint="启动 Agent 后运行会出现在这里" icon={<History size={38} />} />
      ) : (
        <div className="runs-table">
          <table>
            <thead>
              <tr><th>任务</th><th>状态</th><th>目标应用</th><th>Run ID</th><th>更新时间</th></tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr
                  key={run.run_id}
                  className={clsx(run.run_id === runId && "active")}
                  onClick={() => selectRun(run.run_id)}
                >
                  <td className="runs-task">{run.task || "（冒烟测试）"}</td>
                  <td><Badge tone={stateTone(run.state)}>{run.state}</Badge></td>
                  <td>{run.target_app_id ?? "—"}</td>
                  <td><code>{run.run_id}</code></td>
                  <td>{run.updated_at ? new Date(run.updated_at).toLocaleString() : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
