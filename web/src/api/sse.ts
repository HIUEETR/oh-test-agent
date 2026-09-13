// SSE 运行事件流封装：按事件名订阅、按 event_id 去重、限次自动重连。
// 浏览器原生 EventSource 在重连时会自动携带 Last-Event-ID，后端据此续传。

import { apiUrl } from "./client";
import { RUN_EVENT_TYPES, type RunEvent } from "./types";

export class RunEventStream {
  private source: EventSource | null = null;
  private errorCount = 0;
  private stopped = false;
  /** 连续错误达到该次数后放弃重连（例如运行记录已被清理）。 */
  private static readonly MAX_CONSECUTIVE_ERRORS = 6;

  constructor(
    private readonly runId: string,
    private readonly onEvent: (event: RunEvent) => void,
    private readonly onEnd: () => void = () => undefined,
  ) {}

  /** 建立连接并开始接收事件；重复调用安全。 */
  start(): void {
    if (this.source) return;
    this.stopped = false;
    const source = new EventSource(apiUrl(`/api/runs/${encodeURIComponent(this.runId)}/events`));
    this.source = source;

    const handler = (message: MessageEvent) => {
      this.errorCount = 0;
      try {
        const event = JSON.parse(String(message.data)) as RunEvent;
        this.onEvent(event);
      } catch {
        // 单帧解析失败不中断整个流，由上层通过轮询兜底。
      }
    };
    for (const name of RUN_EVENT_TYPES) {
      source.addEventListener(name, handler as EventListener);
    }
    // 后端在运行记录消失时会发 error 帧并主动断开。
    source.addEventListener("error", handler as EventListener);

    source.onerror = () => {
      // 事件流正常结束时后端会关闭连接，浏览器随即触发 onerror 并自动重连；
      // 终态后继续重连只会空转，这里限次后放弃并回调 onEnd。
      this.errorCount += 1;
      if (this.errorCount >= RunEventStream.MAX_CONSECUTIVE_ERRORS) this.close();
    };
  }

  /** 显式停止：不再重连。 */
  stop(): void {
    this.stopped = true;
    this.close();
  }

  private close(): void {
    this.source?.close();
    this.source = null;
    if (this.stopped) this.onEnd();
  }
}
