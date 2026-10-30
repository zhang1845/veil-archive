"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { BookOpen, Check, RefreshCw, Sparkles } from "lucide-react";
import * as api from "@/services/admin";
import { SiteHeader } from "@/components/brand/SiteHeader";
import { errorCode } from "@/lib/http";
import { newKey } from "@/lib/id";
import { CLOUDBASE_FREE_MODE } from "@/lib/config";
import {
  activeCreativeStates,
  creativeStateLabel,
  creativeStopMessage,
} from "@/lib/creative";
import { CreativeRoundHistory } from "@/components/admin/CreativeRoundHistory";
import type {
  Candidate,
  CandidateVersion,
  CreativeJob,
  ModelDecision,
  PublicationGate,
} from "@/types/admin";

const dimensions = [
  ["PROVENANCE", "原创与来源", "确认内容来源与使用权，核对生成稿与候选内容。"],
  ["TIMELINE", "时间线", "核对人物行踪、作案时间与不在场证明没有矛盾。"],
  ["EVIDENCE", "证据链", "确认线索足够推导真相，误导可以被证据排除。"],
  [
    "KNOWLEDGE_BOUNDARY",
    "角色知识边界",
    "逐个角色检查私本，确保不会提前获知他人秘密。",
  ],
  ["PLAYABILITY", "可玩性", "按不同角色走完阅读、调查、终局，确认能够完成。"],
];
export default function AdminConsole() {
  const [tab, setTab] = useState("generate");
  const [jobs, setJobs] = useState<CreativeJob[]>([]);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [title, setTitle] = useState("");
  const [theme, setTheme] = useState("");
  const [count, setCount] = useState(4);
  const [maxRevisionRounds, setMaxRevisionRounds] = useState(2);
  const [costCeiling, setCostCeiling] = useState<"0.20" | "0.50" | "1.00">("0.50");
  const [selected, setSelected] = useState<number | null>(null);
  const [version, setVersion] = useState<CandidateVersion | null>(null);
  const [gate, setGate] = useState<PublicationGate | null>(null);
  const [checks, setChecks] = useState<string[]>([]);
  const [summary, setSummary] = useState("");
  const [findings, setFindings] = useState("[]");
  const [approvalNote, setApprovalNote] = useState("");
  const [decisions, setDecisions] = useState<Record<string, ModelDecision>>({});
  const [manualNotes, setManualNotes] = useState<Record<string, string>>({});
  const generationKey = useRef(newKey());
  const selection = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [j, c] = await Promise.all([
        api.listCreativeJobs(),
        api.listReviewCandidates(),
      ]);
      setJobs(j);
      setCandidates(c);
      setError("");
    } catch (err) {
      setError(errorCode(err));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    const timer = setTimeout(() => void refresh(), 0);
    return () => clearTimeout(timer);
  }, [refresh]);
  useEffect(() => {
    if (!jobs.some((j) => activeCreativeStates.has(j.state))) return;
    const timer = setInterval(() => {
      void refresh();
    }, 5000);
    return () => clearInterval(timer);
  }, [jobs, refresh]);

  const readCandidate = async (id: number) => {
    selection.current = id;
    setSelected(id);
    setVersion(null);
    setGate(null);
    setChecks([]);
    setSummary("");
    setFindings("[]");
    setDecisions({});
    setApprovalNote("");
    setTab("review");
    try {
      const [v, g] = await Promise.all([
        api.getCandidate(id),
        api.getPublication(id),
      ]);
      if (selection.current === id) {
        setVersion(v);
        setGate(g);
      }
    } catch (err) {
      setError(errorCode(err));
    }
  };
  const reloadGate = async () => {
    if (selected) setGate(await api.getPublication(selected));
  };
  const run = async (operation: () => Promise<unknown>, message: string) => {
    if (busy) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await operation();
      setNotice(message);
      await refresh();
    } catch (err) {
      setError(errorCode(err));
    } finally {
      setBusy(false);
    }
  };
  const requiredFindings =
    gate?.model_reports.flatMap((r) =>
      (r.report?.findings ?? [])
        .filter((f) => f.severity === "BLOCKER" || f.severity === "WARNING")
        .map((f) => ({ ...f, jobId: r.job_id })),
    ) ?? [];
  const decisionsReady = requiredFindings.every((f) => {
    const d = decisions[`${f.jobId}:${f.id}`];
    return (
      d?.note.trim() && (f.severity !== "BLOCKER" || d.status === "DISMISSED")
    );
  });

  return (
    <>
      <SiteHeader section="studio" />
      <div className="studio-shell">
        <header className="studio-header flex flex-wrap items-start justify-between gap-4">
          <div>
            <p className="eyebrow">
              <span />
              CREATIVE STUDIO / 创作室
            </p>
            <h1 className="mt-5 text-3xl tracking-tight text-ink">
              剧本工作台
            </h1>
            <p className="mt-2 text-sm text-muted">
              把一个故事想法，变成玩家可以进入的案件。
            </p>
          </div>
          <div className="flex gap-3">
            <Link className="btn-secondary" href="/login">
              切换管理账号
            </Link>
            <button className="btn-secondary" onClick={() => void refresh()}>
              <RefreshCw size={15} />
              刷新
            </button>
          </div>
        </header>
        <nav
          aria-label="工作台环节"
          className="studio-tabs flex flex-wrap gap-2"
        >
          {[
            ["generate", "01 AI 创作"],
            ["review", "02 审核与发布"],
          ].map(([id, label]) => (
            <button
              key={id}
              aria-current={tab === id ? "page" : undefined}
              className={tab === id ? "btn-primary" : "btn-secondary"}
              onClick={() => setTab(id)}
            >
              {label}
            </button>
          ))}
        </nav>
        {error && (
          <div
            role="alert"
            className="panel mb-5 border-danger/50 text-sm text-danger"
          >
            操作未完成 · {error}
            <p className="mt-2">
              <Link href="/login" className="underline">
                登录管理员账号
              </Link>
              ；若审核依据已变化，请重新读取候选。
            </p>
          </div>
        )}
        {notice && (
          <p role="status" className="mb-5 text-sm text-accent">
            {notice}
          </p>
        )}
        {loading && <p role="status">读取工作台…</p>}
        {tab === "generate" && (
          <div className="grid grid-cols-1 gap-8 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
            {CLOUDBASE_FREE_MODE && (
              <aside id="cloudbase-creative-status" className="panel border-accent/50 lg:col-span-2" role="note">
                <h2 className="text-lg text-accent">当前免费试玩环境 · AI 创作尚未接通</h2>
                <p className="mt-2 text-sm leading-7 text-muted">
                  AI 创作、自动修稿与模型复审尚未接通，暂时不能创建生成任务。你仍可查看已有候选并完成人工审核。
                </p>
                <button type="button" className="btn-secondary mt-4" onClick={() => setTab("review")}>
                  查看候选与人工审核
                </button>
              </aside>
            )}
            <form
              className="panel space-y-5"
              onSubmit={(e) => {
                e.preventDefault();
                if (CLOUDBASE_FREE_MODE) return;
                void run(async () => {
                  await api.createCreativeJob({
                    idempotency_key: generationKey.current,
                    title,
                    theme,
                    player_count: count,
                    max_revision_rounds: maxRevisionRounds,
                    cost_ceiling_cny: costCeiling,
                  });
                  generationKey.current = newKey();
                  setTitle("");
                  setTheme("");
                }, "创作任务已排队，完成后会出现在审核候选中。");
              }}
            >
              <h2 className="flex items-center gap-2 text-xl text-ink">
                <Sparkles size={20} />
                新建原创剧本
              </h2>
              <label className="block text-sm">
                剧本标题
                <input
                  required
                  maxLength={100}
                  className="field mt-2"
                  placeholder="例如：雨夜末班车"
                  value={title}
                  onChange={(e) => {
                    setTitle(e.target.value);
                    generationKey.current = newKey();
                  }}
                />
              </label>
              <label className="block text-sm">
                题材与创作要求
                <textarea
                  required
                  minLength={5}
                  maxLength={1200}
                  rows={6}
                  className="field mt-2 resize-y"
                  placeholder="故事发生在哪里？希望有哪些人物关系、悬念和氛围？"
                  value={theme}
                  onChange={(e) => {
                    setTheme(e.target.value);
                    generationKey.current = newKey();
                  }}
                />
              </label>
              <label className="block text-sm">
                角色数量
                <select
                  className="field mt-2"
                  value={count}
                  onChange={(e) => {
                    setCount(Number(e.target.value));
                    generationKey.current = newKey();
                  }}
                >
                  {[3, 4, 5, 6].map((n) => (
                    <option key={n} value={n}>
                      {n} 个角色 · 1 名真人，其余由 AI 扮演
                    </option>
                  ))}
                </select>
              </label>
              <fieldset className="space-y-4 rounded-md border border-line p-4">
                <legend className="px-1 text-sm text-ink">自动修稿与复审</legend>
                <label className="block text-sm">
                  最多自动修稿
                  <select
                    className="field mt-2"
                    value={maxRevisionRounds}
                    onChange={(e) => {
                      setMaxRevisionRounds(Number(e.target.value));
                      generationKey.current = newKey();
                    }}
                  >
                    {[0, 1, 2, 3].map((rounds) => (
                      <option key={rounds} value={rounds}>
                        {rounds === 0 ? "关闭 · 审核后交给管理员" : `${rounds} 轮`}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="block text-sm">
                  整个任务的费用上限
                  <select
                    className="field mt-2"
                    value={costCeiling}
                    onChange={(e) => {
                      setCostCeiling(e.target.value as typeof costCeiling);
                      generationKey.current = newKey();
                    }}
                  >
                    {(["0.20", "0.50", "1.00"] as const).map((cost) => (
                      <option key={cost} value={cost}>¥{cost}</option>
                    ))}
                  </select>
                </label>
                <p className="text-xs leading-relaxed text-muted">
                  发现警告或阻断问题后，AI 按审核意见修改，再重新编译与审核。达到轮数或预算上限、稿件没有变化时停止，保留每一版供你检查。
                </p>
              </fieldset>
              <p className="text-sm leading-relaxed text-muted">
                {maxRevisionRounds > 0
                  ? `生成初稿后最多自动修稿 ${maxRevisionRounds} 轮，总费用上限 ¥${costCeiling}。`
                  : `生成初稿并审核，总费用上限 ¥${costCeiling}。`}
                所有调用共用这一上限；预算不足时可能提前停止。AI 流程结束后，仍由你完成人工审核、确认和发布。
              </p>
              <button
                disabled={
                  CLOUDBASE_FREE_MODE || busy || loading || !title.trim() || theme.trim().length < 5
                }
                aria-describedby={CLOUDBASE_FREE_MODE ? "cloudbase-creative-status" : undefined}
                className="btn-primary"
              >
                {busy ? "提交中…" : "创建生成任务"}
              </button>
              <details className="text-sm text-muted">
                <summary className="cursor-pointer">生成服务运行说明</summary>
                {CLOUDBASE_FREE_MODE ? (
                  <p className="mt-3 leading-relaxed">
                    当前云端试玩仅开放候选查看与人工审核，AI 创作和自动修稿暂不可用。下方保留已有创作记录。
                  </p>
                ) : (
                  <>
                    <p className="mt-3 leading-relaxed">
                      需先配置百炼模型密钥与单价，并由本机生成服务处理队列。在项目目录执行下方命令并保持运行。
                    </p>
                    <code className="mt-3 block break-all rounded bg-inset p-3">
                      backend/.venv/bin/python backend/scripts/creative_worker.py
                      --allow-paid --watch
                    </code>
                    <p className="mt-2">
                      新任务会自动处理；每一轮修稿使用独立版本，历史稿件与审核意见保留。中断或用量未知时停止，原调用不会自动重试，需核对后处理。
                    </p>
                  </>
                )}
              </details>
            </form>
            <section>
              <h2 className="mb-4 text-xl text-ink">创作记录</h2>
              <div className="space-y-3">
                {!loading && !jobs.length && (
                  <div className="panel text-sm text-muted">
                    {CLOUDBASE_FREE_MODE
                      ? "当前环境暂无创作任务。可切换至审核与发布，查看已有候选。"
                      : "还没有任务。从左侧写下第一个故事想法。"}
                  </div>
                )}
                {jobs.map((job) => (
                  <article key={job.id} className="panel">
                    <div className="flex flex-wrap justify-between gap-2">
                      <h3 className="text-ink">{job.request.title}</h3>
                      <span className="text-sm text-accent">
                        {creativeStateLabel(job)}
                      </span>
                    </div>
                    <p className="mt-2 text-sm text-muted">
                      {job.request.player_count} 个角色 · 已计入 ¥
                      {job.charged_cost_cny}
                      {" / 上限 ¥"}{job.cost_ceiling_cny}
                    </p>
                    <p className="mt-2 text-xs leading-relaxed text-muted">
                      费用包含调用前预占；用量未知时保留预占，核对后以供应商账单为准。
                    </p>
                    <p className="mt-2 text-xs text-muted">
                      {job.request.max_revision_rounds === undefined
                        ? "单次生成任务"
                        : `最多自动修稿 ${job.request.max_revision_rounds} 轮`}
                      {job.current_round !== undefined && job.current_round > 0
                        ? ` · 当前第 ${job.current_round} 轮修稿`
                        : ""}
                    </p>
                    <p className="mt-2 break-all text-xs text-muted">
                      {job.id}
                    </p>
                    {creativeStopMessage(job.stop_reason) && (
                      <p className="mt-3 text-sm leading-relaxed text-body">
                        {creativeStopMessage(job.stop_reason)}
                      </p>
                    )}
                    {job.error_code && (
                      <p className="mt-3 break-all text-sm text-danger">
                        {job.error_code} ·
                        请核对任务收据与配置，勿重复提交未知调用。
                      </p>
                    )}
                    <CreativeRoundHistory
                      rounds={job.rounds ?? []}
                      onReadCandidate={(id) => void readCandidate(id)}
                    />
                    <div className="mt-4 flex gap-2">
                      {job.candidate_version_id && (
                        <button
                          className="btn-secondary"
                          onClick={() =>
                            void readCandidate(job.candidate_version_id!)
                          }
                        >
                          查看最新候选与审核
                        </button>
                      )}
                      {job.state === "QUEUED" && (
                        <button
                          className="btn-secondary"
                          disabled={busy}
                          onClick={() =>
                            void run(
                              () => api.cancelCreativeJob(job.id),
                              "任务已取消。",
                            )
                          }
                        >
                          取消排队
                        </button>
                      )}
                    </div>
                  </article>
                ))}
              </div>
            </section>
          </div>
        )}
        {tab === "review" && (
          <div className="grid grid-cols-1 gap-6 lg:grid-cols-[240px_minmax(0,1fr)]">
            <aside className="space-y-2">
              <h2 className="mb-3 text-lg text-ink">候选剧本</h2>
              {!candidates.length && (
                <p className="text-sm text-muted">生成完成后在这里审核。</p>
              )}
              {candidates.map((c) => (
                <button
                  key={c.id}
                  onClick={() => void readCandidate(c.id)}
                  className={`block w-full rounded-lg border p-4 text-left ${selected === c.id ? "border-accent bg-surface" : "border-line"}`}
                >
                  <span className="block text-sm text-ink">{c.title}</span>
                  <span className="mt-1 block text-xs text-muted">
                    {c.content_version} · #{c.id}
                  </span>
                </button>
              ))}
            </aside>
            <div className="min-w-0 space-y-5">
              {!selected && (
                <div className="panel text-sm text-muted">
                  选择一份候选，阅读全文、核对模型意见，再记录人工审核。
                </div>
              )}
              {selected && !version && (
                <div role="status" className="panel">
                  正在读取候选…
                </div>
              )}
              {version && gate && (
                <>
                  <section className="panel">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <h2 className="text-xl text-ink">
                        {gate.candidate.title}
                      </h2>
                      <button
                        className="btn-secondary"
                        disabled={busy}
                        onClick={() => void run(reloadGate, "审核依据已刷新。")}
                      >
                        重新读取审核状态
                      </button>
                    </div>
                    <p className="mt-2 text-sm text-muted">
                      先检查全文与各角色视角。需要修改正文时，应重新生成或导入新版本，已发布版本保持固定。
                    </p>
                    <div className="mt-5 space-y-3">
                      {Object.entries(version.package)
                        .filter(([k]) =>
                          [
                            "introduction",
                            "characters",
                            "phases",
                            "knowledge",
                            "evidence",
                            "truth",
                            "settlement",
                            "mechanics",
                          ].includes(k),
                        )
                        .map(([key, value]) => (
                          <details
                            key={key}
                            className="rounded-md border border-line p-3"
                          >
                            <summary className="cursor-pointer text-sm text-ink">
                              {
                                (
                                  {
                                    introduction: "公开开场",
                                    characters: "角色",
                                    phases: "阶段",
                                    knowledge: "角色知识与私本",
                                    evidence: "物证",
                                    truth: "系统真相",
                                    settlement: "结算说明",
                                    mechanics: "调查规则",
                                  } as Record<string, string>
                                )[key]
                              }
                            </summary>
                            <pre className="mt-3 max-h-96 overflow-auto whitespace-pre-wrap break-all text-xs leading-relaxed text-body">
                              {JSON.stringify(value, null, 2)}
                            </pre>
                          </details>
                        ))}
                    </div>
                  </section>
                  <section className="panel">
                    <h2 className="text-lg text-ink">模型审核意见</h2>
                    {!gate.model_reports.length && (
                      <p className="mt-3 text-sm text-muted">
                        模型审核尚未完成，不能发布。
                      </p>
                    )}
                    {gate.model_reports.map((report) => (
                      <div key={report.job_id} className="mt-4">
                        <p className="text-sm">
                          任务 #{report.job_id} · {report.state}
                        </p>
                        <p className="mt-1 text-sm text-muted">
                          {report.report?.summary ?? "尚无有效报告"}
                        </p>
                        {report.report?.findings.map((f) => (
                          <div
                            key={f.id}
                            className="mt-3 rounded-md border border-line p-3"
                          >
                            <p className="text-sm text-ink">
                              {f.severity} · {f.message}
                            </p>
                            <p className="mt-1 text-xs text-muted">
                              {f.target.collection} / {f.target.id ?? "全文"}
                            </p>
                          </div>
                        ))}
                      </div>
                    ))}
                    {requiredFindings.map((f) => {
                      const key = `${f.jobId}:${f.id}`;
                      const d = decisions[key];
                      return (
                        <div
                          className="mt-4 space-y-2 border-t border-line pt-4"
                          key={key}
                        >
                          <label className="block text-sm">
                            处理 {f.id} · {f.severity}
                            <select
                              aria-label={`处理意见 ${f.id}`}
                              className="field mt-2"
                              value={d?.status ?? ""}
                              onChange={(e) =>
                                setDecisions({
                                  ...decisions,
                                  [key]: {
                                    job_id: f.jobId,
                                    finding_id: f.id,
                                    note: d?.note ?? "",
                                    status: e.target
                                      .value as ModelDecision["status"],
                                  },
                                })
                              }
                            >
                              <option value="" disabled>
                                选择处理意见
                              </option>
                              {f.severity !== "BLOCKER" && (
                                <option value="ACKNOWLEDGED">
                                  知悉并接受警告
                                </option>
                              )}
                              <option value="DISMISSED">
                                核对后认定为误报
                              </option>
                            </select>
                          </label>
                          <textarea
                            aria-label={`处理理由 ${f.id}`}
                            className="field"
                            maxLength={4000}
                            placeholder="写明核对依据；真实问题请先修正为新候选，不要用排除意见替代修复。"
                            value={d?.note ?? ""}
                            onChange={(e) =>
                              setDecisions({
                                ...decisions,
                                [key]: {
                                  job_id: f.jobId,
                                  finding_id: f.id,
                                  status: d?.status ?? "DISMISSED",
                                  note: e.target.value,
                                },
                              })
                            }
                          />
                        </div>
                      );
                    })}
                  </section>
                  <form
                    className="panel space-y-4"
                    onSubmit={(e) => {
                      e.preventDefault();
                      void run(async () => {
                        const parsed = JSON.parse(findings);
                        if (!Array.isArray(parsed))
                          throw new Error("审核发现必须是 JSON 数组");
                        await api.saveManualAudit(selected!, {
                          idempotency_key: newKey(),
                          expected_package_hash: version.package_hash,
                          bundle_hash: gate.bundle_hash,
                          report: {
                            schema_version:
                              (
                                {
                                  "script-package/1.2": "script-audit/1.1",
                                  "script-package/1.3": "script-audit/1.2",
                                } as Record<string, string>
                              )[gate.candidate.contract_version] ??
                              "script-audit/1.0",
                            summary,
                            coverage: checks,
                            findings: parsed,
                          },
                        });
                        await reloadGate();
                        setChecks([]);
                        setSummary("");
                        setFindings("[]");
                      }, "人工审核记录已保存；尚未批准或发布。");
                    }}
                  >
                    <h2 className="text-lg text-ink">记录人工审核</h2>
                    {dimensions.map(([id, label, hint]) => (
                      <label
                        key={id}
                        className="flex items-start gap-3 text-sm"
                      >
                        <input
                          type="checkbox"
                          checked={checks.includes(id)}
                          onChange={(e) =>
                            setChecks(
                              e.target.checked
                                ? [...checks, id]
                                : checks.filter((c) => c !== id),
                            )
                          }
                          className="mt-1"
                        />
                        <span>
                          {label}
                          <span className="mt-1 block text-xs leading-relaxed text-muted">
                            {hint}
                          </span>
                        </span>
                      </label>
                    ))}
                    <label className="block text-sm">
                      审核结论
                      <textarea
                        required
                        maxLength={4000}
                        className="field mt-2"
                        rows={3}
                        placeholder="说明实际检查了哪些内容、试玩结果，以及尚存的问题。"
                        value={summary}
                        onChange={(e) => setSummary(e.target.value)}
                      />
                    </label>
                    <details>
                      <summary className="cursor-pointer text-sm text-muted">
                        记录发现的问题（JSON）
                      </summary>
                      <p className="mt-2 text-xs text-muted">
                        有问题时填写
                        id、category、severity、target、message、sources，来源引用须与目标原文一致。无发现保留空数组。
                      </p>
                      <textarea
                        aria-label="人工审核发现"
                        className="field mt-2 font-mono"
                        rows={5}
                        value={findings}
                        onChange={(e) => setFindings(e.target.value)}
                      />
                    </details>
                    <button
                      className="btn-secondary"
                      disabled={
                        busy ||
                        checks.length !== 5 ||
                        !summary.trim() ||
                        !gate.bundle_hash ||
                        !!gate.release
                      }
                    >
                      保存人工审核记录
                    </button>
                  </form>
                  {gate.manual_reports.map((audit) => (
                    <section key={audit.id} className="panel">
                      <h3 className="text-sm text-ink">人工报告 #{audit.id}</h3>
                      <p className="mt-2 text-sm text-muted">
                        {audit.report.summary}
                      </p>
                      {audit.report.findings.map((f) => {
                        const key = `${audit.id}:${f.id}`;
                        const last = audit.dispositions
                          .filter((d) => d.finding_id === f.id)
                          .at(-1);
                        return (
                          <div className="mt-4 space-y-2" key={f.id}>
                            <p className="text-sm">
                              {f.severity} · {f.message} ·{" "}
                              {last?.status ?? "OPEN"}
                            </p>
                            <input
                              className="field"
                              aria-label={`人工问题处理理由 ${key}`}
                              placeholder="核对后填写处理理由"
                              value={manualNotes[key] ?? ""}
                              onChange={(e) =>
                                setManualNotes({
                                  ...manualNotes,
                                  [key]: e.target.value,
                                })
                              }
                            />
                            <div className="flex flex-wrap gap-2">
                              {(f.severity === "BLOCKER"
                                ? ["DISMISSED", "OPEN"]
                                : ["ACKNOWLEDGED", "DISMISSED", "OPEN"]
                              ).map((status) => (
                                <button
                                  key={status}
                                  className="btn-secondary"
                                  disabled={
                                    busy ||
                                    !manualNotes[key]?.trim() ||
                                    !!gate.release
                                  }
                                  onClick={() =>
                                    void run(async () => {
                                      await api.disposeFinding(audit.id, {
                                        idempotency_key: newKey(),
                                        expected_package_hash:
                                          audit.package_hash,
                                        expected_audit_hash: audit.audit_hash,
                                        expected_revision: audit.revision,
                                        finding_id: f.id,
                                        status,
                                        note: manualNotes[key],
                                      });
                                      await reloadGate();
                                    }, "问题处理已记录。")
                                  }
                                >
                                  {
                                    (
                                      {
                                        ACKNOWLEDGED: "知悉",
                                        DISMISSED: "排除误报",
                                        OPEN: "保持待处理",
                                      } as Record<string, string>
                                    )[status]
                                  }
                                </button>
                              ))}
                            </div>
                          </div>
                        );
                      })}
                    </section>
                  ))}
                  <section className="panel space-y-4">
                    <h2 className="text-lg text-ink">确认与发布</h2>
                    <ul className="space-y-2">
                      {gate.checks.map((c) => (
                        <li
                          className={`flex items-start gap-2 text-sm ${c.passed ? "text-body" : "text-danger"}`}
                          key={c.code}
                        >
                          <Check
                            size={15}
                            className="mt-0.5 shrink-0"
                            aria-hidden
                          />
                          {c.passed ? "已通过" : "待完成"} · {c.message}
                        </li>
                      ))}
                    </ul>
                    {gate.release ? (
                      <p role="status" className="text-accent">
                        {gate.release.publication_mode === "LOCAL_TRIAL"
                          ? "已加入本机剧本库，仅作为本地试玩版，尚未完成正式审核。"
                          : "版本已发布，玩家可在剧本库选择。"}
                        <Link href="/scripts" className="ml-2 underline">
                          查看剧本库
                        </Link>
                      </p>
                    ) : (
                      <>
                        <label className="block text-sm">
                          最终确认说明
                          <textarea
                            className="field mt-2"
                            maxLength={4000}
                            value={approvalNote}
                            onChange={(e) => setApprovalNote(e.target.value)}
                            placeholder="确认审核与问题处理已完成，说明发布依据。"
                          />
                        </label>
                        <div className="flex flex-wrap gap-3">
                          <button
                            className="btn-secondary"
                            disabled={
                              busy ||
                              !gate.can_approve ||
                              !decisionsReady ||
                              !approvalNote.trim()
                            }
                            onClick={() =>
                              void run(async () => {
                                await api.approvePublication(selected!, {
                                  idempotency_key: newKey(),
                                  expected_package_hash: version.package_hash,
                                  bundle_hash: gate.bundle_hash,
                                  expected_basis_hash: gate.basis_hash,
                                  model_dispositions: requiredFindings.map(
                                    (f) => decisions[`${f.jobId}:${f.id}`],
                                  ),
                                  note: approvalNote,
                                });
                                await reloadGate();
                              }, "已保存发布确认，请核对后执行发布。")
                            }
                          >
                            <Check size={15} />
                            确认审核结果
                          </button>
                          <button
                            className="btn-primary"
                            disabled={busy || !gate.can_publish}
                            onClick={() =>
                              void run(async () => {
                                await api.publishCandidate(selected!, {
                                  idempotency_key: newKey(),
                                  approval_id: gate.approval!.id,
                                  expected_approval_hash:
                                    gate.approval!.approval_hash,
                                  expected_basis_hash: gate.basis_hash,
                                });
                                await reloadGate();
                              }, "剧本已发布到玩家剧本库。")
                            }
                          >
                            <BookOpen size={15} />
                            发布到玩家剧本库
                          </button>
                        </div>
                      </>
                    )}
                  </section>
                </>
              )}
            </div>
          </div>
        )}
      </div>
    </>
  );
}
