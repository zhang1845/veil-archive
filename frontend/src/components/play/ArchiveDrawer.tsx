"use client";

import { X } from "lucide-react";
import { usePlayUiStore, type ArchiveTab } from "@/stores/playStore";
import {
  MOCK_CHARACTERS,
  MOCK_EVIDENCE,
  MOCK_MEMORIES,
  MOCK_NOTES,
} from "@/lib/mock";
import { cn } from "@/lib/utils";

const TABS: { id: ArchiveTab; label: string }[] = [
  { id: "characters", label: "角色" },
  { id: "evidence", label: "线索" },
  { id: "memories", label: "回忆" },
  { id: "notes", label: "手记" },
];

export function ArchiveDrawer() {
  const activeTab = usePlayUiStore((s) => s.activeArchiveTab);
  const setArchiveTab = usePlayUiStore((s) => s.setArchiveTab);
  const toggleArchive = usePlayUiStore((s) => s.toggleArchive);

  return (
    <aside className="flex w-72 shrink-0 flex-col border-l border-line/60 bg-surface/30">
      <div className="flex items-center justify-between border-b border-line/60 px-4 py-3">
        <span className="font-mono text-[11px] tracking-widest text-muted">
          档案
        </span>
        <button
          onClick={toggleArchive}
          className="text-muted transition-colors hover:text-ink"
          aria-label="收起档案"
        >
          <X size={14} strokeWidth={2} />
        </button>
      </div>

      <div className="flex gap-1 border-b border-line/60 px-2 py-2">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            onClick={() => setArchiveTab(tab.id)}
            className={cn(
              "rounded-sm px-2.5 py-1 text-[12px] transition-colors",
              activeTab === tab.id
                ? "bg-inset text-ink"
                : "text-muted hover:text-body",
            )}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <div className="flex-1 space-y-1 overflow-y-auto p-3">
        {activeTab === "characters" &&
          MOCK_CHARACTERS.map((c) => (
            <div
              key={c.id}
              className={cn(
                "rounded-md px-2.5 py-2 text-[13px]",
                c.self ? "text-accent" : "text-body hover:bg-inset",
              )}
            >
              {c.name}
              {c.self && <span className="ml-2 text-[11px] text-muted">（你）</span>}
            </div>
          ))}

        {activeTab === "evidence" &&
          MOCK_EVIDENCE.map((e) => (
            <div
              key={e.id}
              className="rounded-md border border-line/50 px-2.5 py-2.5"
            >
              <div className="text-[13px] font-medium text-ink">{e.title}</div>
              <div className="mt-0.5 text-[12px] text-muted">{e.note}</div>
            </div>
          ))}

        {activeTab === "memories" &&
          MOCK_MEMORIES.map((m) => (
            <div
              key={m.id}
              className="rounded-md border border-secondary/30 px-2.5 py-2.5"
            >
              <div className="text-[13px] font-medium text-body">{m.title}</div>
              <div className="mt-0.5 text-[12px] text-muted">{m.note}</div>
            </div>
          ))}

        {activeTab === "notes" &&
          (MOCK_NOTES.length ? (
            MOCK_NOTES.map((n) => (
              <div
                key={n.id}
                className="rounded-md border border-line/50 px-2.5 py-2.5 text-[13px] text-body"
              >
                {n.text}
              </div>
            ))
          ) : (
            <p className="px-2 py-4 text-[13px] text-muted">还没有手记。</p>
          ))}
      </div>
    </aside>
  );
}
