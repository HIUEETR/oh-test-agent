// 当前元素表：展示最新快照识别出的可交互元素（点击/输入/只读）。

import { MousePointerClick } from "lucide-react";
import { EmptyState } from "../../components/ui/primitives";
import { useConsole } from "../../stores/console";

export function ElementTable() {
  const latest = useConsole((state) => state.trace?.snapshots.at(-1) ?? null);
  const elements = latest?.elements ?? [];

  return (
    <section className="panel">
      <div className="card-heading">
        <span>当前元素</span>
        <small>{elements.length} detected</small>
      </div>
      <div className="element-table">
        {elements.slice(0, 24).map((element) => (
          <div className="element-row" key={element.element_id}>
            <span className="element-type">{element.type || "Node"}</span>
            <div style={{ minWidth: 0 }}>
              <strong>{element.content || element.key || element.id}</strong>
              <small>{element.key || element.id || element.source}</small>
            </div>
            <span className={element.clickable || element.editable ? "tag active" : "tag"}>
              {element.editable ? "input" : element.clickable ? "click" : "read"}
            </span>
          </div>
        ))}
        {elements.length === 0 && (
          <EmptyState title="暂无元素" hint="截图完成后展示识别出的控件" icon={<MousePointerClick size={34} />} />
        )}
      </div>
    </section>
  );
}
