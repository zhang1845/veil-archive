import { http } from "@/lib/http";
import type {
  ActionBody,
  AskBody,
  CreatePlayBody,
  CreateSessionBody,
  LibraryResult,
  OpeningSession,
  PlayView,
  Release,
  SpeakBody,
} from "@/types/api";

interface Envelope<T> {
  success: boolean;
  data: T;
}

async function unwrap<T>(
  promise: Promise<{ data: Envelope<T> }>,
): Promise<T> {
  const res = await promise;
  return res.data.data;
}

export function listReleases() {
  return unwrap<Release[]>(http.get("/api/fusion/package-releases"));
}

export function createSession(body: CreateSessionBody) {
  return unwrap<OpeningSession>(
    http.post("/api/fusion/package-sessions", body),
  );
}

export function getSession(sessionId: string) {
  return unwrap<OpeningSession>(
    http.get(`/api/fusion/package-sessions/${sessionId}`),
  );
}

export function findPlay(openingSessionId: string) {
  return unwrap<PlayView | null>(
    http.get("/api/fusion/package-plays", {
      params: { opening_session_id: openingSessionId },
    }),
  );
}

export function createPlay(body: CreatePlayBody) {
  return unwrap<PlayView>(http.post("/api/fusion/package-plays", body));
}

export function getPlay(playId: string) {
  return unwrap<PlayView>(http.get(`/api/fusion/package-plays/${playId}`));
}

export function act(playId: string, body: ActionBody) {
  return unwrap<PlayView>(
    http.post(`/api/fusion/package-plays/${playId}/actions`, body),
  );
}

export function ask(playId: string, body: AskBody) {
  return unwrap<PlayView>(
    http.post(`/api/fusion/package-plays/${playId}/ask`, body),
  );
}

export function speak(playId: string, body: SpeakBody) {
  return unwrap<PlayView>(
    http.post(`/api/fusion/package-plays/${playId}/discussion`, body),
  );
}

export function listLibrary(offset = 0, limit = 20) {
  return unwrap<LibraryResult>(
    http.get("/api/fusion/package-play-library", {
      params: { offset: String(offset), limit: String(limit) },
    }),
  );
}
