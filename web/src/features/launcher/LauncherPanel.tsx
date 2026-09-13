// 启动器面板：目标解析、任务输入、运行模式、探索策略与启动/停止控制。

import { CircleStop, Play, Search, Settings2, TerminalSquare } from "lucide-react";
import { Badge } from "../../components/ui/primitives";
import { useConsole } from "../../stores/console";
import { DEFAULT_POLICY } from "../../stores/console";
import type { DiscoveryPolicy } from "../../api/types";

const MODES: Array<[string, string]> = [
  ["regression", "回归测试"],
  ["exploration", "探索诊断"],
  ["stability", "稳定性测试"],
  ["reproduction", "问题诊断"],
];

export function LauncherPanel() {
  const targetKind = useConsole((state) => state.targetKind);
  const targetValue = useConsole((state) => state.targetValue);
  const task = useConsole((state) => state.task);
  const mode = useConsole((state) => state.mode);
  const policy = useConsole((state) => state.policy);
  const runBusy = useConsole((state) => state.runBusy);
  const operation = useConsole((state) => state.operation);
  const runId = useConsole((state) => state.runId);
  const trace = useConsole((state) => state.trace);
  const discovery = useConsole((state) => state.discovery);
  const patchForm = useConsole((state) => state.patchForm);
  const resolveTarget = useConsole((state) => state.resolveTarget);
  const startRun = useConsole((state) => state.startRun);
  const stopRun = useConsole((state) => state.stopRun);

  const operationBusy = operation !== "idle";
  const profileStatus = discovery?.profile_status
    ?? trace?.profile_snapshot?.status
    ?? (trace?.live_mode ? "实时模式" : trace?.profile_status_at_start ?? "未创建");

  return (
    <section className="panel panel-pad">
      <h3 className="section-title"><TerminalSquare size={17} />目标与任务</h3>

      <div className="target-kind segmented" role="group" aria-label="目标输入类型">
        <button type="button" className={targetKind === "app_name" ? "active" : ""} onClick={() => patchForm({ targetKind: "app_name" })}>应用名称</button>
        <button type="button" className={targetKind === "bundle_name" ? "active" : ""} onClick={() => patchForm({ targetKind: "bundle_name" })}>bundleName</button>
      </div>
      <label htmlFor="target">{targetKind === "app_name" ? "已安装应用名称" : "精确 bundleName"}</label>
      <div className="input-action">
        <input
          id="target"
          value={targetValue}
          onChange={(event) => patchForm({ targetValue: event.target.value })}
          placeholder={targetKind === "app_name" ? "例如：备忘录" : "com.example.app"}
        />
        <button type="button" className="secondary" onClick={resolveTarget} disabled={runBusy || operationBusy || !targetValue.trim()}>
          <Search size={15} />解析
        </button>
      </div>

      <label htmlFor="task">自然语言测试目标（可选）</label>
      <textarea
        id="task"
        value={task}
        onChange={(event) => patchForm({ task: event.target.value })}
        rows={3}
        placeholder="留空时执行启动、探索、返回和重启恢复冒烟测试"
      />

      <div className="field-row">
        <div>
          <label htmlFor="mode">运行模式</label>
          <select id="mode" value={mode} onChange={(event) => patchForm({ mode: event.target.value })}>
            {MODES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </div>
        <div>
          <label>当前 Profile</label>
          <div className="static-field"><Badge tone="brand">{profileStatus}</Badge></div>
        </div>
      </div>

      <PolicyEditor policy={policy} onChange={(next) => patchForm({ policy: next })} />

      <div className="button-row">
        <button type="button" className="primary" onClick={startRun} disabled={runBusy || operationBusy || !targetValue.trim()}>
          <Play size={16} />启动 Agent
        </button>
        <button type="button" className="danger" onClick={stopRun} disabled={!runBusy || !runId}>
          <CircleStop size={16} />停止
        </button>
      </div>
    </section>
  );
}

/** 探索策略编辑器：页面上限、每页动作与时长边界。 */
function PolicyEditor({ policy, onChange }: { policy: DiscoveryPolicy; onChange: (value: DiscoveryPolicy) => void }) {
  const patch = (value: Partial<DiscoveryPolicy>) => onChange({ ...policy, ...value });
  return (
    <details className="policy-editor">
      <summary><Settings2 size={14} />探索策略与安全边界</summary>
      <label className="switch-row">
        <input type="checkbox" checked={policy.enabled} onChange={(event) => patch({ enabled: event.target.checked })} />
        <span>Profile 缺失时自动发现</span>
      </label>
      <div className="limit-grid">
        <label>页面上限
          <input type="number" min={1} value={policy.max_pages} onChange={(event) => patch({ max_pages: Number(event.target.value) || DEFAULT_POLICY.max_pages })} />
        </label>
        <label>每页动作
          <input type="number" min={1} value={policy.max_actions_per_page} onChange={(event) => patch({ max_actions_per_page: Number(event.target.value) || DEFAULT_POLICY.max_actions_per_page })} />
        </label>
        <label>秒数上限
          <input type="number" min={30} value={policy.max_duration_seconds} onChange={(event) => patch({ max_duration_seconds: Number(event.target.value) || DEFAULT_POLICY.max_duration_seconds })} />
        </label>
      </div>
    </details>
  );
}
