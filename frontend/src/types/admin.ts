export interface CreativeJob {
  id: string;
  request: {
    title: string;
    theme: string;
    player_count: number;
    max_revision_rounds?: number;
    cost_ceiling_cny?: "0.20" | "0.50" | "1.00";
  };
  state: string;
  error_code: string | null;
  authoring_job_id: number | null;
  candidate_version_id: number | null;
  charged_cost_cny: string;
  cost_ceiling_cny: string;
  workflow_version?: "creative-revision/1.0";
  current_round?: number;
  stop_reason?:
    | "CLEAN_AUDIT"
    | "ROUND_LIMIT"
    | "BUDGET_LIMIT"
    | "NO_CHANGE"
    | "PIPELINE_BLOCKED"
    | "RESULT_UNKNOWN"
    | "LEGACY_SINGLE_PASS"
    | null;
  rounds?: CreativeRound[];
}
export interface CreativeRound {
  round_index: number;
  content_version: string;
  state:
    | "GENERATING"
    | "REVISING"
    | "COMPILING"
    | "COMPLETED"
    | "BLOCKED"
    | "NEEDS_RECONCILIATION"
    | "NO_CHANGE";
  authoring_job_id: number | null;
  candidate_version_id: number | null;
  bundle_hash: string | null;
  draft_hash: string | null;
  audit_summary: string | null;
  findings: Finding[];
  report_hash: string | null;
  charged_cost_cny: string;
  changed_fields: string[];
  error_code: string | null;
}
export interface Candidate {
  id: number;
  title: string;
  content_version: string;
  contract_version: string;
  package_hash: string;
}
export interface Finding {
  id: string;
  category: string;
  severity: string;
  message: string;
  target: { collection: string; id: string | null };
  sources: { source_id: string; anchor?: string; page?: number }[];
}
export interface ModelDecision {
  job_id: number;
  finding_id: string;
  status: "ACKNOWLEDGED" | "DISMISSED";
  note: string;
}
export interface ManualAudit {
  id: number;
  audit_hash: string;
  package_hash: string;
  revision: number;
  report: { summary: string; findings: Finding[] };
  dispositions: { finding_id: string; status: string; note?: string }[];
}
export interface PublicationGate {
  basis_hash: string;
  bundle_hash: string | null;
  candidate: Candidate;
  checks: { code: string; passed: boolean; message: string }[];
  model_reports: {
    job_id: number;
    state: string;
    report: { summary: string; findings: Finding[] } | null;
  }[];
  manual_reports: ManualAudit[];
  can_approve: boolean;
  can_publish: boolean;
  approval: { id: number; approval_hash: string; valid: boolean } | null;
  release: { id: number; publication_mode?: "LOCAL_TRIAL" } | null;
}
export interface CandidateVersion {
  id: number;
  package_hash: string;
  package: Record<string, any>;
}
