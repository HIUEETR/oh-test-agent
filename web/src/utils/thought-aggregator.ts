// 思考流聚合器：把原始 RunEvent 流聚合成面向人的「思考/行动/门禁」块。
// 纯函数、无副作用，便于单元测试与 useMemo 缓存。

import type {
  ActionResultView,
  AdvisorVerdictView,
  CandidateDigestEntry,
  PlannedStepView,
  RunEvent,
  ToolDecisionView,
} from "../api/types";

/** 异常类别 → 中文标签（与 ``api/defects.ts::ANOMALY_KIND_LABELS`` 同源口径）。 */
const ANOMALY_KIND_LABELS: Record<string, string> = {
  cppcrash: "C++ 崩溃",
  jscrash: "JS 崩溃",
  appfreeze: "应用冻屏",
  anr: "无响应 (ANR)",
  white_screen: "白屏 / 黑屏",
  page_unresponsive: "页面无响应",
  layout_anomaly: "布局异常",
  memory_growth: "内存增长",
  locator_stale: "定位器失效",
};

export type ThoughtPhase = "think" | "act" | "gate" | "fail";

export type ThoughtBlock =
  | { kind: "plan"; id: string; at: string; phase: ThoughtPhase; title: string; steps: PlannedStepView[]; mock: boolean }
  | {
      kind: "perception"; id: string; at: string; phase: ThoughtPhase; title: string;
      snapshotId: string; imagePath: string; summary: string; elementCount: number | null;
    }
  | {
      kind: "step"; id: string; at: string; phase: ThoughtPhase; title: string;
      stepId: string; instruction: string; decision: ToolDecisionView | null; result: ActionResultView | null;
    }
  | { kind: "assertion"; id: string; at: string; phase: ThoughtPhase; title: string; passed: boolean; detail: string }
  | {
      kind: "advisor"; id: string; at: string; phase: ThoughtPhase; title: string;
      pageId: string; source: string; reuse: boolean;
      verdict: AdvisorVerdictView | null; candidates: CandidateDigestEntry[];
    }
  | {
      kind: "page"; id: string; at: string; phase: ThoughtPhase; title: string;
      pagePath: string; imagePath: string; elementCount: number; order: number;
    }
  | {
      kind: "anomaly"; id: string; at: string; phase: ThoughtPhase; title: string;
      kindLabel: string; severity: string; detail: string;
    }
  | { kind: "notice"; id: string; at: string; phase: ThoughtPhase; title: string; detail: string };

/** 判断 discovery_progress 事件属于哪个阶段（payload.stage 由后端注入）。 */
function progressStage(payload: Record<string, unknown>): string {
  return typeof payload.stage === "string" ? payload.stage : "";
}

/** 从事件 payload 中读取候选摘要列表。 */
function readCandidates(payload: Record<string, unknown>): CandidateDigestEntry[] {
  const raw = payload.candidates;
  return Array.isArray(raw) ? (raw as CandidateDigestEntry[]) : [];
}

function readVerdict(payload: Record<string, unknown>): AdvisorVerdictView | null {
  const raw = payload.verdict;
  if (!raw || typeof raw !== "object") return null;
  return raw as AdvisorVerdictView;
}

/** 事件的一级分类：决定思考块的颜色相位。 */
function noticePhase(level: NoticeLevel): ThoughtPhase {
  if (level === "error") return "fail";
  if (level === "warn") return "gate";
  if (level === "success") return "act";
  return "think";
}

type NoticeLevel = "info" | "success" | "warn" | "error";

/** 探索停止原因的可读描述：探索早停不是整个运行的终点，后续仍会进行 Profile 验证与回放。 */
const STOP_REASON_LABELS: Record<string, string> = {
  admission_metrics_reached: "已达准入指标，探索提前完成，继续 Profile 验证与回放",
  page_limit: "达到页面上限，继续 Profile 验证与回放",
  duration_limit: "达到时长上限，继续 Profile 验证与回放",
  queue_exhausted: "候选页面探索完毕，继续 Profile 验证与回放",
  stopped_by_user: "已由用户停止探索",
  disabled: "自动探索未启用",
};

function describeStopReason(reason: string): string {
  return STOP_REASON_LABELS[reason] ?? `探索停止：${reason}`;
}

/** 已知事件的展示规则表：level 决定相位，部分事件跳过（由专用块或页面图呈现）。 */
const NOTICE_RULES: Record<string, { level: NoticeLevel; skip?: boolean }> = {
  run_started: { level: "info" },
  preflight_passed: { level: "success" },
  target_resolved: { level: "info" },
  target_candidates_found: { level: "warn" },
  target_started: { level: "info" },
  original_task_started: { level: "info" },
  profile_found: { level: "info" },
  profile_revalidation_started: { level: "info" },
  profile_revalidation_finished: { level: "success" },
  profile_live_mode: { level: "warn" },
  profile_incremental: { level: "info" },
  profile_harvested: { level: "success" },
  profile_draft_saved: { level: "success" },
  profile_verification_started: { level: "info" },
  profile_verification_round_started: { level: "info" },
  profile_verification_round_finished: { level: "success" },
  profile_promoted: { level: "success" },
  hypium_replay_started: { level: "info" },
  hypium_replay_finished: { level: "success" },
  discovery_started: { level: "info" },
  discovery_finished: { level: "success" },
  discovery_path_blocked: { level: "warn" },
  locator_candidate_observed: { level: "info", skip: true },
  edge_created: { level: "info", skip: true },
  script_generated: { level: "success" },
  execution_started: { level: "info" },
  execution_finished: { level: "success" },
  run_failed: { level: "error" },
  run_finished: { level: "success" },
};

/**
 * 把事件列表聚合为思考块序列。
 * 输入须按 event_id 升序；结果中：
 * - plan_created → 计划块（模型规划输出）
 * - screen_captured/elements_detected → 感知块（视觉输入理解 + 元素规模）
 * - action_started/action_finished → 步骤块（模型工具决策 + 执行结果）
 * - assertion_* → 断言块
 * - discovery_progress(stage=advisor) → 顾问块（页面理解 + 推荐/回避）
 * - page_discovered → 页面块；其余按规则表转为通知块
 */
export function aggregateThoughts(events: RunEvent[]): ThoughtBlock[] {
  const blocks: ThoughtBlock[] = [];
  // 快速索引：元素计数需要回填最近的感知块；步骤结果需要回填匹配的步骤块。
  const perceptionBySnapshot = new Map<string, ThoughtBlock & { kind: "perception" }>();
  const stepByStepId = new Map<string, ThoughtBlock & { kind: "step" }>();

  for (const event of events) {
    const payload = event.payload ?? {};
    const id = `evt-${event.event_id}`;
    switch (event.type) {
      case "plan_created": {
        const steps = Array.isArray(payload.steps) ? (payload.steps as PlannedStepView[]) : [];
        blocks.push({
          kind: "plan", id, at: event.timestamp, phase: "think", title: event.message,
          steps, mock: Boolean(payload.mock),
        });
        break;
      }
      case "screen_captured": {
        const snapshotId = String(payload.snapshot_id ?? "");
        const block: ThoughtBlock & { kind: "perception" } = {
          kind: "perception", id, at: event.timestamp, phase: "think", title: event.message,
          snapshotId,
          imagePath: typeof payload.image_path === "string" ? payload.image_path : "",
          summary: typeof payload.summary === "string" ? payload.summary : "",
          elementCount: null,
        };
        blocks.push(block);
        if (snapshotId) perceptionBySnapshot.set(snapshotId, block);
        break;
      }
      case "elements_detected": {
        const snapshotId = String(payload.snapshot_id ?? "");
        const target = snapshotId ? perceptionBySnapshot.get(snapshotId) : undefined;
        if (target) {
          target.elementCount = Number(payload.count ?? 0);
          // 合并视觉请求（计划 5.1）：帧先推送画面，观测摘要在随后的 elements_detected 里回填，
          // 因此这里补写摘要，避免界面丢失 LLM 对页面的理解。
          const summary = typeof payload.summary === "string" ? payload.summary : "";
          if (summary) target.summary = summary;
        }
        break;
      }
      case "action_started": {
        const stepId = String(payload.step_id ?? "");
        const decision = typeof payload.decision === "object" && payload.decision !== null
          ? (payload.decision as ToolDecisionView)
          : null;
        const block: ThoughtBlock & { kind: "step" } = {
          kind: "step", id, at: event.timestamp, phase: "act", title: event.message,
          stepId, instruction: event.message, decision, result: null,
        };
        blocks.push(block);
        if (stepId) stepByStepId.set(stepId, block);
        break;
      }
      case "action_finished": {
        const stepId = String(payload.step_id ?? "");
        const existing = stepId ? stepByStepId.get(stepId) : undefined;
        const result: ActionResultView = {
          step_id: stepId,
          tool: String(payload.tool ?? ""),
          success: Boolean(payload.success),
          duration_ms: typeof payload.duration_ms === "number" ? payload.duration_ms : undefined,
          error: typeof payload.error === "string" ? payload.error : null,
          warnings: Array.isArray(payload.warnings) ? (payload.warnings as string[]) : undefined,
        };
        if (existing) {
          existing.result = result;
          existing.phase = result.success ? "act" : "fail";
          existing.title = event.message;
        } else {
          blocks.push({
            kind: "step", id, at: event.timestamp, phase: result.success ? "act" : "fail",
            title: event.message, stepId, instruction: event.message, decision: null, result,
          });
        }
        break;
      }
      case "assertion_passed":
      case "assertion_failed": {
        const passed = event.type === "assertion_passed";
        blocks.push({
          kind: "assertion", id, at: event.timestamp, phase: passed ? "gate" : "fail",
          title: event.message, passed,
          detail: [payload.kind, payload.target, payload.message]
            .filter((value) => typeof value === "string" && value)
            .join(" · "),
        });
        break;
      }
      case "discovery_progress": {
        const stage = progressStage(payload);
        if (stage !== "advisor") break; // advisor_turn 留痕由顾问对话视图呈现，不在思考流重复展开
        blocks.push({
          kind: "advisor", id, at: event.timestamp, phase: "think",
          title: String(payload.source) === "reuse" ? "顾问复用既有建议" : "顾问给出页面建议",
          pageId: String(payload.page_id ?? ""), source: String(payload.source ?? ""),
          reuse: payload.source === "reuse",
          verdict: readVerdict(payload), candidates: readCandidates(payload),
        });
        break;
      }
      case "page_discovered": {
        blocks.push({
          kind: "page", id, at: event.timestamp, phase: "act", title: event.message,
          pagePath: String(payload.page_path ?? ""),
          imagePath: typeof payload.image_path === "string" ? payload.image_path : "",
          elementCount: Number(payload.element_count ?? 0),
          order: Number(payload.discovered_order ?? 0),
        });
        break;
      }
      case "anomaly_detected":
      case "defect_recorded": {
        // Phase 2/3：运行中发现的异常与已入库的缺陷都必须**醒目**地进入思考流，
        // 而不是被折叠成普通通知。critical 用 fail 相位（红色），其余用 gate（黄色）。
        const kind = String(payload.kind ?? "");
        const severity = String(payload.severity ?? "warning");
        blocks.push({
          kind: "anomaly", id, at: event.timestamp,
          phase: severity === "critical" ? "fail" : "gate",
          title: event.message,
          kindLabel: ANOMALY_KIND_LABELS[kind] ?? kind ?? event.type,
          severity,
          detail: [payload.page_path, payload.action_id]
            .filter((value) => typeof value === "string" && value)
            .join(" · "),
        });
        break;
      }
      case "discovery_path_blocked": {
        const action = payload.action && typeof payload.action === "object"
          ? (payload.action as { target_text?: string; kind?: string }).target_text ?? ""
          : "";
        blocks.push({
          kind: "notice", id, at: event.timestamp, phase: "gate", title: event.message,
          detail: action || String(payload.reason ?? payload.blocked_reason ?? ""),
        });
        break;
      }
      default: {
        const rule = NOTICE_RULES[event.type];
        if (!rule || rule.skip) {
          if (rule) break;
          // 未知事件兜底：仍以通知形式呈现，保证流完整可读。
          blocks.push({ kind: "notice", id, at: event.timestamp, phase: "think", title: event.message, detail: event.type });
          break;
        }
        let level = rule.level;
        if (event.type === "profile_revalidation_finished" || event.type === "profile_verification_round_finished"
          || event.type === "hypium_replay_finished" || event.type === "execution_finished") {
          level = payload.passed === false ? "error" : "success";
        }
        const detail = typeof payload.stop_reason === "string" ? describeStopReason(payload.stop_reason)
          : typeof payload.error === "string" ? payload.error
          : typeof payload.task === "string" ? payload.task
          : typeof payload.bundle_name === "string" ? payload.bundle_name
          : typeof payload.reason === "string" ? payload.reason
          : "";
        blocks.push({ kind: "notice", id, at: event.timestamp, phase: noticePhase(level), title: event.message, detail });
        break;
      }
    }
  }
  return blocks;
}
