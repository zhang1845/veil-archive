import assert from "node:assert/strict";
import { test } from "node:test";
import {
  activeCreativeStates,
  changedSectionLabels,
  creativeRoundLabel,
  creativeRoundStateLabel,
  creativeStateLabel,
  creativeStopMessage,
} from "../src/lib/creative";

test("legacy jobs remain readable without a revision record", () => {
  assert.equal(creativeStateLabel({ state: "COMPLETED" }), "等待人工审核");
  assert.equal(creativeStopMessage(undefined), null);
  assert.equal(creativeStopMessage(null), null);
});

test("an AI report without blocking findings still requires a human review", () => {
  assert.equal(
    creativeStateLabel({ state: "COMPLETED", stop_reason: "CLEAN_AUDIT" }),
    "AI 审核结束，等待人工审核",
  );
  assert.match(creativeStopMessage("CLEAN_AUDIT")!, /仍需管理员/);
  assert.doesNotMatch(creativeRoundStateLabel("COMPLETED"), /通过|已发布/);
});

test("the repair loop keeps polling while active and stops at human attention", () => {
  assert.equal(activeCreativeStates.has("REVISING"), true);
  assert.equal(activeCreativeStates.has("COMPILING"), true);
  assert.equal(activeCreativeStates.has("REVIEW_REQUIRED"), false);
  assert.equal(activeCreativeStates.has("NEEDS_RECONCILIATION"), false);
});

for (const [reason, expected] of [
  ["ROUND_LIMIT", /轮数上限/],
  ["BUDGET_LIMIT", /停止后续调用/],
  ["NO_CHANGE", /与上一版相同/],
  ["RESULT_UNKNOWN", /勿重复提交/],
  ["PIPELINE_BLOCKED", /未完成/],
  ["LEGACY_SINGLE_PASS", /单次生成/],
] as const) {
  test(`${reason} explains why automatic changes stopped`, () => {
    assert.match(creativeStopMessage(reason)!, expected);
  });
}

test("round labels distinguish the initial draft from subsequent repairs", () => {
  assert.equal(creativeRoundLabel({ round_index: 0 }), "初稿");
  assert.equal(creativeRoundLabel({ round_index: 2 }), "第 2 轮修稿");
});

test("changed sections collapse repeated field paths into readable labels", () => {
  assert.deepEqual(
    changedSectionLabels(["/characters/0/secret", "characters[1].background", "evidence.0.text", "truth"]),
    ["角色资料与私本", "物证", "案件真相"],
  );
});
