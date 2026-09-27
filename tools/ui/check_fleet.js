/*
 * Дешёвая проверка живого парка в браузере: поток (SSE), кнопки маршрутов,
 * метки джерела на них (GPS/sim), источник «симулятор», режим «рух».
 *
 * Зачем отдельно от tools/ui/check_emulator.js: тот прогоняет ВЕСЬ сценарий
 * эмулятора, включая /api/plan, — то есть тратит ключ OpenRouter. Здесь только
 * парк: кнопки AI не жмём, зато проверяем то, что не видно в pytest — реальный
 * EventSource, фильтр маркеров и переключение привода парка.
 *
 * Запуск: python main.py (в одном окне), node tools/ui/check_fleet.js (в другом).
 * Аргумент — адрес страницы (по умолчанию локальный emulator.html).
 * Скриншот — tools/ui/out/fleet_filter.png.
 */
const path = require('path');
const puppeteer = require('puppeteer-extra');
const StealthPlugin = require('puppeteer-extra-plugin-stealth');

puppeteer.use(StealthPlugin());

const URL = process.argv[2] || 'http://127.0.0.1:8000/ui/emulator.html';
const MODEL_TIME = '2026-09-17T12:00';
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

(async () => {
  const browser = await puppeteer.launch({ headless: 'new', args: ['--no-sandbox'] });
  const page = await browser.newPage();
  const errors = [];
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', (e) => errors.push(String(e)));

  // Перехват запросов парка: проверяем, что троллейбусы выключены именно
  // в ЗАПРОСЕ (vehicle_types=bus), а не только спрятаны на карте.
  const fleetRequests = [];
  page.on('request', (req) => {
    const url = req.url();
    if (url.includes('/api/fleet/stream') || url.includes('/api/live?')) {
      fleetRequests.push(url);
    }
  });

  await page.goto(URL, { waitUntil: 'domcontentloaded' });
  await page.setViewport({ width: 1400, height: 900 });

  // Кнопки маршрутов приходят из /api/manifest (38) — до вмикання парку.
  await page.waitForFunction(
    () => document.querySelectorAll('.route-chip').length > 20, { timeout: 20000 });
  const chips = await page.$$eval('.route-chip', (els) => els.map((el) => el.dataset.key));
  console.log('кнопок маршрутів:', chips.length, '| приклади:', chips.slice(0, 4).join(', '));

  // Парк выключен по умолчанию: подсказка рядом с галочкой — единственный
  // видимый на телефоне сигнал об этом (#fleet-status ниже списка маршрутов).
  const hintOff = await page.$eval('#fleet-toggle-note', (el) => el.textContent.trim());
  console.log('парк вимкнено, підказка:', hintOff || '(порожньо)');

  await page.click('#fleet-toggle');
  await page.waitForFunction(
    () => document.querySelectorAll('.veh-marker').length > 5, { timeout: 30000 });
  const first = await page.evaluate(() => ({
    markers: document.querySelectorAll('.veh-marker').length,
    feed: (document.getElementById('fleet-feed') || {}).textContent || '',
    source: (document.querySelector('#fleet-source button[aria-pressed="true"]') || {}).dataset
      ? document.querySelector('#fleet-source button[aria-pressed="true"]').dataset.source : '',
  }));
  console.log('після вмикання: маркерів', first.markers,
    '| джерело:', first.source, '| канал:', first.feed.trim());
  const hintOn = await page.$eval('#fleet-toggle-note', (el) => el.textContent.trim());
  console.log('після вмикання, підказка:', hintOn || '(порожньо)');

  await page.$eval('#model-time', (input, value) => {
    input.value = value;
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }, MODEL_TIME);
  await sleep(2500);

  // Троллейбусы: по умолчанию выключены и в ЗАПРОСЕ (vehicle_types=bus), и на
  // карте. Проверяем оба уровня: чипы (8 троллейбусных = aria-pressed false)
  // и число маркеров до/после галочки «тролейбуси».
  const trolleyOff = await page.evaluate(() => ({
    off: document.querySelectorAll('.route-chip[aria-pressed="false"]').length,
    checked: (document.getElementById('fleet-trolley') || {}).checked === true,
    markers: document.querySelectorAll('.veh-marker').length,
  }));
  const askedBusOnly = fleetRequests.some((url) => url.includes('vehicle_types=bus'));
  console.log('за замовчуванням: вимкнених кнопок', trolleyOff.off,
    '| галочка увімкнена:', trolleyOff.checked, '| маркерів', trolleyOff.markers);

  await page.click('#fleet-trolley');
  await sleep(2500);
  const trolleyOn = await page.evaluate(() => ({
    off: document.querySelectorAll('.route-chip[aria-pressed="false"]').length,
    markers: document.querySelectorAll('.veh-marker').length,
  }));
  const askedTrolleys = fleetRequests.some((url) => url.includes('vehicle_types=bus%2Ctrolley'));
  console.log('після галочки: вимкнених кнопок', trolleyOn.off, '| маркерів', trolleyOn.markers,
    '| в запросах bus:', askedBusOnly, '| bus+trolley:', askedTrolleys);

  // Фильтр: «жодного» → 0 машин, «усі» → все обратно, один маршрут → минус его машины.
  await page.click('#fleet-route-none');
  await sleep(500);
  const none = await page.evaluate(() => ({
    markers: document.querySelectorAll('.veh-marker').length,
    filter: (document.getElementById('fleet-filter-status') || {}).textContent || '',
    // Крайні режими фільтра: «жодного» має бути натиснутим, інакше на телефоні
    // не відрізнити «фільтр стоїть» від «ТС не їдуть».
    pressed: (document.getElementById('fleet-route-none') || {}).getAttribute
      ? document.getElementById('fleet-route-none').getAttribute('aria-pressed') : '',
  }));
  console.log('«жодного»: маркерів', none.markers, '| фільтр:', none.filter.trim(),
    '| натиснуто:', none.pressed);

  await page.click('#fleet-route-all');
  await sleep(500);
  const all = await page.evaluate(() => ({
    markers: document.querySelectorAll('.veh-marker').length,
    filter: (document.getElementById('fleet-filter-status') || {}).textContent || '',
    pressed: (document.getElementById('fleet-route-all') || {}).getAttribute
      ? document.getElementById('fleet-route-all').getAttribute('aria-pressed') : '',
  }));
  console.log('«усі»: маркерів', all.markers, '| фільтр:', all.filter.trim(),
    '| натиснуто:', all.pressed);

  const chipKey = chips[chips.length - 1];
  await page.click('.route-chip[data-key="' + chipKey + '"]');
  await sleep(500);
  const one = await page.evaluate(() => ({
    markers: document.querySelectorAll('.veh-marker').length,
    off: document.querySelectorAll('.route-chip[aria-pressed="false"]').length,
    filter: (document.getElementById('fleet-filter-status') || {}).textContent || '',
  }));
  console.log('сховано', chipKey, '-> маркерів', one.markers,
    '| кнопок вимкнено:', one.off, '| фільтр:', one.filter.trim());

  // Диагностика блока кнопок: 38 маршрутов, порядок и прокрутка (высота блока
  // ограничена, поэтому «на глаз» по скриншоту список читается неверно).
  const grid = await page.evaluate(() => {
    const box = document.getElementById('fleet-routes');
    const keys = [...box.querySelectorAll('.route-chip')].map((el) => el.dataset.key);
    return {
      count: keys.length,
      first: keys[0],
      last: keys[keys.length - 1],
      scrollTop: Math.round(box.scrollTop),
      hidden: box.scrollHeight - box.clientHeight,
    };
  });
  console.log('блок маршрутів: усього', grid.count, '| перший', grid.first,
    '| останній', grid.last, '| прокрутка', grid.scrollTop, 'із', grid.hidden);

  // Перемикання джерела: симулятор (в тестах трекера немає — це й перевіряємо).
  await page.click('#fleet-source button[data-source="sim"]');
  await sleep(2500);
  const simMode = await page.evaluate(() => ({
    markers: document.querySelectorAll('.veh-marker').length,
    feed: (document.getElementById('fleet-feed') || {}).textContent || '',
  }));
  console.log('джерело=sim: маркерів', simMode.markers, '| канал:', simMode.feed.trim());

  // «Рух»: поллинг вместо потока (модельное время идёт).
  await page.click('#fleet-play');
  await sleep(3000);
  const play = await page.evaluate(() => ({
    label: (document.getElementById('fleet-play') || {}).textContent || '',
    status: (document.getElementById('fleet-status') || {}).textContent || '',
    feed: (document.getElementById('fleet-feed') || {}).textContent || '',
  }));
  console.log('«рух»:', play.label.trim(), '|', play.status.trim(), '| канал:', play.feed.trim());

  // Метки джерела на чипах маршрутів: локально трекера немає, тому всі
  // маршрути з машинами — «sim», решта — «none». Сума мусить дати 38 кнопок.
  const marksSim = await page.evaluate(() => ({
    gps: document.querySelectorAll('.route-chip[data-source="gps"]').length,
    sim: document.querySelectorAll('.route-chip[data-source="sim"]').length,
    none: document.querySelectorAll('.route-chip[data-source="none"]').length,
    legend: (document.getElementById('fleet-routes-legend') || {}).textContent || '',
  }));
  console.log('метки без трекера: GPS', marksSim.gps, '| sim', marksSim.sim,
    '| none', marksSim.none, '| легенда:', marksSim.legend.trim());

  // GPS-стан метки: трекера локально немає, тому підмішуємо змішаний сріз
  // через публічний міні-API (той самий applySnapshot, що й кадр потоку).
  const marksGps = await page.evaluate(() => {
    const chip = document.querySelector('.route-chip[data-source="sim"]');
    if (!chip) return null;
    const key = chip.dataset.key;
    const type = key.slice(0, key.indexOf('|'));
    const label = key.slice(key.indexOf('|') + 1);
    window.Emulator.applySnapshot({
      source: 'mixed',
      vehicles: [{
        vehicle_type: type, route_label: label, board_number: 'TEST-001',
        lat: 48.2921, lon: 25.9358, is_live: true, speed_kmh: 18.4,
        heading_deg: 100, source: 'real',
      }],
    });
    const after = document.querySelector('.route-chip[data-key="' + key + '"]');
    return {
      key: key,
      source: after ? after.dataset.source : '',
      badge: after ? (after.querySelector('.route-chip-src') || {}).textContent : '',
      gpsChecked: after ? after.classList.contains('is-gps') : false,
      gps: document.querySelectorAll('.route-chip[data-source="gps"]').length,
      sim: document.querySelectorAll('.route-chip[data-source="sim"]').length,
      legend: (document.getElementById('fleet-routes-legend') || {}).textContent || '',
    };
  });
  console.log('підмішаний real:', marksGps && marksGps.key, '-> метка', marksGps && marksGps.badge,
    '| data-source', marksGps && marksGps.source, '| GPS-кнопок', marksGps && marksGps.gps,
    '| легенда:', marksGps ? marksGps.legend.trim() : '(нема)');
  await page.screenshot({ path: path.join(__dirname, 'out', 'fleet_filter.png') });

  console.log('помилок консолі:', errors.length, errors.join(' | '));
  await browser.close();

  const ok = first.markers > 5 && first.source === 'auto' && /потік/.test(first.feed) &&
    // троллейбусы: выключены в запросе и на карте, включаются галочкой
    trolleyOff.off === 8 && trolleyOff.checked === false && askedBusOnly &&
    trolleyOn.off === 0 && trolleyOn.markers > trolleyOff.markers && askedTrolleys &&
    none.markers === 0 && all.markers > 5 && one.markers < all.markers && one.off === 1 &&
    // крайние режимы фильтра видны на кнопках, подсказка парка гаснет/загорается
    none.pressed === 'true' && all.pressed === 'true' && hintOff.length > 0 && hintOn === '' &&
    // ровно 38 кнопок: алиасы перевозчика («3/3a») не должны давать вторую кнопку
    grid.count === 38 && grid.first === 'bus|1' && grid.last === 'trolley|8' &&
    // метки джерела: без трекера чипи з машинами — sim, у легенді «жодного»;
    // GPS-метка з'являється, коли підмішали real-сріз (window.Emulator)
    marksSim.gps === 0 && marksSim.sim > 0 &&
    marksSim.gps + marksSim.sim + marksSim.none === 38 && /жодного/.test(marksSim.legend) &&
    marksGps && marksGps.source === 'gps' && marksGps.badge === 'GPS' && marksGps.gpsChecked &&
    marksGps.gps === 1 && /1 із 38/.test(marksGps.legend) &&
    /пауза/.test(play.label) && errors.length === 0;
  console.log(ok ? 'OK: потік і фільтр працюють у браузері.' : 'FAIL');
  process.exit(ok ? 0 : 1);
})().catch((err) => {
  console.error('SMOKE UI ERROR:', err);
  process.exit(1);
});
