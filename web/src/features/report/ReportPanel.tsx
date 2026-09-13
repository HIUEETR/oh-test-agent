// 报告面板：探测 HTML 报告可用性，内嵌展示并支持下载。

import { useEffect } from "react";
import { Braces, Download, RefreshCw } from "lucide-react";
import { EmptyState } from "../../components/ui/primitives";
import { useConsole } from "../../stores/console";
import { apiUrl } from "../../api/client";

export function ReportPanel() {
  const runId = useConsole((state) => state.runId);
  const reportReady = useConsole((state) => state.reportReady);
  const reportLoading = useConsole((state) => state.reportLoading);
  const reportRevision = useConsole((state) => state.reportRevision);
  const trace = useConsole((state) => state.trace);
  const probeReport = useConsole((state) => state.probeReport);

  const traceRevision = trace?.revision ?? trace?.updated_at ?? trace?.state ?? "none";
  const reportUrl = apiUrl(
    `/api/runs/${encodeURIComponent(runId)}/report?revision=${encodeURIComponent(`${traceRevision}-${reportRevision}`)}`,
  );

  // 进入报告页、运行切换或回放结束后自动探测一次。
  useEffect(() => {
    if (runId) void probeReport();
  }, [runId, reportRevision, probeReport]);

  if (!runId) {
    return (
      <section className="panel">
        <div className="report-card"><EmptyState title="尚无可查看的报告" hint="启动运行并生成脚本后可查看 HTML 报告" icon={<Braces size={38} />} /></div>
      </section>
    );
  }

  return (
    <section className="panel">
      <div className="report-toolbar">
        <button type="button" className="secondary" onClick={probeReport} disabled={reportLoading}>
          <RefreshCw size={14} />重试报告
        </button>
        {reportReady && (
          <a className="report-download" href={`${reportUrl}&download=true`}>
            <Download size={14} />下载 HTML 报告
          </a>
        )}
      </div>
      <div className="report-card">
        {reportLoading
          ? <EmptyState title="正在探测报告" />
          : reportReady
            ? <iframe key={reportUrl} title="测试运行报告" src={reportUrl} />
            : <EmptyState title="报告尚未就绪" hint="生成或回放完成后可以再次探测" icon={<Braces size={38} />} />}
      </div>
    </section>
  );
}
