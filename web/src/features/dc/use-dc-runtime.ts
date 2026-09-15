// DC 模式运行时编排：SSE 事件流。
// 与 Live Mode 的 use-run-runtime.ts 不同，DC 前端只依赖 SSE，不并行轮询。
// 不修改 use-run-runtime.ts（隔离要求）。

import { useEffect, useRef } from "react";
import { DcEventStream } from "../../api/dc-sse";
import { useDcConsole } from "../../stores/dc-console";

/** 绑定当前 activeSessionId 的 SSE 事件流；session 切换时自动重建。 */
export function useDcRuntime(): void {
  const activeSessionId = useDcConsole((state) => state.activeSessionId);
  const streamRef = useRef<DcEventStream | null>(null);

  useEffect(() => {
    if (!activeSessionId) return;
    const { appendEvent, refreshSession, setConnection } = useDcConsole.getState();
    const stream = new DcEventStream(
      activeSessionId,
      appendEvent,
      () => setConnection("closed"),
      // 重连成功后对账一次：补齐 SSE buffer 溢出而丢失的历史事件
      () => void refreshSession(true),
      // 连接状态：connecting/open/reconnecting/closed，供操作日志区分空状态
      (state) => setConnection(state),
    );
    streamRef.current = stream;
    stream.start();
    return () => {
      stream.stop();
      streamRef.current = null;
      setConnection("idle");
    };
  }, [activeSessionId]);
}
