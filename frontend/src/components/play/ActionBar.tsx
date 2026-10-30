"use client";

import { ChevronRight, Flag, HelpCircle, MessagesSquare, Phone, Search } from "lucide-react";
import { usePlayUiStore } from "@/stores/playStore";
import { usePlaySessionStore } from "@/stores/playSessionStore";

export function ActionBar({ contact = "唐小姐" }: { contact?: string }) {
  const currentAct = usePlayUiStore((s) => s.currentAct);
  const setActionMode = usePlayUiStore((s) => s.setActionMode);
  const play = usePlaySessionStore((s) => s.play);
  const performAction = usePlaySessionStore((s) => s.performAction);

  const available = play?.mechanics?.available_actions ?? [];
  const firstAction = available[0];

  const investigate = () => {
    if (firstAction) {
      void performAction("PERFORM_ACTION", { action_id: firstAction.id });
    }
  };

  const primary = (label: string, icon: React.ReactNode, onClick: () => void) => (
    <button
      onClick={onClick}
      className="inline-flex items-center gap-1.5 rounded-md bg-accent px-4 py-2 text-[14px] font-medium text-canvas transition-opacity hover:opacity-90"
    >
      {icon}
      {label}
    </button>
  );

  const secondary = (label: string, icon: React.ReactNode, onClick: () => void) => (
    <button
      onClick={onClick}
      className="inline-flex items-center gap-1.5 rounded-md border border-line px-4 py-2 text-[13px] text-body transition-colors hover:border-strong-line"
    >
      {icon}
      {label}
    </button>
  );

  if (play?.settled) {
    return (
      <footer className="shrink-0 border-t border-line/60 px-5 py-3">
        <div className="mx-auto flex max-w-2xl items-center gap-2 text-[13px] text-muted">
          <Flag size={14} />
          本局已结算
        </div>
      </footer>
    );
  }

  return (
    <footer className="shrink-0 border-t border-line/60 px-5 py-3">
      <div className="mx-auto flex max-w-2xl items-center gap-2">
        {currentAct === "reading" &&
          primary("继续阅读", <ChevronRight size={15} />, () =>
            void performAction("ADVANCE_PHASE"),
          )}

        {currentAct === "investigation" && (
          <>
            {primary(firstAction?.label ?? "调查", <Search size={15} />, investigate)}
            {secondary("公开讨论", <MessagesSquare size={15} />, () =>
              setActionMode("discuss"),
            )}
            {secondary(`私聊${contact}`, <Phone size={15} />, () =>
              setActionMode("ask"),
            )}
            {secondary("提问", <HelpCircle size={15} />, () =>
              setActionMode("ask"),
            )}
          </>
        )}

        {currentAct === "finale" &&
          primary("结算揭晓", <Flag size={15} />, () =>
            void performAction("SETTLE"),
          )}
      </div>
    </footer>
  );
}
