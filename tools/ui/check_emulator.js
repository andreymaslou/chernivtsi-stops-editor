/*
 * Проверка эмулятора «глазами»: открывает страницу в Chromium, проверяет DOM,
 * снимает скриншоты и убеждается, что ТС реально двигаются.
 *
 * Запуск из корня репозитория:
 *   node tools/ui/check_emulator.js
 *   node tools/ui/check_emulator.js --url http://169.58.82.105:8000/ui/emulator.html --out C:\\Temp\\ui_vps
 *
 * --url — какой эмулятор проверять, --text — фраза для сценария плана,
 * --out — куда сложить PNG и report.json (по умолчанию tools/ui/out).
 *
 * Что проверяется:
 *   1. страница грузится без ошибок консоли и без упавших запросов;
 *   2. легенда карти: 5 рядів за специфікацією, CSS, клик и свайп не двигают карту;
 *   3. чекбокс «живий парк» рисует машины стрелками (у каждой data-heading);
 *   4. режим «▶ рух» реально двигает машины (пиксельные позиции меняются);
 *   5. фраза -> план: полілінії маршруту + ТС плана теж стрелками;
 *   5.1 карта плану (UX): номери кроків у кольорі маршруту з пульсацією, шеврони
 *      напрямку (азимут звіряється з `leg.path`), хвости маршруту, фініш 🏁,
 *      іконка 🚶 на пешій пересадці, цифри в панелі = бейджам на карті;
 *   5.2 картки варіантів (§13 брифа): дві картки над саммарі, перша активна,
 *      «програшна» цифра підсвічена приглушеним (⚠️, не червоним) з поясненням;
 *      клік по другій картці перемальовує лінії на карті й цифри в саммарі;
 *      телеметрія §13.3: подія йде і при показі карток (chosen=null), і при
 *      кліку (chosen=id), з тим самим порядком карток, що на екрані;
 *   6. попап машини: структура, світлий бейдж, екранування зовнішніх рядків;
 *   7. розвантаження при віддаленні: zoom < 13 — крапки без номера й стрілки,
 *      zoom >= 13 — повний маркер, і крапка не з'їжджає з координати;
 *   7.1 бейдж «SIM» на віртуальних машинах (§3): на стенді всі машини sim —
 *      плашка та клас .sim є на кожному маркері, data-source збігається;
 *   8. на 390 px: контроли парка видны, легенда влезает в экран, нижняя полоса
 *      (подсказка о клике / легенда / кнопка «Емулятор») не перекрывается, а на
 *      /ui/editor.html ещё и адаптив — карта на весь экран, панели открываются
 *      шторками (сверху редактор, снизу эмулятор).
 *
 * Отчёт и PNG: tools/ui/out/ (в git не попадает).
 */
const fs = require('fs');
const path = require('path');
const puppeteer = require('puppeteer-extra');
const StealthPlugin = require('puppeteer-extra-plugin-stealth');

puppeteer.use(StealthPlugin());

const args = process.argv.slice(2);
function argValue(name, fallback) {
  const index = args.indexOf(name);
  return index >= 0 && args[index + 1] ? args[index + 1] : fallback;
}

const URL = argValue('--url', 'http://127.0.0.1:8000/ui/emulator.html');
const PHRASE = argValue('--text', 'Я на Соборці, їду на Гравітон');
// Фіксований час плану (опціонально): інтерсепт POST /api/plan і додає ?now.
// Потрібен, коли на поточний час у маршруту лише 1 пересадка — тоді сервер
// чесно віддає один варіант, і блок карток не має що перевіряти.
const PLAN_NOW = argValue('--plan-now', '');
// Час у моделі фіксуємо в робочому вікні маршрутів (06:00–22:00): симулятор
// «затискає» ніч до середини дня, тобто поза вікном парк у моделі НЕРУХОМИЙ —
// інакше перевірка «машины двигаются» залежала б від годинника машини.
const MODEL_TIME = argValue('--model-time', '2026-09-17T12:00');
const OUT = path.resolve(argValue('--out', path.join(__dirname, 'out')));
fs.mkdirSync(OUT, { recursive: true });

const report = { url: URL, phrase: PHRASE, checks: {}, errors: [], shots: [] };

function check(name, value, note) {
  report.checks[name] = { value, note: note || '' };
  console.log((value ? '  OK   ' : '  FAIL ') + name + (note ? ' — ' + note : ''));
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** #rrggbb -> 'rgb(r, g, b)': в таком виде цвета отдаёт getComputedStyle. */
function rgb(hex) {
  const value = parseInt(hex.slice(1), 16);
  return 'rgb(' + ((value >> 16) & 255) + ', ' + ((value >> 8) & 255) + ', ' + (value & 255) + ')';
}

const probe = () => ({
  markers: document.querySelectorAll('.veh-marker').length,
  wraps: [...document.querySelectorAll('.veh-wrap')].map((el) => ({
    heading: el.getAttribute('data-heading'),
    board: el.getAttribute('data-board'),
    source: el.getAttribute('data-source'),
    planned: el.classList.contains('planned'),
    stopped: el.classList.contains('stopped'),
    target: el.classList.contains('target'),
    sim: el.classList.contains('sim'),
  })),
  // Бейдж «SIM» на віртуальних машинах (§3: симулятор не видається за живий GPS)
  simBadges: [...document.querySelectorAll('.veh-sim-badge')].map((el) => el.textContent.trim()),
  targetBoards: [...document.querySelectorAll('.veh-wrap.target')]
    .map((el) => el.getAttribute('data-board')),
  targetMarkers: document.querySelectorAll('.veh-marker.is-target').length,
  // Геометрия маркера из спецификации: круг r=11, стрелка в <g> с rotate(...),
  // подпись — вне группы (не крутится), цвет заливки стрелки = цвет обводки.
  geometry: (() => {
    const wrap = document.querySelector('.veh-wrap');
    if (!wrap) return null;
    const svg = wrap.querySelector('svg');
    const circle = svg.querySelector('circle');
    const arrow = svg.querySelector('.veh-arrow');
    const group = svg.querySelector('.veh-arrow-group') || svg.querySelector('g');
    const text = svg.querySelector('text');
    return {
      viewBox: svg.getAttribute('viewBox'),
      radius: circle.getAttribute('r'),
      arrowInGroup: !!group && arrow.parentNode === group,
      groupTransform: group ? group.getAttribute('transform') : '',
      textOutsideGroup: !!text && text.parentNode !== group,
      textAnchor: text ? text.getAttribute('text-anchor') : '',
      heading: wrap.getAttribute('data-heading'),
    };
  })(),
  transforms: [...document.querySelectorAll('.veh-marker')].map((el) => el.style.transform),
  // Пеший отрезок проверяем синтетическим планом: у реальных фраз пешей ноги
  // может не быть вовсе (старт и финиш совпадают с остановками посадки), а
  // проверить пунктир, иконку и новый фолбэк «прямая между остановками» надо.
  // После замера возвращаем настоящий план на карту.
  mapWalk: (() => {
    const api = window.Emulator || {};
    const real = (typeof api.lastPlan === 'function' ? api.lastPlan() : null) ||
      (typeof state !== 'undefined' ? state.lastPlan : null);
    if (!real || typeof api.renderPlan !== 'function' || typeof api.clearLayers !== 'function') {
      return null;
    }
    const rides = real.legs.filter((leg) => leg.type === 'transit' && Array.isArray(leg.path));
    if (!rides.length) return null;
    const synthetic = {
      legs: [
        rides[0],
        { type: 'transfer', kind: 'walk', at: 'Тестова зупинка', walk_min: 2, wait_min: 1 },
        rides[rides.length - 1],
      ],
      vehicles: [],
      to_stop_id: real.to_stop_id,
    };
    api.clearLayers();
    api.renderPlan(synthetic);
    const walkLine = document.querySelector('.plan-walk-line');
    const out = {
      icons: document.querySelectorAll('.plan-walk-icon').length,
      lines: document.querySelectorAll('.plan-walk-line').length,
      dash: walkLine ? getComputedStyle(walkLine).strokeDasharray.replace(/px/g, '') : null,
      steps: document.querySelectorAll('.plan-step').length,
      tails: document.querySelectorAll('.plan-tail').length,
    };
    api.clearLayers();
    api.renderPlan(real);
    return out;
  })(),
  polylines: document.querySelectorAll('.leaflet-overlay-pane path').length,
  // Карта плана (UX-апгрейд): номера шагов, шевроны направления, хвосты,
  // финиш, иконка пешего отрезка. Ожидаемые значения считаем из ответа сервера
  // (state.lastPlan), поэтому проверка ловит и «не нарисовали», и «нарисовали
  // не то».
  mapUx: (() => {
    const api = window.Emulator || {};
    const plan = (typeof api.lastPlan === 'function' ? api.lastPlan() : null) ||
      ((typeof state !== 'undefined' && state.lastPlan) ? state.lastPlan : null);
    const transit = plan ? plan.legs.filter((leg) => leg.type === 'transit') : [];
    const walkLegs = plan ? plan.legs.filter((leg) => leg.kind === 'walk') : [];
    const bearingOf = (lat1, lon1, lat2, lon2) => {
      const mid = ((lat1 + lat2) / 2) * Math.PI / 180;
      const dlat = lat2 - lat1;
      const dlon = (lon2 - lon1) * Math.cos(mid);
      if (!dlat && !dlon) return 0;
      return (Math.atan2(dlon, dlat) * 180) / Math.PI % 360;
    };
    // Очiкуваний азимут шеврона: азимут сегмента посередині. Маршрут може
    // проходити одну й ту саму дорогу «туди-назад» (вилет/петля) — тоді у
    // різних сегментів однаковий центр, але азимути розходяться на 180°.
    // Шеврон малюється на конкретному сегменті, тому збираємо всіх кандидатів
    // i засчитуємо збіг із будь-яким з них.
    const expectedBearings = (lat, lon) => {
      const out = [];
      for (const leg of transit) {
        const path = Array.isArray(leg.path) ? leg.path : [];
        for (let i = 0; i < path.length - 1; i += 1) {
          const a = path[i];
          const b = path[i + 1];
          const at = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
          if (Math.abs(at[0] - lat) < 1e-9 && Math.abs(at[1] - lon) < 1e-9) {
            const value = bearingOf(a[0], a[1], b[0], b[1]);
            out.push((value + 360) % 360);
          }
        }
      }
      return out.length ? out : null;
    };
    const chevrons = [...document.querySelectorAll('.plan-arrow-icon')]
      .map((el) => {
        const lat = Number(el.getAttribute('data-lat'));
        const lon = Number(el.getAttribute('data-lon'));
        const want = expectedBearings(lat, lon);
        const got = Number(el.getAttribute('data-bearing'));
        return {
          want, got,
          ok: want !== null && Number.isFinite(got) &&
            want.some((item) => Math.abs(item - got) < 0.6),
        };
      });
    const steps = [...document.querySelectorAll('.plan-step')].map((el) => {
      const style = getComputedStyle(el);
      return {
        step: el.getAttribute('data-step'),
        colour: el.getAttribute('data-colour'),
        background: style.backgroundColor,
        width: parseFloat(style.width),
        animation: style.animationName,
        text: el.textContent.trim(),
      };
    });
    const line = (selector) => {
      const el = document.querySelector(selector);
      const style = el ? getComputedStyle(el) : null;
      return style ? {
        count: document.querySelectorAll(selector).length,
        opacity: Number(style.strokeOpacity),
        weight: style.strokeWidth,
      } : null;
    };
    const walkLine = document.querySelector('.plan-walk-line');
    return {
      hasPlan: !!plan,
      transit: transit.length,
      // Хвіст маршруту малюється лише для ніг з path.length > 1: у дуже
      // короткої ноги (1 точка) «обрізати нічого», sliceOffset не знаходить
      // зрізу — хвоста не буде, і це не помилка.
      transitWithTail: transit.filter((leg) =>
        Array.isArray(leg.path) && leg.path.length > 1).length,
      walkLegs: walkLegs.length,
      steps,
      chevrons,
      chevronsOk: chevrons.filter((item) => item.ok).length,
      tails: line('.plan-tail'),
      active: line('.plan-active'),
      walkLine: walkLine
        ? getComputedStyle(walkLine).strokeDasharray.replace(/px/g, '') : null,
      walkIcons: document.querySelectorAll('.plan-walk-icon').length,
      finish: document.querySelectorAll('.plan-finish').length,
      panelDots: [...document.querySelectorAll('#answer .step-dot')]
        .map((el) => el.textContent.trim()),
      stopDots: document.querySelectorAll('.plan-stop-dot').length,
      expectedStops: transit.reduce((sum, leg) =>
        sum + (Array.isArray(leg.stops) ? leg.stops.length : 0), 0),
    };
  })(),
  // CSS спецификации: проверяем правила на одноразовых элементах, а не «на глаз».
  // Данные симулятора не дают «зупинено» (speed_kmh — средняя скорость маршрута),
  // поэтому правило stopped иначе не увидеть вообще.
  css: (() => {
    const host = document.createElement('div');
    host.innerHTML =
      '<div class="veh-wrap planned pr"><svg class="p"></svg></div>' +
      '<div class="veh-wrap stopped st"><svg class="s"><circle class="c" r="11"/>' +
      '<path class="veh-arrow" d="M0 0"/></svg></div>' +
      '<div class="veh-wrap target tr"><svg class="t"></svg></div>';
    document.body.appendChild(host);
    const style = (selector) => getComputedStyle(host.querySelector(selector));
    const out = {
      // opacity/filter не наследуются, поэтому читаем обёртку .veh-wrap,
      // а не вложенный svg (у него своя тень).
      plannedOpacity: style('.pr').opacity,
      plannedGrayscale: style('.pr').filter,
      plannedSvgShadow: style('.p').filter,
      stoppedArrowDisplay: style('.s .veh-arrow').display,
      stoppedCircleAnimation: style('.s .c').animationName,
      targetAnimation: style('.t').animationName,
      targetShadow: style('.t').filter,
    };
    host.remove();
    return out;
  })(),
  // Розвантаження при віддаленні: клас на <body> + реальна геометрія маркера.
  // anchor — куди Leaflet мав поставити точку: зламаний transform-origin зсунув
  // би центр на ~6 px (маркер 36 px стискається до 23 px).
  zoom: (() => {
    const wrap = document.querySelector('.veh-wrap');
    const box = wrap ? wrap.getBoundingClientRect() : null;
    const svg = wrap ? wrap.querySelector('svg') : null;
    const text = svg ? svg.querySelector('text') : null;
    const arrow = svg ? svg.querySelector('.veh-arrow-group') : null;
    let anchor = null;
    if (wrap && box) {
      const icon = wrap.closest('.veh-marker');
      let layer = null;
      map.eachLayer((item) => { if (!layer && item._icon === icon) layer = item; });
      if (layer) {
        const area = map.getContainer().getBoundingClientRect();
        const point = map.latLngToContainerPoint(layer.getLatLng());
        anchor = [Math.round(area.left + point.x), Math.round(area.top + point.y)];
      }
    }
    return {
      level: map.getZoom(),
      dots: document.body.classList.contains('zoom-out'),
      width: box ? Math.round(box.width * 10) / 10 : 0,
      transform: wrap ? getComputedStyle(wrap).transform : '',
      transition: wrap ? getComputedStyle(wrap).transitionProperty : '',
      textDisplay: text ? getComputedStyle(text).display : 'none',
      arrowDisplay: arrow ? getComputedStyle(arrow).display : 'none',
      centre: box ? [Math.round(box.left + box.width / 2), Math.round(box.top + box.height / 2)] : null,
      anchor,
    };
  })(),
  // Попап машини: структура (п. 2 специфікації) + екранування зовнішніх рядків.
  // «hostile» — рядки, якими перевізник міг би вставити тег у попап.
  popup: (() => {
    const shot = (vehicle) => {
      const host = document.createElement('div');
      // vehiclePopup живёт внутри IIFE emulator.js — наружу торчит только через
      // публичный мини-API (window.Emulator), см. web/emulator.js.
      host.innerHTML = window.Emulator.vehiclePopup(vehicle);
      document.body.appendChild(host);
      const head = host.querySelector('.veh-popup-head');
      const badge = head ? head.querySelector('.badge') : null;
      const out = {
        head: !!head,
        body: !!host.querySelector('.veh-popup-body'),
        divider: head ? getComputedStyle(head).borderBottomWidth : '',
        badge: badge ? badge.textContent.trim() : '',
        badgeBackground: badge ? getComputedStyle(badge).backgroundColor : '',
        tags: host.querySelectorAll('i, img, script').length,
        text: host.textContent.replace(/\s+/g, ' ').trim(),
      };
      host.remove();
      return out;
    };
    return {
      live: shot({
        is_live: true, route_label: '7', board_number: '7-014', speed_kmh: 22.5,
        heading_deg: 123.4, gpstime: '12:30:05', age_seconds: 164, progress: 0.42,
      }),
      planned: shot({ is_live: false, route_label: '5', board_number: '5-010' }),
      hostile: shot({
        is_live: true, route_label: '<i>5</i>', board_number: '"><img src=x>',
        gpstime: '<script>alert(1)</script>',
      }),
    };
  })(),
  // Легенда карти: 4 ряда из спецификации (п. 2) + свои цвета иконок и стили
  // контейнера (п. 2.2). Отдельно сравниваем прямоугольник с атрибуцией OSM:
  // оба контрола живут в правом нижнем углу и не должны пересекаться.
  legend: (() => {
    const box = document.querySelector('.map-legend');
    if (!box) return null;
    const rows = [...box.querySelectorAll('.legend-row')].map((row) => {
      const icon = row.querySelector('.legend-icon');
      const style = getComputedStyle(icon);
      return {
        // icon лежит внутри строки: из текста его вырезаем, иначе «● Живий».
        text: row.textContent.replace(icon.textContent, '').trim(),
        icon: icon.textContent.trim(),
        colour: style.color,
        background: style.backgroundColor,
        spacing: style.letterSpacing,
        shadow: style.textShadow,
      };
    });
    const style = getComputedStyle(box);
    const rect = box.getBoundingClientRect();
    const attribution = document.querySelector('.leaflet-control-attribution');
    const other = attribution ? attribution.getBoundingClientRect() : null;
    const crosses = (a, b) => !(a.bottom <= b.top || a.top >= b.bottom ||
      a.right <= b.left || a.left >= b.right);
    const area = document.querySelector('.leaflet-container').getBoundingClientRect();
    return {
      rows,
      background: style.backgroundColor,
      borderWidth: style.borderTopWidth,
      borderColour: style.borderTopColor,
      radius: style.borderRadius,
      fontSize: style.fontSize,
      blur: style.backdropFilter || style.webkitBackdropFilter || '',
      bottomRight: !!box.closest('.leaflet-bottom.leaflet-right'),
      aria: box.getAttribute('aria-label'),
      attrOverlap: other ? crosses(rect, other) : null,
      insideMap: rect.left >= area.left - 1 && rect.right <= area.right + 1 &&
        rect.top >= area.top - 1 && rect.bottom <= area.bottom + 1,
      rect: {
        left: Math.round(rect.left), top: Math.round(rect.top),
        right: Math.round(rect.right), bottom: Math.round(rect.bottom),
      },
    };
  })(),
  answer: (document.getElementById('answer') || {}).textContent || '',
  fleetStatus: (document.getElementById('fleet-status') || {}).textContent || '',
  status: (document.getElementById('status') || {}).textContent || '',
  playLabel: (document.getElementById('fleet-play') || {}).textContent || '',
});
(async () => {
  const browser = await puppeteer.launch({ headless: 'new', args: ['--no-sandbox'] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1400, height: 900 });
  // Headless Chrome по умолчанию отдаёт prefers-reduced-motion: reduce, и наши
  // пульсации честно выключаются (@media в emulator.html). Для проверки
  // анимации явно просим «как у обычного пользователя» — а сам выключатель
  // проверяем отдельным ассертом ниже.
  await page.emulateMediaFeatures([{ name: 'prefers-reduced-motion', value: 'no-preference' }]);

  page.on('console', (message) => {
    if (message.type() === 'error') report.errors.push('console: ' + message.text());
  });
  page.on('pageerror', (error) => report.errors.push('pageerror: ' + error.message));
  page.on('requestfailed', (request) => report.errors.push('requestfailed: ' + request.url()));
  page.on('response', (response) => {
    if (response.status() >= 400) {
      report.errors.push('HTTP ' + response.status() + ': ' + response.url());
    }
  });
  // Телеметрія вибору варіанта (§13.3): UI шле POST /api/telemetry/plan_choice
  // при появі карток (chosen=null) і при кліку по них. Запити fire-and-forget,
  // але їх треба бачити в перевірці — збираємо тіла тут.
  const telemetry = [];
  page.on('request', (request) => {
    if (request.method() === 'POST' && /\/api\/telemetry\/plan_choice/.test(request.url())) {
      let body = null;
      try { body = request.postData() ? JSON.parse(request.postData()) : null; }
      catch (err) { body = { _raw: request.postData() }; }
      telemetry.push(body);
    }
  });
  // --plan-now: підставляємо фіксований час у запит плану (див. PLAN_NOW).
  if (PLAN_NOW) {
    await page.setRequestInterception(true);
    page.on('request', (request) => {
      if (request.method() === 'POST' && /\/api\/plan$/.test(request.url()) &&
          request.postData()) {
        try {
          const body = JSON.parse(request.postData());
          if (!body.now) body.now = PLAN_NOW;
          request.continue({ postData: JSON.stringify(body) });
          return;
        } catch (err) { /* битий body — пропускаємо як є */ }
      }
      request.continue();
    });
  }

  await page.goto(URL, { waitUntil: 'networkidle2', timeout: 60000 });
  await sleep(1500);
  const initial = await page.evaluate(probe);
  check('страница загрузилась', !/не вдалось/.test(initial.status), initial.status.trim());
  check('контроли парка на месте',
    !!(await page.$('#fleet-toggle')) && !!(await page.$('#fleet-play')) && !!(await page.$('#model-time')));
  check('до включения парка машин нет', initial.markers === 0, 'маркеров: ' + initial.markers);




  // --- 2.1 Попап машини (специфікація дизайну, п. 2) -----------------------
  // Розмітка будується vehiclePopup() — читаємо те, що реально вийшло в DOM.
  const livePopup = initial.popup.live;
  check('попап структурований за специфікацією',
    livePopup.head && livePopup.body && livePopup.divider === '1px' &&
    /Маршрут 7/.test(livePopup.text) && /Борт: 7-014/.test(livePopup.text) &&
    /Швидкість: 22\.5 км\/год/.test(livePopup.text) && /Курс: 123°/.test(livePopup.text) &&
    /Рейс: 42%/.test(livePopup.text) && /Дані: 12:30:05 \(164 с тому\)/.test(livePopup.text),
    livePopup.text.slice(0, 150));

  check('попап екранує зовнішні рядки (XSS)',
    initial.popup.hostile.tags === 0 && /<i>5<\/i>/.test(initial.popup.hostile.text) &&
    /<script>alert\(1\)<\/script>/.test(initial.popup.hostile.text),
    'тегів із даних перевізника: ' + initial.popup.hostile.tags + ' | текст: ' +
    initial.popup.hostile.text.slice(0, 130));

  // Бейдж живе на світлому попапі Leaflet: прозорий фон = невидимий підпис.
  // Перевірка ловить помилку в імені класу (у спеці був `planned`, у CSS `.plan`).
  const opaque = (value) => !!value && value !== 'transparent' && !/rgba\(0, 0, 0, 0\)/.test(value);
  check('бейдж попапа читається на світлому тлі',
    initial.popup.live.badge === 'GPS' && initial.popup.planned.badge === 'Розклад' &&
    opaque(initial.popup.live.badgeBackground) && opaque(initial.popup.planned.badgeBackground),
    '"GPS": ' + initial.popup.live.badgeBackground + ', "Розклад": ' +
    initial.popup.planned.badgeBackground);

  // --- 3. Живой парк ------------------------------------------------------
  await page.click('#fleet-toggle');
  await page.waitForFunction(
    () => document.querySelectorAll('.veh-marker').length > 5, { timeout: 30000 });
  // Час моделі — фіксований (див. MODEL_TIME): поза вікном роботи маршрутів
  // симулятор віддає одну й ту саму позицію парку, і «рух» довести неможливо.
  await page.$eval('#model-time', (input, value) => {
    input.value = value;
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }, MODEL_TIME);
  await sleep(1200);
  const fleet = await page.evaluate(probe);
  const headings = fleet.wraps
    .map((item) => Number(item.heading))
    .filter((value) => Number.isFinite(value));
  const rotated = headings.filter((value) => Math.abs(value) > 0.5).length;
  check('машины нарисованы', fleet.markers > 5, 'маркеров: ' + fleet.markers);
  check('у машин есть курс', rotated > 0, 'ненулевых курсов: ' + rotated + ' из ' + headings.length);
  check('в статусе видно время', /час:/.test(fleet.fleetStatus), fleet.fleetStatus.trim());

  // --- 3.1 Бейдж «SIM» на віртуальних машинах (§3 брифа) ------------------
  // Стенд працює на PARK_SOURCE=sim, тож УСІ машини віртуальні: плашка «SIM»
  // має бути на кожному маркері (джерело беремо з кореневого source знімка —
  // в моно-режимі машини поле source не несуть). В змішаному парку плачка
  // стоїть лише на sim-машинах (перевіряємо через data-source).
  const allSim = fleet.wraps.length > 0 && fleet.wraps.every((item) => item.sim);
  const badgesOk = fleet.simBadges.length === fleet.wraps.length &&
    fleet.simBadges.every((text) => text === 'SIM');
  check('віртуальні машини помічені «SIM»',
    allSim && badgesOk,
    'sim-класів: ' + fleet.wraps.filter((item) => item.sim).length + ' із ' + fleet.wraps.length +
      ', бейджів: ' + fleet.simBadges.length);
  check('data-source маркера збігається з класом sim',
    fleet.wraps.length > 0 && fleet.wraps.every((item) =>
      (item.source === 'sim') === item.sim),
    'джерела: ' + [...new Set(fleet.wraps.map((item) => item.source || '—'))].join(', '));
  await page.screenshot({ path: path.join(OUT, 'fleet.png') });
  report.shots.push('fleet.png');

  // --- 4. Движение --------------------------------------------------------
  await page.click('#fleet-play');
  await sleep(1200);
  const before = await page.evaluate(probe);
  await sleep(2600);
  const after = await page.evaluate(probe);
  const beforeSet = new Set(before.transforms);
  const moved = after.transforms.filter((value) => !beforeSet.has(value)).length;
  check('режим «рух» включился', /пауза/.test(after.playLabel), after.playLabel.trim());
  check('машины двигаются', moved > 0,
    'сменили позицию: ' + moved + ' из ' + after.transforms.length);
  check('время в модели идёт', before.fleetStatus !== after.fleetStatus, after.fleetStatus.trim());
  await page.screenshot({ path: path.join(OUT, 'fleet_play.png') });
  report.shots.push('fleet_play.png');
  await page.click('#fleet-play');  // пауза

  // --- 4.1 Розвантаження при віддаленні (специфікація дизайну, п. 4) ------
  // Порог зуму живе в DOT_ZOOM_BELOW (web/emulator.js). Ставимо зум справжнім
  // map.setZoom(), щоб перевіряти саме обробник 'zoomend', а не клас руками.
  const atZoom = async (level) => {
    await page.evaluate((value) => new Promise((resolve) => {
      if (map.getZoom() === value) { resolve(); return; }
      map.once('zoomend', () => setTimeout(resolve, 150));
      map.setZoom(value);
    }), level);
    // У headless кадри рідкі: фіксований таймаут ловив transition масштабу в
    // підльоті («matrix(0.84)» навіть за 900 мс після zoomend) — і зум-пункти
    // флейкали без жодної реальної поломки (docs/STATUS.md §5 п.9). Чекаємо
    // саме усталення масштабу першого маркера: це і є предмет перевірки.
    // Не усталилось за 5 с — probe зафіксує поточний стан, асерти проваляться.
    await page.waitForFunction(() => {
      const wrap = document.querySelector('.veh-wrap');
      if (!wrap) return true;
      const matrix = new DOMMatrixReadOnly(getComputedStyle(wrap).transform);
      const target = document.body.classList.contains('zoom-out') ? 0.65 : 1;
      return Math.abs(matrix.a - target) < 0.02;
    }, { timeout: 5000, polling: 150 }).catch(() => {});
    return page.evaluate(probe);
  };
  const dots = await atZoom(12);
  await page.screenshot({ path: path.join(OUT, 'zoom12_dots.png') });
  report.shots.push('zoom12_dots.png');
  const full = await atZoom(14);
  await page.screenshot({ path: path.join(OUT, 'zoom14_markers.png') });
  report.shots.push('zoom14_markers.png');

  check('при віддаленні маркери стають крапками',
    dots.zoom.dots && /matrix\(0\.6[5-7]/.test(dots.zoom.transform) &&
    dots.zoom.textDisplay === 'none' && dots.zoom.arrowDisplay === 'none',
    'зум ' + dots.zoom.level + ': клас zoom-out=' + dots.zoom.dots + ', transform=' +
    dots.zoom.transform + ', номер=' + dots.zoom.textDisplay + ', стрілка=' + dots.zoom.arrowDisplay);

  check('при наближенні маркер повертається',
    !full.zoom.dots && full.zoom.textDisplay !== 'none' && full.zoom.arrowDisplay !== 'none' &&
    full.zoom.width > dots.zoom.width,
    'зум ' + full.zoom.level + ': номер=' + full.zoom.textDisplay + ', стрілка=' +
    full.zoom.arrowDisplay + ', ширина маркера ' + full.zoom.width + ' px проти ' +
    dots.zoom.width + ' px');

  // Маркер має лишатись на своїй координаті: зламаний transform-origin зсунув би
  // центр на пів-різниці стиснення (~6 px) і машини «з'їхали б» з вулиць.
  const drift = (state) => (state.zoom.centre && state.zoom.anchor)
    ? Math.max(Math.abs(state.zoom.centre[0] - state.zoom.anchor[0]),
      Math.abs(state.zoom.centre[1] - state.zoom.anchor[1])) : null;
  const driftFull = drift(full);
  const driftDots = drift(dots);
  check('крапка не з\'їжджає з координати',
    driftFull !== null && driftDots !== null && driftFull <= 10 && driftDots <= 10,
    'зсув центру маркера від точки Leaflet: ' + driftFull + ' px при 14, ' +
    driftDots + ' px при 12');

  check('стиснення маркера плавне (transition містить transform)',
    /transform/.test(full.zoom.transition),
    'transition-property: ' + full.zoom.transition);

  // --- 5. План маршрута ---------------------------------------------------
  await page.click('#ask-text');
  await page.type('#ask-text', PHRASE);
  await page.click('#ask-btn');
  await page.waitForFunction(
    () => {
      const answer = document.getElementById('answer');
      return answer && answer.style.display !== 'none' && answer.textContent.trim().length > 10;
    },
    { timeout: 90000 });
  await sleep(1500);
  const plan = await page.evaluate(probe);
  check('план показан', /план маршруту|розбір фрази|уточнення/.test(plan.answer),
    plan.answer.replace(/\s+/g, ' ').slice(0, 120));
  check('на карте есть лінія маршруту', plan.polylines > 0, 'path-элементов: ' + plan.polylines);
  check('ТС плана тоже стрелками', plan.markers > 0, 'маркеров: ' + plan.markers);
  check('маркер ТС за специфікацією',
    !!plan.geometry && plan.geometry.viewBox === '0 0 64 64' && plan.geometry.radius === '16' &&
    plan.geometry.arrowInGroup && plan.geometry.textOutsideGroup &&
    plan.geometry.textAnchor === 'middle',
    JSON.stringify(plan.geometry));
  check('стрілка повертається за курсом маркера',
    !!plan.geometry && plan.geometry.groupTransform === 'rotate(' + plan.geometry.heading + ' 32 32)',
    plan.geometry ? plan.geometry.groupTransform + ' при курсі ' + plan.geometry.heading : 'нет маркеров');
  // Анимации состояний на странице редактора живут в emulator-panel.css и носят
  // префикс `emu-` (пространство имён: редактор и эмулятор делят одну страницу,
  // см. docs/STATUS.md §5 п.9, пункт «б»). На /ui/emulator.html имена без префикса.
  // Поэтому сверяем имя без префикса — так проверка ловит и «переименовали», и
  // «правило пропало», но не путается со страницей.
  const pulseBase = (value) => String(value || '').replace(/^emu-/, '');
  check('CSS станів за специфікацією',
    plan.css.plannedOpacity === '0.6' && /grayscale/.test(plan.css.plannedGrayscale) &&
    plan.css.stoppedArrowDisplay === 'none' &&
    pulseBase(plan.css.stoppedCircleAnimation) === 'pulse-stopped' &&
    pulseBase(plan.css.targetAnimation) === 'pulse-target' &&
    /gold|rgb\(255, 215, 0\)/i.test(plan.css.targetShadow),
    'planned=' + plan.css.plannedOpacity + '/' + plan.css.plannedGrayscale +
    ' stopped=' + plan.css.stoppedArrowDisplay + '/' + plan.css.stoppedCircleAnimation +
    ' target=' + plan.css.targetAnimation + '/' + plan.css.targetShadow);

  // Цель плана («сідати: <борт>») обязана светиться тем же бортом, что в тексте плана.
  const boarding = (plan.answer.match(/сідати: ([^,\s]+)/) || [])[1] || null;
  if (boarding && boarding !== 'ТЗ') {
    check('ціль плана підсвічена', plan.targetBoards.includes(boarding),
      'у плані «сідати: ' + boarding + '», підсвічено: ' + (plan.targetBoards.join(', ') || 'нікого') +
      ', маркерів із is-target: ' + plan.targetMarkers);
  } else {
    check('ціль плана підсвічена', true, 'у плані немає живого ТС для посадки — цілі немає');
  }

  // --- 5.1 Карта плана: номери кроків, шеврони, хвости, фініш ---------------
  const ux = plan.mapUx || {};
  const stepNumbers = (ux.steps || []).map((item) => item.step).join(',');
  check('номери кроків на карті',
    !!ux.hasPlan && ux.transit > 0 && ux.steps.length === ux.transit,
    'бейджів: ' + ux.steps.length + ' [' + stepNumbers + '] при ' + ux.transit + ' поїздках');
  check('бейдж кроку крупний і пульсує',
    ux.steps.length > 0 && ux.steps.every((item) => item.width >= 28 && item.animation !== 'none'),
    ux.steps.map((item) => '№' + item.step + ': ' + item.width + 'px/' + item.animation).join(', '));
  check('колір бейджа = колір маршруту',
    ux.steps.length > 0 && ux.steps.every((item) => item.background === rgb(item.colour)),
    ux.steps.map((item) => '№' + item.step + ': ' + item.background + ' vs ' +
      rgb(item.colour)).join(', '));
  check('шеврони напрямку на активній лінії',
    ux.chevrons.length > 0 && ux.chevronsOk === ux.chevrons.length,
    'шевронів: ' + ux.chevrons.length + ', з вірним азимутом: ' + ux.chevronsOk +
    (ux.chevronsOk === ux.chevrons.length ? '' : ', розбіжності: ' +
      JSON.stringify(ux.chevrons.filter((item) => !item.ok).slice(0, 3))));
  check('точки проміжних зупинок', ux.stopDots === ux.expectedStops,
    'точок: ' + ux.stopDots + ' (очікується ' + ux.expectedStops + ')');
  check('хвости маршруту напівпрозорі',
    !!ux.tails && !!ux.active && ux.tails.count === ux.transitWithTail &&
    ux.tails.opacity > 0 && ux.tails.opacity < ux.active.opacity,
    ux.tails ? 'хвостів: ' + ux.tails.count + ' (ніг з path>1: ' + ux.transitWithTail +
      ' із ' + ux.transit + '), opacity ' + ux.tails.opacity +
      ' проти активної ' + ux.active.opacity : 'немає .plan-tail');
  check('іконка пішохода на пересадці',
    !!plan.mapWalk && plan.mapWalk.icons === 1 && plan.mapWalk.lines === 1,
    plan.mapWalk ? 'іконок: ' + plan.mapWalk.icons + ', пунктирів: ' + plan.mapWalk.lines +
      ' (синтетичний план: пеша нога без геометрії → пряма між зупинками)'
      : 'немає синтетичного плану');
  check('пунктир пешої ноги не змінився',
    !!plan.mapWalk && String(plan.mapWalk.dash).replace(/\s+/g, '') === '1,10',
    'stroke-dasharray: ' + (plan.mapWalk ? plan.mapWalk.dash : 'немає'));
  check('фініш позначено', ux.finish === 1, 'іконок фінішу: ' + ux.finish);
  check('цифри кроків у панелі = бейджам на карті',
    (ux.panelDots || []).length === ux.transit &&
    (ux.panelDots || []).join(',') === stepNumbers,
    'у панелі: ' + JSON.stringify(ux.panelDots) + ', на карті: [' + stepNumbers + ']');

  // Доступность: при prefers-reduced-motion пульсація бейджа зобов'язана
  // вимкнутись (правило живе в @media emulator.html).
  await page.emulateMediaFeatures([{ name: 'prefers-reduced-motion', value: 'reduce' }]);
  const reducedPulse = await page.evaluate(() => {
    const el = document.querySelector('.plan-step');
    return el ? getComputedStyle(el).animationName : null;
  });
  await page.emulateMediaFeatures([{ name: 'prefers-reduced-motion', value: 'no-preference' }]);
  check('пульсація вимикається на prefers-reduced-motion', reducedPulse === 'none',
    'animation-name при reduce: ' + reducedPulse);
  const plannedCount = plan.wraps.filter((item) => item.planned).length;
  const stoppedCount = plan.wraps.filter((item) => item.stopped).length;
  console.log('       станів: live=' + (plan.wraps.length - plannedCount) + ' planned=' + plannedCount +
    ' stopped=' + stoppedCount + ' target=' + plan.targetBoards.length);
  await page.screenshot({ path: path.join(OUT, 'plan.png') });
  report.shots.push('plan.png');

  // --- 5.2 Картки варіантів плану (поставка 1, §13 брифа) ------------------
  // Сервер кладе в /api/plan масив variants: перший елемент — кореневий план
  // («Швидкий», він уже на карті), другий — прогін «≤1 пересадка» («Дешевий»).
  // Картки живуть у #plan-variants НАД саммарі (#answer). Клік по другій
  // мусить: зупинити голос, перемалювати план і цифри панелі, підсвітити картку.
  const variantsProbe = () => page.evaluate(() => {
    const box = document.getElementById('plan-variants');
    const answer = document.getElementById('answer');
    const cards = box ? Array.prototype.slice.call(box.querySelectorAll('.variant-card')) : [];
    const geometry = Array.prototype.map.call(
      document.querySelectorAll('.plan-active'),
      (path) => path.getAttribute('d'),
    ).join('|');
    let hash = 0;
    for (let i = 0; i < geometry.length; i += 1) {
      hash = ((hash * 31) + geometry.charCodeAt(i)) | 0;
    }
    const last = window.Emulator && window.Emulator.lastPlan();
    return {
      boxPresent: !!box,
      hidden: box ? box.hidden : true,
      count: cards.length,
      ids: cards.map((card) => card.getAttribute('data-variant-id')),
      texts: cards.map((card) => card.textContent.replace(/\s+/g, ' ').trim()),
      tags: cards.map((card) => {
        const el = card.querySelector('.variant-tag');
        return el ? el.textContent.trim() : '';
      }),
      notes: cards.map((card) => {
        const el = card.querySelector('.variant-note');
        return el ? el.textContent.trim() : '';
      }),
      noteColour: (() => {
        const el = box ? box.querySelector('.variant-card .variant-note') : null;
        return el ? getComputedStyle(el).color : null;
      })(),
      activeId: (() => {
        const el = box ? box.querySelector('.variant-card.active') : null;
        return el ? el.getAttribute('data-variant-id') : null;
      })(),
      worseCount: cards.reduce((sum, card) =>
        sum + card.querySelectorAll('.variant-num.worse').length, 0),
      summary: answer ? answer.textContent.replace(/\s+/g, ' ').trim() : '',
      planId: last ? (last.id || null) : null,
      lines: document.querySelectorAll('.plan-active').length,
      linesHash: hash,
    };
  });

  const variantsBefore = await variantsProbe();
  if (variantsBefore.count >= 2) {
    check('карточки вариантов отрисованы над саммари',
      variantsBefore.boxPresent && !variantsBefore.hidden && variantsBefore.count === 2,
      'карточек: ' + variantsBefore.count + ', id: [' + variantsBefore.ids.join(', ') + ']');
    check('корневой план — первая карточка (активна по умолчанию)',
      variantsBefore.activeId === variantsBefore.ids[0],
      'активная: ' + variantsBefore.activeId);
    check('карточка показывает тег, транспорт, цену и время',
      variantsBefore.tags.every(Boolean) &&
      variantsBefore.texts.every((text) => /грн/.test(text) && /хв/.test(text)),
      variantsBefore.texts.join(' | '));
    check('проигравший параметр подсвечен ⚠️ с объяснением',
      variantsBefore.worseCount > 0 && variantsBefore.notes.filter(Boolean).every((note) =>
        /дорожче|довше/.test(note)),
      'пояснения: [' + variantsBefore.notes.join('] [') + '], подсвеченных цифр: ' +
      variantsBefore.worseCount);
    check('подсветка «проигравшего» не чисто красная',
      variantsBefore.noteColour === null ||
      (variantsBefore.noteColour !== 'rgb(255, 0, 0)' &&
       variantsBefore.noteColour !== rgb('#ff5c5c')),
      'цвет пояснения: ' + variantsBefore.noteColour);

    // Телеметрія §13.3: картки показали → UI шле подію з chosen_variant_id = null
    // («показали, але не вибрали» — обов'язкова метрика). variant_order має
    // збігатися з порядком карток на екрані.
    await sleep(400);
    const shown = telemetry.find((body) => body && body.chosen_variant_id === null);
    check('телеметрія «показ карток» відправлена',
      !!shown && shown.variant_order.length === variantsBefore.count &&
      shown.variant_order.join(',') === variantsBefore.ids.join(','),
      shown ? 'order: [' + shown.variant_order.join(', ') + '], device: ' + shown.device_id +
        ', client: ' + shown.client + ', запитів: ' + telemetry.length
        : 'запитів: ' + telemetry.length);
    check('оффер телеметрії відповідає контракту лога',
      !!shown && Array.isArray(shown.offer) && shown.offer.length === variantsBefore.count &&
      shown.offer.every((entry) => entry && entry.id && Array.isArray(entry.tags) &&
        Number.isFinite(Number(entry.total_min)) && Number.isFinite(Number(entry.price_grn)) &&
        entry.source && entry.legs_signature),
      shown ? 'записів: ' + shown.offer.length + ', підписи: [' +
        shown.offer.map((entry) => entry.legs_signature).join('] [') + '], джерела: ' +
        shown.offer.map((entry) => entry.source).join(',')
        : 'немає запиту');

    await page.click('.plan-variants .variant-card[data-variant-id="' +
      variantsBefore.ids[1] + '"]');
    await sleep(1200);
    const variantsAfter = await variantsProbe();
    check('клик по второй карточке сделал её активной',
      variantsAfter.activeId === variantsBefore.ids[1],
      'активная: ' + variantsAfter.activeId);
    check('клик переключил план без перезапроса',
      variantsAfter.planId === variantsBefore.ids[1] &&
      variantsAfter.planId !== variantsBefore.planId,
      'последний план: ' + variantsBefore.planId + ' → ' + variantsAfter.planId);
    check('после клика линии на карте изменились',
      variantsAfter.lines > 0 &&
      (variantsAfter.linesHash !== variantsBefore.linesHash ||
       variantsAfter.lines !== variantsBefore.lines),
      'линий: ' + variantsBefore.lines + ' → ' + variantsAfter.lines +
      ', hash ' + variantsBefore.linesHash + ' → ' + variantsAfter.linesHash);
    check('после клика цифры в саммари изменились',
      variantsAfter.summary !== variantsBefore.summary,
      'саммари: «' + variantsAfter.summary.slice(0, 90) + '»');

    // Телеметрія §13.3: клік → подія з обраним варіантом; дефолт залишається
    // першою карткою (роутер так і віддає — перший елемент variants).
    await sleep(400);
    const chosen = telemetry.find((body) => body &&
      body.chosen_variant_id === variantsBefore.ids[1]);
    check('телеметрія «клік по картці» відправлена',
      !!chosen && chosen.default_variant_id === variantsBefore.ids[0],
      chosen ? 'обрано: ' + chosen.chosen_variant_id + ' із order [' +
        chosen.variant_order.join(', ') + '], запитів: ' + telemetry.length
        : 'запитів: ' + telemetry.length);
    await page.screenshot({ path: path.join(OUT, 'variants.png') });
    report.shots.push('variants.png');

    // Возвращаем первый вариант — остальной сценарий смотрит дефолтный вид.
    await page.click('.plan-variants .variant-card[data-variant-id="' +
      variantsBefore.ids[0] + '"]');
    await sleep(800);
  } else {
    console.log('        (карточки вариантов: пропущено — сервер дал один вариант, это честный ответ)');
    check('один вариант — карточек нет (только стандартный текст)',
      variantsBefore.count === 0 && variantsBefore.hidden,
      'карточек: ' + variantsBefore.count + ', панель скрыта: ' + variantsBefore.hidden);
  }

  // Перед переходом на мобильную ширину убеждаемся, что десктопная вёрстка цела:
  // обёртка шторки (.drawer) не должна ничего сдвинуть — обе левые колонки слева
  // от карты, панель эмулятора справа поверх неё, кнопки/затемнение скрыты.
  // На отдельной /ui/emulator.html этих элементов нет — проверки пропускаются.
  const desktopLayout = await page.evaluate(() => {
    const box = (sel) => {
      const el = document.querySelector(sel);
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return { left: Math.round(r.left), right: Math.round(r.right) };
    };
    const display = (sel) => {
      const el = document.querySelector(sel);
      return el ? getComputedStyle(el).display : null;
    };
    return {
      forms: box('.sidebar-forms'),
      list: box('.sidebar-list'),
      map: box('.map-container'),
      emu: box('#emu-panel'),
      fab: display('#sheet-toggle-editor'),
      scrim: display('#sheet-scrim'),
    };
  });
  if (desktopLayout.forms && desktopLayout.list && desktopLayout.map) {
    check('десктоп: обидві колонки ліворуч від карти',
      desktopLayout.forms.right <= desktopLayout.map.left + 2 &&
      desktopLayout.list.right <= desktopLayout.map.left + 2,
      'список до ' + desktopLayout.list.right + ', карта з ' + desktopLayout.map.left);
  }
  if (desktopLayout.emu && desktopLayout.map) {
    check('десктоп: панель емулятора справа поверх карти',
      desktopLayout.emu.right >= desktopLayout.map.right - 2,
      'панель до ' + desktopLayout.emu.right + ' / карта ' + desktopLayout.map.right);
  }
  if (desktopLayout.fab !== null) {
    check('десктоп: кнопки й затемнення шторок сховані',
      desktopLayout.fab === 'none' && desktopLayout.scrim === 'none',
      'кнопка ' + desktopLayout.fab + ', затемнення ' + desktopLayout.scrim);
  }

  // --- 6. Мобильный вид (шторки) ------------------------------------------
  await page.setViewport({ width: 390, height: 844, isMobile: true });
  await sleep(1200);
  await page.screenshot({ path: path.join(OUT, 'mobile.png') });
  report.shots.push('mobile.png');
  const mobile = await page.evaluate(() => {
    const box = document.getElementById('fleet-toggle').getBoundingClientRect();
    const legend = document.querySelector('.map-legend');
    const legendBox = legend ? legend.getBoundingClientRect() : null;
    return {
      visible: box.width > 0 && box.height > 0,
      width: box.width,
      legendFits: !!legendBox && legendBox.left >= 0 && legendBox.right <= window.innerWidth &&
        legendBox.top >= 0 && legendBox.bottom <= window.innerHeight,
      legendWidth: legendBox ? Math.round(legendBox.width) : 0,
    };
  });
  check('на 390 px контроли парка видны', mobile.visible, 'ширина ' + Math.round(mobile.width) + 'px');

  // Нижня смуга екрана: підказка про клік (.map-hint), легенда карти
  // (.map-legend) і кнопка «🤖 Емулятор» ділять один кут. Правила підйому
  // легенди — mobile-блок у emulator-panel.css, підказки — у style.css.
  const bottomBand = await page.evaluate(() => {
    const box = (sel) => {
      const el = document.querySelector(sel);
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return {
        left: Math.round(r.left), right: Math.round(r.right),
        top: Math.round(r.top), bottom: Math.round(r.bottom),
      };
    };
    const hit = (a, b) => !!a && !!b &&
      a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
    const hint = box('.map-hint');
    const legend = box('.map-legend');
    const fab = box('#sheet-toggle-emu');
    return {
      hint, legend, fab,
      hintInsideScreen: !!hint && hint.left >= 0 && hint.right <= window.innerWidth &&
        hint.top >= 0 && hint.bottom <= window.innerHeight,
      hintVsLegend: hit(hint, legend),
      hintVsFab: hit(hint, fab),
      legendVsFab: hit(legend, fab),
    };
  });
  if (bottomBand.hint && bottomBand.legend) {
    check('на 390 px підказка не налазить на легенду', !bottomBand.hintVsLegend,
      'підказка ' + JSON.stringify(bottomBand.hint) + ', легенда ' + JSON.stringify(bottomBand.legend));
    check('на 390 px підказка в межах екрана', bottomBand.hintInsideScreen,
      JSON.stringify(bottomBand.hint));
  } else {
    console.log('        (підказка про клік: пропущено — на цій сторінці немає .map-hint)');
  }
  if (bottomBand.fab && bottomBand.hint) {
    check('на 390 px кнопка емулятора не налазить на підказку', !bottomBand.hintVsFab,
      'кнопка ' + JSON.stringify(bottomBand.fab) + ', підказка ' + JSON.stringify(bottomBand.hint));
  }
  if (bottomBand.fab && bottomBand.legend) {
    check('на 390 px кнопка емулятора не налазить на легенду', !bottomBand.legendVsFab,
      'кнопка ' + JSON.stringify(bottomBand.fab) + ', легенда ' + JSON.stringify(bottomBand.legend));
  }

  // --- 6.1 Адаптив объединённой страницы: карта + шторки -------------------
  // Шторки есть только на /ui/editor.html (на отдельной /ui/emulator.html нет
  // ни #drawer-editor, ни кнопок) — поэтому проверяем по факту наличия.
  const hasSheets = await page.evaluate(() => !!document.getElementById('drawer-editor'));
  if (hasSheets) {
    const layout = await page.evaluate(() => {
      const box = (el) => el.getBoundingClientRect();
      const map = box(document.querySelector('.map-container'));
      const drawer = box(document.getElementById('drawer-editor'));
      const emu = box(document.getElementById('emu-panel'));
      const fabEditor = box(document.getElementById('sheet-toggle-editor'));
      const fabEmu = box(document.getElementById('sheet-toggle-emu'));
      return {
        vw: window.innerWidth, vh: window.innerHeight,
        mapW: Math.round(map.width), mapH: Math.round(map.height),
        drawerBottom: Math.round(drawer.bottom),
        emuTop: Math.round(emu.top),
        fabEditorTop: Math.round(fabEditor.top),
        fabEmuBottom: Math.round(fabEmu.bottom),
        hidden: 'none',
        fabDisplay: getComputedStyle(document.getElementById('sheet-toggle-editor')).display,
      };
    });
    check('карта займає весь вьюпорт',
      layout.mapH >= layout.vh - 2 && layout.mapW >= layout.vw - 2,
      layout.mapW + 'x' + layout.mapH + ' з ' + layout.vw + 'x' + layout.vh);
    check('обидві шторки за замовчуванням сховані',
      layout.drawerBottom <= 0 && layout.emuTop >= layout.vh,
      'редактор bottom ' + layout.drawerBottom + ', емулятор top ' + layout.emuTop);
    check('кнопки шторок видно',
      layout.fabDisplay !== layout.hidden && layout.fabEditorTop >= 0 && layout.fabEmuBottom <= layout.vh,
      'редактор top ' + layout.fabEditorTop + ', емулятор bottom ' + layout.fabEmuBottom);

    // Верхня шторка: редактор виїжджає зверху + затемнення
    await page.click('#sheet-toggle-editor');
    await sleep(600);
    const editorSheet = await page.evaluate(() => {
      const el = document.getElementById('drawer-editor');
      const r = el.getBoundingClientRect();
      return {
        top: Math.round(r.top),
        visible: r.bottom > 0 && r.top > -2 && getComputedStyle(el).visibility === 'visible',
        scrim: document.getElementById('sheet-scrim').classList.contains('show'),
      };
    });
    check('кнопка «Редактор» відкриває верхню шторку',
      editorSheet.visible && editorSheet.scrim, 'top ' + editorSheet.top);

    // Шторка має бути НАД затемненням: інакше вона видима, але «неактивна» —
    // усі кліки/тапи ловить .sheet-scrim (z-index 1240), а базовий z-index
    // панелі емулятора 1100 (у .drawer 1245 — тому верхня працювала).
    const drawerHit = await page.evaluate((id) => {
      const el = document.getElementById(id);
      const r = el.getBoundingClientRect();
      const target = document.elementFromPoint(
        Math.round(r.left + r.width / 2), Math.round(r.top + r.height / 2));
      return {
        target: target ? (target.id || String(target.className) || target.tagName) : null,
        inside: !!(target && el.contains(target)),
      };
    }, 'drawer-editor');
    check('верхня шторка ловить кліки (а не затемнення)', drawerHit.inside,
      'у центрі шторки лежить: ' + drawerHit.target);
    await page.screenshot({ path: path.join(OUT, 'mobile-sheet-editor.png') });
    report.shots.push('mobile-sheet-editor.png');

    // Нижня шторка: емулятор виїжджає знизу, редактор мусить закритися сам
    await page.click('#sheet-toggle-emu');
    await sleep(600);
    const emuSheet = await page.evaluate(() => {
      const el = document.getElementById('emu-panel');
      const r = el.getBoundingClientRect();
      return {
        bottom: Math.round(r.bottom),
        visible: r.top < window.innerHeight && getComputedStyle(el).visibility === 'visible',
        editorBottom: Math.round(document.getElementById('drawer-editor').getBoundingClientRect().bottom),
      };
    });
    check('кнопка «Емулятор» відкриває нижню шторку',
      emuSheet.visible && emuSheet.bottom >= layout.vh - 2, 'bottom ' + emuSheet.bottom);
    check('одночасно відкрита лише одна шторка', emuSheet.editorBottom <= 0,
      'редактор bottom ' + emuSheet.editorBottom);

    // Та сама перевірка для нижньої шторки + «живий» тест: у поле запиту
    // всередині шторки мусить доходити ввід (без звернення до AI — Enter не тиснемо).
    const emuHit = await page.evaluate((id) => {
      const el = document.getElementById(id);
      const inside = (target) => !!(target && el.contains(target));
      const r = el.getBoundingClientRect();
      const target = document.elementFromPoint(
        Math.round(r.left + r.width / 2), Math.round(r.top + r.height / 2));
      const field = document.getElementById('ask-text');
      const f = field.getBoundingClientRect();
      const fieldTarget = document.elementFromPoint(
        Math.round(f.left + f.width / 2), Math.round(f.top + f.height / 2));
      return {
        target: target ? (target.id || String(target.className) || target.tagName) : null,
        inside: inside(target),
        fieldInside: inside(fieldTarget),
      };
    }, 'emu-panel');
    check('нижня шторка ловить кліки (а не затемнення)',
      emuHit.inside && emuHit.fieldInside,
      'у центрі шторки: ' + emuHit.target + ', над полем запиту: ' + emuHit.fieldInside);

    await page.click('#ask-text');
    await page.type('#ask-text', '!');
    await sleep(200);
    const typed = await page.evaluate(() => {
      const field = document.getElementById('ask-text');
      return {
        endsWithBang: field.value.endsWith('!'),
        focused: document.activeElement && document.activeElement.id,
        open: document.getElementById('emu-panel').classList.contains('open'),
      };
    });
    check('поле запиту в шторці приймає ввід і шторка не закривається',
      typed.endsWithBang && typed.focused === 'ask-text' && typed.open,
      'текст закінчується «!»: ' + typed.endsWithBang + ', фокус: ' + typed.focused +
      ', шторка відкрита: ' + typed.open);

    await page.screenshot({ path: path.join(OUT, 'mobile-sheet-emu.png') });
    report.shots.push('mobile-sheet-emu.png');

    // Закриття кнопкою «▾» у заголовку шторки
    await page.click('#emu-panel [data-sheet-close="emu"]');
    await sleep(600);
    const closed = await page.evaluate(() => ({
      emuTop: Math.round(document.getElementById('emu-panel').getBoundingClientRect().top),
      scrim: document.getElementById('sheet-scrim').classList.contains('show'),
    }));
    check('кнопка «▾» згортає шторку', closed.emuTop >= layout.vh - 2 && !closed.scrim,
      'top ' + closed.emuTop);
  } else {
    console.log('        (шторки: пропущено — це окрема сторінка /ui/emulator.html)');
  }

  // Плитки OSM — внешний и «best effort» ресурс: один не доехавший тайл не
  // должен валить проверку (сеть на VPS/локалке флапает). Свои запросы и
  // ошибки JS по-прежнему считаются ошибками.
  report.errors = report.errors.filter((text) => !/favicon/.test(text)
    && !/tile\.openstreetmap\.org/.test(text));
  check('ошибок консоли нет', report.errors.length === 0, report.errors.join(' | '));

  await browser.close();
  fs.writeFileSync(path.join(OUT, 'report.json'), JSON.stringify(report, null, 1), 'utf-8');
  const failed = Object.entries(report.checks)
    .filter(([, item]) => !item.value).map(([name]) => name);
  console.log('\nотчёт: ' + path.join(OUT, 'report.json') + ' | скриншоты: ' + report.shots.join(', '));
  if (failed.length) {
    console.log('ПРОВАЛЕНО: ' + failed.join(', '));
    process.exit(1);
  }
  console.log('CHECK_EMULATOR_OK');
})().catch((error) => {
  console.error('ошибка проверки: ' + error.message);
  process.exit(2);
});