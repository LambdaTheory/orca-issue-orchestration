// Local addition: subprocess regression tests for pack.mjs decision budgets
// and Chinese output. Apache-2.0; see ../LICENSE.
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { runInNewContext } from 'node:vm';

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

function withPackedPage(count, check, html = page(count)) {
  const dir = mkdtempSync(join(tmpdir(), 'html-plan-pack-'));
  try {
    const input = join(dir, 'plan.html');
    const output = join(dir, 'plan.packed.html');
    writeFileSync(input, html, 'utf8');
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


test('versioned PRD view preserves document identity, scope and question mapping offline', () => {
  const source = readFileSync(new URL('../examples/prd-confirmation.html', import.meta.url), 'utf8');
  withPackedPage(1, ({ report, packed }) => {
    assert.doesNotMatch(report, quantityWarning);
    assert.match(report, /2 asset\(s\) inlined/);
    assert.ok(packed.includes('<title>预约取消 PRD-DEMO v1 CLAR-DEMO r1</title>'));
    assert.ok(packed.includes('<h1>预约取消 PRD-DEMO v1 CLAR-DEMO r1</h1>'));
    for (const metadata of ['data-prd-id="PRD-DEMO"', 'data-prd-version="v1"',
      'data-clarification-id="CLAR-DEMO"', 'data-round="r1"']) {
      assert.ok(packed.includes(metadata), metadata);
    }
    assert.ok(packed.includes('id="q1-cancel-retry"'));
    assert.ok(packed.includes('Q-1 → R-2 / AC-2'));
    assert.ok(packed.includes('不能确认整份需求'));
    assert.ok(packed.includes('协调者校验版本后更新同一份 PRD'));
    assert.doesNotMatch(source, /(?:src|href)="https?:/);
    assert.ok(!source.includes('fetch('));
    assert.doesNotMatch(packed, /<link[^>]+href="[^"]*htmlplan\.css"/);
    assert.doesNotMatch(packed, /<script[^>]+src="[^"]*htmlplan\.js"/);
  }, source);
});


test('Respond exports PRD identity and visible question ID without treating defaults as approval', () => {
  const runtime = readFileSync(new URL('./htmlplan.js', import.meta.url), 'utf8');
  const start = runtime.indexOf('function buildResponse() {');
  const end = runtime.indexOf(String.fromCharCode(10) + '}', start) + 2;
  assert.ok(start >= 0 && end > start);
  const title = '预约取消 PRD-DEMO v1 CLAR-DEMO r1';
  const control = { type: 'radio', name: 'q1-cancel-retry', dataset: {}, matches: () => false };
  const ask = {
    id: 'q1-cancel-retry',
    querySelector: (selector) => selector.startsWith(':scope')
      ? { textContent: 'Q-1：临时失败后是否提供手动重试入口？' } : control,
    closest: () => ({ dataset: { no: '2' } }),
  };
  const context = {
    $: (selector) => selector === 'h1' ? { textContent: title } : null,
    $$: (selector, root) => selector === 'doc-ask' ? [ask] : root === ask ? [control] : [],
    readAnswers: () => ({ 'q1-cancel-retry': 'retry' }),
    document: { title: 'fallback title' },
    S: { defaults: { 'q1-cancel-retry': 'retry' }, seen: {}, strikes: {}, comments: {} },
    same: (a, b) => a === b,
    optionLabel: () => '提供重试入口',
    CSS: { escape: (value) => value },
    machines: {},
  };
  const response = runInNewContext(runtime.slice(start, end) + '; buildResponse();', context);
  assert.ok(response.md.startsWith('# Re: ' + title));
  assert.ok(response.md.includes('Q-1：临时失败后是否提供手动重试入口？'));
  assert.ok(response.md.includes('_(not opened; default kept)_'));
  assert.equal(response.nChanged, 0);
  assert.equal(response.nAsks, 1);
});

test('new PRD versions and rounds have distinct browser draft keys', () => {
  const runtime = readFileSync(new URL('./htmlplan.js', import.meta.url), 'utf8');
  const keyLine = runtime.split(String.fromCharCode(10)).find((line) => line.startsWith('const KEY = '));
  assert.ok(keyLine);
  const keyFor = (title) => runInNewContext(keyLine + '; KEY;', {
    location: { pathname: '/clarification/plan.packed.html' }, document: { title },
  });
  assert.notEqual(keyFor('PRD-DEMO v1 CLAR-DEMO r1'), keyFor('PRD-DEMO v2 CLAR-DEMO r1'));
  assert.notEqual(keyFor('PRD-DEMO v1 CLAR-DEMO r1'), keyFor('PRD-DEMO v1 CLAR-DEMO r2'));
});
