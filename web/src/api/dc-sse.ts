// DC 模式 SSE 事件流封装。
// 参考 api/sse.ts 结构但独立实现，订阅 /api/dc/sessions/{id}/events。
// 不修改 api/sse.ts（隔离要求）。

import { apiUrl } from "./client";
import { DC_EVENT_TYPES, type DcEvent } from "./dc-types";

/** 连接状态：connecting=首次连接中，open=已连上，reconnecting=断线等待重连，closed=已停止。 */
export type DcStreamState = "connecting" | "open" | "reconnecting" | "closed";

export class DcEventStream {
  private source: EventSource | null = null;
  private errorCount = 0;
  private stopped = false;
  private opened = false;
  private static readonly MAX_CONSECUTIVE_ERRORS = 6;

  constructor(
    private readonly sessionId: string,
    private readonly onEvent: (event: DcEvent) => void,
    private readonly onEnd: () => void = () => undefined,
    /** 断线重连成功后回调：调用方用它做一次 refreshSession 对账（补齐 buffer 溢出丢失的事件）。 */
    private readonly onReconnect: () => void = () => undefined,
    /** 连接状态变化回调：用于显示连接/重连状态，避免把同步中显示成「尚无操作记录」。 */
    private readonly onStateChange: (state: DcStreamState) => void = () => undefined,
  ) {}

  /** 建立连接并开始接收事件；重复调用安全。 */
  start(): void {
    if (this.source) return;
    this.stopped = false;
    this.onStateChange("connecting");
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

    // 浏览器 EventSource 会自动重连并携带 Last-Event-ID；首次之外的 open 视为重连
    source.onopen = () => {
      this.errorCount = 0;
      this.onStateChange("open");
      if (this.opened) this.onReconnect();
      this.opened = true;
    };

    source.onerror = () => {
      this.errorCount += 1;
      // 断线：保留已有行，只把连接状态标为「重连中」，不关闭流
      this.onStateChange(this.stopped ? "closed" : "reconnecting");
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
    this.onStateChange("closed");
    if (this.stopped) this.onEnd();
  }
}
