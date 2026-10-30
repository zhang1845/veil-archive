"use client";

import { Archive, RotateCcw } from "lucide-react";
import { ACTS } from "@/types/play";
import { usePlayUiStore } from "@/stores/playStore";
import { cn } from "@/lib/utils";

export function PhaseBar({
  title,
  points,
}: {
  title: string;
  points?: number;
}) {
  const currentAct = usePlayUiStore((s) => s.currentAct);
  const toggleArchive = usePlayUiStore((s) => s.toggleArchive);
  const currentIndex = ACTS.findIndex((a) => a.id === currentAct);

  return (
    <header className="flex h-12 shrink-0 items-center justify-between border-b border-line/60 px-5">
      <div className="flex items-center gap-5">
        <span className="text-[14px] font-medium text-ink">{title}</span>

        <nav className="flex items-center gap-1.5 text-[12px]">
          {ACTS.map((act, i) => {
            const state =
              i < currentIndex
                ? "done"
                : i === currentIndex
                  ? "current"
                  : "future";
            return (
              <span key={act.id} className="flex items-center gap-1.5">
                {i > 0 && <span className="text-strong-line">▸</span>}
                <span
                  className={cn(
                    state === "current" && "text-accent",
                    state === "done" && "text-muted",
                    state === "future" && "text-muted/60",
                  )}
                >
                  {act.label}
                </span>
              </span>
            );
          })}
        </nav>

        {typeof points === "number" && (
          <span className="font-mono text-[11px] text-muted">
            剩余 {points} 调查点
          </span>
        )}
      </div>

      <div className="flex items-center gap-4 text-[13px] text-body">
        <button
          onClick={toggleArchive}
          className="inline-flex items-center gap-1.5 transition-colors hover:text-ink"
        >
          <Archive size={14} strokeWidth={2} />
          档案
        </button>
        <button className="inline-flex items-center gap-1.5 transition-colors hover:text-ink">
          <RotateCcw size={14} strokeWidth={2} />
          回看上一轮
        </button>
        <button className="text-muted transition-colors hover:text-danger">
          退出本局
        </button>
      </div>
    </header>
  );
}
