// DC 模式聊天界面：消息流 + 输入框。
// 用户气泡/助手气泡/工具调用块三种消息类型。
// status=thinking/acting 时禁用输入并显示"Agent 正在自主执行..."。

import { useEffect, useRef, useState } from "react";
import { Send, Square } from "lucide-react";
import { useDcConsole } from "../../stores/dc-console";
import type { DcChatMessage } from "../../api/dc-types";
import { Markdown } from "../../components/ui/primitives";

export function DcChat() {
  const messages = useDcConsole((state) => state.messages);
  const status = useDcConsole((state) => state.status);
  const sending = useDcConsole((state) => state.sending);
  const sendMessage = useDcConsole((state) => state.sendMessage);
  const stopTurn = useDcConsole((state) => state.stopTurn);

  const [draft, setDraft] = useState("");
  const listRef = useRef<HTMLDivElement>(null);
  const busy = status === "thinking" || status === "acting";

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

  return (
    <section className="panel dc-chat">
      <div className="card-heading">
        <span>对话</span>
        <small>{messages.length} 条消息</small>
      </div>

      {/* 消息流 */}
      <div className="dc-message-list" ref={listRef}>
        {messages.length === 0 && (
          <div className="dc-chat-empty">
            <p>发送消息开始与 Agent 对话</p>
            <p className="dc-chat-hint">Agent 会自主调用 HDC 工具操作设备，直到任务完成</p>
          </div>
        )}
        {messages.map((message) => (
          <MessageBubble key={message.id} message={message} />
        ))}
        {busy && (
          <div className="dc-message dc-message-assistant dc-thinking">
            <div className="dc-thinking-dots">
              <span /><span /><span />
            </div>
            <small>Agent 正在自主执行...</small>
          </div>
        )}
      </div>

      {/* 输入区 */}
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
        {busy ? (
          <button
            type="button"
            className="secondary compact danger"
            onClick={() => void stopTurn()}
            aria-label="停止执行"
          >
            <Square size={15} />停止
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
    </section>
  );
}

/** 单条消息气泡 */
function MessageBubble({ message }: { message: DcChatMessage }) {
  const [expanded, setExpanded] = useState(false);

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

  if (message.role === "tool") {
    return (
      <div className="dc-message dc-message-tool">
        <button
          type="button"
          className="dc-tool-call"
          onClick={() => setExpanded(!expanded)}
          aria-expanded={expanded}
        >
          <span className="dc-tool-icon">⚙</span>
          <span className="dc-tool-text">{message.content}</span>
          <span className="dc-tool-toggle">{expanded ? "▾" : "▸"}</span>
        </button>
        {expanded && message.invocations && (
          <div className="dc-tool-detail">
            {message.invocations.map((inv) => (
              <pre key={inv.invocation_id}>
                {JSON.stringify(inv.args, null, 2)}
                {inv.error ? `\nERROR: ${inv.error}` : ""}
              </pre>
            ))}
          </div>
        )}
      </div>
    );
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

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch {
    return "";
  }
}
