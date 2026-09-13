// 运行时编排：SSE 事件流 + 串行轮询。
// 事件流负责实时增量；轮询负责 trace/discovery 快照与终态判定，两者互补。

import { useEffect, useRef } from "react";
import { RunEventStream } from "../api/sse";
import { TERMINAL_STATES } from "../api/types";
import { useConsole } from "../stores/console";

/** 绑定当前 runId 的事件流与轮询循环；runId 变化时自动重建。 */
export function useRunRuntime(): void {
  const runId = useConsole((state) => state.runId);
  const operation = useConsole((state) => state.operation);
  const streamRef = useRef<RunEventStream | null>(null);

  // SSE：实时事件增量。
  useEffect(() => {
    if (!runId) return;
    const { appendEvent } = useConsole.getState();
    const stream = new RunEventStream(runId, appendEvent);
    streamRef.current = stream;
    stream.start();
    return () => {
      stream.stop();
      streamRef.current = null;
    };
  }, [runId]);

  // 运行进入终态后延迟收流：历史运行打开时 trace 先到、SSE 历史事件后到，
  // 立即停止会丢失整段思考流回放；给事件流 3 秒窗口补齐历史。
  const state = useConsole((state) => state.trace?.state ?? null);
  const replayPending = useConsole((state) => state.trace?.replay_status === "pending");
  useEffect(() => {
    if (state && TERMINAL_STATES.has(state) && !replayPending) {
      const timer = window.setTimeout(() => streamRef.current?.stop(), 3000);
      return () => window.clearTimeout(timer);
    }
  }, [state, replayPending]);

  // 轮询：串行刷新 trace + discovery，直到终态且回放不处于 pending。
  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    let timer = 0;
    const poll = async () => {
      const { refreshTrace, refreshDiscovery } = useConsole.getState();
      const next = await Promise.all([refreshTrace(true), refreshDiscovery(true)]);
      const trace = next[0];
      const pending = trace?.replay_status === "pending" || operation === "executing";
      if (cancelled) return;
      if (!trace || !TERMINAL_STATES.has(trace.state) || pending) {
        timer = window.setTimeout(poll, trace ? 900 : 550);
      }
    };
    void poll();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [runId, operation]);
}
