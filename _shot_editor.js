// ВРЕМЕННО: открывает локальный редактор (localhost:8080) в Chromium,
// вводит пароль, снимает скриншот и собирает ошибки консоли.
const puppeteer = require('puppeteer-extra');
const StealthPlugin = require('puppeteer-extra-plugin-stealth');
const fs = require('fs');
const path = require('path');
puppeteer.use(StealthPlugin());

const SHOT = path.join(__dirname, '_editor_shot.png');
const REP = path.join(__dirname, '_editor_report.txt');

(async () => {
  const errors = [], logs = [];
  const browser = await puppeteer.launch({ headless: 'new', args: ['--no-sandbox'] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1500, height: 900 });

  page.on('dialog', async d => {
    logs.push(`dialog(${d.type()}): ${d.message()}`);
    await d.accept('15201520');
  });
  page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  page.on('requestfailed', r => errors.push('requestfailed: ' + r.url() + ' -> ' + (r.failure() && r.failure().errorText)));

  await page.goto('http://localhost:8080/', { waitUntil: 'networkidle2', timeout: 45000 });
  await new Promise(r => setTimeout(r, 4000));

  const info = await page.evaluate(() => ({
    title: document.title,
    mapVisible: !!document.querySelector('#map .leaflet-tile-pane'),
    tiles: document.querySelectorAll('#map img.leaflet-tile').length,
    markers: document.querySelectorAll('.dir-marker').length,
    routeItems: document.querySelectorAll('#routes-checkboxes .route-check-item').length,
    paletteDots: document.querySelectorAll('#color-palette button').length,
    stopCards: document.querySelectorAll('.stop-card').length,
    routesBtnLabel: (document.getElementById('routes-btn-label') || {}).textContent,
    routeLabel: (document.getElementById('route-label') || {}).textContent,
    dir: (document.getElementById('direction') || {}).value,
    hasAIButton: !!document.querySelector('[data-ai], #btn-ai, .ai-btn'),
    buttonsOnPage: [...document.querySelectorAll('button')].map(b => (b.id || '') + ':' + (b.textContent || '').trim().slice(0, 18)).filter(s => s.length > 1)
  }));

  await page.screenshot({ path: SHOT });
  await browser.close();

  const out = [];
  out.push('=== состояние редактора ===');
  out.push(JSON.stringify(info, null, 2));
  out.push('');
  out.push('=== логи диалогов ===');
  out.push(logs.join('\n') || '(нет)');
  out.push('');
  out.push('=== ошибки/сетевые сбои (' + errors.length + ') ===');
  out.push(errors.slice(0, 40).join('\n') || '(нет)');
  fs.writeFileSync(REP, out.join('\n'), 'utf8');
  console.log('screenshot ok');
})();
