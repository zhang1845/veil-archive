import type { DepositionEntry } from "@/types/play";
import { cn } from "@/lib/utils";

export function Deposition({ entry }: { entry: DepositionEntry }) {
  const { seq, speaker, self, text } = entry;
  return (
    <div className="flex gap-4">
      <span className="w-9 shrink-0 pt-0.5 text-right font-mono text-[12px] leading-6 text-muted">
        #{String(seq).padStart(2, "0")}
      </span>
      <div className={cn("min-w-0", self && "border-l-2 border-accent pl-4")}>
        <div
          className={cn(
            "text-[13px] font-medium",
            self ? "text-accent" : "text-body",
          )}
        >
          {speaker}
        </div>
        <p className="mt-1 text-[15px] leading-relaxed text-body">{text}</p>
      </div>
    </div>
  );
}
