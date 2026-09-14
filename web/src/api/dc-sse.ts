// DC 模式 SSE 事件流封装。
// 参考 api/sse.ts 结构但独立实现，订阅 /api/dc/sessions/{id}/events。
// 不修改 api/sse.ts（隔离要求）。

import { apiUrl } from "./client";
import { DC_EVENT_TYPES, type DcEvent } from "./dc-types";

export class DcEventStream {
  private source: EventSource | null = null;
  private errorCount = 0;
  private stopped = false;
  private static readonly MAX_CONSECUTIVE_ERRORS = 6;

  constructor(
    private readonly sessionId: string,
    private readonly onEvent: (event: DcEvent) => void,
    private readonly onEnd: () => void = () => undefined,
  ) {}

  /** 建立连接并开始接收事件；重复调用安全。 */
  start(): void {
    if (this.source) return;
    this.stopped = false;
    const source = new EventSource(apiUrl(`/api/dc/sessions/${encodeURIComponent(this.sessionId)}/events`));
    this.source = source;

    const handler = (message: MessageEvent) => {
      this.errorCount = 0;
      try {
        const event = JSON.parse(String(message.data)) as DcEvent;
        this.onEvent(event);
      } catch {
        // 单帧解析失败不中断整个流
      }
    };
    for (const name of DC_EVENT_TYPES) {
      source.addEventListener(name, handler as EventListener);
    }
    source.addEventListener("error", handler as EventListener);

    source.onerror = () => {
      this.errorCount += 1;
      if (this.errorCount >= DcEventStream.MAX_CONSECUTIVE_ERRORS) this.close();
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
