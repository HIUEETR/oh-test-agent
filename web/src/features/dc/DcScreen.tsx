// DC 模式设备截图面板：从 dc-console store 读取最新截图 URL。
// 不复用 features/live/DeviceScreen.tsx（隔离要求）。

import { useDcConsole } from "../../stores/dc-console";
import { EmptyState, RetryImage } from "../../components/ui/primitives";

export function DcScreen() {
  const latestScreenshotUrl = useDcConsole((state) => state.latestScreenshotUrl);
  const session = useDcConsole((state) => state.session);

  return (
    <section className="panel dc-screen">
      <div className="card-heading">
        <span>设备画面</span>
        <small>{session ? `L${session.tier}` : "等待会话"}</small>
      </div>
      <div className="device-stage">
        {latestScreenshotUrl ? (
          <RetryImage src={latestScreenshotUrl} alt="当前 OpenHarmony 设备截图" />
        ) : (
          <EmptyState title="等待 Agent 采集设备画面" hint="创建会话并发送消息后，截图会实时显示" />
        )}
      </div>
    </section>
  );
}
