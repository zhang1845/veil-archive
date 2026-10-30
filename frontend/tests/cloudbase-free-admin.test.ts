import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { test } from "node:test";

function render(freeMode: boolean): string {
  return execFileSync(process.execPath, ["--import", "tsx", "--input-type=module", "-e", `
    import { createElement } from 'react';
    import { renderToStaticMarkup } from 'react-dom/server';
    const module = await import('./src/components/admin/AdminConsole.tsx');
    const AdminConsole = module.default.default ?? module.default;
    console.log(renderToStaticMarkup(createElement(AdminConsole)));
  `], { encoding: "utf8", env: { ...process.env, NEXT_PUBLIC_CLOUDBASE_FREE_MODE: String(freeMode) } });
}

test("free cloud workbench clearly discloses unavailable AI, disables generation and keeps review navigation", () => {
  const html = render(true);
  assert.match(html, /当前免费试玩环境 · AI 创作尚未接通/);
  assert.match(html, /AI 创作、自动修稿与模型复审尚未接通/);
  assert.match(html, /查看候选与人工审核/);
  assert.match(html, /<button[^>]*disabled=""[^>]*aria-describedby="cloudbase-creative-status"[^>]*>创建生成任务<\/button>/);
  assert.match(html, /题材与创作要求/);
  assert.doesNotMatch(html, /--allow-paid --watch/);
});

test("ordinary workbench retains its existing creation form and local worker instructions", () => {
  const html = render(false);
  assert.doesNotMatch(html, /当前免费试玩环境|cloudbase-creative-status/);
  assert.match(html, /创建生成任务/);
  assert.match(html, /--allow-paid --watch/);
});

test("the build flag is injected only by static export, overriding an unrelated public environment value", () => {
  for (const staticExport of [true, false]) {
    const result = execFileSync(process.execPath, ["--import", "tsx", "--input-type=module", "-e", `
      const module = await import('./next.config.ts');
      const config = module.default.default ?? module.default;
      console.log(config.env.NEXT_PUBLIC_CLOUDBASE_FREE_MODE);
    `], { encoding: "utf8", env: { ...process.env, CLOUDBASE_STATIC_EXPORT: String(staticExport),
      NEXT_PUBLIC_API_URL: "https://api.example.invalid", NEXT_PUBLIC_CLOUDBASE_FREE_MODE: "true" } });
    assert.equal(result.trim(), String(staticExport));
  }
});
