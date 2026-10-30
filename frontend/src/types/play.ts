export type PlayStatus = "TEXT_PLAY" | "SETTLED";

/** 三幕：阅读 → 调查 → 终局（视觉合并，映射后端多阶段） */
export type ActId = "reading" | "investigation" | "finale";

export interface Act {
  id: ActId;
  label: string;
}

export const ACTS: Act[] = [
  { id: "reading", label: "阅读" },
  { id: "investigation", label: "调查" },
  { id: "finale", label: "终局" },
];

/** 证词条目：一条叙事流消息（真人或 AI） */
export interface DepositionEntry {
  id: string;
  seq: number;
  speaker: string;
  self?: boolean;
  text: string;
}

export interface CharacterBrief {
  id: string;
  name: string;
  self?: boolean;
}

export interface ArchiveGroup {
  id: string;
  label: string;
  count?: number;
}
