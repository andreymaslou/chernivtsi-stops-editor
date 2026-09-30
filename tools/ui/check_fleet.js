/*
 * Дешёвая проверка живого парка в браузере: поток (SSE), кнопки маршрутов,
 * метки джерела на них (GPS/sim), хрестик ❌ «GPS маршруту мертвий» (§3.6),
 * голосовий моніторинг маршрутів (фільтр парку за номером), источник
 * «симулятор», режим «рух».
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
    // Хрестиків у режимі «симулятор» бути не повинно: GPS там і не просили,
    // «жодної реальної машини» — це сам режим, а не втрата звʼязку.
    deads: document.querySelectorAll('.route-chip.is-dead').length,
    legend: (document.getElementById('fleet-routes-legend') || {}).textContent || '',
  }));
  console.log('метки без трекера: GPS', marksSim.gps, '| sim', marksSim.sim,
    '| none', marksSim.none, '| хрестиків', marksSim.deads,
    '| легенда:', marksSim.legend.trim());

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

  // Хрестик «GPS мертвий» (§3.6): маршрут, у срезі якого немає жодної машини з
  // реальним джерелом, — це фоллбек на симулятор (координат не приходило
  // понад годину). Трекера локально немає, тому обидва стани підмішуємо тим
  // самим applySnapshot: спершу срез з однією sim-машиною (мертві всі 38
  // маршрутів), потім — з real-машиною того ж маршруту (хрестик гасне, метка
  // стає GPS). Режим повертаємо на «авто»: у «симуляторі» хрестиків немає за
  // задумом, і це вже перевірено вище (marksSim.deads).
  await page.click('#fleet-source button[data-source="auto"]');
  const deadMarks = await page.evaluate(() => {
    const chip = document.querySelector('.route-chip[data-source="sim"]') ||
      document.querySelector('.route-chip');
    if (!chip) return null;
    const key = chip.dataset.key;
    const type = key.slice(0, key.indexOf('|'));
    const label = key.slice(key.indexOf('|') + 1);
    const vehicle = (source, board) => ({
      vehicle_type: type, route_label: label, board_number: board,
      lat: 48.2921, lon: 25.9358, is_live: source === 'real', speed_kmh: 18.4,
      heading_deg: 100, source: source,
    });
    // Хрестик мусить бути не лише в DOM, а й видимий: CSS показує його
    // виключно під класом .is-dead (інакше він тягнув би ширину чипа).
    const read = () => {
      const el = document.querySelector('.route-chip[data-key="' + key + '"]');
      const cross = el ? el.querySelector('.route-chip-dead') : null;
      return {
        dead: el ? el.classList.contains('is-dead') : null,
        cross: cross ? cross.textContent : '',
        shown: cross ? cross.offsetWidth > 0 : false,
        badge: el ? (el.querySelector('.route-chip-src') || {}).textContent : '',
        source: el ? el.dataset.source : '',
      };
    };
    const legendOf = () =>
      (document.getElementById('fleet-routes-legend') || {}).textContent || '';

    window.Emulator.applySnapshot({ source: 'mixed', vehicles: [vehicle('sim', 'SIM-FALLBACK')] });
    const simOnly = read();
    const simOnlyCount = document.querySelectorAll('.route-chip.is-dead').length;
    const simOnlyLegend = legendOf();

    window.Emulator.applySnapshot({ source: 'mixed', vehicles: [vehicle('real', 'TEST-REAL')] });
    const realBack = read();
    const realCount = document.querySelectorAll('.route-chip.is-dead').length;
    const realLegend = legendOf();

    return {
      key: key, simOnly: simOnly, simOnlyCount: simOnlyCount,
      simOnlyLegend: simOnlyLegend, realBack: realBack, realCount: realCount,
      realLegend: realLegend,
    };
  });
  console.log('хрестик ❌:', deadMarks && deadMarks.key,
    '| sim-сріз ->', deadMarks && (deadMarks.simOnly.shown ? deadMarks.simOnly.cross : 'нема'),
    '(мертвих', deadMarks && deadMarks.simOnlyCount + ')',
    '| real-сріз ->', deadMarks && (deadMarks.realBack.shown ? deadMarks.realBack.cross : 'нема'),
    '(мертвих', deadMarks && deadMarks.realCount + ', метка',
    deadMarks && deadMarks.realBack.source + ')',
    '| легенда:', deadMarks ? deadMarks.realLegend.trim() : '(нема)');
  await page.screenshot({ path: path.join(__dirname, 'out', 'fleet_filter.png') });

  // Голосовой мониторинг маршрутов (идея 2026-09-28, renderMonitorRoutes):
  // сервер вернул mode=monitor_routes — фронт не рисует путь А→Б, а применяет
  // фильтр парка к названным маршрутам. LLM и ключ OpenRouter тут не нужны:
  // подаём ответ напрямую через мини-API — тем же путём его отдал бы
  // /api/plan, если бы владелец сказал «покажи дев'ятку і десятку».
  const monitor = await page.evaluate(async () => {
    window.Emulator.applySnapshot({
      source: 'mixed',
      vehicles: [
        { vehicle_type: 'bus', route_label: '9', board_number: 'M-9', lat: 48.2921,
          lon: 25.9358, is_live: true, speed_kmh: 20, heading_deg: 90, source: 'sim' },
        { vehicle_type: 'bus', route_label: '10', board_number: 'M-10', lat: 48.2931,
          lon: 25.9368, is_live: true, speed_kmh: 20, heading_deg: 90, source: 'sim' },
        { vehicle_type: 'bus', route_label: '15', board_number: 'M-15', lat: 48.2941,
          lon: 25.9378, is_live: true, speed_kmh: 20, heading_deg: 90, source: 'sim' },
      ],
    });
    const before = document.querySelectorAll('.veh-marker').length;
    await window.Emulator.renderMonitorRoutes({
      mode: 'monitor_routes',
      routes: [
        { key: 'bus|9', type: 'bus', label: '9', number: '9' },
        { key: 'bus|10', type: 'bus', label: '10', number: '10' },
      ],
      missing_routes: [],
      // Геометрия линий: у 9-го два направления, у 10-го — одно. Вторая линия
      // 10-го не должна появиться (её и не прислали), а линия 15-го — тоже:
      // рисуем ТОЛЬКО запрошенные маршруты (идея 2026-09-29, п.2).
      shapes: [
        { key: 'bus|9', type: 'bus', label: '9', directions: [
          { direction: 'A', coords: [[48.2921, 25.9358], [48.2961, 25.9428]] },
          { direction: 'B', coords: [[48.2961, 25.9428], [48.2921, 25.9358]] },
        ] },
        { key: 'bus|10', type: 'bus', label: '10', directions: [
          { direction: 'A', coords: [[48.2931, 25.9368], [48.2991, 25.9318]] },
        ] },
        { key: 'bus|15', type: 'bus', label: '15', directions: [
          { direction: 'A', coords: [[48.2941, 25.9378], [48.3011, 25.9278]] },
        ] },
      ],
      message: 'Показую маршрути 9 та 10 — на карті лише їхні машини.',
      speech: { text: 'Показую маршрути 9 та 10.', lang: 'uk-UA' },
    });
    const chip15 = document.querySelector('.route-chip[data-key="bus|15"]');
    const shapesDrawn = document.querySelectorAll('.monitor-shape').length;
    // Выход из режима — любой следующий ответ ("Очистити карту", план, новый
    // мониторинг) чистит layerGroup: линии обязаны исчезнуть сами.
    window.Emulator.clearLayers();
    const shapesAfterClear = document.querySelectorAll('.monitor-shape').length;
    return {
      before: before,
      after: document.querySelectorAll('.veh-marker').length,
      on: [...document.querySelectorAll('.route-chip[aria-pressed="true"]')]
        .map((el) => el.dataset.key),
      off: document.querySelectorAll('.route-chip[aria-pressed="false"]').length,
      chip15Off: chip15 ? chip15.getAttribute('aria-pressed') : null,
      fleet: (document.getElementById('fleet-toggle') || {}).checked === true,
      shapes: { drawn: shapesDrawn, afterClear: shapesAfterClear },
      speech: window.Emulator.lastSpeech(),
      answer: (document.getElementById('answer') || {}).textContent || '',
      status: (document.getElementById('status') || {}).textContent || '',
    };
  });
  console.log('моніторинг маршрутів: увімкнено', monitor.on.join(' + '),
    '| маркерів', monitor.before, '->', monitor.after,
    '| кнопок вимкнено', monitor.off, '| парк увімкнено:', monitor.fleet);
  console.log('  голос:', JSON.stringify(monitor.speech),
    '| панель:', monitor.answer.trim().slice(0, 80),
    '| статус:', monitor.status.trim());
  console.log('  лінії маршрутів:', monitor.shapes.drawn,
    '(очікується 3: два напрямки 9-го + один у 10-го; линия 15-го не рисуется)',
    '| після виходу з режиму:', monitor.shapes.afterClear);

  // Уточнення типу ТС (бриф 01.10.2026, renderMonitorClarify): «покажи 4» без
  // типу — номер спільний, фронт малює три карточки (🚌/🚎/обидва). Тап по
  // карточці показує відповідний парк із ГОТОВОГО payload (routes+shapes), без
  // запиту й без пам'яті діалогу.
  const clarify = await page.evaluate(async () => {
    window.Emulator.applySnapshot({
      source: 'mixed',
      vehicles: [
        { vehicle_type: 'bus', route_label: '4', board_number: 'B-4', lat: 48.2921,
          lon: 25.9358, is_live: true, speed_kmh: 20, heading_deg: 90, source: 'sim' },
        { vehicle_type: 'trolley', route_label: '4', board_number: 'T-4', lat: 48.2931,
          lon: 25.9368, is_live: true, speed_kmh: 20, heading_deg: 90, source: 'sim' },
      ],
    });
    const option = (id, label, routes) => ({
      id, label, routes, shapes: [], missing_routes: [],
      message: 'Показую маршрут 4 — на карті лише його машини.',
      speech: { text: 'Показую маршрут 4.', lang: 'uk-UA' },
    });
    window.Emulator.renderMonitorClarify({
      mode: 'monitor_clarify',
      message: "Маршрут 4 є і в автобусів, і в тролейбусів — що показати?",
      speech: { text: "Маршрут 4 є і в автобусів, і в тролейбусів. Що показати?", lang: 'uk-UA' },
      clarify_options: [
        option('bus', '🚌 Автобуси', [{ key: 'bus|4', type: 'bus', label: '4', number: '4' }]),
        option('trolley', '🚎 Тролейбуси', [{ key: 'trolley|4', type: 'trolley', label: '4', number: '4' }]),
        option('both', '🚌🚎 Обидва', [
          { key: 'bus|4', type: 'bus', label: '4', number: '4' },
          { key: 'trolley|4', type: 'trolley', label: '4', number: '4' },
        ]),
      ],
    });
    const box = document.getElementById('monitor-clarify');
    const cards = box ? Array.prototype.slice.call(box.querySelectorAll('.clarify-card')) : [];
    const choices = cards.map((c) => c.getAttribute('data-clarify-choice'));
    const tags = cards.map((c) => ((c.querySelector('.variant-tag') || {}).textContent) || '');
    const lines = cards.map((c) => ((c.querySelector('.variant-route') || {}).textContent) || '');
    const question = (document.getElementById('answer') || {}).textContent || '';
    const trolleyCard = box ? box.querySelector('.clarify-card[data-clarify-choice="trolley"]') : null;
    if (trolleyCard) trolleyCard.click();
    await new Promise((r) => setTimeout(r, 300));
    const chipTrolley = document.querySelector('.route-chip[data-key="trolley|4"]');
    const chipBus = document.querySelector('.route-chip[data-key="bus|4"]');
    return {
      count: cards.length,
      choices: choices,
      tags: tags,
      lines: lines,
      question: question,
      hiddenAfter: box ? box.hidden : null,
      on: [...document.querySelectorAll('.route-chip[aria-pressed="true"]')].map((el) => el.dataset.key),
      chipTrolleyOff: chipTrolley ? chipTrolley.getAttribute('aria-pressed') : null,
      chipBusOff: chipBus ? chipBus.getAttribute('aria-pressed') : null,
      speech: window.Emulator.lastSpeech(),
    };
  });
  console.log('уточнення типу ТС: карточок', clarify.count,
    '| вибір:', clarify.choices.join(','), '| теги:', clarify.tags.join(' | '));
  console.log('  питання:', clarify.question.trim().slice(0, 64),
    '| рядки:', clarify.lines.join(' || '));
  console.log('  тап 🚎 -> увімкнено', clarify.on.join(','),
    '| trolley|4:', clarify.chipTrolleyOff, '/ bus|4:', clarify.chipBusOff,
    '| карточки сховано:', clarify.hiddenAfter, '| голос:', clarify.speech);

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
    // хрестик ❌ «GPS мертвий» (§3.6): у «симуляторі» його немає взагалі, а в
    // змішаному срізі мертві всі маршрути без real-машини — і жодного після
    // того, як real прийшов на цей маршрут
    marksSim.deads === 0 &&
    deadMarks && deadMarks.simOnly.dead === true && deadMarks.simOnly.cross === '❌' &&
    deadMarks.simOnly.shown && deadMarks.simOnlyCount === 38 &&
    /без GPS понад 1 годину: 38/.test(deadMarks.simOnlyLegend) &&
    deadMarks.realBack.dead === false && deadMarks.realBack.shown === false &&
    deadMarks.realBack.badge === 'GPS' && deadMarks.realBack.source === 'gps' &&
    deadMarks.realCount === 37 && /без GPS понад 1 годину: 37/.test(deadMarks.realLegend) &&
    // голосовий моніторинг: парк сужено до 9 і 10, 15 сховано, фраза озвучена
    monitor.before === 3 && monitor.after === 2 &&
    monitor.on.join(',') === 'bus|9,bus|10' && monitor.off === 36 &&
    monitor.chip15Off === 'false' && monitor.fleet &&
    /Показую маршрути 9 та 10\./.test(monitor.speech) &&
    /моніторинг/.test(monitor.answer) && /9 та 10/.test(monitor.answer) &&
    /показую маршрути: 9, 10/.test(monitor.status) &&
    // лінії маршрутів: тільки запрошені, і зникають разом із режимом
    monitor.shapes.drawn === 3 && monitor.shapes.afterClear === 0 &&
    // уточнення типу ТС: три карточки (bus/trolley/both), тап 🚎 сужує парк
    clarify.count === 3 && clarify.choices.join(',') === 'bus,trolley,both' &&
    clarify.tags.join('|') === '🚌 Автобуси|🚎 Тролейбуси|🚌🚎 Обидва' &&
    /🚌 4/.test(clarify.lines[0]) && /🚎 4/.test(clarify.lines[1]) &&
    /уточнення/.test(clarify.question) && /4 є і в автобусів/.test(clarify.question) &&
    clarify.on.join(',') === 'trolley|4' &&
    clarify.chipTrolleyOff === 'true' && clarify.chipBusOff === 'false' &&
    clarify.hiddenAfter === true && /Показую маршрут 4/.test(clarify.speech) &&
    /пауза/.test(play.label) && errors.length === 0;
  console.log(ok ? 'OK: потік і фільтр працюють у браузері.' : 'FAIL');
  process.exit(ok ? 0 : 1);
})().catch((err) => {
  console.error('SMOKE UI ERROR:', err);
  process.exit(1);
});
