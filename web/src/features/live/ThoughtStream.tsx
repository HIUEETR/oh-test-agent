// 思考流：实时呈现大模型的「输入理解 → 决策 → 行动 → 断言」过程。
// 数据来自 thought-aggregator 聚合的事件块；本组件只负责渲染与自动跟随滚动。

import { useEffect, useMemo, useRef } from "react";
import {
  AlertTriangle, BrainCircuit, CheckCircle2, CircleAlert, Crosshair, Eye, ListChecks, ScanEye,
  MapPinned, Sparkles, XCircle,
} from "lucide-react";
import clsx from "clsx";
import { Badge, EmptyState, Markdown, RetryImage } from "../../components/ui/primitives";
import { useConsole } from "../../stores/console";
import { artifactUrl } from "../../utils/artifact";
import { formatDuration, timeLabel } from "../../utils/format";
import { aggregateThoughts, type ThoughtBlock } from "../../utils/thought-aggregator";

type StepBlock = Extract<ThoughtBlock, { kind: "step" }>;

/** 把工具决策的参数转为可读的键值对（排除空值）。 */
function decisionParams(decision: StepBlock["decision"]): Array<[string, string]> {
  if (!decision) return [];
  const entries: Array<[string, string]> = [];
  if (decision.target) entries.push(["目标", decision.target]);
  if (decision.text) entries.push(["输入", decision.text]);
  if (decision.coordinate) entries.push(["坐标", decision.coordinate.join(", ")]);
  if (decision.direction) entries.push(["方向", decision.direction]);
  if (decision.wait_seconds) entries.push(["等待", `${decision.wait_seconds}s`]);
  return entries;
}

export function ThoughtStream() {
  const runId = useConsole((state) => state.runId);
  const events = useConsole((state) => state.events);
  const blocks = useMemo(() => aggregateThoughts(events), [events]);
  const containerRef = useRef<HTMLDivElement>(null);
  const followRef = useRef(true);

  // 自动跟随：仅当用户停留在底部附近时才滚动到最新块，避免打断回看。
  useEffect(() => {
    const container = containerRef.current;
    if (!container || !followRef.current) return;
    container.scrollTop = container.scrollHeight;
  }, [blocks.length]);

  if (!runId || blocks.length === 0) {
    return (
      <div className="thought-stream">
        <EmptyState
          title="思考流待命"
          hint="启动运行后，这里会实时展示模型对页面的理解、规划与每一步工具决策"
          icon={<BrainCircuit size={38} />}
        />
      </div>
    );
  }

  return (
    <div
      className="thought-stream"
      ref={containerRef}
      onScroll={(event) => {
        const el = event.currentTarget;
        followRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
      }}
    >
      {blocks.map((block) => <ThoughtBlockView key={block.id} block={block} runId={runId} />)}
    </div>
  );
}

function ThoughtBlockView({ block, runId }: { block: ThoughtBlock; runId: string }) {
  return (
    <article className={clsx("thought-block", `phase-${block.phase}`, "thought-enter")}>
      <header><BlockTitle block={block} /></header>
      <BlockBody block={block} runId={runId} />
    </article>
  );
}

function BlockTitle({ block }: { block: ThoughtBlock }) {
  const icon = {
    plan: <ListChecks size={13} />,
    perception: <ScanEye size={13} />,
    step: <Crosshair size={13} />,
    assertion: block.kind === "assertion" && block.passed ? <CheckCircle2 size={13} /> : <XCircle size={13} />,
    advisor: <BrainCircuit size={13} />,
    page: <MapPinned size={13} />,
    // Phase 2/3：运行中发现的异常用警示三角，critical 时整块走 fail 相位（红色）。
    anomaly: <AlertTriangle size={13} />,
    notice: block.kind === "notice" && block.phase === "fail" ? <CircleAlert size={13} /> : <Sparkles size={13} />,
  }[block.kind];
  return (
    <h4>
      {icon}
      {block.title}
      <time>{timeLabel(block.at)}</time>
    </h4>
  );
}

function BlockBody({ block, runId }: { block: ThoughtBlock; runId: string }) {
  switch (block.kind) {
    case "plan":
      return (
        <div className="thought-body">
          {block.mock && <p><strong>Mock 模型</strong>生成的计划仅供链路验证。</p>}
          <ol className="plan-steps">
            {block.steps.map((step) => (
              <li key={step.step_id}>
                <span>{step.instruction}</span>
                <span className="plan-tool">{step.tool}{step.target ? ` · ${step.target}` : ""}</span>
              </li>
            ))}
          </ol>
        </div>
      );
    case "perception":
      return (
        <div className="thought-body">
          {block.summary && <Markdown text={block.summary} />}
          {block.imagePath && (
            <a className="snapshot-chip" href={artifactUrl(runId, block.imagePath)} target="_blank" rel="noreferrer">
              <RetryImage src={artifactUrl(runId, block.imagePath)} alt="输入给视觉模型的页面截图" />
              <small>模型输入 · 页面截图</small>
            </a>
          )}
          {block.elementCount !== null && <p>识别到 <strong>{block.elementCount}</strong> 个可交互元素</p>}
        </div>
      );
    case "step": {
      const params = decisionParams(block.decision);
      return (
        <div className="thought-body">
          <div className="step-card">
            <div className="step-line">
              {block.decision && <span className="step-tool">{block.decision.tool}</span>}
              <span className="step-target">{block.decision?.reasoning || block.instruction}</span>
            </div>
            {params.length > 0 && (
              <ul className="step-params">
                {params.map(([key, value]) => <li key={key}><b>{key}</b>：{value}</li>)}
              </ul>
            )}
            {block.result && (
              <p className={block.result.success ? "" : "step-error"}>
                {block.result.success
                  ? `执行成功${block.result.duration_ms ? ` · ${formatDuration(block.result.duration_ms)}` : ""}`
                  : `执行失败：${block.result.error ?? "未知错误"}`}
              </p>
            )}
          </div>
        </div>
      );
    }
    case "assertion":
      return <div className="thought-body"><p>{block.detail || block.title}</p></div>;
    case "advisor": {
      const verdict = block.verdict;
      const labelOf = (index: number) => block.candidates.find((item) => item.index === index)?.label ?? `#${index}`;
      const kindOf = (index: number) => block.candidates.find((item) => item.index === index)?.kind ?? "";
      return (
        <div className="thought-body">
          {verdict ? (
            <>
              <p><strong>页面理解：</strong>{verdict.page_summary || "（模型未给出摘要）"}</p>
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
            </>
          ) : (
            <p>{block.reuse ? "命中已见过的页面结构，沿用上一轮建议。" : "本轮顾问未返回结构化建议，已回退启发式排序。"}</p>
          )}
          {block.source && !block.reuse && <p><Eye size={12} /> 来源：{block.source === "model" ? "LLM 顾问" : block.source}</p>}
        </div>
      );
    }
    case "page":
      return (
        <div className="thought-body">
          <p><strong>{block.pagePath || block.title}</strong> · {block.elementCount} 元素</p>
          {block.imagePath && (
            <a className="snapshot-chip" href={artifactUrl(runId, block.imagePath)} target="_blank" rel="noreferrer">
              <RetryImage src={artifactUrl(runId, block.imagePath)} alt="新页面截图" />
              <small>STATE {block.order}</small>
            </a>
          )}
        </div>
      );
    case "anomaly":
      return (
        <div className="thought-body anomaly-body">
          <p>
            <Badge tone={block.severity === "critical" ? "danger" : "warn"}>{block.severity}</Badge>
            <strong> {block.kindLabel}</strong>
            {block.detail ? <> · {block.detail}</> : null}
          </p>
        </div>
      );
    case "notice":
      return <div className="thought-body">{block.detail && <p>{block.detail}</p>}</div>;
  }
}
