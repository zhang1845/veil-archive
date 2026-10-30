import type { CharacterBrief, DepositionEntry } from "@/types/play";

export const MOCK_TITLE = "孽岛疑云";

export const MOCK_DEPOSITIONS: DepositionEntry[] = [
  {
    id: "d1",
    seq: 7,
    speaker: "顾二",
    text: "那天夜里我确实不在书房……我从晚饭后就一直在前院和车夫说话。",
  },
  {
    id: "d2",
    seq: 8,
    speaker: "你",
    self: true,
    text: "那你当时在哪？",
  },
  {
    id: "d3",
    seq: 9,
    speaker: "唐小姐",
    text: "顾二在撒谎。晚饭后我见过他，就站在书房外头的回廊下。",
  },
  {
    id: "d4",
    seq: 10,
    speaker: "你",
    self: true,
    text: "唐小姐，你亲眼看见他站在回廊下？",
  },
];

export const MOCK_CHARACTERS: CharacterBrief[] = [
  { id: "tang", name: "唐小姐", self: true },
  { id: "gu", name: "顾二" },
  { id: "wang", name: "王管家" },
  { id: "shen", name: "沈先生" },
];

export const MOCK_EVIDENCE = [
  { id: "e1", title: "书房门锁", note: "门锁有撬动痕迹" },
  { id: "e2", title: "半封信", note: "落款「顾」字，被撕去一半" },
  { id: "e3", title: "怀表", note: "停在夜里十一点十七分" },
  { id: "e4", title: "回廊脚印", note: "泥迹，朝书房方向" },
];

export const MOCK_MEMORIES = [
  { id: "m1", title: "顾二的旧账", note: "他欠着沈先生一笔钱" },
  { id: "m2", title: "那晚的咳嗽声", note: "你听见书房里有人咳嗽" },
];

export const MOCK_NOTES = [
  { id: "n1", text: "怀表停在 23:17，与顾二说的离开时间对不上。" },
];
