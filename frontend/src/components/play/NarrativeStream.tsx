import type { DepositionEntry } from "@/types/play";
import { Deposition } from "@/components/play/Deposition";

export function NarrativeStream({ entries }: { entries: DepositionEntry[] }) {
  return (
    <div className="mx-auto max-w-2xl space-y-8">
      {entries.map((entry) => (
        <Deposition key={entry.id} entry={entry} />
      ))}
    </div>
  );
}
