import assert from "node:assert/strict";
import { test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { TrialNotice } from "../src/components/play/TrialNotice";
import type { LocalTrial, PublicPreview } from "../src/types/api";

const preview: PublicPreview = {
  label: "公开试玩版",
  notice: "原创基础文字体验，尚未完成正式审核。",
  model_audit_complete: false,
  ai_dialogue_available: false,
};

test("public preview disclosure is explicit and never labels the artifact local-only", () => {
  const html = renderToStaticMarkup(createElement(TrialNotice, { publicPreview: preview }));
  assert.match(html, /公开试玩 · 原创测试剧本/);
  assert.match(html, /尚未完成正式模型审核/);
  assert.match(html, /AI 对话未开启/);
  assert.doesNotMatch(html, /本地试玩/);
});

test("local trial retains its supplied local-only notice and normal releases gain no trial claim", () => {
  const local: LocalTrial = { ...preview, label: "本地试玩版", notice: "仅供本机体验。" };
  const html = renderToStaticMarkup(createElement(TrialNotice, { localTrial: local }));
  assert.match(html, /本地试玩版/);
  assert.match(html, /仅供本机体验/);
  assert.doesNotMatch(html, /公开试玩/);
  assert.equal(renderToStaticMarkup(createElement(TrialNotice)), "");
});
