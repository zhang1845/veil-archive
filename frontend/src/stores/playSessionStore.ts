import axios from "axios";
import { create } from "zustand";
import { ensureSession } from "@/services/auth";
import {
  act,
  ask,
  createPlay,
  findPlay,
  getPlay,
  getSession,
  speak,
} from "@/services/play";
import { newKey } from "@/lib/id";
import { errorCode } from "@/lib/http";
import type {
  ActionBody,
  AskBody,
  PlayActionName,
  PlayView,
  ReleaseCharacter,
  SpeakBody,
} from "@/types/api";

export type SessionMode = "connecting" | "live" | "error";
type Pending = { playId: string } & (
  | { kind: "act"; body: ActionBody }
  | { kind: "ask"; body: AskBody }
  | { kind: "speak"; body: SpeakBody }
);
interface PlaySessionState {
  mode: SessionMode;
  title: string;
  play: PlayView | null;
  characters: ReleaseCharacter[];
  error: string | null;
  busy: boolean;
  pending: Pending | null;
  bootstrap: (resume: {
    playId?: string;
    openingSessionId?: string;
  }) => Promise<void>;
  refresh: () => Promise<void>;
  retryPending: () => Promise<boolean>;
  performAction: (
    action: PlayActionName,
    target?: ActionBody["target"],
  ) => Promise<boolean>;
  speakText: (text: string) => Promise<boolean>;
  askQuestion: (characterId: string, question: string) => Promise<boolean>;
}
let loading: { key: string; promise: Promise<void> } | null = null;
let loadRevision = 0;
const pendingKey = (id: string) => `pending-command:${id}`;

export const usePlaySessionStore = create<PlaySessionState>((set, get) => {
  const execute = async (pending: Pending): Promise<boolean> => {
    if (get().busy) return false;
    set({ busy: true, error: null, pending });
    sessionStorage.setItem(pendingKey(pending.playId), JSON.stringify(pending));
    try {
      const play =
        pending.kind === "act"
          ? await act(pending.playId, pending.body)
          : pending.kind === "ask"
            ? await ask(pending.playId, pending.body)
            : await speak(pending.playId, pending.body);
      sessionStorage.removeItem(pendingKey(pending.playId));
      set({ play, pending: null, error: null });
      return true;
    } catch (err) {
      // Explicit client refusals can be corrected. Network/5xx outcomes must be
      // reconciled by replaying the SAME command, never issuing another AI call.
      if (
        axios.isAxiosError(err) &&
        err.response &&
        err.response.status >= 400 &&
        err.response.status < 500 &&
        err.response.status !== 408
      ) {
        sessionStorage.removeItem(pendingKey(pending.playId));
        set({ pending: null });
      }
      set({ error: errorCode(err) });
      return false;
    } finally {
      set({ busy: false });
    }
  };
  return {
    mode: "connecting",
    title: "",
    play: null,
    characters: [],
    error: null,
    busy: false,
    pending: null,
    bootstrap: async (resume) => {
      const key = JSON.stringify(resume);
      if (loading?.key === key) return loading.promise;
      const revision = ++loadRevision;
      const promise = (async () => {
        set({
          mode: "connecting",
          play: null,
          characters: [],
          error: null,
          pending: null,
        });
        try {
          await ensureSession();
          let play: PlayView;
          if (resume.playId) play = await getPlay(resume.playId);
          else if (resume.openingSessionId) {
            const opening = await getSession(resume.openingSessionId);
            play =
              (await findPlay(opening.session_id)) ??
              (await createPlay({
                opening_session_id: opening.session_id,
                idempotency_key: `play-${opening.session_id}`,
              }));
            sessionStorage.removeItem(
              `opening:${opening.release_id}:${opening.selected_character_id}`,
            );
          } else throw new Error("请先选择剧本和角色");
          if (revision !== loadRevision) return;
          let pending: Pending | null = null;
          try {
            pending = JSON.parse(
              sessionStorage.getItem(pendingKey(play.play_id)) ?? "null",
            );
          } catch {
            /* no saved command */
          }
          set({
            mode: "live",
            play,
            title: play.script?.title ?? "剧本",
            characters: play.characters ?? [],
            pending,
            error: pending ? "上次操作的结果尚未核对，请先核对原请求。" : null,
          });
        } catch (err) {
          if (revision === loadRevision)
            set({ mode: "error", error: errorCode(err) });
        }
      })();
      loading = { key, promise };
      try {
        await promise;
      } finally {
        if (loading?.promise === promise) loading = null;
      }
    },
    refresh: async () => {
      const { play, busy } = get();
      if (!play || busy) return;
      try {
        set({ play: await getPlay(play.play_id), error: null });
      } catch (err) {
        set({ error: errorCode(err) });
      }
    },
    retryPending: async () => {
      const p = get().pending;
      return p ? execute(p) : false;
    },
    performAction: async (action, target = null) => {
      const { play, pending, busy } = get();
      if (!play || pending || busy) return false;
      return execute({
        playId: play.play_id,
        kind: "act",
        body: {
          expected_revision: play.revision ?? 0,
          idempotency_key: newKey(),
          action,
          ...(target ? { target } : {}),
        },
      });
    },
    speakText: async (text) => {
      const { play, pending, busy } = get();
      if (!play || pending || busy) return false;
      return execute({
        playId: play.play_id,
        kind: "speak",
        body: {
          schema_version: "package-discussion-command/1.0",
          action: "SPEAK",
          expected_revision: play.revision ?? 0,
          idempotency_key: newKey(),
          text,
        },
      });
    },
    askQuestion: async (characterId, question) => {
      const { play, pending, busy } = get();
      if (!play || pending || busy) return false;
      if (play.public_preview || play.local_trial) {
        set({ error: "当前试玩未开启 AI 对话。可继续搜证、公开发言和推进阶段。" });
        return false;
      }
      return execute({
        playId: play.play_id,
        kind: "ask",
        body: {
          expected_revision: play.revision ?? 0,
          idempotency_key: newKey(),
          character_id: characterId,
          question,
        },
      });
    },
  };
});
