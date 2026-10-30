"use client";

import { useState } from "react";
import { usePlayUiStore } from "@/stores/playStore";

export function Composer({
  placeholder,
  onSend,
}: {
  placeholder: string;
  onSend: (text: string) => void;
}) {
  const setActionMode = usePlayUiStore((s) => s.setActionMode);
  const [value, setValue] = useState("");

  const submit = () => {
    const text = value.trim();
    if (!text) return;
    onSend(text);
    setValue("");
    setActionMode("idle");
  };

  return (
    <div className="shrink-0 border-t border-line/60 px-5 py-3">
      <div className="mx-auto flex max-w-2xl items-center gap-2">
        <input
          autoFocus
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
            if (e.key === "Escape") setActionMode("idle");
          }}
          placeholder={placeholder}
          className="flex-1 rounded-md border border-line bg-inset px-3 py-2 text-[14px] text-body outline-none placeholder:text-muted focus:border-body"
        />
        <button
          onClick={() => setActionMode("idle")}
          className="rounded-md border border-line px-3 py-2 text-[13px] text-body transition-colors hover:border-strong-line"
        >
          取消
        </button>
        <button
          onClick={submit}
          disabled={!value.trim()}
          className="rounded-md bg-accent px-4 py-2 text-[14px] font-medium text-canvas transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
        >
          发送
        </button>
      </div>
    </div>
  );
}
