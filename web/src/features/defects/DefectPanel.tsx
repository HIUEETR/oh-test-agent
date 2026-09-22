// 缺陷面板（Phase 5.2）：左侧列表 + 右侧证据链与处置。
//
// 「缺陷」是 Phase 3 起的一等产物：运行中或事后分析发现的异常在这里可直接查询、人工判定，
// 并一键转成复现用例（Phase 4 闭环）。

import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, ExternalLink, FileSearch, RefreshCw, XCircle } from "lucide-react";

import { apiError } from "../../api/client";
import {
  ANOMALY_KIND_LABELS,
  DEFECT_STATUS_LABELS,
  defectArtifactUrl,
  getDefect,
  listDefects,
  patchDefect,
  toBugRepro,
} from "../../api/defects";
import type { AnomalySeverity, DefectRecord, DefectStatus, DefectSummary } from "../../api/types";
import { Badge, EmptyState } from "../../components/ui/primitives";

export type DefectPanelProps = {
  /** 只显示某次运行的缺陷。 */
  runId?: string;
  /** 只显示某应用的缺陷。 */
  bundleName?: string;
};

const SEVERITY_ORDER: Record<string, number> = { info: 0, warning: 1, critical: 2 };

type Tone = "ok" | "warn" | "danger" | "brand" | "neutral";

export function severityTone(severity: AnomalySeverity | string): Tone {
  if (severity === "critical") return "danger";
  if (severity === "warning") return "warn";
  return "neutral";
}

export function statusTone(status: DefectStatus | string): Tone {
  if (status === "confirmed") return "danger";
  if (status === "not_reproduced" || status === "dismissed") return "ok";
  return "warn";
}

function formatTime(value?: string): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("zh-CN", { hour12: false });
}

/** 缺陷列表项：标题 + 类别/严重度/状态徽章 + 出现次数。 */
export function DefectRow({
  defect,
  selected,
  onSelect,
}: {
  defect: DefectSummary;
  selected: boolean;
  onSelect: (defectId: string) => void;
}) {
  return (
    <button
      type="button"
      className={`defect-row${selected ? " active" : ""}`}
      aria-current={selected ? "true" : undefined}
      onClick={() => onSelect(defect.defect_id)}
    >
      <span className="defect-row-title">{defect.title_zh || defect.defect_id}</span>
      <span className="defect-row-meta">
        <Badge tone={severityTone(defect.severity)}>{defect.severity}</Badge>
        <Badge>{ANOMALY_KIND_LABELS[defect.kind] ?? defect.kind}</Badge>
        <Badge tone={statusTone(defect.status)}>{DEFECT_STATUS_LABELS[defect.status] ?? defect.status}</Badge>
        {defect.occurrences > 1 && <Badge tone="brand">×{defect.occurrences}</Badge>}
      </span>
      <span className="defect-row-sub">
        {defect.page_path || "—"} · {formatTime(defect.last_seen_at)}
      </span>
    </button>
  );
}

/** 右侧详情：证据链、触发动作、状态操作、生成复现用例。 */
export function DefectDetail({
  defect,
  onChanged,
}: {
  defect: DefectRecord | null;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [reproResult, setReproResult] = useState("");

  if (!defect) {
    return (
      <EmptyState
        title="选择一条缺陷"
        hint="查看证据链、触发动作与处置入口"
        icon={<FileSearch size={30} />}
      />
    );
  }

  const act = async (label: string, run: () => Promise<void>) => {
    setBusy(label);
    setError("");
    try {
      await run();
      onChanged();
    } catch (cause) {
      setError(apiError(label, cause));
    } finally {
      setBusy("");
    }
  };

  const evidencePaths = defect.evidence_paths ?? [];
  const findings = defect.findings ?? [];

  return (
    <div className="defect-detail">
      <div className="card-heading">
        <span>{defect.title_zh}</span>
        <small>{defect.defect_id}</small>
      </div>
      <div className="defect-row-meta">
        <Badge tone={severityTone(defect.severity)}>{defect.severity}</Badge>
        <Badge>{ANOMALY_KIND_LABELS[defect.kind] ?? defect.kind}</Badge>
        <Badge tone={statusTone(defect.status)}>{DEFECT_STATUS_LABELS[defect.status] ?? defect.status}</Badge>
        <Badge tone="brand">出现 {defect.occurrences} 次</Badge>
      </div>

      {error && <p className="note bad" role="alert">{error}</p>}

      <dl className="defect-facts">
        <div><dt>包名</dt><dd>{defect.bundle_name || "—"}</dd></div>
        <div><dt>页面</dt><dd>{defect.page_path || "—"}</dd></div>
        <div><dt>触发动作</dt><dd>{defect.action_id || "—"}</dd></div>
        <div><dt>首次</dt><dd>{formatTime(defect.first_seen_at)}</dd></div>
        <div><dt>最近</dt><dd>{formatTime(defect.last_seen_at)}</dd></div>
        <div><dt>设备</dt><dd>{defect.device_id || "—"}</dd></div>
      </dl>

      {defect.summary_zh && <p className="note">{defect.summary_zh}</p>}

      <div className="defect-section">
        <h4>证据链</h4>
        {evidencePaths.length === 0 && <p className="note">无产物路径（证据在 finding 的 evidence 字段里）</p>}
        <ul className="defect-evidence">
          {evidencePaths.map((path) => (
            <li key={path}>
              <a href={defectArtifactUrl(defect.defect_id, path)} target="_blank" rel="noreferrer">
                {path} <ExternalLink size={11} aria-hidden="true" />
              </a>
            </li>
          ))}
        </ul>
        {findings.length > 0 && (
          <details className="defect-findings">
            <summary>{findings.length} 条 finding 明细</summary>
            <ul>
              {findings.map((finding, index) => (
                <li key={`${finding.action_id || "na"}-${index}`}>
                  <Badge tone={severityTone(finding.severity)}>{finding.severity}</Badge>{" "}
                  <code>{finding.kind}</code> {finding.summary_zh}
                  {finding.detail ? <div className="note">{finding.detail}</div> : null}
                </li>
              ))}
            </ul>
          </details>
        )}
      </div>

      {defect.repro_case_id && (
        <div className="defect-section">
          <h4>复现用例</h4>
          <p>
            <code>{defect.repro_case_id}</code>
            {defect.repro_execution_id ? <> · 执行 <code>{defect.repro_execution_id}</code></> : null}
          </p>
        </div>
      )}

      {defect.notes && (
        <div className="defect-section">
          <h4>备注</h4>
          <p className="note">{defect.notes}</p>
        </div>
      )}

      <div className="defect-section defect-actions">
        <h4>处置</h4>
        <div className="defect-action-row">
          <button
            type="button"
            className="secondary compact"
            disabled={Boolean(busy) || defect.status === "confirmed"}
            onClick={() =>
              void act("确认缺陷", async () => {
                await patchDefect(defect.defect_id, { status: "confirmed" });
              })
            }
          >
            <CheckCircle2 size={13} aria-hidden="true" /> 确认缺陷
          </button>
          <button
            type="button"
            className="secondary compact"
            disabled={Boolean(busy) || defect.status === "dismissed"}
            onClick={() =>
              void act("标记误报", async () => {
                await patchDefect(defect.defect_id, { status: "dismissed", notes: "人工判定为误报" });
              })
            }
          >
            <XCircle size={13} aria-hidden="true" /> 标记误报
          </button>
          <button
            type="button"
            className="primary compact"
            disabled={Boolean(busy) || defect.status === "dismissed"}
            onClick={() =>
              void act("生成复现用例", async () => {
                const result = await toBugRepro(defect.defect_id, true);
                setReproResult(result.case_id ? `已生成用例 ${result.case_id}` : "已提交复现用例请求");
              })
            }
          >
            <RefreshCw size={13} aria-hidden="true" /> 生成复现用例
          </button>
        </div>
        {reproResult && <p className="note ok">{reproResult}</p>}
      </div>
    </div>
  );
}

/** 缺陷面板：列表 + 详情。 */
export function DefectPanel({ runId, bundleName }: DefectPanelProps) {
  const [items, setItems] = useState<DefectSummary[]>([]);
  const [selected, setSelected] = useState("");
  const [detail, setDetail] = useState<DefectRecord | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [statusFilter, setStatusFilter] = useState<DefectStatus | "">("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const response = await listDefects({
        run_id: runId,
        bundle_name: bundleName,
        status: statusFilter || undefined,
        limit: 200,
      });
      const sorted = [...response.defects].sort(
        (left, right) =>
          (SEVERITY_ORDER[right.severity] ?? 0) - (SEVERITY_ORDER[left.severity] ?? 0) ||
          right.occurrences - left.occurrences,
      );
      setItems(sorted);
      setSelected((current) => {
        if (sorted.length === 0) return "";
        return sorted.some((item) => item.defect_id === current) ? current : sorted[0].defect_id;
      });
    } catch (cause) {
      setError(apiError("加载缺陷列表失败", cause));
    } finally {
      setLoading(false);
    }
  }, [runId, bundleName, statusFilter]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const record = await getDefect(selected);
        if (!cancelled) setDetail(record);
      } catch (cause) {
        if (!cancelled) setError(apiError("加载缺陷详情失败", cause));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selected]);

  const criticalCount = useMemo(
    () => items.filter((item) => item.severity === "critical").length,
    [items],
  );

  return (
    <section className="panel">
      <div className="card-heading">
        <span>缺陷</span>
        <small>
          {items.length} 条{criticalCount > 0 ? ` · critical ${criticalCount}` : ""}
        </small>
      </div>
      <p className="note">
        缺陷是附加结论：不改变用例的 passed / 失败判定，但会与运行状态对账。
      </p>
      <div className="defect-toolbar">
        <label className="defect-filter">
          状态
          <select
            aria-label="按状态过滤"
            value={statusFilter}
            onChange={(event) => setStatusFilter(event.target.value as DefectStatus | "")}
          >
            <option value="">全部</option>
            <option value="suspected">待确认</option>
            <option value="confirmed">已确认</option>
            <option value="not_reproduced">未复现</option>
            <option value="dismissed">已忽略</option>
          </select>
        </label>
        <button type="button" className="secondary compact" onClick={() => void load()} disabled={loading}>
          <RefreshCw size={13} aria-hidden="true" /> {loading ? "加载中…" : "刷新"}
        </button>
      </div>

      {error && <p className="note bad" role="alert">{error}</p>}
      {!loading && items.length === 0 && !error && (
        <EmptyState
          title="暂无缺陷"
          hint="运行中发现或事后分析发现的异常会出现在这里"
          icon={<AlertTriangle size={30} />}
        />
      )}
      {items.length > 0 && (
        <div className="defect-layout">
          <div className="defect-list" role="list">
            {items.map((item) => (
              <DefectRow
                key={item.defect_id}
                defect={item}
                selected={item.defect_id === selected}
                onSelect={setSelected}
              />
            ))}
          </div>
          <DefectDetail defect={detail} onChanged={() => void load()} />
        </div>
      )}
    </section>
  );
}

export default DefectPanel;
