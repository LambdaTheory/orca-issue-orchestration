#!/usr/bin/env node
// 需求文档自查：亮色、暗色、手机三张整页图，每张编号图的单图（最后一帧），横向溢出检查，可选截动画中间帧。
// 用法：node shot.mjs <页面.html> <输出目录> [--playwright <含 node_modules/playwright 的目录>] [--frames fig6:1.2,3.9 ...]
import { createRequire } from 'node:module';
import { mkdirSync } from 'node:fs';
import { resolve, basename } from 'node:path';
import { pathToFileURL } from 'node:url';
// Local adaptation: preserve Windows drives and escape Unicode, spaces and # in file URLs.

const args = process.argv.slice(2);
const opt = (name) => { const i = args.indexOf(name); return i >= 0 ? args.splice(i, 2)[1] : undefined; };
const pw = opt('--playwright') || process.env.PLAYWRIGHT_DIR;
const frames = []; for (let f; (f = opt('--frames')); ) frames.push(f);
const [pageArg, outArg] = args;
if (!pageArg || !outArg) { console.error('用法：node shot.mjs <页面.html> <输出目录> [--playwright 目录] [--frames fig6:1.2,3.9]'); process.exit(2); }

let chromium;
for (const base of [pw && resolve(pw, 'package.json'), resolve(process.cwd(), 'package.json'), import.meta.url]) {
  if (!base) continue;
  try { ({ chromium } = createRequire(base)('playwright')); break; } catch {}
}
if (!chromium) {
  console.error('找不到 playwright。任选其一：\n  在当前目录 npm i -D playwright && npx playwright install chromium\n  --playwright <某个装了 playwright 的项目目录>，或设 PLAYWRIGHT_DIR');
  process.exit(1);
}

const page = pathToFileURL(resolve(pageArg)).href;
const out = resolve(outArg); mkdirSync(out, { recursive: true });
const stem = basename(pageArg, '.html');
const b = await chromium.launch();

for (const [name, width, colorScheme] of [['desk-light', 1280, 'light'], ['desk-dark', 1280, 'dark'], ['phone', 390, 'light']]) {
  const p = await b.newPage({ viewport: { width, height: 900 }, colorScheme, reducedMotion: 'reduce' });
  await p.goto(page); await p.waitForTimeout(300);
  await p.screenshot({ path: `${out}/${stem}-${name}.png`, fullPage: true });
  const over = await p.evaluate(() => {
    const w = innerWidth;
    const bad = [...document.querySelectorAll('body *')]
      .filter(e => e.getBoundingClientRect().right > w + 1 && !e.closest('.scroller, .tablewrap'))
      .slice(0, 5).map(e => `${e.tagName.toLowerCase()}.${e.className} ${(e.textContent || '').trim().slice(0, 24)}`);
    return { by: document.documentElement.scrollWidth - w, bad };
  });
  console.log(`${name}: 横向溢出 ${over.by}px${over.bad.length ? '，超出的元素：\n  ' + over.bad.join('\n  ') : ''}`);
  if (name !== 'phone') {
    const ids = await p.$$eval('figure[id^="fig"]', fs => fs.map(f => f.id));
    for (const id of ids) {
      const f = p.locator('#' + id); await f.scrollIntoViewIfNeeded();
      await f.screenshot({ path: `${out}/${stem}-${id}-${colorScheme}.png` });
    }
  }
  await p.close();
}

for (const spec of frames) {
  const [id, times] = spec.split(':');
  const p = await b.newPage({ viewport: { width: 1280, height: 900 } });
  await p.goto(page); await p.waitForTimeout(200);
  const f = p.locator('#' + id); await f.scrollIntoViewIfNeeded();
  const t0 = Date.now();
  for (const t of times.split(',').map(Number)) {
    const wait = t * 1000 - (Date.now() - t0); if (wait > 0) await p.waitForTimeout(wait);
    await f.screenshot({ path: `${out}/${stem}-${id}-t${t}.png` });
  }
  await p.close();
}
await b.close();
console.log(`图在 ${out}`);
