// 设备画面：跟随最新截图，带加载失败重试与分辨率信息。

import { useConsole } from "../../stores/console";
import { EmptyState, RetryImage } from "../../components/ui/primitives";
import { artifactUrl } from "../../utils/artifact";
import { imagePath } from "../../utils/artifact";

export function DeviceScreen() {
  const runId = useConsole((state) => state.runId);
  const latest = useConsole((state) => state.trace?.snapshots.at(-1) ?? null);

  return (
    <section className="panel">
      <div className="card-heading">
        <span>设备画面</span>
        <small>{latest ? `${latest.width} × ${latest.height}` : "等待截图"}</small>
      </div>
      <div className="device-stage">
        {latest && runId
          ? <RetryImage src={artifactUrl(runId, imagePath(latest))} alt="当前 OpenHarmony 设备截图" />
          : <EmptyState title="等待 Agent 采集设备画面" hint="启动运行后，最新截图会实时显示在这里" />}
      </div>
    </section>
  );
}
