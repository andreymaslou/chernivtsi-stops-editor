/*
 * UI-проверка контекстной панели сленга в редакторе.
 *
 * Запуск (нужен python main.py на 127.0.0.1:8000):
 *   node tools/ui/check_slang_panel.js
 *   node tools/ui/check_slang_panel.js https://host/ui/editor.html --user u --pass p
 *
 * Тест намеренно кликает по настоящему маркеру активного маршрута, а не
 * выставляет hidden=false вручную: так проверяется весь путь пользователя.
 */
const puppeteer = require('puppeteer-extra');
const StealthPlugin = require('puppeteer-extra-plugin-stealth');

puppeteer.use(StealthPlugin());

const args = process.argv.slice(2);
function argValue(name, fallback) {
  const index = args.indexOf(name);
  return index >= 0 && args[index + 1] ? args[index + 1] : fallback;
}
function positionalUrl() {
  for (let i = 0; i < args.length; i += 1) {
    if (i > 0 && args[i - 1].startsWith('--')) continue;
    if (!args[i].startsWith('--')) return args[i];
  }
  return '';
}

const URL = argValue('--url', positionalUrl() || 'http://127.0.0.1:8000/ui/editor.html');
const AUTH_USER = argValue('--user', '');
const AUTH_PASS = argValue('--pass', '');
const AUTH = AUTH_USER && AUTH_PASS ? { username: AUTH_USER, password: AUTH_PASS } : null;
let failed = 0;

function check(name, value, note = '') {
  if (!value) failed += 1;
  console.log(`${value ? '  OK   ' : '  FAIL '}${name}${note ? ` — ${note}` : ''}`);
}

async function newPage(browser, viewport) {
  const page = await browser.newPage();
  if (AUTH) await page.authenticate(AUTH);
  // Обязательно до goto: иначе повторный прогон в том же Chromium может
  // использовать старый slang-panel.css с тем же URL-версией.
  await page.setCacheEnabled(false);
  await page.setViewport(viewport);
  return page;
}

async function waitForRouteAndMarker(page) {
  await page.waitForSelector('.route-line-cb[data-num="1"]', { timeout: 15000 });
  await page.evaluate(() => window.toggleRoute('1', true));
  await page.waitForSelector('.dir-marker', { visible: true, timeout: 20000 });
  // На мобильном маркер может перекрываться верхним слоем, поэтому отправляем
  // DOM-событие самому Leaflet-элементу: проверяется его реальный click handler,
  // а не hit-testing конкретной точки Puppeteer.
  await page.evaluate(() => {
    const marker = document.querySelector('.dir-marker');
    if (!marker) throw new Error('маркер остановки не найден');
    marker.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
  });
  await page.waitForFunction(() => {
    const panel = document.getElementById('slang-panel');
    const status = document.getElementById('slang-panel-status');
    if (!panel || !status) return false;
    const rect = panel.getBoundingClientRect();
    return !panel.hidden && panel.classList.contains('open') &&
      rect.width > 0 && rect.height > 0 && !status.textContent.includes('Завантаження');
  }, { timeout: 20000 });
}

async function metrics(page) {
  return page.evaluate(() => {
    const panel = document.getElementById('slang-panel');
    const map = document.querySelector('.map-container');
    const rect = (el) => {
      const r = el.getBoundingClientRect();
      return {
        left: r.left, top: r.top, right: r.right, bottom: r.bottom,
        width: r.width, height: r.height,
      };
    };
    return {
      panel: rect(panel), map: rect(map),
      viewport: { width: innerWidth, height: innerHeight },
      hidden: panel.hidden, open: panel.classList.contains('open'),
      display: getComputedStyle(panel).display,
      position: getComputedStyle(panel).position,
      cssBottom: getComputedStyle(panel).bottom,
      status: document.getElementById('slang-panel-status').textContent,
    };
  });
}

const near = (a, b, tolerance = 2) => Math.abs(a - b) <= tolerance;

(async () => {
  const browser = await puppeteer.launch({ headless: 'new', args: ['--no-sandbox'] });
  console.log(`Проверяю ${URL}${AUTH ? ` | Basic Auth: ${AUTH_USER}` : ' | без пароля'}`);
  const errors = [];
  try {
    const desktop = await newPage(browser, { width: 1400, height: 900 });
    desktop.on('pageerror', (error) => errors.push(`desktop: ${error}`));
    await desktop.goto(URL, { waitUntil: 'networkidle2', timeout: 45000 });
    await waitForRouteAndMarker(desktop);
    const d = await metrics(desktop);
    check('desktop: панель открылась после клика по маркеру', d.open && !d.hidden && d.display !== 'none', d.status);
    check('desktop: панель имеет ненулевые размеры', d.panel.width > 0 && d.panel.height > 0, `${Math.round(d.panel.width)}x${Math.round(d.panel.height)}`);
    check('desktop: панель начинается с левого верхнего угла карты', near(d.panel.left, d.map.left + 18) && near(d.panel.top, d.map.top + 18), `panel ${Math.round(d.panel.left)},${Math.round(d.panel.top)} / map ${Math.round(d.map.left)},${Math.round(d.map.top)}`);
    check('desktop: панель не выходит за карту по горизонтали', d.panel.left >= d.map.left - 2 && d.panel.right <= d.map.right + 2, `right ${Math.round(d.panel.right)}, map right ${Math.round(d.map.right)}`);
    check('desktop: панель позиционируется absolute', d.position === 'absolute', d.position);
    await desktop.close();

    const mobile = await newPage(browser, { width: 390, height: 844 });
    mobile.on('pageerror', (error) => errors.push(`mobile: ${error}`));
    await mobile.goto(URL, { waitUntil: 'networkidle2', timeout: 45000 });
    await waitForRouteAndMarker(mobile);
    const m = await metrics(mobile);
    check('mobile: панель открылась после клика по маркеру', m.open && !m.hidden && m.display !== 'none', m.status);
    check('mobile: панель растянута по ширине карты', near(m.panel.left, m.map.left) && near(m.panel.right, m.map.right), `panel ${Math.round(m.panel.left)}..${Math.round(m.panel.right)} / map ${Math.round(m.map.left)}..${Math.round(m.map.right)}`);
    check('mobile: низ панели совпадает с низом map-container', near(m.panel.bottom, m.map.bottom), `panel bottom ${Math.round(m.panel.bottom)}, map bottom ${Math.round(m.map.bottom)}`);
    check('mobile: панель находится внизу карты', m.panel.top > m.map.top && m.panel.height > 0 && m.panel.height <= m.map.height + 2, `top ${Math.round(m.panel.top)}, height ${Math.round(m.panel.height)}, map ${Math.round(m.map.height)}`);
    check('mobile: CSS действительно bottom: 0', m.cssBottom === '0px', m.cssBottom);
    await mobile.close();

    check('ошибок JavaScript нет', errors.length === 0, errors.join(' | '));
  } finally {
    await browser.close();
  }
  if (failed) {
    console.log(`CHECK_SLANG_PANEL_FAILED: ${failed}`);
    process.exit(1);
  }
  console.log('CHECK_SLANG_PANEL_OK');
})().catch((error) => {
  console.error(`ошибка проверки: ${error.message}`);
  process.exit(1);
});
