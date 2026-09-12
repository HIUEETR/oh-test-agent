// 原始事件日志：每条事件可展开查看完整 payload JSON（payload 查看器）。

import { useEffect, useRef } from "react";
import clsx from "clsx";
import { Activity, Radio } from "lucide-react";
import { EmptyState, JsonViewer } from "../../components/ui/primitives";
import { useConsole } from "../../stores/console";
import { timeLabel } from "../../utils/format";
import type { RunEvent } from "../../api/types";

/** 事件类型的圆点配色。 */
function dotClass(type: string): string {
  if (["run_finished", "script_generated", "assertion_passed", "action_finished", "profile_promoted"].includes(type)) return "dot-ok";
  if (["run_failed", "assertion_failed"].includes(type)) return "dot-error";
  if (["target_candidates_found", "profile_live_mode", "discovery_path_blocked"].includes(type)) return "dot-warn";
  if (["plan_created", "action_started", "discovery_progress"].includes(type)) return "dot-brand";
  return "";
}

export function EventTimeline() {
  const events = useConsole((state) => state.events);
  const containerRef = useRef<HTMLDivElement>(null);
  const followRef = useRef(true);

  useEffect(() => {
    const container = containerRef.current;
    if (!container || !followRef.current) return;
    container.scrollTop = container.scrollHeight;
  }, [events.length]);

  if (events.length === 0) {
    return (
      <div className="event-log">
        <EmptyState title="暂无事件" hint="启动运行后显示规划、工具调用和断言事件" icon={<Activity size={38} />} />
      </div>
    );
  }

  return (
    <div
      className="event-log"
      ref={containerRef}
      onScroll={(event) => {
        const el = event.currentTarget;
        followRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
      }}
    >
      {events.map((event) => <EventRow key={event.event_id} event={event} />)}
    </div>
  );
}

function EventRow({ event }: { event: RunEvent }) {
  return (
    <details className="event-row">
      <summary>
        <i className={clsx("event-dot", dotClass(event.type))} aria-hidden="true" />
        <span className="event-text">
          <strong>{event.message}</strong>
          <small>{event.type}</small>
        </span>
        <time>{timeLabel(event.timestamp)}</time>
      </summary>
      <JsonViewer value={event.payload} />
    </details>
  );
}

/** live 页内「思考流 / 事件日志」的视图切换。 */
export function LiveViewToggle({ mode, onChange }: { mode: "thoughts" | "events"; onChange: (mode: "thoughts" | "events") => void }) {
  return (
    <div className="segmented" role="tablist" aria-label="实时视图模式">
      <button type="button" className={mode === "thoughts" ? "active" : ""} onClick={() => onChange("thoughts")}>
        思考流
      </button>
      <button type="button" className={mode === "events" ? "active" : ""} onClick={() => onChange("events")}>
        <Radio size={12} /> 事件日志
      </button>
    </div>
  );
}
