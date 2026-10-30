import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import { AxiosError, type InternalAxiosRequestConfig } from "axios";
import { http } from "../src/lib/http";
import { hasAdminSession, passwordLogin } from "../src/services/auth";

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
let calls = 0;
const reply = (
  config: InternalAxiosRequestConfig,
  data: unknown,
  status = 200,
) => ({ config, data, status, statusText: "QA", headers: {} });
beforeEach(() => {
  storage.values.clear();
  calls = 0;
});

test("no login means no workbench identity request", async () => {
  http.defaults.adapter = async (config) => {
    calls++;
    return reply(config, {});
  };
  assert.equal(await hasAdminSession(), false);
  assert.equal(calls, 0);
});

for (const identity of [
  { is_admin: false, is_active: true },
  { is_admin: true, is_active: false },
  { is_admin: "true", is_active: true },
  {},
]) {
  test(`non-admin, inactive or malformed identity denied: ${JSON.stringify(identity)}`, async () => {
    storage.setItem("access_token", "guest");
    http.defaults.adapter = async (config) => {
      assert.equal(config.url, "/api/auth/me");
      return reply(config, identity);
    };
    assert.equal(await hasAdminSession(), false);
  });
}

test("server-confirmed active administrator is allowed", async () => {
  storage.setItem("access_token", "admin");
  http.defaults.adapter = async (config) =>
    reply(config, { is_admin: true, is_active: true });
  assert.equal(await hasAdminSession(), true);
});

for (const status of [401, 403]) {
  test(`expired or refused session redirects to login: ${status}`, async () => {
    storage.setItem("access_token", "expired");
    http.defaults.adapter = async (config) => {
      throw new AxiosError(
        "refused",
        "ERR_BAD_REQUEST",
        config,
        undefined,
        reply(config, {}, status),
      );
    };
    assert.equal(await hasAdminSession(), false);
  });
}

test("network failure is visible instead of granting access", async () => {
  storage.setItem("access_token", "unknown");
  http.defaults.adapter = async () => {
    throw new Error("network unavailable");
  };
  await assert.rejects(hasAdminSession(), /network unavailable/);
});

test("player login cannot enter workbench or replace current credentials", async () => {
  storage.setItem("access_token", "original-guest");
  http.defaults.adapter = async (config) =>
    reply(config, {
      access_token: "new-player",
      user: { is_admin: false, is_active: true },
    });
  await assert.rejects(passwordLogin("player", "password", true), /管理员账号/);
  assert.equal(storage.getItem("access_token"), "original-guest");
});

test("administrator login persists identity only after permission check", async () => {
  http.defaults.adapter = async (config) => {
    assert.deepEqual(JSON.parse(config.data), {
      username: "admin",
      password: "fixture-password",
    });
    return reply(config, {
      access_token: "admin-token",
      refresh_token: "admin-refresh",
      user: { is_admin: true, is_active: true },
    });
  };
  await passwordLogin("admin", "fixture-password", true);
  assert.equal(storage.getItem("access_token"), "admin-token");
  assert.equal(storage.getItem("refresh_token"), "admin-refresh");
});
