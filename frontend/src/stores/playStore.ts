import { create } from "zustand";
import type { ActId } from "@/types/play";

export type ArchiveTab = "characters" | "evidence" | "memories" | "notes";

/** 底部行动模式：idle=无输入；discuss=公开讨论；ask=提问/私聊。 */
export type ActionMode = "idle" | "discuss" | "ask";

interface PlayUiState {
  archiveOpen: boolean;
  activeArchiveTab: ArchiveTab;
  currentAct: ActId;
  actionMode: ActionMode;
  toggleArchive: () => void;
  setArchiveTab: (tab: ArchiveTab) => void;
  setCurrentAct: (act: ActId) => void;
  setActionMode: (mode: ActionMode) => void;
}

export const usePlayUiStore = create<PlayUiState>((set) => ({
  archiveOpen: true,
  activeArchiveTab: "characters",
  currentAct: "investigation",
  actionMode: "idle",
  toggleArchive: () => set((s) => ({ archiveOpen: !s.archiveOpen })),
  setArchiveTab: (tab) => set({ activeArchiveTab: tab }),
  setCurrentAct: (act) => set({ currentAct: act }),
  setActionMode: (mode) => set({ actionMode: mode }),
}));
