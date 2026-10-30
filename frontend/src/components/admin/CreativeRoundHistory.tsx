import type { CreativeRound } from "@/types/admin";
import {
  changedSectionLabels,
  creativeRoundLabel,
  creativeRoundStateLabel,
} from "@/lib/creative";

export function CreativeRoundHistory({
  rounds,
  onReadCandidate,
}: {
  rounds: CreativeRound[];
  onReadCandidate: (id: number) => void;
}) {
  if (!rounds.length) return null;
  return (
    <details className="mt-4 rounded-md border border-line p-3">
      <summary className="cursor-pointer text-sm text-ink">
        查看创作与修稿记录 · {rounds.length} 版
      </summary>
      <ol className="mt-4 space-y-4">
        {rounds.map((round) => {
          const sections = changedSectionLabels(round.changed_fields ?? []);
          return (
            <li key={round.round_index} className="border-t border-line pt-4 first:border-0 first:pt-0">
              <div className="flex flex-wrap items-start justify-between gap-2 text-sm">
                <h4 className="text-ink">{creativeRoundLabel(round)} · {round.content_version}</h4>
                <span className="text-muted">{creativeRoundStateLabel(round.state)}</span>
              </div>
              <p className="mt-2 text-xs text-muted">本轮已计入 ¥{round.charged_cost_cny}</p>
              {sections.length > 0 && (
                <p className="mt-2 text-sm text-body">已修改：{sections.join("、")}</p>
              )}
              <p className="mt-2 whitespace-pre-wrap break-words text-sm text-body">
                {round.audit_summary ?? "尚无有效审核报告。"}
              </p>
              {round.report_hash && round.findings.length === 0 && (
                <p className="mt-2 text-xs text-muted">本轮模型未记录问题，仍需人工审核。</p>
              )}
              {round.findings.length > 0 && (
                <ul aria-label={`${creativeRoundLabel(round)}的审核发现`} className="mt-3 space-y-2">
                  {round.findings.map((finding) => (
                    <li key={finding.id} className="rounded border border-line p-2 text-sm">
                      <span className={finding.severity === "BLOCKER" ? "text-danger" : "text-accent"}>
                        {finding.severity === "BLOCKER" ? "阻断" : finding.severity === "WARNING" ? "警告" : "建议"}
                      </span>
                      <span className="whitespace-pre-wrap break-words text-body"> · {finding.message}</span>
                    </li>
                  ))}
                </ul>
              )}
              {round.error_code && (
                <p className="mt-2 break-all text-xs text-danger">本轮未完成 · {round.error_code}</p>
              )}
              {round.candidate_version_id && (
                <button className="btn-secondary mt-3" onClick={() => onReadCandidate(round.candidate_version_id!)}>
                  查看这一版候选与审核
                </button>
              )}
            </li>
          );
        })}
      </ol>
    </details>
  );
}
