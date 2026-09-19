// DC 模式聊天界面：消息流 + 输入框。
// 消息按角色渲染：user（右对齐气泡）/ thinking（可折叠思考块）/ narration（模型叙述）/
// tool（工具卡，running → success/failed 就地更新）/ assistant（最终回复）。
// status=thinking/acting 时禁用输入并按阶段显示文案。

import { useEffect, useRef, useState } from "react";
import { Send, Square } from "lucide-react";
import { useDcConsole } from "../../stores/dc-console";
import type { DcChatMessage } from "../../api/dc-types";
import { Markdown } from "../../components/ui/primitives";
import { DcActivityBar } from "./DcActivityBar";
import { DcTokenStatsBar } from "./DcTokenStatsBar";
import { DcTierMenu } from "./DcTierMenu";
import { DcComposerActions } from "./DcComposerActions";

/** 一次渲染的消息上限：超出时默认只渲染最近 N 条，可手动展开更早内容。 */
const RENDER_WINDOW = 200;

export function DcChat() {
  const messages = useDcConsole((state) => state.messages);
  const status = useDcConsole((state) => state.status);
  const sending = useDcConsole((state) => state.sending);
  const cancelStatus = useDcConsole((state) => state.cancelStatus);
  const sendMessage = useDcConsole((state) => state.sendMessage);
  const stopTurn = useDcConsole((state) => state.stopTurn);

  const [draft, setDraft] = useState("");
  const [showAll, setShowAll] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);
  const busy = status === "thinking" || status === "acting" || cancelStatus === "confirming";
  const stopping = cancelStatus === "confirming";

  // 自动滚动到底部
  useEffect(() => {
    if (listRef.current) {
      listRef.current.scrollTop = listRef.current.scrollHeight;
    }
  }, [messages]);

  const handleSend = () => {
    const text = draft.trim();
    if (!text || sending || busy) return;
    setDraft("");
    void sendMessage(text);
  };

  const handleKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      handleSend();
    }
  };

  const hiddenCount = showAll ? 0 : Math.max(0, messages.length - RENDER_WINDOW);
  const visibleMessages = hiddenCount > 0 ? messages.slice(hiddenCount) : messages;

  return (
    <section className="panel dc-chat">
      <div className="card-heading">
        <span>对话</span>
        <small>{messages.length} 条消息</small>
      </div>

      {/* 当前活动区：独立于模型文本，始终显示阶段/调用/耗时/超时状态 */}
      <DcActivityBar />

      {/* 消息流 */}
      <div className="dc-message-list" ref={listRef}>
        {messages.length === 0 && (
          <div className="dc-chat-empty">
            <p>发送消息开始与 Agent 对话</p>
            <p className="dc-chat-hint">Agent 会自主调用 HDC 工具操作设备，直到任务完成</p>
          </div>
        )}
        {hiddenCount > 0 && (
          <button type="button" className="dc-message-more" onClick={() => setShowAll(true)}>
            加载更早的 {hiddenCount} 条消息
          </button>
        )}
        {visibleMessages.map((message) => (
          <MessageBubble key={message.id} message={message} busy={busy} />
        ))}
        {busy && (
          <div className="dc-message dc-message-assistant dc-thinking">
            <div className="dc-thinking-dots">
              <span /><span /><span />
            </div>
            <small>
              {stopping
                ? "正在确认设备状态..."
                : status === "acting" ? "Agent 正在执行工具..." : "Agent 正在思考..."}
            </small>
          </div>
        )}
      </div>

      {/* 输入区（DeepSeek 式 composer）：textarea 独占整行，控制条在下方
          （左：工具层级下拉 + 脚本/蒸馏 pill，右：发送或停止）。 */}
      <div className="dc-input-row">
        <textarea
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={busy ? "Agent 正在执行中..." : "输入消息，Enter 发送，Shift+Enter 换行"}
          rows={2}
          disabled={busy || sending}
          aria-label="对话输入"
        />
        <div className="dc-composer-bar">
          <div className="dc-composer-left">
            <DcTierMenu />
            <DcComposerActions />
          </div>
          {busy ? (
            <button
              type="button"
              className="secondary compact danger dc-stop-button"
              onClick={() => void stopTurn()}
              disabled={stopping}
              aria-label={stopping ? "正在确认设备状态" : "停止执行"}
            >
              {stopping ? (
                <><span className="dc-tool-spinner" aria-hidden="true" />正在确认设备状态</>
              ) : (
                <><Square size={15} />停止</>
              )}
            </button>
          ) : (
            <button
              type="button"
              className="primary compact"
              onClick={handleSend}
              disabled={!draft.trim() || sending}
              aria-label="发送消息"
            >
              <Send size={15} />发送
            </button>
          )}
        </div>
      </div>

      {/* 输入框下方：本会话 token 用量与缓存命中率（数据来自 provider 真实响应） */}
      <DcTokenStatsBar />
    </section>
  );
}

/** 单条消息：按角色分派渲染 */
function MessageBubble({ message, busy }: { message: DcChatMessage; busy: boolean }) {
  if (message.role === "user") {
    return (
      <div className="dc-message dc-message-user">
        <div className="dc-message-content">
          <Markdown text={message.content} />
        </div>
        <small className="dc-message-time">{formatTime(message.timestamp)}</small>
      </div>
    );
  }

  if (message.role === "thinking") {
    return <ThinkingBlock message={message} busy={busy} />;
  }

  if (message.role === "narration") {
    return (
      <div className="dc-message dc-message-narration">
        <div className="dc-message-content">
          <Markdown text={message.content} />
        </div>
        <small className="dc-message-time">{formatTime(message.timestamp)}</small>
      </div>
    );
  }

  if (message.role === "tool") {
    return <ToolCard message={message} />;
  }

  // assistant
  return (
    <div className="dc-message dc-message-assistant">
      <div className="dc-message-content">
        <Markdown text={message.content} />
      </div>
      <small className="dc-message-time">{formatTime(message.timestamp)}</small>
    </div>
  );
}

/** 思考块：默认展开，轮次结束后自动折叠 */
function ThinkingBlock({ message, busy }: { message: DcChatMessage; busy: boolean }) {
  const [expanded, setExpanded] = useState(true);

  useEffect(() => {
    if (!busy) setExpanded(false);
  }, [busy]);

  return (
    <div className="dc-message dc-message-thinking">
      <button
        type="button"
        className="dc-thinking-head"
        onClick={() => setExpanded(!expanded)}
        aria-expanded={expanded}
      >
        <span>💭 思考</span>
        <span className="dc-tool-toggle">{expanded ? "▾" : "▸"}</span>
      </button>
      {expanded && <div className="dc-thinking-body">{message.content}</div>}
      <small className="dc-message-time">{formatTime(message.timestamp)}</small>
    </div>
  );
}

/** 工具卡：running 显示 spinner，终态按 succeeded/失败/超时/取消/未知分档显示 */
function ToolCard({ message }: { message: DcChatMessage }) {
  const [expanded, setExpanded] = useState(false);
  const running = message.toolStatus === "running";
  const failed = message.toolStatus === "failed";
  const tone = message.toolState ?? (running ? "running" : failed ? "failed" : "succeeded");
  const toneClass = tone === "running" ? "" : ` state-${tone}`;
  const args = message.toolArgs ? JSON.stringify(message.toolArgs, null, 2) : "";

  return (
    <div className={`dc-message dc-message-tool${failed ? " failed" : ""}${toneClass}`}>
      <button
        type="button"
        className="dc-tool-call"
        onClick={() => setExpanded(!expanded)}
        aria-expanded={expanded}
      >
        <span className="dc-tool-icon">
          {running ? <span className="dc-tool-spinner" aria-hidden="true" /> : toolIcon(tone)}
        </span>
        <span className="dc-tool-text">{message.content}</span>
        <span className="dc-tool-toggle">{expanded ? "▾" : "▸"}</span>
      </button>
      {expanded && (
        <div className="dc-tool-detail">
          {args && <pre>{args}</pre>}
          {message.toolResult && <pre>{message.toolResult}</pre>}
          {message.toolEffectStatus === "unknown" && (
            <pre className="dc-tool-effect-warn">副作用未确认：继续前需重新观测设备状态</pre>
          )}
          {message.toolErrorCode && <pre className="dc-tool-effect-warn">错误码：{message.toolErrorCode}</pre>}
        </div>
      )}
    </div>
  );
}

function toolIcon(tone: string): string {
  switch (tone) {
    case "succeeded": return "✓";
    case "failed": return "✗";
    case "timed_out": return "⏱";
    case "cancelled": return "⊘";
    default: return "?";
  }
}

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch {
    return "";
  }
}
