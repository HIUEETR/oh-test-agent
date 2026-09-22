"""根据运行轨迹生成 HTML 报告与对应的结构化 JSON 报告。"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from .analysis.defects import SEVERITY_ORDER, record_from_finding
from .models import AnomalyFinding, ExecutionAnalysis, RunState, RunTrace
from .storage import ArtifactStore

_ARTIFACT_SUFFIXES = (".jpeg", ".jpg", ".png", ".webp", ".json", ".log", ".txt", ".xml", ".html", ".zip")
_EVIDENCE_EXCERPT_CHARS = 300

_PHASE_LABELS = {
    "in_run": "运行中",
    "post_hoc": "事后",
    "exploration": "探索期",
    "replay": "回放",
}


class ReportBuilder:
    """将动作、脚本覆盖、回放证据和失败摘要组织为运行报告。"""

    def __init__(self, artifacts: ArtifactStore):
        self.artifacts = artifacts

    def build(self, trace: RunTrace) -> Path:
        """写入 HTML 与 JSON 报告，并返回 HTML 报告的绝对路径。"""
        run_dir = self.artifacts.run_dir(trace.run_id)
        report_path = run_dir / "reports" / "report.html"
        snapshots = {item.snapshot_id: item for item in trace.snapshots}
        cards: list[str] = []
        for action in trace.actions:
            snapshot = snapshots.get(action.after_snapshot_id or action.before_snapshot_id or "")
            image = self._image_markup(trace.run_id, run_dir, snapshot.image_path) if snapshot else ""
            cards.append(self._step_markup(trace.run_id, action, image))
        document = self._document(trace, "".join(cards), run_dir)
        report_path.write_text(document, encoding="utf-8")
        self.artifacts.write_json(run_dir / "reports" / "report.json", trace)
        return report_path.resolve()

    # ------------------------------------------------------------------ 步骤卡片

    @staticmethod
    def _step_markup(run_id: str, action: Any, image: str) -> str:
        """渲染一个步骤卡片：**失败原因与绕路标记必须可见**（G4：不掩盖）。

        历史实现只渲染 ``step_id · tool`` 与参数字典，``action.error`` 一次都不显示，
        于是「失败后靠绕路完成」在报告里完全看不出来。
        """
        del run_id
        lines = [
            f'<article class="step"><h3>{html.escape(str(action.step_id))} · {html.escape(str(action.tool))}</h3>',
            f"<p>{html.escape(json.dumps(action.params, ensure_ascii=False))}</p>",
        ]
        if not action.success:
            lines.append(f'<p class="bad">✗ 失败：{html.escape(str(action.error or "未知原因"))}</p>')
        elif action.error:
            lines.append(f'<p class="warn">⚠ {html.escape(str(action.error))}</p>')
        if action.workaround is not None:
            workaround = action.workaround
            lines.append(
                f'<p class="warn">↳ 绕路：{html.escape(workaround.corrective_tool or "纠正动作")}'
                f"{' → ' + html.escape(workaround.corrective_target) if workaround.corrective_target else ''}"
                f" 后达成目标（原始失败：{html.escape(workaround.original_error[:200])}）</p>"
            )
        if action.anomaly is not None:
            finding = action.anomaly
            lines.append(
                f'<p class="bad">⚠ 运行中发现异常：'
                f"{html.escape(str(finding.kind))}（{html.escape(finding.severity)}）"
                f"{html.escape(finding.summary_zh)}</p>"
            )
        if action.assertion:
            assertion_class = "ok" if action.assertion.passed else "bad"
            lines.append(f'<p class="assertion {assertion_class}">{html.escape(action.assertion.message)}</p>')
        if image:
            lines.append(image)
        lines.append("</article>")
        return "".join(lines)

    @staticmethod
    def _image_markup(run_id: str, run_dir: Path, image_path: Path) -> str:
        try:
            relative = image_path.resolve().relative_to(run_dir.resolve()).as_posix()
        except ValueError:
            return ""
        return (
            f'<img src="/api/runs/{html.escape(run_id, quote=True)}/artifacts/'
            f'{html.escape(relative, quote=True)}" alt="运行截图">'
        )

    # ------------------------------------------------------------------ 章节

    @staticmethod
    def _coverage_markup(trace: RunTrace) -> str:
        generated = trace.generated
        if generated is None:
            return '<section class="summary"><h2>脚本覆盖范围</h2><p>未生成 Hypium 脚本。</p></section>'
        reasons = "".join(f"<li>{html.escape(reason)}</li>" for reason in generated.confidence_factors)
        # 质量提示：不阻断执行，只用黄色与「置信度」一格表达可信程度。
        quality = (
            f"<p>质量提示（不影响执行）：</p><ul class='warn'>{reasons}</ul>"
            if reasons
            else "<p>质量提示（不影响执行）：无</p>"
        )
        confidence_label = {"high": "高", "medium": "中", "low": "低"}.get(generated.confidence, "低")
        promotion = ""
        if not generated.promotion_eligible and generated.promotion_blockers:
            blockers = "；".join(html.escape(item) for item in generated.promotion_blockers)
            promotion = f"<p><small>不作为 Profile 晋级证据（{blockers}）。</small></p>"
        return f"""<section class="summary"><h2>脚本覆盖范围</h2><div class="grid">
<div class="metric">用途<br><b>{html.escape(generated.purpose)}</b></div>
<div class="metric">可执行<br><b>{"是" if generated.replay_eligible else "否"}</b></div>
<div class="metric">置信度<br><b>{confidence_label}</b></div>
<div class="metric">源动作<br><b>{generated.source_action_count}</b></div>
<div class="metric">纳入脚本<br><b>{generated.included_action_count}</b></div>
<div class="metric">省略动作<br><b>{generated.omitted_action_count}</b></div></div>
{quality}{promotion}</section>"""

    @staticmethod
    def _replay_markup(trace: RunTrace) -> str:
        rows = []
        for replay in trace.replays:
            error = html.escape(replay.error.message) if replay.error else ""
            exit_code = replay.exit_code if replay.exit_code is not None else ""
            evidence = "<br>".join(html.escape(path) for path in replay.evidence_paths)
            rows.append(
                f"<tr><td>{replay.attempt}</td><td>{html.escape(replay.status)}</td><td>{exit_code}</td>"
                f"<td>{'是' if replay.timed_out else '否'}</td><td>{error}</td><td>{evidence}</td></tr>"
            )
        body = "".join(rows) or '<tr><td colspan="6">尚无回放结果</td></tr>'
        progress = f"完成 {trace.replay_completed} / {trace.replay_total}；通过 {trace.replay_passed}"
        return f"""<section class="summary"><h2>回放结果</h2>
<p>状态：<b>{html.escape(trace.replay_status)}</b>；{progress}</p>
<table><thead><tr><th>尝试</th><th>状态</th><th>退出码</th><th>超时</th><th>错误</th><th>证据</th></tr></thead>
<tbody>{body}</tbody></table></section>"""

    @staticmethod
    def _analysis_markup(analysis: ExecutionAnalysis | None) -> str:
        """渲染「执行结果分析」章节；``None`` 时返回空串，既有报告输出保持不变。"""
        if analysis is None:
            return ""
        run_id = analysis.subject_id.split("#", 1)[0] or analysis.subject_id
        if analysis.healthy:
            banner = '<p class="ok">健康</p>'
        else:
            banner = f'<p class="bad">发现 {len(analysis.findings)} 项异常</p>'
        ordered = sorted(
            analysis.findings,
            key=lambda item: -SEVERITY_ORDER.get(item.severity, 0),
        )
        rows = "".join(ReportBuilder._finding_row(finding, run_id) for finding in ordered)
        body = rows or '<tr><td colspan="5">无异常发现</td></tr>'
        symptom = ""
        if analysis.symptom_reproduced is not None:
            symptom_class = "ok" if analysis.symptom_reproduced else "bad"
            verdict = "已复现" if analysis.symptom_reproduced else "未复现"
            symptom = f'<p class="{symptom_class}">症状复现：<b>{verdict}</b></p>'
        meta = (
            f"<p>对象：<code>{html.escape(analysis.subject)}</code>"
            f" · 编号：<code>{html.escape(analysis.subject_id)}</code>"
            f" · 包名：<b>{html.escape(analysis.bundle_name or '—')}</b>"
            f" · 设备：<b>{html.escape(analysis.device_id or '—')}</b>"
            f" · 日志覆盖：<b>{html.escape(analysis.log_coverage)}</b></p>"
        )
        return f"""<section class="summary"><h2>执行结果分析</h2>
{banner}{meta}
<table><thead><tr><th>类别</th><th>严重度</th><th>阶段</th><th>摘要</th><th>证据摘录</th></tr></thead>
<tbody>{body}</tbody></table>{symptom}</section>"""

    @staticmethod
    def _finding_row(finding: AnomalyFinding, run_id: str) -> str:
        """渲染一条 finding：类别 / 严重度 / 阶段 / 摘要 / 证据摘录与产物链接。"""
        severity_class = "ok" if finding.severity == "info" else "bad"
        excerpt = json.dumps(finding.evidence, ensure_ascii=False)
        if len(excerpt) > _EVIDENCE_EXCERPT_CHARS:
            excerpt = excerpt[:_EVIDENCE_EXCERPT_CHARS] + "…"
        detail = f"<br><small>{html.escape(finding.detail)}</small>" if finding.detail else ""
        links = ReportBuilder._artifact_links(finding.evidence, run_id)
        if finding.screenshot:
            links += ReportBuilder._artifact_link(finding.screenshot, run_id)
        return (
            f'<tr><td><span class="{severity_class}">{html.escape(str(finding.kind))}</span></td>'
            f'<td><span class="{severity_class}">{html.escape(finding.severity)}</span></td>'
            f"<td>{html.escape(_PHASE_LABELS.get(finding.phase, finding.phase))}</td>"
            f"<td>{html.escape(finding.summary_zh)}{detail}</td>"
            f"<td>{html.escape(excerpt)}{links}</td></tr>"
        )

    @staticmethod
    def _artifact_link(path: str, run_id: str) -> str:
        """渲染单条产物链接（空路径返回空串）。"""
        if not path:
            return ""
        return (
            f'<br><a href="/api/runs/{html.escape(run_id, quote=True)}/artifacts/'
            f'{html.escape(path, quote=True)}">{html.escape(path)}</a>'
        )

    @staticmethod
    def _artifact_links(evidence: dict[str, Any], run_id: str) -> str:
        """把证据里的 Run 内相对产物路径渲染成现有 ``/api/runs/{id}/artifacts`` 链接。"""
        links = [
            f'<a href="/api/runs/{html.escape(run_id, quote=True)}/artifacts/{html.escape(path, quote=True)}">'
            f"{html.escape(path)}</a>"
            for path in ReportBuilder._artifact_paths(evidence)
        ]
        return "<br>" + "<br>".join(links) if links else ""

    @staticmethod
    def _artifact_paths(evidence: dict[str, Any]) -> list[str]:
        """挑出证据中 Run 内相对产物路径；绝对路径与裸文件名不建链接。"""
        paths: list[str] = []
        for value in evidence.values():
            candidates = value if isinstance(value, list) else [value]
            for candidate in candidates:
                if not isinstance(candidate, str):
                    continue
                text = candidate.replace("\\", "/")
                parts = text.split("/")
                if "/" not in text or text.startswith("/") or ":" in parts[0] or ".." in parts:
                    continue
                if text.lower().endswith(_ARTIFACT_SUFFIXES) and text not in paths:
                    paths.append(text)
        return paths

    # ------------------------------------------------------------------ 缺陷对账

    @staticmethod
    def defect_rows(trace: RunTrace) -> list[Any]:
        """本次运行涉及的缺陷：``trace.defects``（运行中）+ ``analysis.findings``（事后），按 id 去重。"""
        findings: list[AnomalyFinding] = list(trace.defects)
        if trace.analysis is not None:
            findings.extend(trace.analysis.findings)
        records: dict[str, Any] = {}
        bundle = ReportBuilder._bundle_name(trace)
        for finding in findings:
            record = record_from_finding(finding, bundle_name=bundle, run_id=trace.run_id)
            existing = records.get(record.defect_id)
            if existing is None:
                records[record.defect_id] = record
            else:
                merged = existing.model_copy(
                    update={
                        "findings": [*existing.findings, *record.findings],
                        "evidence_paths": list(dict.fromkeys([*existing.evidence_paths, *record.evidence_paths])),
                        "severity": (
                            existing.severity
                            if SEVERITY_ORDER.get(existing.severity, 0) >= SEVERITY_ORDER.get(record.severity, 0)
                            else record.severity
                        ),
                    },
                    deep=True,
                )
                records[record.defect_id] = merged
        ordered = sorted(
            records.values(),
            key=lambda item: (-SEVERITY_ORDER.get(item.severity, 0), item.defect_id),
        )
        return ordered

    @staticmethod
    def _bundle_name(trace: RunTrace) -> str:
        for profile in (trace.profile_snapshot, getattr(trace.resolved_target, "profile_snapshot", None)):
            bundle = getattr(profile, "bundle_name", "") if profile is not None else ""
            if bundle:
                return str(bundle)
        resolved = getattr(trace, "resolved_target", None)
        return str(getattr(resolved, "bundle_name", "") or "")

    @classmethod
    def _defect_markup(cls, trace: RunTrace) -> str:
        """渲染「疑似应用缺陷」章节；无缺陷时返回空串（既有报告输出保持不变）。"""
        records = cls.defect_rows(trace)
        if not records:
            return ""
        rows: list[str] = []
        for record in records:
            row_class = "critical" if record.severity == "critical" else "warning"
            links = "".join(cls._artifact_link(path, trace.run_id) for path in record.evidence_paths[:5])
            hint = record.summary_zh or record.title_zh
            rows.append(
                f'<tr class="defect-{row_class}">'
                f"<td><code>{html.escape(record.defect_id)}</code></td>"
                f"<td>{html.escape(str(record.kind))}</td>"
                f"<td>{html.escape(record.severity)}</td>"
                f"<td>{html.escape(record.title_zh)}</td>"
                f"<td>{html.escape(record.page_path or '—')}</td>"
                f"<td>{html.escape(record.action_id or '—')}</td>"
                f"<td>{record.occurrences}</td>"
                f"<td>{html.escape(hint[:200])}{links}</td>"
                "</tr>"
            )
        criticals = sum(1 for record in records if record.severity == "critical")
        return f"""<section class="summary"><h2>疑似应用缺陷</h2>
<p>共 <b>{len(records)}</b> 条（critical <b>{criticals}</b> 条）；按严重度排序。
缺陷是**附加结论**：不改变用例的 passed / 失败判定。</p>
<table><thead><tr><th>缺陷 ID</th><th>类别</th><th>严重度</th><th>标题</th>
<th>页面</th><th>触发动作</th><th>出现次数</th><th>证据 / 复现建议</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></section>"""

    @classmethod
    def _reconcile_markup(cls, trace: RunTrace) -> str:
        """报告顶部**对账横幅**：把 run 状态与执行结果分析显式对起来。

        历史缺口：一个 run 若 agent 步骤都跑完但分析抓到 critical ``cppcrash``，报告顶部状态是
        ``completed``，下面是一张红色异常表 —— 没有任何逻辑把两者联系起来。
        """
        markup = ""
        critical_count = sum(1 for finding in cls._all_findings(trace) if finding.severity == "critical")
        if trace.state == RunState.COMPLETED and critical_count:
            markup += (
                '<div class="banner-bad"><b>注意：</b>'
                f"运行状态为 completed，但执行结果分析发现 {critical_count} 项 critical 异常。"
                "用例通过 ≠ 应用无缺陷。</div>"
            )
        if trace.workaround_count:
            markup += (
                f'<div class="banner-warn">{trace.workaround_count} 个步骤是靠恢复循环绕路完成的，详见步骤卡片。</div>'
            )
        return markup

    @staticmethod
    def _all_findings(trace: RunTrace) -> list[AnomalyFinding]:
        findings = list(trace.defects)
        if trace.analysis is not None:
            findings.extend(trace.analysis.findings)
        return findings

    @staticmethod
    def _failure_markup(trace: RunTrace) -> str:
        failures = []
        if trace.agent_error or trace.error:
            failures.append(f"Agent：{trace.agent_error or trace.error}")
        failures.extend(f"回放 {item.attempt}：{item.error.message}" for item in trace.replays if item.error)
        content = "".join(f'<li class="bad">{html.escape(item)}</li>' for item in failures)
        return f'<section class="summary"><h2>失败摘要</h2><ul>{content or "<li>无失败</li>"}</ul></section>'

    # ------------------------------------------------------------------ 文档

    def _document(self, trace: RunTrace, cards: str, run_dir: Path) -> str:
        del run_dir
        css = """
body { font-family: "HarmonyOS Sans SC", Inter, "Microsoft YaHei", sans-serif; background: #eef3f9; color: #17212b;
  margin: 0; padding: 32px; }
main { max-width: 1180px; margin: auto; }
.summary, .step { background: rgba(255, 255, 255, 0.88); border: 1px solid rgba(23, 58, 94, 0.12); border-radius: 16px;
  padding: 20px; margin: 16px 0; box-shadow: 0 8px 24px rgba(23, 58, 94, 0.06); }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }
.metric { background: #f2f7fc; padding: 12px; border-radius: 10px; }
h1 { color: #0b48c4; } h2 { font-size: 17px; }
img { display: block; max-width: 420px; max-height: 620px; object-fit: contain; border-radius: 12px;
  border: 1px solid #d7e2ee; background: #f7fafd; margin-top: 12px; }
.ok { color: #0c7a48; } .bad { color: #b53539; }
/* 质量提示用黄色：与 .bad（红色，真正的阻断/失败）区分开。 */
.warn { color: #9a6400; }
code { color: #0b48c4; background: rgba(10, 89, 247, 0.08); padding: 1px 6px; border-radius: 5px; }
table { width: 100%; border-collapse: collapse; margin-top: 12px; }
th, td { border-bottom: 1px solid #e3ecf5; padding: 10px; text-align: left; }
.banner-bad { background: #fdecec; border: 1px solid #e9a6a6; color: #8f2226; border-radius: 10px;
  padding: 12px 16px; margin: 12px 0; }
.banner-warn { background: #fff6e6; border: 1px solid #edc98a; color: #8a5a10; border-radius: 10px;
  padding: 12px 16px; margin: 12px 0; }
tr.defect-critical td { background: #fdecec; }
tr.defect-warning td { background: #fff8ec; }
"""
        error = html.escape(trace.agent_error or trace.error or "")
        target = trace.resolved_target.model_dump(mode="json") if trace.resolved_target else {}
        profile = trace.profile_snapshot.model_dump(mode="json") if trace.profile_snapshot else {}
        discovery = trace.discovery_result or {}
        verification = trace.verification_result or {}
        gate_markup = f"""<section class="summary"><h2>Profile bootstrap</h2>
<p>阶段：<b>{html.escape(trace.phase)}</b> · Profile：<b>{html.escape(str(profile.get("status") or "none"))}</b>
· 临时结果：<b>{"是" if trace.provisional else "否"}</b></p>
<p>目标：<code>{html.escape(str(target.get("bundle_name") or trace.target_app_id))}</code>
· 探索页面：{len(discovery.get("pages", []))} · 验证轮次：{len(verification.get("rounds", []))}
· Profile Hypium 回放：{len(trace.profile_validation_replays)}</p></section>"""
        coverage = self._coverage_markup(trace)
        replays = self._replay_markup(trace)
        analysis = self._analysis_markup(trace.analysis)
        defects = self._defect_markup(trace)
        reconcile = self._reconcile_markup(trace)
        failures = self._failure_markup(trace)
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width">
<title>Harmony Test Agent · {html.escape(trace.run_id)}</title>
<style>{css}</style></head><body><main>
<h1>OpenHarmony 多模态测试 Agent 报告</h1>
<section class="summary"><p><code>{html.escape(trace.run_id)}</code></p>
<h2>{html.escape(trace.task)}</h2><div class="grid">
<div class="metric">状态<br><b>{html.escape(trace.state)}</b></div>
<div class="metric">Agent 结果<br><b>{html.escape(trace.agent_outcome)}</b></div>
<div class="metric">模型<br><b>{html.escape(trace.model_used)}</b></div>
<div class="metric">页面<br><b>{len(trace.graph.nodes)}</b></div>
<div class="metric">动作<br><b>{len(trace.actions)}</b></div>
<div class="metric">断言<br><b>{len(trace.assertions)}</b></div></div>
{"<p class='bad'>" + error + "</p>" if error else ""}</section>
{reconcile}{gate_markup}{coverage}{replays}{analysis}{defects}{failures}{cards}
</main></body></html>"""
