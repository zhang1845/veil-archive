import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import { AxiosError, type InternalAxiosRequestConfig } from "axios";
import { http, errorCode } from "../src/lib/http";
import { saveToken } from "../src/services/auth";

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
const storage = new MemoryStorage();
Object.assign(globalThis, { window: {}, localStorage: storage });
const reply = (
  config: InternalAxiosRequestConfig,
  data: unknown,
  status = 200,
) => ({ config, data, status, statusText: "QA", headers: {} });
const refused = (
  config: InternalAxiosRequestConfig,
  status = 401,
  data: unknown = {
    success: false,
    message: "无效的认证令牌",
    error_code: "AUTH_ERROR",
  },
) =>
  new AxiosError(
    `Request failed with status code ${status}`,
    "ERR_BAD_REQUEST",
    config,
    undefined,
    reply(config, data, status),
  );
const tokens = {
  access_token: "renewed-admin",
  refresh_token: "rotated-admin-refresh",
};

beforeEach(() => {
  storage.values.clear();
  saveToken({ access_token: "old-admin", refresh_token: "original-refresh" });
});

test("an unauthorized read renews and retries with the same identity, without a guest login", async () => {
  const calls: string[] = [];
  http.defaults.adapter = async (config) => {
    calls.push(config.url!);
    if (config.url === "/api/auth/refresh") {
      assert.equal(config.headers.Authorization, undefined);
      assert.deepEqual(JSON.parse(config.data), {
        refresh_token: "original-refresh",
      });
      return reply(config, tokens);
    }
    if (config.headers.Authorization === "Bearer old-admin")
      throw refused(config);
    assert.equal(config.headers.Authorization, "Bearer renewed-admin");
    return reply(config, { success: true, data: [] });
  };
  assert.deepEqual(
    (await http.get("/api/fusion/package-releases")).data.data,
    [],
  );
  assert.deepEqual(calls, [
    "/api/fusion/package-releases",
    "/api/auth/refresh",
    "/api/fusion/package-releases",
  ]);
  assert.equal(storage.getItem("refresh_token"), "rotated-admin-refresh");
});

test("concurrent reads share a single rotating refresh token", async () => {
  let refreshCalls = 0;
  let finish!: () => void;
  const gate = new Promise<void>((resolve) => {
    finish = resolve;
  });
  let entered!: () => void;
  const started = new Promise<void>((resolve) => {
    entered = resolve;
  });
  http.defaults.adapter = async (config) => {
    if (config.url === "/api/auth/refresh") {
      refreshCalls++;
      entered();
      await gate;
      return reply(config, tokens);
    }
    if (config.headers.Authorization === "Bearer old-admin")
      throw refused(config);
    return reply(config, config.headers.Authorization);
  };
  const requests = Promise.all([
    http.get("/first"),
    http.get("/second"),
    http.get("/third"),
  ]);
  await started;
  finish();
  const responses = await requests;
  assert.equal(refreshCalls, 1);
  assert.ok(responses.every((r) => r.data === "Bearer renewed-admin"));
});

test("a late response for the old access token reuses the replacement without rotating again", async () => {
  let releaseLate!: () => void;
  const late = new Promise<void>((resolve) => {
    releaseLate = resolve;
  });
  let refreshCalls = 0;
  http.defaults.adapter = async (config) => {
    if (config.url === "/api/auth/refresh") {
      refreshCalls++;
      return reply(config, tokens);
    }
    if (config.headers.Authorization === "Bearer old-admin") {
      if (config.url === "/late") await late;
      throw refused(config);
    }
    return reply(config, config.headers.Authorization);
  };
  const waiting = http.get("/late");
  await http.get("/first");
  releaseLate();
  assert.equal((await waiting).data, "Bearer renewed-admin");
  assert.equal(refreshCalls, 1);
});

test("refused refresh preserves the account and never falls back to an anonymous identity", async () => {
  const paths: string[] = [];
  http.defaults.adapter = async (config) => {
    paths.push(config.url!);
    throw refused(config);
  };
  await assert.rejects(http.get("/private-records"), (error) =>
    errorCode(error).includes("登录状态已失效"),
  );
  assert.deepEqual(paths, ["/private-records", "/api/auth/refresh"]);
  assert.equal(storage.getItem("access_token"), "old-admin");
  assert.equal(storage.getItem("refresh_token"), "original-refresh");
});

test("missing refresh token produces a recoverable failure instead of a guest identity", async () => {
  storage.removeItem("refresh_token");
  let calls = 0;
  http.defaults.adapter = async (config) => {
    calls++;
    throw refused(config);
  };
  await assert.rejects(http.get("/private-records"));
  assert.equal(calls, 1);
  assert.equal(storage.getItem("access_token"), "old-admin");
});

test("network failure during renewal keeps credentials and allows a later attempt", async () => {
  http.defaults.adapter = async (config) => {
    if (config.url === "/api/auth/refresh")
      throw new AxiosError("Network Error", "ERR_NETWORK", config);
    throw refused(config);
  };
  await assert.rejects(http.get("/catalog"), (error) =>
    errorCode(error).includes("无法连接服务"),
  );
  assert.equal(storage.getItem("refresh_token"), "original-refresh");
  http.defaults.adapter = async (config) => {
    if (config.url === "/api/auth/refresh") return reply(config, tokens);
    if (config.headers.Authorization === "Bearer old-admin")
      throw refused(config);
    return reply(config, "recovered");
  };
  assert.equal((await http.get("/catalog")).data, "recovered");
});

test("a second refusal after renewal stops rather than looping", async () => {
  let reads = 0,
    renewals = 0;
  http.defaults.adapter = async (config) => {
    if (config.url === "/api/auth/refresh") {
      renewals++;
      return reply(config, tokens);
    }
    reads++;
    throw refused(config);
  };
  await assert.rejects(http.get("/catalog"));
  assert.equal(reads, 2);
  assert.equal(renewals, 1);
});

test("forbidden reads do not renew or downgrade the account", async () => {
  let calls = 0;
  http.defaults.adapter = async (config) => {
    calls++;
    throw refused(config, 403, { message: "需要管理员权限" });
  };
  await assert.rejects(
    http.get("/admin-data"),
    (error) => errorCode(error) === "需要管理员权限",
  );
  assert.equal(calls, 1);
  assert.equal(storage.getItem("access_token"), "old-admin");
});

test("commands are not silently replayed, including paid model requests", async () => {
  let calls = 0;
  http.defaults.adapter = async (config) => {
    calls++;
    throw refused(config);
  };
  await assert.rejects(
    http.post("/api/fusion/package-plays/qa/ask", {
      idempotency_key: "same-command",
    }),
  );
  assert.equal(calls, 1);
});

test("failed password login is never renewed or sent with an old bearer", async () => {
  let calls = 0;
  http.defaults.adapter = async (config) => {
    calls++;
    assert.equal(config.url, "/api/auth/login");
    assert.equal(config.headers.Authorization, undefined);
    throw refused(config, 401, { detail: "用户名或密码错误" });
  };
  await assert.rejects(
    http.post("/api/auth/login", {}),
    (error) => errorCode(error) === "用户名或密码错误",
  );
  assert.equal(calls, 1);
});

test("a login that completes during refresh cannot be overwritten by the old response", async () => {
  let finish!: () => void, entered!: () => void;
  const gate = new Promise<void>((resolve) => {
    finish = resolve;
  });
  const started = new Promise<void>((resolve) => {
    entered = resolve;
  });
  http.defaults.adapter = async (config) => {
    if (config.url === "/api/auth/refresh") {
      entered();
      await gate;
      return reply(config, tokens);
    }
    if (config.headers.Authorization === "Bearer old-admin")
      throw refused(config);
    return reply(config, config.headers.Authorization);
  };
  const request = http.get("/catalog");
  await started;
  saveToken({ access_token: "new-login", refresh_token: "new-login-refresh" });
  finish();
  assert.equal((await request).data, "Bearer new-login");
  assert.equal(storage.getItem("refresh_token"), "new-login-refresh");
});

test("saving an identity without a refresh token removes another account's stale refresh", () => {
  saveToken({ access_token: "new-identity" });
  assert.equal(storage.getItem("refresh_token"), null);
});

test("domain errors retain their code while transport errors have a readable message", () => {
  const config = {
    url: "/api/fusion/package-releases",
  } as InternalAxiosRequestConfig;
  assert.equal(
    errorCode(
      refused(config, 409, { detail: "PACKAGE_PLAY_PHASE_BUDGET_REMAINS" }),
    ),
    "PACKAGE_PLAY_PHASE_BUDGET_REMAINS",
  );
  assert.equal(errorCode(refused(config)), "登录状态已失效，请重新登录。");
});
