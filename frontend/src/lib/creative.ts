import type { CreativeJob, CreativeRound } from "../types/admin";

const stateLabels: Record<string, string> = {
  QUEUED: "等待生成",
  GENERATING: "正在创作初稿",
  REVISING: "正在根据审核意见修稿",
  COMPILING: "正在编译与审核",
  COMPLETED: "等待人工审核",
  REVIEW_REQUIRED: "自动流程已停止，待人工处理",
  BLOCKED: "需要处理",
  NEEDS_RECONCILIATION: "结果待核对",
  CANCELLED: "已取消",
  NO_CHANGE: "稿件未变化",
};

export const activeCreativeStates = new Set([
  "QUEUED",
  "GENERATING",
  "REVISING",
  "COMPILING",
]);

export function creativeStateLabel(job: Pick<CreativeJob, "state" | "stop_reason">) {
  if (job.state === "COMPLETED" && job.stop_reason === "CLEAN_AUDIT") {
    return "AI 审核结束，等待人工审核";
  }
  return stateLabels[job.state] ?? "状态待核对";
}

export function creativeStopMessage(reason: CreativeJob["stop_reason"]) {
  const messages: Record<string, string> = {
    CLEAN_AUDIT: "本轮 AI 审核未发现警告或阻断问题，仍需管理员阅读全文、完成人工审核并确认发布。",
    ROUND_LIMIT: "已达到自动修稿轮数上限，仍有审核问题，需管理员检查最新候选。",
    BUDGET_LIMIT: "剩余预算不足以继续，已停止后续调用。请检查已有版本与审核结果。",
    NO_CHANGE: "修稿内容与上一版相同，已停止重复修改，需管理员检查剩余问题。",
    PIPELINE_BLOCKED: "创作、编译或审核环节未完成，已保留记录，需人工检查。",
    RESULT_UNKNOWN: "调用结果或用量尚未核实，已暂停流程；请核对收据，勿重复提交。",
    LEGACY_SINGLE_PASS: "此任务采用单次生成流程，审核发现由管理员处理。",
  };
  return reason ? messages[reason] ?? "流程已停止，请核对任务记录。" : null;
}

export function creativeRoundLabel(round: Pick<CreativeRound, "round_index">) {
  return round.round_index === 0 ? "初稿" : `第 ${round.round_index} 轮修稿`;
}

export function creativeRoundStateLabel(state: CreativeRound["state"]) {
  return state === "COMPLETED" ? "编译与审核结束" : stateLabels[state] ?? "状态待核对";
}

export function changedSectionLabels(fields: string[]) {
  const labels: Record<string, string> = {
    introduction: "公开开场",
    characters: "角色资料与私本",
    evidence: "物证",
    truth: "案件真相",
    ending: "结局说明",
  };
  return [...new Set(fields.map((field) => {
    const section = field.replace(/^\//, "").split(/[./\[]/, 1)[0];
    return labels[section] ?? "剧本内容";
  }))];
}
