// 顾问对话视图：完整展示探索顾问每轮 LLM 调用的输入（截图+候选列表）与输出（结构化建议）。
// 数据源：/api/runs/{id}/discovery 的 advisor_log（后端增量字段），轮询期间持续增长。

import { BrainCircuit, MessageSquareQuote, ScanEye } from "lucide-react";
import { EmptyState, JsonViewer, RetryImage } from "../../components/ui/primitives";
import { useConsole } from "../../stores/console";
import { artifactUrl } from "../../utils/artifact";
import type { AdvisorTurnRecord, AdvisorVerdictEntry } from "../../api/types";

export function AdvisorPanel() {
  const runId = useConsole((state) => state.runId);
  const discovery = useConsole((state) => state.discovery);
  const health = useConsole((state) => state.health);
  const turns = discovery?.advisor_log ?? [];

  const verdictCount = turns.filter((turn) => turn.output).length;
  const mockModel = Boolean(health && !health.model.configured);

  return (
    <section className="panel">
      <div className="advisor-header">
        <h3 className="section-title" style={{ margin: 0 }}><BrainCircuit size={17} />探索顾问对话</h3>
        <div className="stats">
          <span className="badge badge-brand">{turns.length} 轮调用</span>
          <span className="badge badge-ok">{verdictCount} 轮结构化建议</span>
          {discovery?.advisor_turns !== undefined && <span className="badge">有效对话轮 {discovery.advisor_turns}</span>}
        </div>
      </div>
      {mockModel && (
        <p className="note">当前为 Mock 模式：探索不启用 LLM 顾问。配置视觉模型后，这里会展示完整的「输入 ↔ 输出」对话留痕。</p>
      )}

      <div className="advisor-list">
        {turns.map((turn) => <AdvisorTurn key={turn.turn} runId={runId} turn={turn} />)}
        {/* 旧运行没有对话留痕，但逐页结论（advisor_verdicts）始终可用 */}
        {turns.length === 0 && (discovery?.advisor_verdicts?.length ?? 0) > 0 && (
          <VerdictCards verdicts={discovery!.advisor_verdicts!} />
        )}
        {turns.length === 0 && (discovery?.advisor_verdicts?.length ?? 0) === 0 && (
          <EmptyState
            title={runId ? "顾问尚未介入" : "尚无顾问对话"}
            hint={runId
              ? "探索模式且启用顾问时，每次 LLM 调用的输入与输出都会留痕在这里"
              : "启动探索运行后，模型逐页给出的建议将按对话轮展示"}
            icon={<BrainCircuit size={38} />}
          />
        )}
      </div>
    </section>
  );
}

/** 逐页结论卡片：按页面展示顾问的摘要、推荐/回避与理由（含候选可读标签）。 */
function VerdictCards({ verdicts }: { verdicts: AdvisorVerdictEntry[] }) {
  if (!verdicts?.length) return null;
  return (
    <>
      {verdicts.map((verdict) => {
        const labelOf = (index: number) =>
          verdict.candidates?.find((item) => item.index === index)?.label ?? `#${index}`;
        const kindOf = (index: number) =>
          verdict.candidates?.find((item) => item.index === index)?.kind ?? "";
        return (
          <article className="advisor-turn" key={verdict.identity}>
            <div className="advisor-card input-card">
              <div className="advisor-card-title"><ScanEye size={13} />页面</div>
              <div className="advisor-card-body"><code>{verdict.identity}</code></div>
            </div>
            <div className="advisor-card output-card">
              <div className="advisor-card-title">
                <MessageSquareQuote size={13} />顾问结论
                <small>{verdict.source}</small>
              </div>
              <div className="advisor-card-body">
                <p><strong>页面理解：</strong>{verdict.page_summary || "（无摘要）"}</p>
                {(verdict.recommended.length > 0 || verdict.avoid.length > 0) && (
                  <div className="verdict-tags">
                    {verdict.recommended.map((index) => (
                      <span className="verdict-tag reco" key={`r-${index}`}>推荐 · {kindOf(index)}「{labelOf(index)}」</span>
                    ))}
                    {verdict.avoid.map((index) => (
                      <span className="verdict-tag avoid" key={`a-${index}`}>回避 · {kindOf(index)}「{labelOf(index)}」</span>
                    ))}
                  </div>
                )}
                {verdict.reason && <p>{verdict.reason}</p>}
              </div>
            </div>
          </article>
        );
      })}
    </>
  );
}

function AdvisorTurn({ runId, turn }: { runId: string; turn: AdvisorTurnRecord }) {
  const snapshotUrl = runId && turn.snapshot_path ? artifactUrl(runId, turn.snapshot_path) : "";
  return (
    <article className="advisor-turn">
      {/* 输入侧：模型看到的页面截图 + 构造的候选列表文本 */}
      <div className="advisor-card input-card">
        <div className="advisor-card-title">
          <ScanEye size={13} />输入
          <small>第 {turn.turn} 轮 · {turn.page_path}</small>
        </div>
        <div className="advisor-card-body">
          {snapshotUrl && <img src={snapshotUrl} alt={`第 ${turn.turn} 轮输入截图`} loading="lazy" />}
          <pre>{turn.input || "（本轮未能构造输入）"}</pre>
        </div>
      </div>
      {/* 输出侧：结构化建议 + 原始 JSON 切换 */}
      <div className="advisor-card output-card">
        <div className="advisor-card-title">
          <MessageSquareQuote size={13} />输出
          <small>{turn.source === "model" ? `LLM · ${turn.elapsed_ms ?? "?"} ms` : turn.source}</small>
        </div>
        <div className="advisor-card-body">
          {turn.output ? (
            <>
              <p><strong>页面理解：</strong>{turn.output.page_summary || "（无摘要）"}</p>
              {turn.output.recommended.length > 0 && <p><strong>推荐动作：</strong>{turn.output.recommended.map((index) => `#${index}`).join("、")}</p>}
              {turn.output.avoid.length > 0 && <p><strong>回避动作：</strong>{turn.output.avoid.map((index) => `#${index}`).join("、")}</p>}
              <p><strong>理由：</strong>{turn.output.reason || "（无）"}</p>
              <details className="advisor-raw-toggle">
                <summary>查看原始 JSON</summary>
                <JsonViewer value={turn.output} />
              </details>
            </>
          ) : (
            <p>{turn.error ? `调用失败：${turn.error}（已回退启发式排序）` : "本轮未返回结构化建议，已回退启发式排序。"}</p>
          )}
        </div>
      </div>
    </article>
  );
}
