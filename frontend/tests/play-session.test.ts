import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import axios, { AxiosError, type InternalAxiosRequestConfig } from "axios";
import { http } from "../src/lib/http";
import { usePlaySessionStore } from "../src/stores/playSessionStore";
import type { PlayView } from "../src/types/api";

class MemoryStorage {
  values = new Map<string, string>();
  getItem(key: string) {
    return this.values.get(key) ?? null;
  }
  setItem(key: string, value: string) {
    this.values.set(key, value);
  }
  removeItem(key: string) {
    this.values.delete(key);
  }
}
const local = new MemoryStorage(),
  session = new MemoryStorage();
Object.assign(globalThis, {
  window: {},
  localStorage: local,
  sessionStorage: session,
});
const view: PlayView = {
  play_id: "play-qa",
  selected_character_id: "third",
  characters: [
    { id: "first", name: "甲" },
    { id: "third", name: "丙" },
  ],
  script: { title: "选中的故事" },
  revision: 2,
  settled: false,
  status: "TEXT_PLAY",
};
let requests: { url: string; body: any; auth: string }[];
let handler: (config: InternalAxiosRequestConfig) => Promise<any>;
const response = (config: InternalAxiosRequestConfig, data: any) => ({
  data: { success: true, data },
  status: 200,
  statusText: "OK",
  headers: {},
  config,
});

beforeEach(() => {
  local.values.clear();
  session.values.clear();
  local.setItem("access_token", "existing-admin");
  usePlaySessionStore.setState({
    mode: "connecting",
    play: null,
    pending: null,
    busy: false,
    characters: [],
    error: null,
  });
  requests = [];
  handler = async (config) => response(config, view);
  http.defaults.adapter = async (config) => {
    requests.push({
      url: config.url!,
      body: config.data ? JSON.parse(config.data) : undefined,
      auth: config.headers.Authorization as string,
    });
    return handler(config);
  };
});

test("resuming uses the existing identity and exact play without auto selecting", async () => {
  await usePlaySessionStore.getState().bootstrap({ playId: "play-qa" });
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, "/api/fusion/package-plays/play-qa");
  assert.equal(requests[0].auth, "Bearer existing-admin");
  assert.equal(
    usePlaySessionStore.getState().play?.selected_character_id,
    "third",
  );
});

test("opening resume does not depend on release catalog and reuses deterministic creation key", async () => {
  handler = async (config) =>
    response(
      config,
      config.url?.includes("package-sessions/")
        ? {
            session_id: "package-opening",
            release_id: 9,
            selected_character_id: "third",
          }
        : config.method === "get"
          ? null
          : view,
    );
  await usePlaySessionStore
    .getState()
    .bootstrap({ openingSessionId: "package-opening" });
  assert.equal(
    requests.some((r) => r.url.includes("package-releases")),
    false,
  );
  assert.deepEqual(requests.at(-1)?.body, {
    opening_session_id: "package-opening",
    idempotency_key: "play-package-opening",
  });
});

test("strict mode duplicate mounts share one bootstrap", async () => {
  await Promise.all([
    usePlaySessionStore.getState().bootstrap({ playId: "play-qa" }),
    usePlaySessionStore.getState().bootstrap({ playId: "play-qa" }),
  ]);
  assert.equal(requests.length, 1);
});

test("failed resume reports an error and never switches to fake game data", async () => {
  handler = async () => {
    throw new Error("offline");
  };
  await usePlaySessionStore.getState().bootstrap({ playId: "play-qa" });
  assert.equal(usePlaySessionStore.getState().mode, "error");
  assert.equal(usePlaySessionStore.getState().play, null);
});

test("phase advancement omits the forbidden null target", async () => {
  usePlaySessionStore.setState({ play: view });
  assert.equal(
    await usePlaySessionStore.getState().performAction("ADVANCE_PHASE"),
    true,
  );
  assert.equal("target" in requests[0].body, false);
  assert.equal(requests[0].body.expected_revision, 2);
});

test("share sends exactly the selected material reference", async () => {
  usePlaySessionStore.setState({ play: view });
  await usePlaySessionStore
    .getState()
    .performAction("SHARE_MATERIAL", {
      collection: "knowledge",
      id: "third-clue",
    });
  assert.deepEqual(requests[0].body.target, {
    collection: "knowledge",
    id: "third-clue",
  });
});

test("unknown AI response blocks new commands and retries the same idempotency key", async () => {
  usePlaySessionStore.setState({ play: view });
  handler = async () => {
    throw new Error("network lost after dispatch");
  };
  assert.equal(
    await usePlaySessionStore.getState().askQuestion("first", "当时你在哪里？"),
    false,
  );
  assert.ok(usePlaySessionStore.getState().pending);
  assert.equal(
    await usePlaySessionStore.getState().performAction("ADVANCE_PHASE"),
    false,
  );
  assert.equal(requests.length, 1);
  handler = async (config) => response(config, { ...view, revision: 4 });
  assert.equal(await usePlaySessionStore.getState().retryPending(), true);
  assert.deepEqual(requests[1].body, requests[0].body);
  assert.equal(usePlaySessionStore.getState().pending, null);
  assert.equal(session.getItem("pending-command:play-qa"), null);
});

test("pending request survives reload and explicit refusal releases the command", async () => {
  const pending = {
    kind: "act",
    playId: "play-qa",
    body: {
      action: "ADVANCE_PHASE",
      expected_revision: 2,
      idempotency_key: "original-key",
    },
  };
  session.setItem("pending-command:play-qa", JSON.stringify(pending));
  await usePlaySessionStore.getState().bootstrap({ playId: "play-qa" });
  assert.deepEqual(usePlaySessionStore.getState().pending, pending);
  handler = async (config) => {
    throw new AxiosError("conflict", "ERR_BAD_REQUEST", config, undefined, {
      ...response(config, {}),
      status: 409,
    });
  };
  await usePlaySessionStore.getState().retryPending();
  assert.equal(usePlaySessionStore.getState().pending, null);
  assert.equal(session.getItem("pending-command:play-qa"), null);
  assert.equal(axios.isAxiosError(new AxiosError()), true);
});

test("public preview and local trial both refuse new AI asks before network dispatch", async () => {
  for (const field of ["public_preview", "local_trial"] as const) {
    usePlaySessionStore.setState({
      play: {
        ...view,
        [field]: {
          label: field === "public_preview" ? "公开试玩版" : "本地试玩版",
          notice: "AI 对话关闭",
          model_audit_complete: false,
          ai_dialogue_available: false,
        },
      },
    });
    assert.equal(await usePlaySessionStore.getState().askQuestion("first", "当时在哪里？"), false);
    assert.match(usePlaySessionStore.getState().error ?? "", /未开启 AI 对话/);
    assert.equal(usePlaySessionStore.getState().pending, null);
  }
  assert.equal(requests.length, 0);
});
