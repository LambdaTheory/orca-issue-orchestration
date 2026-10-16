// Local addition: subprocess regression tests for pack.mjs decision budgets
// and Chinese output. Apache-2.0; see ../LICENSE.
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const packPath = fileURLToPath(new URL('./pack.mjs', import.meta.url));
const quantityWarning = /doc-plan: \d+ decisions —/;

function page(count) {
  const questions = Array.from({ length: count }, (_, i) => `
    <doc-ask id="choice-${i}">
      <p>待决产品问题${i + 1}选择哪个方案？</p>
      <label><input type="radio" name="choice-${i}" value="a" checked>方案甲</label>
      <label><input type="radio" name="choice-${i}" value="b">方案乙</label>
    </doc-ask>`).join('\n');
  return `<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<title>需求澄清摘要</title>
<link rel="stylesheet" href="htmlplan.css">
<script src="htmlplan.js" defer></script>
<body>
<header><h1>需求澄清摘要</h1></header>
<main><doc-plan>
  <doc-claim>
    <p>用户可以查看待确认的需求。</p>
    <doc-mock frame="none" w="440"><template><p>中文方案展示</p></template></doc-mock>
    ${questions}
  </doc-claim>
  <doc-claim aux="scope"><p>本轮不开始开发。</p><ul><li>保留现有行为。</li></ul></doc-claim>
</doc-plan></main>
</body></html>`;
}

function withPackedPage(count, check) {
  const dir = mkdtempSync(join(tmpdir(), 'html-plan-pack-'));
  try {
    const input = join(dir, 'plan.html');
    const output = join(dir, 'plan.packed.html');
    writeFileSync(input, page(count), 'utf8');
    const result = spawnSync(process.execPath,
      [packPath, input, '--root', dir, '-o', output],
      { cwd: dir, encoding: 'utf8', timeout: 30_000 });
    assert.ifError(result.error);
    const report = result.stdout + result.stderr;
    assert.equal(result.status, 0, report);
    check({ report, packed: readFileSync(output, 'utf8') });
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

for (const count of [0, 1, 5]) {
  test(`${count} decisions pack without a decision-count warning`, () => {
    withPackedPage(count, ({ report }) => {
      assert.doesNotMatch(report, quantityWarning);
    });
  });
}

test('6 decisions warn with the 0–5 and explicit-adoption guidance', () => {
  withPackedPage(6, ({ report }) => {
    assert.match(report, /doc-plan: 6 decisions — 0–5 allowed per page/);
    assert.match(report, /never pad a quota/);
    assert.match(report, /recommended defaults are not approval and require explicit user adoption/);
  });
});

test('Chinese content packs successfully with both runtime assets inlined', () => {
  withPackedPage(1, ({ report, packed }) => {
    assert.match(report, /2 asset\(s\) inlined/);
    assert.match(packed, /<html data-htmlplan-packed lang="zh-CN">/);
    assert.match(packed, /<title>需求澄清摘要<\/title>/);
    assert.match(packed, /用户可以查看待确认的需求。/);
    assert.match(packed, /待决产品问题1选择哪个方案？/);
    assert.match(packed, /方案甲/);
    assert.match(packed, /<style data-htmlplan>/);
    assert.match(packed, /<script data-htmlplan>/);
    assert.doesNotMatch(packed, /<link[^>]+href="htmlplan\.css"/);
    assert.doesNotMatch(packed, /<script[^>]+src="htmlplan\.js"/);
    // English-oriented grammar warnings for Chinese are allowed.
  });
});
