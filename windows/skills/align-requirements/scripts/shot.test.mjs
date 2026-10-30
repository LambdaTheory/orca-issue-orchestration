import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { test } from 'node:test';

const script = fileURLToPath(new URL('./shot.mjs', import.meta.url));
const skill = dirname(dirname(script));

// The CLI contract uses an explicit fake Playwright module; real screenshots are a separate check.
function withFakeBrowser(check) {
  const temp = mkdtempSync(join(tmpdir(), 'align-shot-'));
  try {
    const input = join(temp, '需求 page #1.html');
    const output = join(temp, 'shots');
    const log = join(temp, 'browser.jsonl');
    const dependencies = join(temp, 'browser-runtime');
    const packageDir = join(dependencies, 'node_modules', 'playwright');
    mkdirSync(packageDir, { recursive: true });
    writeFileSync(input, '<!doctype html><title>需求 v1</title>', 'utf8');
    writeFileSync(join(dependencies, 'package.json'), '{}');
    writeFileSync(join(packageDir, 'package.json'), JSON.stringify({ name: 'playwright', main: 'index.cjs' }));
    const fake = [
      "const { appendFileSync, writeFileSync } = require('node:fs');",
      "const record = (value) => appendFileSync(process.env.SHOT_TEST_LOG, JSON.stringify(value) + String.fromCharCode(10));",
      "exports.chromium = { launch: async () => ({",
      "  newPage: async (options) => {",
      "    record({ event: 'page', options });",
      "    return {",
      "      goto: async (url) => record({ event: 'goto', url }),",
      "      waitForTimeout: async () => {},",
      "      screenshot: async (options) => { record({ event: 'screenshot', ...options }); writeFileSync(options.path, 'fake screenshot'); },",
      "      evaluate: async () => ({ by: 0, bad: [] }),",
      "      $$eval: async () => ['fig1'],",
      "      locator: (selector) => ({ scrollIntoViewIfNeeded: async () => {}, screenshot: async (options) => { record({ event: 'figure', selector, ...options }); writeFileSync(options.path, 'fake figure'); } }),",
      "      close: async () => record({ event: 'page-close' }),",
      "    };",
      "  },",
      "  close: async () => record({ event: 'browser-close' }),",
      "}) };",
    ].join(String.fromCharCode(10));
    writeFileSync(join(packageDir, 'index.cjs'), fake);
    const run = (extra = []) => spawnSync(process.execPath,
      [script, input, output, '--playwright', dependencies, ...extra],
      { cwd: temp, encoding: 'utf8', timeout: 30_000,
        env: { ...process.env, SHOT_TEST_LOG: log } });
    const events = () => readFileSync(log, 'utf8').trim().split(String.fromCharCode(10)).map(JSON.parse);
    check({ run, events, input, output });
  } finally {
    rmSync(temp, { recursive: true, force: true });
  }
}

test('missing page or output gives usage before launching a browser', () => {
  const result = spawnSync(process.execPath, [script], { encoding: 'utf8', timeout: 10_000 });
  assert.equal(result.status, 2);
  assert.match(result.stderr, /用法/);
});

test('file URLs preserve Windows drives, spaces, Unicode and literal fragments', () => {
  withFakeBrowser(({ run, events, input }) => {
    const result = run();
    assert.ifError(result.error);
    assert.equal(result.status, 0, result.stdout + result.stderr);
    const urls = events().filter((event) => event.event === 'goto').map((event) => event.url);
    assert.equal(urls.length, 3);
    for (const url of urls) {
      assert.equal(url, pathToFileURL(input).href);
      assert.ok(url.includes('%23'));
      assert.equal(new URL(url).hash, '');
      assert.equal(fileURLToPath(url), input);
    }
  });
});

test('shot CLI checks light, dark, phone and numbered figures with reduced motion', () => {
  withFakeBrowser(({ run, events }) => {
    const result = run();
    assert.equal(result.status, 0, result.stdout + result.stderr);
    const record = events();
    const pages = record.filter((event) => event.event === 'page');
    assert.deepEqual(pages.map((event) => [event.options.viewport.width, event.options.colorScheme]),
      [[1280, 'light'], [1280, 'dark'], [390, 'light']]);
    assert.ok(pages.every((event) => event.options.reducedMotion === 'reduce'));
    assert.equal(record.filter((event) => event.event === 'screenshot').length, 3);
    assert.equal(record.filter((event) => event.event === 'figure').length, 2);
    assert.equal(record.filter((event) => event.event === 'browser-close').length, 1);
    assert.equal((result.stdout.match(/横向溢出 0px/g) || []).length, 3);
  });
});

test('optional animation frames keep their figure selector and timestamp', () => {
  withFakeBrowser(({ run, events }) => {
    const result = run(['--frames', 'fig1:0']);
    assert.equal(result.status, 0, result.stdout + result.stderr);
    const frame = events().find((event) => event.event === 'figure' && event.path.endsWith('-fig1-t0.png'));
    assert.ok(frame);
    assert.equal(frame.selector, '#fig1');
  });
});

test('example exposes page and PRD versions and has no built-in answer export', () => {
  const source = readFileSync(join(skill, 'examples', 'prd-confirmation.html'), 'utf8');
  for (const value of ['预约取消需求 v1', 'data-page-version="v1"', 'PRD-DEMO v1', 'CLAR-DEMO r1',
    '待你定', '当前对话', '不能确认整份需求', '未评论或浏览页面都不是批准']) {
    assert.ok(source.includes(value), value);
  }
  assert.doesNotMatch(source, /<doc-ask|<doc-plan|Copy response|Respond/);
  assert.doesNotMatch(source, /(?:src|href)="https?:/);
  assert.doesNotMatch(source, /fetch\s*\(|localStorage/);
  assert.doesNotMatch(source, /href="#s1-1">1.1　如何……/);
  const ids = [...source.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
  assert.equal(new Set(ids).size, ids.length, 'duplicate section or figure ids');
  for (const match of source.matchAll(/href="#([^"]+)"/g)) {
    assert.ok(ids.includes(match[1]), 'missing section ' + match[1]);
  }
});

test('source template keeps its own layout and local-only interaction', () => {
  const source = readFileSync(join(skill, 'assets', 'template.html'), 'utf8');
  for (const value of ['class="abstract"', 'class="toc"', '决定记录', '术语', 'prefers-reduced-motion']) {
    assert.ok(source.includes(value), value);
  }
  assert.doesNotMatch(source, /<doc-plan|<doc-ask|fetch\s*\(|localStorage/);
  assert.doesNotMatch(source, /(?:src|href)="https?:/);
});
