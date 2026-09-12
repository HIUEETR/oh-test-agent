// 运行环境健康面板：设备、模型 Provider 与 Hypium 可用性。

import { Bot, MonitorSmartphone, RefreshCw, ServerCog, ShieldCheck } from "lucide-react";
import { useConsole } from "../../stores/console";

export function HealthPanel() {
  const health = useConsole((state) => state.health);
  const loading = useConsole((state) => state.healthLoading);
  const loadHealth = useConsole((state) => state.loadHealth);

  return (
    <section className="panel panel-pad">
      <h3 className="section-title">
        <ServerCog size={17} />运行环境
        <button type="button" className="icon-button" onClick={loadHealth} disabled={loading} aria-label="刷新环境状态">
          <RefreshCw size={14} />
        </button>
      </h3>
      <div className="health-row">
        <span className="health-icon"><MonitorSmartphone size={16} /></span>
        <div><small>HDC Device</small><strong>{health?.device.id ?? "unknown"}</strong></div>
        <i className={health?.device.connected ? "ok" : ""} />
      </div>
      <div className="health-row">
        <span className="health-icon"><Bot size={16} /></span>
        <div><small>Agent Provider</small><strong>{health?.model.provider ?? "unknown"}</strong></div>
        <i className={health ? "ok" : ""} />
      </div>
      <div className="health-row">
        <span className="health-icon"><ShieldCheck size={16} /></span>
        <div><small>Hypium</small><strong>{health?.hypium.version ?? "not found"}</strong></div>
        <i className={health?.hypium.importable ? "ok" : ""} />
      </div>
    </section>
  );
}
