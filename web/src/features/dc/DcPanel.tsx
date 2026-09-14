// DC 模式主面板：两列 grid（中=聊天，右=截图+操作日志+脚本按钮）+ 顶部工具栏。
// 完全不引用 stores/console.ts（隔离要求）。

import { useEffect } from "react";
import { Bot, Plus, Trash2, Zap } from "lucide-react";
import { useDcConsole } from "../../stores/dc-console";
import { useDcRuntime } from "./use-dc-runtime";
import { DcChat } from "./DcChat";
import { DcScreen } from "./DcScreen";
import { DcOperationLog } from "./DcOperationLog";
import { DcScriptDialog } from "./DcScriptDialog";
import { DcTierPicker } from "./DcTierPicker";
import { Badge, StatusDot } from "../../components/ui/primitives";
import type { DcSessionSummary } from "../../api/dc-types";

export function DcPanel() {
  useDcRuntime();

  const sessions = useDcConsole((state) => state.sessions);
  const activeSessionId = useDcConsole((state) => state.activeSessionId);
  const session = useDcConsole((state) => state.session);
  const status = useDcConsole((state) => state.status);
  const error = useDcConsole((state) => state.error);
  const loadSessions = useDcConsole((state) => state.loadSessions);
  const createSession = useDcConsole((state) => state.createSession);
  const selectSession = useDcConsole((state) => state.selectSession);
  const closeSession = useDcConsole((state) => state.closeSession);

  useEffect(() => {
    void loadSessions();
  }, [loadSessions]);

  const busy = status === "thinking" || status === "acting";
  const activeSessions = sessions.filter((item) => item.active !== false);
  const historySessions = sessions.filter((item) => item.active === false);
  const isActiveSession = sessions.some((item) => item.session_id === activeSessionId && item.active !== false);

  return (
    <div className="dc-grid">
      {/* 顶部工具栏 */}
      <div className="dc-toolbar panel">
        <div className="dc-toolbar-left">
          <Zap size={17} aria-hidden="true" />
          <strong>直流模式</strong>
          <Badge tone="brand">DC</Badge>
          {session && (
            <StatusDot
              ok={session.status !== "closed"}
              label={busy ? "Agent 执行中" : session.status === "closed" ? "已关闭" : "就绪"}
            />
          )}
        </div>
        <div className="dc-toolbar-right">
          {/* 会话选择器：活跃会话 + 可恢复的历史会话 */}
          <select
            value={activeSessionId}
            onChange={(event) => selectSession(event.target.value)}
            aria-label="选择 DC 会话"
            disabled={sessions.length === 0}
          >
            <option value="">选择会话</option>
            {activeSessions.length > 0 && (
              <optgroup label="活跃会话">
                {activeSessions.map((s) => (
                  <option key={s.session_id} value={s.session_id}>
                    {sessionLabel(s)} · {s.turn_count} 轮
                  </option>
                ))}
              </optgroup>
            )}
            {historySessions.length > 0 && (
              <optgroup label="历史会话（可恢复）">
                {historySessions.map((s) => (
                  <option key={s.session_id} value={s.session_id}>
                    {sessionLabel(s)} · {s.turn_count} 轮
                  </option>
                ))}
              </optgroup>
            )}
          </select>
          <button
            type="button"
            className="secondary compact"
            onClick={() => void createSession()}
            disabled={busy}
          >
            <Plus size={14} />新建会话
          </button>
          {activeSessionId && isActiveSession && (
            <button
              type="button"
              className="secondary compact danger"
              onClick={() => void closeSession()}
              disabled={busy}
            >
              <Trash2 size={14} />关闭
            </button>
          )}
        </div>
      </div>

      {error && <div className="banner error-banner dc-error" role="alert">{error}</div>}

      {session?.restored && (
        <div className="banner info-banner dc-restored" role="status">
          <div className="banner-body">
            <strong>已恢复历史会话</strong>
            <span>
              {session.restored_context === "text"
                ? "模型上下文按历史轮次重建（截图与工具原始输出不重放），可继续对话。"
                : session.restored_context === "full"
                  ? "已还原完整对话上下文，可继续对话。"
                  : "该会话没有可还原的模型上下文，仅保留消息与工具记录。"}
            </span>
          </div>
        </div>
      )}

      {/* 主内容区：中=聊天，右=截图+日志 */}
      <div className="dc-main">
        <div className="dc-center">
          {activeSessionId ? (
            <DcChat />
          ) : (
            <div className="panel dc-empty">
              <Bot size={38} />
              <strong>创建或选择一个 DC 会话</strong>
              <span>直流模式支持连续多轮对话，Agent 自主调用 HDC 工具操作设备</span>
            </div>
          )}
        </div>
        <div className="dc-side">
          <DcScreen />
          <DcTierPicker />
          <DcOperationLog />
          <DcScriptDialog />
        </div>
      </div>
    </div>
  );
}

/** 会话下拉标签：短 id + 层级 + 最近活跃时间。 */
function sessionLabel(summary: DcSessionSummary): string {
  const stamp = summary.last_active_at ?? summary.created_at;
  let time = "";
  try {
    time = new Date(stamp).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  } catch {
    time = "";
  }
  const id = summary.session_id.replace(/^dc-/, "").slice(0, 22);
  return `${id} · L${summary.tier}${time ? ` · ${time}` : ""}`;
}
