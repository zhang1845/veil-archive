import { http } from "@/lib/http";
import type {
  Candidate,
  CandidateVersion,
  CreativeJob,
  PublicationGate,
} from "@/types/admin";

interface Envelope<T> {
  success: boolean;
  data: T;
}

async function unwrap<T>(promise: Promise<{ data: Envelope<T> }>): Promise<T> {
  const res = await promise;
  return res.data.data;
}

export function listAuthoringJobs() {
  return unwrap<unknown[]>(http.get("/api/admin/fusion/authoring-jobs"));
}

export function listReviewCandidates() {
  return unwrap<Candidate[]>(http.get("/api/admin/fusion/review-candidates"));
}

const base = "/api/admin/fusion";
export const listCreativeJobs = () =>
  unwrap<CreativeJob[]>(http.get(`${base}/creative-jobs`));
export const createCreativeJob = (body: {
  idempotency_key: string;
  title: string;
  theme: string;
  player_count: number;
  max_revision_rounds: number;
  cost_ceiling_cny: "0.20" | "0.50" | "1.00";
}) => unwrap<CreativeJob>(http.post(`${base}/creative-jobs`, body));
export const cancelCreativeJob = (id: string) =>
  unwrap<CreativeJob>(http.post(`${base}/creative-jobs/${id}/cancel`));
export const getCandidate = (id: number) =>
  unwrap<CandidateVersion>(http.get(`${base}/script-packages/${id}`));
export const getPublication = (id: number) =>
  unwrap<PublicationGate>(
    http.get(`${base}/script-packages/${id}/publication`),
  );
export const saveManualAudit = (id: number, body: object) =>
  unwrap(http.post(`${base}/script-packages/${id}/audits`, body));
export const approvePublication = (id: number, body: object) =>
  unwrap(http.post(`${base}/script-packages/${id}/approvals`, body));
export const publishCandidate = (id: number, body: object) =>
  unwrap(http.post(`${base}/script-packages/${id}/publish`, body));
export const disposeFinding = (id: number, body: object) =>
  unwrap(http.post(`${base}/script-audits/${id}/dispositions`, body));
export const submitPackage = (body: object) =>
  unwrap(http.post(`${base}/script-imports`, body));

export function listSourceBundles() {
  return unwrap<unknown[]>(http.get("/api/admin/fusion/source-bundles"));
}
