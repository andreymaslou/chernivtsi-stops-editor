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
 *   2. легенда карти: 4 ряда за спецификацией, CSS, клик и свайп не двигают карту;
 *   3. чекбокс «живий парк» рисует машины стрелками (у каждой data-heading);
 *   4. режим «▶ рух» реально двигает машины (пиксельные позиции меняются);
 *   5. фраза -> план: полілінії маршруту + ТС плана теж стрелками;
 *   6. на 390 px контроли парка видны и легенда влезает в экран.
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
    planned: el.classList.contains('planned'),
    stopped: el.classList.contains('stopped'),
    target: el.classList.contains('target'),
  })),
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
      strokeMatchesFill: circle.getAttribute('stroke') === arrow.getAttribute('fill'),
      arrowInGroup: !!group && arrow.parentNode === group,
      groupTransform: group ? group.getAttribute('transform') : '',
      textOutsideGroup: !!text && text.parentNode !== group,
      textAnchor: text ? text.getAttribute('text-anchor') : '',
      heading: wrap.getAttribute('data-heading'),
    };
  })(),
  transforms: [...document.querySelectorAll('.veh-marker')].map((el) => el.style.transform),
  polylines: document.querySelectorAll('.leaflet-overlay-pane path').length,
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

  await page.goto(URL, { waitUntil: 'networkidle2', timeout: 60000 });
  await sleep(1500);
  const initial = await page.evaluate(probe);
  check('страница загрузилась', !/не вдалось/.test(initial.status), initial.status.trim());
  check('контроли парка на месте',
    !!(await page.$('#fleet-toggle')) && !!(await page.$('#fleet-play')) && !!(await page.$('#model-time')));
  check('до включения парка машин нет', initial.markers === 0, 'маркеров: ' + initial.markers);

  // --- 2. Легенда карти ----------------------------------------------------
  // Статичный контрол: проверяем сразу после загрузки, пока карта пустая —
  // так контрольный клик «мимо легенды» никуда не попадает.
  const legendProbe = initial.legend;
  const legendRows = legendProbe ? legendProbe.rows : [];
  check('легенда карти на місці',
    !!legendProbe && legendProbe.bottomRight && legendRows.length === 4 && legendProbe.insideMap,
    legendProbe ? 'кут «правий нижній»=' + legendProbe.bottomRight + ', рядків: ' + legendRows.length +
      ', у межах карти=' + legendProbe.insideMap + ', ' + JSON.stringify(legendProbe.rect) +
      ', aria="' + legendProbe.aria + '"' : 'немає .map-legend');

  const LEGEND_SPEC = [
    { text: 'Живий (GPS)', colour: rgb('#43c463'), glow: false },
    { text: 'За розкладом', colour: rgb('#8b9096'), glow: false },
    { text: 'Ваша посадка', colour: rgb('#ffd700'), glow: true },
    { text: 'Пішки', colour: '', glow: false },
  ];
  const legendOk = !!legendProbe && LEGEND_SPEC.every((want, index) => {
    const row = legendRows[index];
    if (!row || row.text !== want.text) return false;
    if (want.colour && row.colour !== want.colour) return false;
    if (want.glow && !row.shadow.includes(rgb('#ffd700'))) return false;
    return true;
  });
  check('легенда за специфікацією', legendOk, legendRows.map((row) => row.icon + ' ' + row.text +
    ' [' + row.colour + (row.shadow && row.shadow !== 'none' ? ' +glow' : '') + ']').join(' | '));

  const walkRow = legendRows[3] || {};
  check('CSS легенди за специфікацією',
    !!legendProbe && legendProbe.background === 'rgba(30, 33, 38, 0.85)' &&
    legendProbe.borderWidth === '1px' && legendProbe.borderColour === rgb('#333941') &&
    legendProbe.radius === '8px' && legendProbe.fontSize === '12px' &&
    /blur\(4px\)/.test(legendProbe.blur) && walkRow.spacing === '2px',
    legendProbe ? legendProbe.background + ' / ' + legendProbe.borderWidth + ' ' +
      legendProbe.borderColour + ' / r' + legendProbe.radius + ' / ' + legendProbe.fontSize +
      ' / ' + legendProbe.blur + ' / крок пунктиру=' + walkRow.spacing : 'немає легенди');

  check('легенда не перекриває атрибуцію OSM', !!legendProbe && legendProbe.attrOverlap === false,
    legendProbe ? (legendProbe.attrOverlap === false ? 'легенда стоїть над атрибуцією, перетину немає'
      : 'прямокутники перетинаються') : 'немає легенди');

  // Клік і свайп по легенді не мають провалюватись у карту (вимога приймання).
  // Міряємо саме поведінку карти: її власну подію 'click' і центр після драга.
  // Leaflet 1.9 не зупиняє DOM-бабблинг кліку, тому слухати .leaflet-container
  // безглуздо: він ставить на елемент прапорець _leaflet_disable_click, який
  // ігнорує сам движок карти.
  const points = await page.evaluate(() => {
    window.__mapClicks = 0;
    map.on('click', () => { window.__mapClicks += 1; });
    const box = document.querySelector('.map-legend');
    window.__legendDisableFlag = box._leaflet_disable_click === true;
    const rect = box.getBoundingClientRect();
    const area = document.querySelector('.leaflet-container').getBoundingClientRect();
    return {
      legend: { x: Math.round(rect.left + rect.width / 2), y: Math.round(rect.top + rect.height / 2) },
      free: { x: Math.round(area.left + area.width / 2), y: Math.round(area.top + area.height / 2) },
    };
  });
  const centre = () => page.evaluate(() => map.getCenter().toString());
  const mapClicks = () => page.evaluate(() => window.__mapClicks);

  await page.mouse.click(points.legend.x, points.legend.y);   // 1) клик по легенді
  await sleep(200);
  const clicksFromLegend = await mapClicks();
  await page.mouse.click(points.free.x, points.free.y);       // 2) контроль: клик мимо легенди
  await sleep(200);
  const clicksFromMap = await mapClicks();

  const centreBefore = await centre();
  await page.mouse.move(points.legend.x, points.legend.y);    // 3) свайп з легенди
  await page.mouse.down();
  await page.mouse.move(points.legend.x + 130, points.legend.y + 60, { steps: 12 });
  await page.mouse.up();
  await sleep(300);
  const centreAfterLegendDrag = await centre();
  await page.mouse.move(points.free.x, points.free.y);        // 4) контроль: свайп по карті
  await page.mouse.down();
  await page.mouse.move(points.free.x + 130, points.free.y + 60, { steps: 12 });
  await page.mouse.up();
  await sleep(300);
  const centreAfterMapDrag = await centre();
  const legendDisableFlag = await page.evaluate(() => window.__legendDisableFlag);

  check('клік і свайп по легенді не провалюються в карту',
    clicksFromLegend === 0 && clicksFromMap > 0 && centreAfterLegendDrag === centreBefore &&
    centreAfterMapDrag !== centreBefore,
    'кліки по карті: з легенди ' + clicksFromLegend + ', з карти ' + clicksFromMap +
    ' | центр: до свайпу легенди ' + centreBefore + ', після свайпу легенди ' + centreAfterLegendDrag +
    ', після контрольного свайпу ' + centreAfterMapDrag +
    ' | _leaflet_disable_click=' + legendDisableFlag);

  // --- 3. Живой парк ------------------------------------------------------
  await page.click('#fleet-toggle');
  await page.waitForFunction(
    () => document.querySelectorAll('.veh-marker').length > 5, { timeout: 30000 });
  await sleep(1200);
  const fleet = await page.evaluate(probe);
  const headings = fleet.wraps
    .map((item) => Number(item.heading))
    .filter((value) => Number.isFinite(value));
  const rotated = headings.filter((value) => Math.abs(value) > 0.5).length;
  check('машины нарисованы', fleet.markers > 5, 'маркеров: ' + fleet.markers);
  check('у машин есть курс', rotated > 0, 'ненулевых курсов: ' + rotated + ' из ' + headings.length);
  check('в статусе видно время', /час:/.test(fleet.fleetStatus), fleet.fleetStatus.trim());
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
    !!plan.geometry && plan.geometry.viewBox === '0 0 36 36' && plan.geometry.radius === '11' &&
    plan.geometry.strokeMatchesFill && plan.geometry.arrowInGroup && plan.geometry.textOutsideGroup &&
    plan.geometry.textAnchor === 'middle',
    JSON.stringify(plan.geometry));
  check('стрілка повертається за курсом маркера',
    !!plan.geometry && plan.geometry.groupTransform === 'rotate(' + plan.geometry.heading + ' 18 18)',
    plan.geometry ? plan.geometry.groupTransform + ' при курсі ' + plan.geometry.heading : 'нет маркеров');
  check('CSS станів за специфікацією',
    plan.css.plannedOpacity === '0.6' && /grayscale/.test(plan.css.plannedGrayscale) &&
    plan.css.stoppedArrowDisplay === 'none' && plan.css.stoppedCircleAnimation === 'pulse-stopped' &&
    plan.css.targetAnimation === 'pulse-target' && /gold|rgb\(255, 215, 0\)/i.test(plan.css.targetShadow),
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
  const plannedCount = plan.wraps.filter((item) => item.planned).length;
  const stoppedCount = plan.wraps.filter((item) => item.stopped).length;
  console.log('       станів: live=' + (plan.wraps.length - plannedCount) + ' planned=' + plannedCount +
    ' stopped=' + stoppedCount + ' target=' + plan.targetBoards.length);
  await page.screenshot({ path: path.join(OUT, 'plan.png') });
  report.shots.push('plan.png');

  // --- 6. Мобильный вид ---------------------------------------------------
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
  check('на 390 px легенда в межах екрана', mobile.legendFits,
    'ширина легенди ' + mobile.legendWidth + 'px з 390px');

  report.errors = report.errors.filter((text) => !/favicon/.test(text));
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