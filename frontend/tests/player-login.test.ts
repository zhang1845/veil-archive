import assert from "node:assert/strict";
import { test } from "node:test";
import { playerReturnPath } from "../src/lib/player-login";

test("player login resumes only player routes", () => {
  assert.equal(playerReturnPath("/scripts"), "/scripts");
  assert.equal(playerReturnPath("/records"), "/records");
  assert.equal(playerReturnPath("/play?play_id=fixture#evidence"), "/play?play_id=fixture#evidence");
});

for (const target of [null, "", "https://external.invalid/", "//external.invalid/", "/\\external.invalid/", "/admin", "/scripts/../admin", "/play/unknown", "javascript:alert(1)"]) {
  test(`unsafe player return path falls back: ${String(target)}`, () => {
    assert.equal(playerReturnPath(target), "/scripts");
  });
}
