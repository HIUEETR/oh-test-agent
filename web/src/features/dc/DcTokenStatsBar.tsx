// 输入框下方的 token 用量条：本会话累计 token 与缓存命中率。
//
// 数据全部来自 provider 真实响应（后端 DcTokenUsage），不做任何估算：
// - 累计值由服务端算好并通过 token_usage_updated 事件下发（整值覆盖，幂等）；
// - 缓存命中率分母用 input_tokens，因为该 provider 的 prompt_tokens 已包含缓存命中部分；
// - 无数据（Mock 模式 / 旧会话快照）时显示空态，不显示 0% 或 NaN。

import { useDcConsole, selectTokenStats } from "../../stores/dc-console";

/** 千分位；后端给的是整数 token 数。 */
function formatTokens(value: number): string {
  return value.toLocaleString("en-US");
}

/** 百分比：一位小数，命中率为 0 时也显示 0.0%（这是真实值，不是缺数据）。 */
function formatPercent(rate: number): string {
  return `${(rate * 100).toFixed(1)}%`;
}

export function DcTokenStatsBar() {
  const stats = useDcConsole(selectTokenStats);

  if (!stats.hasData) {
    return (
      <div className="dc-token-stats" role="status" aria-label="本会话模型用量">
        <small className="dc-token-empty">尚无用量数据</small>
      </div>
    );
  }

  const budget =
    stats.requestLimit > 0 ? `${stats.requests}/${stats.requestLimit}` : `${stats.requests}`;

  return (
    <div className="dc-token-stats" role="status" aria-label="本会话模型用量">
      <span className="dc-token-item" title="本会话累计输入 + 输出 token（不含缓存写）">
        本会话 <strong>{formatTokens(stats.totalTokens)}</strong> tokens
      </span>
      {stats.cacheHitRate !== null && (
        <span
          className="dc-token-item"
          title="缓存命中率 = 缓存读取 token ÷ 输入 token（该 provider 的输入已包含缓存命中部分）"
        >
          缓存命中 <strong>{formatPercent(stats.cacheHitRate)}</strong>
        </span>
      )}
      <span
        className="dc-token-item dc-token-detail"
        title="输入 = prompt_tokens（含缓存命中部分）；输出 = completion_tokens；缓存读 = prompt_tokens_details.cached_tokens"
      >
        输入 {formatTokens(stats.inputTokens)} · 输出 {formatTokens(stats.outputTokens)} · 缓存读{" "}
        {formatTokens(stats.cacheReadTokens)}
      </span>
      <span className="dc-token-item dc-token-detail" title="本会话累计模型请求次数（DC 每轮一个工具）">
        请求 {budget}
      </span>
      {stats.contextFillRate !== null && (
        <span className="dc-token-item dc-token-detail" title="最近一次请求的输入 token 占上下文窗口的比例">
          上下文 {formatPercent(stats.contextFillRate)}
        </span>
      )}
    </div>
  );
}
