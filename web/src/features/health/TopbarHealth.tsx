// 顶栏环境芯片组：设备 / Provider / Hypium 的紧凑状态摘要（原左栏「运行环境」面板收敛而来）。

import type { ReactNode } from "react";
import { Bot, MonitorSmartphone, RefreshCw, ShieldCheck } from "lucide-react";
import clsx from "clsx";
import { useConsole } from "../../stores/console";

export function TopbarHealth() {
  const health = useConsole((state) => state.health);
  const loading = useConsole((state) => state.healthLoading);
  const loadHealth = useConsole((state) => state.loadHealth);

  return (
    <div className="topbar-env" aria-label="运行环境">
      <EnvChip
        icon={<MonitorSmartphone size={13} />}
        text={health?.device.id ?? "unknown"}
        ok={Boolean(health?.device.connected)}
        title="HDC Device"
      />
      <EnvChip
        icon={<Bot size={13} />}
        text={health?.model.provider ?? "unknown"}
        ok={Boolean(health)}
        title="Agent Provider"
      />
      <EnvChip
        icon={<ShieldCheck size={13} />}
        text={health?.hypium.version ?? "not found"}
        ok={Boolean(health?.hypium.importable)}
        title="Hypium"
      />
      <button
        type="button"
        className="icon-button"
        onClick={() => void loadHealth()}
        disabled={loading}
        aria-label="刷新环境状态"
      >
        <RefreshCw size={13} />
      </button>
    </div>
  );
}

/** 单个环境芯片：图标 + 文本 + 状态圆点，title 提供完整语义。 */
function EnvChip({ icon, text, ok, title }: { icon: ReactNode; text: string; ok: boolean; title: string }) {
  return (
    <span className="env-chip" title={`${title}: ${text}`}>
      {icon}
      <span className="env-chip-text">{text}</span>
      <i className={clsx("env-dot", ok && "ok")} />
    </span>
  );
}
