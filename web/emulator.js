/*
 * Логіка AI-емулятора: питаємо бекенд, малюємо план, скаржимось на дурний результат.
 *
 * Принцип: сторінка НЕ вважає маршрут сама — вона показує те, що порахував
 * сервер. Кнопка «🚩 Це бред» відправляє кейс у data/feedback, щоб потім
 * розібрати його без здогадок («що саме тоді повернув сервер»).
 *
 * Встраиваемость (web/editor.html): файл завёрнут в IIFE, потому что редактор
 * и эмулятор делят глобальную область — два `const map`/`const state` в ней не
 * уживаются (второй скрипт падал бы с SyntaxError). Карту берём общую:
 * window.TRANSPORT_MAP публикует web/app.js; если её нет (отдельная страница
 * /ui/emulator.html) — создаём свою, как раньше.
 */
(function () {

const EXAMPLES = [
  'Я на Соборці, треба на Гравітон',
  'з Калинки до Універу',
  'Тралка → Гравітон',
  'як доїхати до Готелю Буковина',
];

const state = {
  stops: {},
  last: null,
  // Живые ТС: ключ «маршрут|борт» -> { marker, from, to, t0, vehicle }.
  // Один словарь на всё: машины из плана и из живого парка не дублируются.
  vehicles: new Map(),
  // Борт-номера «первых нужных ТС» из плана (leg.live_bus). Живёт отдельно от
  // словаря машин: парк каждые 0.9 с приходит новыми объектами, а подсветка
  // цели обязана пережить обновление снимка.
  targetBoards: new Set(),
  // Джерело парку для всього знімка: "sim" | "real" | "mixed" (auto). У mixed
  // режимі кожна машина несе своє поле source, а в моно-режимі його немає —
  // тоді бейдж «SIM» малюється за кореневим джерелом усього знімка.
  fleetSource: '',
  fleetOn: false,
  playing: false,
  modelNow: null,      // «машина часу»: ISO без секунд, null = реальное время
  fleetTimer: null,
  animFrame: null,
  // План маршруту: останній ответ сервера и слои по шагам. Нужны, чтобы
  // «крок плану» в тексте можно было связать с его линией на карте.
  lastPlan: null,
  planStepLayers: {},
  // Картки варіантів плану (поставка 1, §13 брифа): останній оффер з
  // /api/plan та id обраної карточки. Кореневий план — завжди перший варіант.
  variantOffer: null,
  activeVariantId: null,
};

// ---------------------------------------------------------------------------
// Карта
// ---------------------------------------------------------------------------

// Карта: на объединённой странице берём инстанс редактора (один Leaflet на
// #map), на отдельной странице создаём свой — вместе со слоем тайлов.
const sharedMap = window.TRANSPORT_MAP;
const map = sharedMap || L.map('map', { zoomControl: true }).setView([48.2921, 25.9358], 13);
if (!sharedMap) window.map = map;
if (!sharedMap) {
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '© OpenStreetMap',
  }).addTo(map);
}
const layerGroup = L.layerGroup().addTo(map);

// Слой живых ТС — отдельно от плана: маршрут можно перерисовать, а парк при
// этом продолжал бы ехать. Машины из плана попадают сюда же (дублей нет:
// ключ «тип|маршрут|борт», см. upsertVehicle).
const vehicleLayer = L.layerGroup().addTo(map);

// На телефоне карта сначала может быть нулевой высоты — просим пересчитать.
window.addEventListener('load', () => setTimeout(() => map.invalidateSize(), 200));
window.addEventListener('resize', () => map.invalidateSize());

function clearLayers() {
  layerGroup.clearLayers();
  // Карта снова свободна — редактору можно возвращать режим добавления
  // остановок (на отдельной странице редактора нет, вызов ничего не делает).
  setEditorEditMode(true);
}

/**
 * Просмотр плана на объединённой странице: пока на карте маршрут или живой
 * парк, редактор не должен добавлять остановки по клику. Если панель встроена
 * в редактор (window.RouteEditor публикует web/app.js) — переключаем его режим.
 */
function setEditorEditMode(on) {
  if (window.RouteEditor && typeof window.RouteEditor.setEditMode === 'function') {
    window.RouteEditor.setEditMode(on);
  }
}

function setStatus(text, kind) {
  const el = document.getElementById('status');
  el.textContent = text;
  el.className = 'status' + (kind ? ' ' + kind : '');
}

// ---------------------------------------------------------------------------
// Справочник остановок (нужен, чтобы рисовать маркеры по stop_id)
// ---------------------------------------------------------------------------

async function loadStops() {
  try {
    const res = await fetch('/api/stops');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    (data.stops || []).forEach((stop) => { state.stops[stop.id] = stop; });
    setStatus('довідник зупинок: ' + data.count + ' — можна питати');
  } catch (err) {
    setStatus('не вдалось завантажити довідник зупинок: ' + err.message, 'error');
  }
}

// ---------------------------------------------------------------------------
// Запит до сервера
// ---------------------------------------------------------------------------

async function postJson(url, body) {
  return fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

async function ask() {
  const input = document.getElementById('ask-text');
  const text = input.value.trim();
  if (!text) {
    setStatus('спочатку напишіть фразу', 'error');
    return;
  }

  const askButton = document.getElementById('ask-btn');
  askButton.disabled = true;
  setStatus('думаю...');

  try {
    // Спершу пробуємо повноцінний план (з'явиться разом із роутером),
    // і лише якщо його ще немає — відкат на простий розбір фрази /api/route.
    let response = await postJson('/api/plan', { text });
    let endpoint = '/api/plan';
    if (response.status === 404 || response.status === 405) {
      response = await postJson('/api/route', { text });
      endpoint = '/api/route';
    }
    if (!response.ok) {
      setStatus('помилка ' + response.status + ': ' + (await response.text()), 'error');
      return;
    }

    const data = await response.json();
    state.last = { endpoint, text, data, at: new Date().toISOString() };
    render(endpoint, data);
    document.getElementById('report-btn').disabled = false;
  } catch (err) {
    setStatus('помилка запиту: ' + err.message, 'error');
  } finally {
    askButton.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Отрисовка
// ---------------------------------------------------------------------------

function markerFor(stop, color, label) {
  return L.circleMarker([stop.lat, stop.lon], {
    radius: 7,
    color: color || '#4f8cff',
    weight: 3,
    fillColor: '#fff',
    fillOpacity: 1,
  }).bindPopup(label || stop.name);
}

// ---------------------------------------------------------------------------
// Живые ТС: стрелка по курсу машины + движение по модельному времени
// ---------------------------------------------------------------------------
//
// Данные уже готовы на бекенде: у каждой машины есть heading_deg (симулятор
// считает его из цепочки маршрута, реальный GPS берёт из поля orientation
// перевозчика), route_colour_hex, board_number, speed_kmh, progress.
// Оформление маркера живёт в одном месте — vehicleIcon() + класс .veh-marker.

const FLEET_TICK_MS = 900;   // как часто обновляем парк в режиме «рух»
const FLEET_STEP_MIN = 1;    // на сколько минут двигаем модель за один тик
const ANIM_MS = 900;         // за сколько миллисекунд «доезжаем» до новой точки

function vehicleKey(vehicle) {
  // Тип ТС обов'язковий у ключі: «5» є і в автобусів, і в тролейбусів, і
  // бортові номери в них можуть збігатися. Без типу маркери двох різних
  // маршрутів перезаписували б один одного.
  return String(vehicle.vehicle_type || '?') + '|' +
    String(vehicle.route_label || '?') + '|' + String(vehicle.board_number || '?');
}

/**
 * Джерело машини: "real" | "sim" | "sched" | "". У змішаному парку (auto) у
 * кожної машини є власне поле source (merge_fleet), а в моно-режимі
 * (PARK_SOURCE=sim/gps) джерело відоме лише для всього знімка — їм і користуємось:
 * на тестовому стенді весь парк віртуальний, і бейдж «SIM» має це показувати.
 */
function vehicleSource(vehicle) {
  if (vehicle && vehicle.source) return String(vehicle.source);
  return state.fleetSource || '';
}

/**
 * Маркер ТС (спецификация дизайна Gemini, docs/BRIEF-emulator-vehicles-visual.md):
 * круг радиусом 11px с номером маршрута + выступающий сверху треугольник-стрелка.
 * Стрелка повёрнута за курсом (0° — на север), круг и подпись — прямые.
 *
 * Состояния — классами на обёртке: live / planned (приглушён), stopped
 * (стрілка ховається, обводка пульсує) / target (золоте пульсування цілі).
 * sim — віртуальна машина (бейдж «SIM», §3 брифа: демо не має видавати
 * симулятор за живий GPS).
 */
function vehicleIcon(vehicle) {
  const heading = Number(vehicle.heading_deg);
  const angle = Number.isFinite(heading) ? heading.toFixed(1) : '0.0';
  const live = !!vehicle.is_live;
  const colour = vehicle.route_colour_hex || '#4f8cff';
  // Підпис і борт теж приходять з API — обидва через esc() (див. vehiclePopup).
  let rawLabel = String(vehicle.route_label || '');
  if (vehicle.vehicle_type === 'trolley') rawLabel += 'т';
  const label = esc(rawLabel.slice(0, 4));
  const stopped = Number(vehicle.speed_kmh) < 3;
  const isTarget = state.targetBoards.has(boardKey(vehicle));
  const source = vehicleSource(vehicle);

  let wrapClass = 'veh-wrap' + (live ? ' live' : ' planned');
  if (stopped) wrapClass += ' stopped';
  if (isTarget) wrapClass += ' target';
  if (source === 'sim') wrapClass += ' sim';

  const html = `
    <div class="${wrapClass}" data-heading="${angle}" data-board="${esc(vehicle.board_number)}" data-source="${esc(source)}">
      <span class="veh-sim-badge" title="Віртуальна машина (симулятор)">SIM</span>
      <svg width="48" height="48" viewBox="0 0 48 48">
        <g class="veh-arrow-group" transform="rotate(${angle} 24 24)">
          <path class="veh-arrow" d="M 24 1 L 36 17 L 12 17 Z" fill="#ffffff" stroke="${colour}" stroke-width="3" stroke-linejoin="round"/>
        </g>
        <circle cx="24" cy="24" r="15" fill="#ffffff" stroke="${colour}" stroke-width="3"/>
        <text x="24" y="28.5" text-anchor="middle" font-family="sans-serif" font-size="13" font-weight="700" fill="#000000">${label}</text>
      </svg>
    </div>`;

  return L.divIcon({
    className: 'veh-marker' + (isTarget ? ' is-target' : ''),
    html,
    iconSize: [48, 48],
    iconAnchor: [24, 24],
    popupAnchor: [0, -24],
  });
}

/** Борт-номер как он приходит и в /api/live, и в leg.live_bus (одно и то же поле).
 *  Дополненный типом ТС: цель подсветки — «садись на автобус 5, борт X»,
 *  и троллейбус 5 с тем же бортом целью не является. */
function boardKey(vehicle) {
  const board = String(vehicle.board_number === undefined || vehicle.board_number === null
    ? '' : vehicle.board_number);
  return String(vehicle.vehicle_type || '') + '|' + board;
}

// ---------------------------------------------------------------------------
// Розвантаження карти при віддаленні (специфікація дизайну Gemini, п. 4)
// ---------------------------------------------------------------------------
//
// Свідомо без MarkerCluster: нижче порога маркер стискається в кольорову
// крапку (номер і стрілка ховаються) одним класом на <body>, а CSS читає його
// (`body.zoom-out .veh-wrap` у web/emulator.html). Якір Leaflet при цьому не
// чіпаємо: transform на обгортці не міняє ні box маркера (36 px), ні
// popupAnchor, тож попап і координата лишаються на місці — це перевіряє UI-тест.

const DOT_ZOOM_BELOW = 13;   // zoom < 13 → крапки, zoom >= 13 → повний маркер

function updateZoomState() {
  const dots = map.getZoom() < DOT_ZOOM_BELOW;
  document.body.classList.toggle('zoom-out', dots);
}

/**
 * Запомнить «первые нужные ТС» из плана и перекрасить уже нарисованные машины.
 * Без параметра — цель снимается (карта очищена, план не построен).
 */
function setTargetBoards(legs) {
  // Ключ — «тип|борт» (см. boardKey): у автобуса 5 и троллейбуса 5 борта
  // могут совпадать, а цель подсветки — только один конкретный маршрут.
  const boards = new Set();
  (Array.isArray(legs) ? legs : []).forEach((leg) => {
    const board = String(leg && leg.live_bus ? leg.live_bus : '').trim();
    const vtype = String(leg && leg.vehicle ? leg.vehicle : '').trim();
    if (board && board !== '?') boards.add(vtype + '|' + board);
  });
  state.targetBoards = boards;
  state.vehicles.forEach((entry) => entry.marker.setIcon(vehicleIcon(entry.vehicle)));
}

/**
 * Екранування зовнішніх рядків: route_label, board_number, gpstime приходять
 * від перевізника, а ми вставляємо їх і в текст, і в атрибут (data-board).
 * `&` екрануємо першим — інакше власні `&amp;` подвояться.
 */
function esc(value) {
  return String(value === undefined || value === null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/**
 * Попап машини (специфікація дизайну Gemini, п. 2): структурований HTML замість
 * сирого тексту з <br>. Leaflet малює попапи світлими, тому верстка під світлу
 * тему — CSS у web/emulator.html (.veh-popup-*).
 */
function vehiclePopup(vehicle) {
  const live = !!vehicle.is_live;
  const source = vehicleSource(vehicle);
  const route = esc(vehicle.route_label || '?');
  const board = esc(vehicle.board_number || '?');
  const speed = Number.isFinite(Number(vehicle.speed_kmh))
    ? '<div>Швидкість: <b>' + esc(vehicle.speed_kmh) + ' км/год</b></div>' : '';
  const heading = Number.isFinite(Number(vehicle.heading_deg))
    ? '<div>Курс: <b>' + Math.round(vehicle.heading_deg) + '°</b></div>' : '';
  const progress = (vehicle.progress !== undefined && vehicle.progress !== null)
    ? '<div class="veh-popup-progress">Рейс: ' + Math.round(vehicle.progress * 100) + '%</div>' : '';
  const time = vehicle.gpstime
    ? '<div class="veh-popup-age">Дані: ' + esc(vehicle.gpstime) +
      (vehicle.age_seconds ? ' (' + Math.round(vehicle.age_seconds) + ' с тому)' : '') + '</div>' : '';

  // Бейдж джерела: SIM — машина віртуальна (симулятор), GPS/Розклад — як раніше.
  // Розміщуємо його після основного, щоб головна відповідь «живий чи ні»
  // залишалася першою — джерело це контекст демо, а не стан рейсу.
  const sourceBadge = source === 'sim'
    ? '<span class="badge sim">Симулятор</span>' : '';

  return '<div class="veh-popup">' +
    '<div class="veh-popup-head">' +
      '<span class="badge ' + (live ? 'live' : 'plan') + '">' + (live ? 'GPS' : 'Розклад') + '</span>' +
      '<strong>Маршрут ' + route + '</strong>' +
      sourceBadge +
    '</div>' +
    '<div class="veh-popup-body">' +
      '<div>Борт: <b>' + board + '</b></div>' +
      speed + heading + progress + time +
    '</div>' +
  '</div>';
}

/** Добавить машину или обновить её положение/курс (без дублей по борт-номеру). */
function upsertVehicle(vehicle) {
  if (vehicle.lat === undefined || vehicle.lon === undefined) return null;
  const key = vehicleKey(vehicle);
  const point = [Number(vehicle.lat), Number(vehicle.lon)];
  const now = typeof performance !== 'undefined' ? performance.now() : Date.now();
  let entry = state.vehicles.get(key);

  if (!entry) {
    const marker = L.marker(point, { icon: vehicleIcon(vehicle), zIndexOffset: 200 })
      .bindPopup(vehiclePopup(vehicle))
      .addTo(vehicleLayer);
    entry = { marker, vehicle, from: L.latLng(point), to: null, t0: now };
    state.vehicles.set(key, entry);
  } else {
    // Двигаемся от текущего положения (возможно, ещё в середине анимации) к новому.
    entry.from = entry.marker.getLatLng();
    entry.to = L.latLng(point);
    entry.t0 = now;
    entry.vehicle = vehicle;
    entry.marker.setIcon(vehicleIcon(vehicle));  // новый курс и цвет
    entry.marker.setPopupContent(vehiclePopup(vehicle));
  }
  return entry;
}

/** Убрать машины, которых нет в новом снимке парка (только для полного снимка). */
function pruneVehicles(seen) {
  state.vehicles.forEach((entry, key) => {
    if (!seen.has(key)) {
      entry.marker.remove();
      state.vehicles.delete(key);
    }
  });
}

/** Плавно доехать до новых точек: rAF крутится только пока есть цель. */
function ensureAnimation() {
  if (state.animFrame !== null) return;
  const step = (now) => {
    state.animFrame = null;
    let pending = false;
    state.vehicles.forEach((entry) => {
      if (!entry.to) return;
      const t = Math.min(1, (now - entry.t0) / ANIM_MS);
      entry.marker.setLatLng([
        entry.from.lat + (entry.to.lat - entry.from.lat) * t,
        entry.from.lng + (entry.to.lng - entry.from.lng) * t,
      ]);
      if (t < 1) pending = true;
      else entry.to = null;  // доехали
    });
    if (pending) state.animFrame = requestAnimationFrame(step);
  };
  state.animFrame = requestAnimationFrame(step);
}

function updateFleetStatus(counts) {
  const el = document.getElementById('fleet-status');
  if (!el) return;
  let live = 0;
  let planned = 0;
  let sim = 0;
  state.vehicles.forEach((entry) => {
    if (entry.vehicle.is_live) live += 1;
    else planned += 1;
    if (vehicleSource(entry.vehicle) === 'sim') sim += 1;
  });
  const time = state.modelNow ? state.modelNow.replace('T', ' ') : 'зараз';
  const targets = [...state.targetBoards];
  // Віртуальні машини рахуємо окремо: на демо видно, де реальний GPS, а де
  // симулятор (§3). Змішаний парк показуємо одразу, моно-режим — ні (там все sim
  // або все real, і сума дублювала б перше число).
  const simNote = sim && state.fleetSource === 'mixed'
    ? ' · сим: ' + sim : '';
  el.textContent =
    'ТС на карті: ' + live + ' живих' + (planned ? ' + ' + planned + ' за розкладом' : '') +
    simNote +
    (counts && counts.total ? ' (у парку ' + counts.total + ')' : '') +
    (targets.length ? ' · ціль: ' + targets.join(', ') : '') +
    ' · час: ' + time + (state.playing ? ' ▶' : '') +
    (state.fleetOn ? '' : ' · парк вимкнено');
}

/** Полный снимок парка: /api/live (с «машиной часу», если время задано). */
async function loadFleet() {
  const params = new URLSearchParams();
  if (state.modelNow) params.set('now', state.modelNow);
  params.set('only_fresh', 'false');  // «за розкладом» тоже показываем (серым)
  try {
    const res = await fetch('/api/live?' + params.toString());
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    // Кореневе джерело знімка: у mixed кожна машина несе своє поле source,
    // а в моно-режимі воно одне для всього парку — бейдж «SIM» малюється по ньому.
    state.fleetSource = String(data.source || '');
    const vehicles = (data.vehicles || []).filter((v) => !v.in_depo);
    const seen = new Set();
    vehicles.forEach((vehicle) => {
      if (upsertVehicle(vehicle)) seen.add(vehicleKey(vehicle));
    });
    pruneVehicles(seen);
    ensureAnimation();
    updateFleetStatus(data.counts);
  } catch (err) {
    updateFleetStatus(null);
    setStatus('не вдалось завантажити живий парк: ' + err.message, 'error');
  }
}

/** ISO без секунд — формат для /api/live?now= и для input[datetime-local]. */
function isoMinute(date) {
  const pad = (value) => String(value).padStart(2, '0');
  return date.getFullYear() + '-' + pad(date.getMonth() + 1) + '-' + pad(date.getDate()) +
    'T' + pad(date.getHours()) + ':' + pad(date.getMinutes());
}

function modelDate() {
  return state.modelNow ? new Date(state.modelNow) : new Date();
}

function syncTimeInput() {
  const input = document.getElementById('model-time');
  if (input) input.value = state.modelNow || isoMinute(new Date());
}

function toggleFleet(on) {
  state.fleetOn = on;
  const box = document.getElementById('fleet-toggle');
  if (box) box.checked = on;
  const play = document.getElementById('fleet-play');
  if (play) play.disabled = !on;

  if (on) {
    syncTimeInput();
    loadFleet();
  } else {
    setPlaying(false);
    pruneVehicles(new Set());  // парк выключен — машин на карте быть не должно
    updateFleetStatus(null);
  }
}

/** «Рух»: каждый тик сдвигаем модельное время и забираем новый снимок парка. */
function setPlaying(on) {
  state.playing = on;
  const button = document.getElementById('fleet-play');
  if (button) button.textContent = on ? ' пауза' : '▶ рух';

  if (on) {
    if (!state.fleetOn) toggleFleet(true);
    if (!state.modelNow) state.modelNow = isoMinute(modelDate());
    syncTimeInput();
    state.fleetTimer = setInterval(() => {
      state.modelNow = isoMinute(new Date(modelDate().getTime() + FLEET_STEP_MIN * 60000));
      syncTimeInput();
      loadFleet();
    }, FLEET_TICK_MS);
    loadFleet();
  } else if (state.fleetTimer) {
    clearInterval(state.fleetTimer);
    state.fleetTimer = null;
  }
  updateFleetStatus(null);
}

function render(endpoint, data) {
  clearLayers();
  if (data.mode === 'clarify' || data.mode === 'no_route') {
    renderInfo(data);
  } else if (Array.isArray(data.legs)) {
    renderPlan(data);
  } else {
    renderRoute(data);
  }
  // Картки варіантів: рендеримо одразу, паралельно з озвученням тексту.
  // Якщо варіант один або немає — карток не буде взагалі (панель сховається).
  renderVariantCards(data);
  document.getElementById('answer').style.display = 'block';
  if (Array.isArray(data.legs)) speakPlanSummary(data);
}

/** Сервер просить уточнити фразу (не впевнений у точках) або маршруту немає. */
function renderInfo(data) {
  const info = data.debug_info || {};
  const parts = [];
  parts.push('<span class="badge plan">' + (data.mode === 'clarify' ? 'уточнення' : 'немає маршруту') + '</span>');
  parts.push(data.note || (data.mode === 'clarify' ? 'переформулюйте, будь ласка, фразу' : 'спробуйте пізніше'));
  if (data.from_name) parts.push('звідки: «' + data.from_name + '» (' + (info.from_type || '?') + ')');
  if (data.to_name) parts.push('куди: «' + data.to_name + '» (' + (info.to_type || '?') + ')');
  if (data.reask) parts.push('підказка: назвіть зупинку або вулицю, наприклад «Соборка», «Гравітон»');
  document.getElementById('answer').innerHTML = parts.join('\n');
  setStatus(data.reask ? 'переформулюйте фразу' : 'маршрут не знайдено', 'error');
}

/** Откат: сервер только понял фразу и вернул две остановки. */
function renderRoute(data) {
  const from = state.stops[data.from_stop_id];
  const to = state.stops[data.to_stop_id];
  const bounds = [];

  if (from) {
    markerFor(from, '#43c463', 'Звідси: ' + from.name).addTo(layerGroup);
    bounds.push([from.lat, from.lon]);
  }
  if (to) {
    markerFor(to, '#ff5c5c', 'Туди: ' + to.name).addTo(layerGroup);
    bounds.push([to.lat, to.lon]);
  }
  if (from && to) {
    L.polyline([[from.lat, from.lon], [to.lat, to.lon]], {
      color: '#4f8cff', weight: 4, dashArray: '8 8', opacity: .8,
    }).addTo(layerGroup);
  }
  if (bounds.length) map.fitBounds(bounds, { padding: [40, 40] });

  const info = data.debug_info || {};
  document.getElementById('answer').innerHTML =
    '<span class="badge plan">розбір фрази</span>\n' +
    'звідки: «' + (from ? from.name : '—') + '» (' + (info.from_type || '?') + ')\n' +
    'куди:   «' + (to ? to.name : '—') + '» (' + (info.to_type || '?') + ')\n' +
    'план маршруту не повернуто — спробуйте ще раз (або натисніть «Це бред»)';
  setStatus('сервер зрозумів тільки точки (план не побудовано)', 'ok');
}

// ---------------------------------------------------------------------------
// План маршруту на карті: хвости, шеврони напрямку, бейджі кроків
// ---------------------------------------------------------------------------
//
// Данные: `leg.path` — активная часть (непрерывный срез), `leg.full_geom` —
// полная геометрия направления, `leg.color` — цвет маршрута. Хвост обрезаем на
// клиенте: полная линия через весь город даёт «простыню» вместо контекста.
const TAIL_CLIP_STOPS = 6;        // ± столько остановок маршрута вокруг ноги
const ARROW_MIN_SEGMENT_M = 800;  // длинный сегмент получает свой шеврон

/** Азимут сегмента (0° — север, по часовой) — та же формула, что в роутере. */
function bearingDeg(lat1, lon1, lat2, lon2) {
  const mid = ((lat1 + lat2) / 2) * Math.PI / 180;
  const dlat = lat2 - lat1;
  const dlon = (lon2 - lon1) * Math.cos(mid);
  if (!dlat && !dlon) return 0;
  return (Math.atan2(dlon, dlat) * 180 / Math.PI + 360) % 360;
}

/** Длина сегмента, м (haversine) — только для порога «длинного сегмента». */
function distanceM(lat1, lon1, lat2, lon2) {
  const radius = 6371000;
  const phi1 = (lat1 * Math.PI) / 180;
  const phi2 = (lat2 * Math.PI) / 180;
  const dphi = ((lat2 - lat1) * Math.PI) / 180;
  const dlambda = ((lon2 - lon1) * Math.PI) / 180;
  const a = Math.sin(dphi / 2) ** 2 +
    Math.cos(phi1) * Math.cos(phi2) * Math.sin(dlambda / 2) ** 2;
  return 2 * radius * Math.asin(Math.min(1, Math.sqrt(a)));
}

/** Где в `full` лежит непрерывный срез `path` (-1, если это не срез). */
function sliceOffset(full, path) {
  for (let start = 0; start + path.length <= full.length; start += 1) {
    let match = true;
    for (let i = 0; i < path.length; i += 1) {
      if (full[start + i][0] !== path[i][0] || full[start + i][1] !== path[i][1]) {
        match = false;
        break;
      }
    }
    if (match) return start;
  }
  return -1;
}

const CHEVRON_SVG = '<svg viewBox="0 0 12 12" aria-hidden="true">' +
  '<path d="M 3 2 L 9 6 L 3 10" fill="none" stroke="#ffffff" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';

/**
 * Промежуточная остановка: аккуратная белая точка с цветной обводкой маршрута.
 */
function stopDot(point, colour) {
  return L.marker(point, {
    interactive: false,
    zIndexOffset: 100,
    icon: L.divIcon({
      className: 'plan-stop',
      html: '<span class="plan-stop-dot" data-lat="' + point[0] + '" data-lon="' + point[1] +
        '" style="--route-colour: ' + colour + '"></span>',
      iconSize: [12, 12],
      iconAnchor: [6, 6],
    }),
  });
}

/** Отдельный шеврон направления по центру сегмента линии. */
function midChevron(point, bearing, colour) {
  // Preserve original bearing for data attribute (used by tests),
  // but rotate the chevron so that 0° (north) points upward.
  // The SVG chevron points to the right (east) by default, so we offset by -90°.
  const dataBearing = bearing.toFixed(1);
  const rotation = ((bearing - 90) % 360).toFixed(1);
  return L.marker(point, {
    interactive: false,
    zIndexOffset: 150,
    icon: L.divIcon({
      className: 'plan-arrow',
      html: '<span class="plan-arrow-icon" data-bearing="' + dataBearing +
        '" data-lat="' + point[0] + '" data-lon="' + point[1] +
        '" style="transform: rotate(' + rotation + 'deg)">' +
        CHEVRON_SVG + '</span>',
      iconSize: [14, 14],
      iconAnchor: [7, 7],
    }),
  });
}

/** Бейдж шага плана: посадка, где пассажиру ждать. Крупный и пульсирует. */
function stepBadge(step, point, colour) {
  return L.marker(point, {
    interactive: false,
    zIndexOffset: 500,
    icon: L.divIcon({
      className: 'plan-step-wrap',
      html: '<div class="plan-step" data-step="' + step + '" data-colour="' + colour +
        '" style="background: ' + colour + '">' + step + '</div>',
      iconSize: [30, 30],
      iconAnchor: [15, 15],
    }),
  });
}

/** Финиш плана — кінцева пассажира. */
function finishBadge(point) {
  return L.marker(point, {
    interactive: false,
    zIndexOffset: 500,
    icon: L.divIcon({
      className: 'plan-finish-wrap',
      html: '<div class="plan-finish">🏁</div>',
      iconSize: [24, 24],
      iconAnchor: [12, 12],
    }),
  });
}

/** Пешеходный отрезок: иконка на середине линии. */
function walkBadge(point) {
  return L.marker(point, {
    interactive: false,
    zIndexOffset: 450,
    icon: L.divIcon({
      className: 'plan-walk-wrap',
      html: '<div class="plan-walk-icon">🚶</div>',
      iconSize: [20, 20],
      iconAnchor: [10, 10],
    }),
  });
}

/** Основной режим: сервер посчитал маршрут (возможно, с пересадкой). */
function renderPlan(plan) {
  // Гасим режим добавления остановок: клики по карте теперь смотрят план
  // (попапы машин, приближение участка), а не ставят новую точку.
  setEditorEditMode(false);

  const bounds = [];
  const lines = [];
  state.lastPlan = plan;
  state.planStepLayers = {};
  // Джерело парку плану (sim/real/mixed): машини в нозі несуть своє джерело
  // лише в mixed-режимі, тож для моно-режиму запам'ятовуємо кореневе.
  if (plan.fleet_source) state.fleetSource = String(plan.fleet_source);

  // Точки стыковки ног: нужны, чтобы нарисовать пешую пересадку, у которой
  // своей геометрии пока нет (роутер отдаёт геометрию только для поездок).
  const legStarts = plan.legs.map((leg) =>
    (leg.type === 'transit' && Array.isArray(leg.path) && leg.path.length ? leg.path[0] : null));
  const legEnds = plan.legs.map((leg) =>
    (leg.type === 'transit' && Array.isArray(leg.path) && leg.path.length
      ? leg.path[leg.path.length - 1] : null));

  let step = 0;

  plan.legs.forEach((leg, index) => {
    if (leg.type === 'transit') {
      step += 1;
      const colour = leg.color || '#4f8cff';
      const path = Array.isArray(leg.path) ? leg.path : [];
      const full = Array.isArray(leg.full_geom) ? leg.full_geom : [];
      const legLayer = L.layerGroup().addTo(layerGroup);
      state.planStepLayers[step] = legLayer;

      // Хвост маршрута: где он идёт до и после нашей ділянки. Обрезаем вокруг
      // активной части — полная линия через весь город была бы шумом.
      const offset = path.length > 1 ? sliceOffset(full, path) : -1;
      if (offset >= 0) {
        const from = Math.max(0, offset - TAIL_CLIP_STOPS);
        const to = Math.min(full.length, offset + path.length + TAIL_CLIP_STOPS);
        const tail = full.slice(from, to);
        if (tail.length > 1) {
          L.polyline(tail, {
            color: colour, weight: 4, opacity: .3, className: 'plan-tail',
          }).addTo(legLayer);
        }
      }

      if (path.length > 1) {
        L.polyline(path, {
          color: colour, weight: 6, opacity: .9, className: 'plan-active',
        }).addTo(legLayer);
        path.forEach((point) => bounds.push(point));
      }

      // Промежуточные остановки: крапки тільки за координатами реальних
      // зупинок ноги (leg.stops від бекенда). path — це OSRM-геометрія
      // (сотні точок форми дороги), і крапка на кожній із них давала б
      // «пил» замість маршруту.
      if (Array.isArray(leg.stops)) {
        leg.stops.forEach((point) => stopDot(point, colour).addTo(legLayer));
      }
      // Направление: помітні білі шеврони. Накопичуємо довжину по точках
      // полілінії та ставимо шеврон у кінці кожної ділянки від ARROW_MIN_SEGMENT_M
      // — інакше на OSRM-геометрії (багато коротких сегментів) шеврони
      // злипаються в одну купу.
      let sinceChevronM = 0;
      for (let i = 0; i < path.length - 1; i += 1) {
        const [aLat, aLon] = path[i];
        const [bLat, bLon] = path[i + 1];
        sinceChevronM += distanceM(aLat, aLon, bLat, bLon);
        if (sinceChevronM >= ARROW_MIN_SEGMENT_M) {
          midChevron([(aLat + bLat) / 2, (aLon + bLon) / 2],
            bearingDeg(aLat, aLon, bLat, bLon), colour).addTo(legLayer);
          sinceChevronM = 0;
        }
      }

      // Посадка: номер шага вместо безликой точки — глаз цепляется сразу.
      if (path.length) stepBadge(step, path[0], colour).addTo(legLayer);

      // Ожидание: если показанная цифра посчитана по расписанию (нет живого борта
      // или живой приедет позже расписания), помечаем её как расчётную, а живой
      // борт показываем отдельной строкой. Так две цифры не спорят — вариант V3,
      // см. docs/REVIEW-f4-wait-display.md §3.
      const schedWait = leg.schedule_wait_min;
      const liveWait = leg.live_wait_min;
      const schedBased = schedWait != null && leg.wait_min != null &&
        Math.abs(leg.wait_min - schedWait) < 0.05;
      const waitNote = leg.wait_min == null ? ''
        : ', чекати ~' + leg.wait_min + ' хв' + (schedBased ? ' (за розкладом)' : '');
      lines.push(
        '<span class="step-dot" data-step="' + step + '" style="background: ' +
        esc(colour) + '">' + step + '</span> ' +
        (leg.vehicle === 'trolley' ? '🚎' : '🚌') + ' ' + esc(leg.route) +
        ': «' + esc(leg.from) + '» → «' + esc(leg.to) + '», ' + leg.travel_min +
        ' хв у дорозі' + waitNote
      );

      // Перший потрібний ТС: те, у що сідати в цій нозі.
      if (leg.live_bus || leg.eta) {
        const liveMinutes = liveWait != null ? liveWait : leg.eta;
        const later = liveWait != null && schedWait != null && liveWait > schedWait + 0.05;
        lines.push('   ↳ сідати: ' + esc(leg.live_bus || 'ТЗ') +
          (liveMinutes != null
            ? (later ? ', живий борт — через ' + liveMinutes + ' хв' : ', буде ~' + liveMinutes)
            : '') +
          (leg.vehicle_state ? ' [' + esc(leg.vehicle_state) + ']' : ''));
      }
    } else if (leg.type === 'transfer') {
      const isWalk = leg.kind === 'walk' || (leg.walk_min && leg.walk_min > 0);
      const icon = isWalk ? '🚶' : '⇄';
      
      let name = '';
      if (index === 0 && isWalk) {
        name = ' Посадка: зупинка «' + esc(leg.at) + '»';
      } else {
        name = isWalk ? ' йдемо до «' + esc(leg.at) + '»' : ' пересадка на «' + esc(leg.at) + '»';
      }
      
      const walkNote = (leg.walk_min && index !== 0) ? ' (' + leg.walk_min + ' хв пішки)' : '';
      const waitNote = leg.wait_min ? ', чекати ~' + leg.wait_min + ' хв' : '';
      const legLayer = L.layerGroup().addTo(layerGroup);

      // Пешая часть: пунктир «як у Google Maps» (кружечки). Своей геометрии у
      // роутера пока нет — берём прямую между остановками ног по обе стороны.
      let walkPath = Array.isArray(leg.path) && leg.path.length > 1 ? leg.path : [];
      if (walkPath.length < 2) {
        const fromPoint = legEnds.slice(0, index).reverse().find(Boolean);
        const toPoint = legStarts.slice(index + 1).find(Boolean);
        if (fromPoint && toPoint) walkPath = [fromPoint, toPoint];
      }
      // Не малюємо пунктир для першого кроку, якщо не знаємо координату юзера
      if (walkPath.length > 1 && index !== 0) {
        L.polyline(walkPath, {
          color: '#808080', weight: 5, dashArray: '1, 10',
          lineCap: 'round', lineJoin: 'round', className: 'plan-walk-line',
        }).addTo(legLayer);
        walkPath.forEach((point) => bounds.push(point));
        walkBadge(walkPath[Math.floor(walkPath.length / 2)]).addTo(legLayer);
      }
      lines.push(icon + name + walkNote + waitNote);
    }
  });

  // Финиш — это кінцева пассажира. Координату берём из справочника остановок:
  // последняя нога бывает пешей, а геометрии у неё пока нет.
  const toStop = state.stops[plan.to_stop_id];
  const finishPoint = toStop ? [toStop.lat, toStop.lon] : legEnds.filter(Boolean).pop();
  if (finishPoint) {
    finishBadge(finishPoint).addTo(layerGroup);
    bounds.push(finishPoint);
  }

  if (Array.isArray(plan.vehicles)) {
    renderVehicles(plan.vehicles, bounds, plan.legs);
  }

  if (bounds.length) map.fitBounds(bounds, { padding: [40, 40] });

  const summary = [];
  if (plan.total_min) summary.push('усього ~' + plan.total_min + ' хв');
  if (plan.price_grn !== undefined) summary.push('вартість ~' + plan.price_grn + ' грн');
  if (plan.transfers !== undefined) summary.push('пересадок: ' + plan.transfers);

  const answer = document.getElementById('answer');
  answer.innerHTML =
    '<span class="badge plan">план маршруту</span> ' + summary.join(' · ') + '\n' + lines.join('\n');
  // «UI Sync»: цифра шага в тексте — это ссылка на его линию на карте.
  answer.onclick = (event) => {
    const dot = event.target.closest ? event.target.closest('.step-dot') : null;
    const layer = dot ? state.planStepLayers[dot.dataset.step] : null;
    if (layer && layer.getBounds().isValid()) {
      map.fitBounds(layer.getBounds(), { padding: [40, 40] });
    }
  };
  setStatus('маршрут побудовано', 'ok');
}

/** ТС на маршрутах плана: та же стрелка, что и в живом парке (дублей нет). */
function renderVehicles(vehicles, bounds, planLegs) {
  if (planLegs !== undefined) setTargetBoards(planLegs);
  if (!Array.isArray(vehicles)) return;
  vehicles.forEach((vehicle) => {
    if (upsertVehicle(vehicle)) bounds.push([vehicle.lat, vehicle.lon]);
  });
  ensureAnimation();
  updateFleetStatus(null);
}

// ---------------------------------------------------------------------------
// Картки варіантів плану («Швидкий» / «Дешевий», §13 брифа, поставка 1)
// ---------------------------------------------------------------------------
//
// Сервер (/api/plan) кладе в відповідь `variants`: перший елемент — кореневий
// план (він уже намальований), другий — прогін «≤1 пересадка» («Дешевий»).
// Карточки малюємо ЛИШЕ коли варіантів ≥ 2: один варіант — це не вибір, а
// порожні чіпи тільки шуміли б. Клік по карточці: (а) зупиняємо озвучення,
// якщо воно грає; (б) clearLayers() + renderPlan(варіант) — логіку renderPlan
// не чіпаємо, вона малює план з об'єкта цілком, тож цифри в саммарі (#answer)
// оновлюються самі; (в) підсвічуємо обрану картку. «Програшні» цифри
// (дорожче/довше) підсвічуємо приглушеним помаранчевим (⚠️), НЕ чистим
// червоним: це чесний розмен час↔гроші, а не помилка.

/** Зупинити озвучення, якщо синтез грає. Голос (Web Speech TTS) — етап 4
 *  плану емулятора, і сторінка його вже вміє вимикати: клік по картці — явна
 *  дія «я вибрав інакше», читати старий варіант поверх нового не можна. */
function stopVoice() {
  if (typeof window.speechSynthesis !== 'undefined' &&
      typeof window.speechSynthesis.cancel === 'function') {
    try { window.speechSynthesis.cancel(); } catch (err) { /* немає голосів */ }
  }
}

/** Озвучення відповіді паралельно з появою карток (Web Speech, uk-UA).
 *  Текст складаємо з цифр плану — сервер голосових фраз не віддає. */
function speakPlanSummary(plan) {
  if (typeof window.speechSynthesis === 'undefined' ||
      typeof window.SpeechSynthesisUtterance !== 'function') return;
  const mins = Number(plan && plan.total_min);
  if (!Number.isFinite(mins)) return;
  try {
    let text = 'План: ' + Math.round(mins) + ' хвилин';
    const price = Number(plan.price_grn);
    if (Number.isFinite(price)) text += ', ' + Math.round(price) + ' гривень';
    const variants = Array.isArray(plan.variants) ? plan.variants : [];
    if (variants.length > 1 && Number.isFinite(Number(variants[1].total_min))) {
      text += '. Або інший варіант: ' + Math.round(Number(variants[1].total_min)) + ' хвилин' +
        (Number.isFinite(Number(variants[1].price_grn))
          ? ', ' + Math.round(Number(variants[1].price_grn)) + ' гривень' : '');
    }
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = 'uk-UA';
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(utterance);
  } catch (err) { /* синтез може бути вимкнений — текст і так видно */ }
}

function hideVariantCards() {
  state.variantOffer = null;
  state.activeVariantId = null;
  const box = document.getElementById('plan-variants');
  if (box) {
    box.hidden = true;
    box.innerHTML = '';
  }
}

/** «Чим їдемо» одним рядком: унікальні лінії поїздок варіанта (🚎 5 + 🚌 20). */
function variantTransportLabel(variant) {
  const seen = [];
  (Array.isArray(variant.legs) ? variant.legs : []).forEach((leg) => {
    if (!leg || leg.type !== 'transit' || !leg.route) return;
    const label = (leg.vehicle === 'trolley' ? '🚎 ' : '🚌 ') + leg.route;
    if (seen.indexOf(label) === -1) seen.push(label);
  });
  return seen.length ? seen.join(' + ') : 'маршрут';
}

// ---------------------------------------------------------------------------
// Телеметрія вибору варіанта (поставка 1, §13.3 брифа)
// ---------------------------------------------------------------------------
//
// Подія — одна JSONL-рядка на сервері (POST /api/telemetry/plan_choice,
// модель PlanChoiceTelemetry). Шлємо двічі: коли показали картки
// (chosen_variant_id = null — обов'язкова метрика «показали, але не вибрали»)
// і коли юзер клікнув по картці (chosen_variant_id = id). Запит fire-and-forget:
// відповідь не чекаємо, помити тільки в консоль — аналіз йде asynchronously,
// UI не має від нього залежати.

const DEVICE_KEY = 'emulator.device_id';

/** Анонімний ідентифікатор приладу: один на браузер, зберігаємо в localStorage. */
function deviceId() {
  const random = () => 'anon-' + Math.random().toString(16).slice(2, 10);
  try {
    let id = localStorage.getItem(DEVICE_KEY);
    if (!id) {
      id = random();
      localStorage.setItem(DEVICE_KEY, id);
    }
    return id;
  } catch (err) {
    // localStorage недоступний (приватний режим) — генеруємо на сесію.
    if (!state.deviceId) state.deviceId = random();
    return state.deviceId;
  }
}

/** Підпис ніг варіанта: «trolley:3:B|walk|bus:9A:A» — щоб порівнювати плани
 *  між собами без важкого повного legs. Те саме поле пише бекенд у тестах. */
function legsSignature(legs) {
  return (Array.isArray(legs) ? legs : []).map((leg) => {
    if (!leg) return '?';
    if (leg.type === 'transit') {
      const dir = leg.direction ? ':' + leg.direction : '';
      return (leg.vehicle || '?') + ':' + (leg.route || '?') + dir;
    }
    if (leg.type === 'transfer') return 'walk';
    return leg.type || '?';
  }).join('|');
}

/** Окремий варіант у форматі контракту лога (§12.2). */
function variantToOfferEntry(variant) {
  const legs = Array.isArray(variant.legs) ? variant.legs : [];
  // Джерело: у змішаному парку source стоїть у кожної ноги, а в моно-режимі —
  // лише кореневе fleet_source усього варіанта. Бакети в логі мають бути
  // чесними навіть тоді, коли ноги мовчать про походження машини.
  let source = variant.fleet_source || '';
  if (!source) {
    const transit = legs.find((leg) => leg && leg.type === 'transit');
    source = (transit && transit.source) || '';
  }
  const waits = legs
    .map((leg) => Number(leg && leg.wait_min))
    .filter((value) => Number.isFinite(value));
  return {
    id: String(variant.id),
    tags: Array.isArray(variant.tags) ? variant.tags : [],
    total_min: variant.total_min,
    price_grn: variant.price_grn,
    transfers: variant.transfers,
    wait_min: waits.length ? Math.min.apply(null, waits) : null,
    source: source || 'unknown',
    legs_signature: legsSignature(legs),
  };
}

/**
 * Тіло події вибору або null, якщо оффера немає (картки не показували).
 * variant_order — id у тому порядку, в якому картки лежать на екрані.
 */
function buildPlanChoiceBody(chosenVariantId) {
  const plan = state.variantOffer;
  if (!plan || !Array.isArray(plan.variants) || plan.variants.length < 2) return null;
  const ids = plan.variants.map((variant) => String(variant.id));
  return {
    ts: new Date().toISOString(),
    from_stop_id: plan.from_stop_id,
    to_stop_id: plan.to_stop_id,
    offer: plan.variants.map(variantToOfferEntry),
    default_variant_id: ids[0],
    variant_order: ids,
    chosen_variant_id: chosenVariantId == null ? null : String(chosenVariantId),
    device_id: deviceId(),
    client: 'web-emulator',
  };
}

/** Fire-and-forget: запис події вибору варіанта в журнал на сервері. */
function sendPlanChoice(chosenVariantId) {
  let body;
  try {
    body = buildPlanChoiceBody(chosenVariantId);
  } catch (err) {
    console.error('telemetry plan_choice: не вдалося зібрати тіло', err);
    return;
  }
  if (!body) return;
  fetch('/api/telemetry/plan_choice', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    keepalive: true,   // запис долетить навіть якщо вкладку вже закривають
  }).catch((err) => {
    console.error('telemetry plan_choice: запит не дістався', err);
  });
}

/**
 * Карточки над саммарі (#plan-variants). Викликається з render(): картки
 * з'являються одразу, паралельно з озвученням тексту. Один варіант або жодного
 * — карток НЕ рендеримо взагалі (панель схована, лишається стандартний текст).
 */
function renderVariantCards(plan) {
  const box = document.getElementById('plan-variants');
  if (!box) return false;
  const variants = plan && Array.isArray(plan.variants) ? plan.variants : [];
  if (variants.length < 2) {
    hideVariantCards();
    return false;
  }

  const times = variants.map((v) => Number(v.total_min)).filter(Number.isFinite);
  const prices = variants.map((v) => Number(v.price_grn)).filter(Number.isFinite);
  const bestTime = Math.min.apply(null, times);
  const bestPrice = Math.min.apply(null, prices);

  state.variantOffer = plan;
  state.activeVariantId = variants[0].id;

  box.innerHTML = '';
  variants.forEach((variant, index) => {
    const time = Number(variant.total_min);
    const price = Number(variant.price_grn);
    const timeWorse = Number.isFinite(time) && time > bestTime + 0.05;
    const priceWorse = Number.isFinite(price) && price > bestPrice + 0.05;
    const notes = [];
    if (timeWorse) notes.push('довше на ' + Math.round(time - bestTime) + ' хв');
    if (priceWorse) notes.push('дорожче на ' + Math.round(price - bestPrice) + ' грн');

    const card = document.createElement('button');
    card.type = 'button';
    card.className = 'variant-card' + (index === 0 ? ' active' : '');
    card.setAttribute('data-variant-id', String(variant.id || index));
    card.setAttribute('aria-pressed', index === 0 ? 'true' : 'false');

    const tag = document.createElement('span');
    tag.className = 'variant-tag';
    tag.textContent = (Array.isArray(variant.tags) && variant.tags.length
      ? variant.tags : ['варіант']).join(' · ');
    card.appendChild(tag);

    const route = document.createElement('span');
    route.className = 'variant-route';
    route.textContent = variantTransportLabel(variant);
    card.appendChild(route);

    const meta = document.createElement('span');
    meta.className = 'variant-meta';
    const addNum = (value, unit, worse) => {
      if (!Number.isFinite(value)) return;
      const span = document.createElement('span');
      span.className = 'variant-num' + (worse ? ' worse' : '');
      span.textContent = (worse ? '⚠️ ' : '') + '~' + Math.round(value) + ' ' + unit;
      meta.appendChild(span);
    };
    addNum(time, 'хв', timeWorse);
    addNum(price, 'грн', priceWorse);
    if (variant.transfers !== undefined && variant.transfers !== null) {
      const span = document.createElement('span');
      span.className = 'variant-num';
      span.textContent = variant.transfers === 0 ? 'без пересадок'
        : variant.transfers + ' перес.';
      meta.appendChild(span);
    }
    card.appendChild(meta);

    if (notes.length) {
      const note = document.createElement('span');
      note.className = 'variant-note';
      note.textContent = notes.join(', ');
      card.appendChild(note);
    }

    card.onclick = () => selectVariant(variant);
    box.appendChild(card);
  });

  box.hidden = false;
  // Метрика «показали, але не вибрали»: шлємо одразу з появою карток,
  // chosen_variant_id = null. Без неї невідомо, чи взагалі юзер їх бачив.
  sendPlanChoice(null);
  return true;
}

/** Клік по картці: голос → стоп, карта → варіант, цифри в саммарі → варіант. */
function selectVariant(variant) {
  if (!variant || !Array.isArray(variant.legs)) return;
  stopVoice();

  // renderPlan читає з об'єкта все (ноги, цифри, ТС, фініш) — віддаємо
  // злиття «корінь відповіді + варіант»: варіант адитивний, from/to зупинок
  // і текст фрази живуть у корені (див. router_layer.py: _variant_entry).
  const root = (state.variantOffer && typeof state.variantOffer === 'object')
    ? state.variantOffer : {};
  const planForMap = Object.assign({}, root, variant);

  clearLayers();
  renderPlan(planForMap);   // лінії/ТС і цифри в саммарі (#answer) з варіанта

  state.activeVariantId = variant.id;
  // Подія вибору: шлємо після перемальовки — план уже на карті, запис йде
  // fire-and-forget і не блокує UI.
  sendPlanChoice(variant.id);
  const box = document.getElementById('plan-variants');
  if (box) {
    Array.prototype.forEach.call(box.querySelectorAll('.variant-card'), (card) => {
      const active = card.getAttribute('data-variant-id') === String(variant.id);
      card.classList.toggle('active', active);
      card.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
  }
}

// ---------------------------------------------------------------------------
// Скарга «це бред»
// ---------------------------------------------------------------------------

function openReportForm() {
  document.getElementById('report-form').classList.remove('hidden');
  const input = document.getElementById('report-comment');
  input.value = '';
  input.focus();
}

function closeReportForm() {
  document.getElementById('report-form').classList.add('hidden');
}

async function sendReport() {
  if (!state.last) {
    setStatus('немає на що скаржитись — спочатку запитайте', 'error');
    return;
  }

  // Кладём в жалобу ВЕСЬ контекст кейса: фразу, ответ сервера и данные клиента.
  const body = {
    kind: document.getElementById('report-kind').value,
    comment: document.getElementById('report-comment').value.trim(),
    user_text: state.last.text,
    response: {
      endpoint: state.last.endpoint,
      shown_at: state.last.at,
      answer: state.last.data,
    },
    client: {
      ua: navigator.userAgent,
      url: location.href,
      lang: navigator.language,
      screen: window.innerWidth + 'x' + window.innerHeight,
    },
  };

  try {
    const res = await postJson('/api/feedback', body);
    if (!res.ok) {
      setStatus('не вдалось зберегти скаргу: ' + (await res.text()), 'error');
      return;
    }
    const saved = await res.json();
    setStatus('дякую! кейс ' + saved.id + ' у логах — розберемо', 'ok');
    closeReportForm();
  } catch (err) {
    setStatus('не вдалось зберегти скаргу: ' + err.message, 'error');
  }
}

// ---------------------------------------------------------------------------
// Ініціалізація
// ---------------------------------------------------------------------------

function renderChips() {
  const box = document.getElementById('chips');
  box.innerHTML = '';
  EXAMPLES.forEach((example) => {
    const chip = document.createElement('span');
    chip.className = 'chip';
    chip.textContent = example;
    chip.onclick = () => {
      document.getElementById('ask-text').value = example;
      ask();
    };
    box.appendChild(chip);
  });
}

document.getElementById('ask-btn').onclick = ask;
document.getElementById('report-btn').onclick = openReportForm;
document.getElementById('report-send').onclick = sendReport;
document.getElementById('report-cancel').onclick = closeReportForm;
document.getElementById('clear-btn').onclick = () => {
  stopVoice();
  hideVariantCards();
  clearLayers();
  clearVehicles();
  document.getElementById('answer').style.display = 'none';
  document.getElementById('report-btn').disabled = true;
  state.last = null;
  setStatus('карту очищено');
};

// --- Управление живым парком и «машиной часу» -------------------------------

function clearVehicles() {
  setPlaying(false);
  pruneVehicles(new Set());
  setTargetBoards(null);   // цель снимается вместе с картой
  updateFleetStatus(null);
}

const fleetToggle = document.getElementById('fleet-toggle');
if (fleetToggle) fleetToggle.onchange = (event) => toggleFleet(event.target.checked);

const fleetPlay = document.getElementById('fleet-play');
if (fleetPlay) fleetPlay.onclick = () => setPlaying(!state.playing);

const fleetNow = document.getElementById('fleet-now');
if (fleetNow) {
  fleetNow.onclick = () => {
    state.modelNow = null;
    syncTimeInput();
    if (state.fleetOn) loadFleet();
    updateFleetStatus(null);
  };
}

const modelTime = document.getElementById('model-time');
if (modelTime) {
  modelTime.onchange = (event) => {
    state.modelNow = event.target.value ? event.target.value : null;
    if (state.fleetOn) loadFleet();
    updateFleetStatus(null);
  };
  syncTimeInput();
}

// --- Легенда карти (специфікація дизайну, п. 2) ------------------------------
//
// Пояснює кольори маркерів ТС і пішохідні пересадки. Живе як звичайний контрол
// Leaflet у нижньому правому куті: у цьому ж куті Leaflet тримає атрибуцію OSM,
// і контроли одного кута стакуються вертикально, тому вони не перекриваються.
// Позначки станів — ті самі значення, що в CSS маркера (живий/за розкладом/
// ціль плана) і в стилі пішохідного переходу (dashArray '1, 10').

const LEGEND_ROWS = [
  { icon: '●', colour: '#43c463', text: 'Живий (GPS)' },
  { icon: '●', colour: '#8b9096', text: 'За розкладом' },
  { icon: '●', colour: '#ffd700', glow: '#ffd700', text: 'Ваша посадка' },
  { icon: '1', colour: '#fff', background: '#4f8cff', text: 'Крок плану' },
  { icon: '--', spacing: '2px', text: 'Пішки' },
];

function legendIconStyle(row) {
  const parts = ['color: ' + (row.colour || 'inherit')];
  if (row.glow) parts.push('text-shadow: 0 0 5px ' + row.glow);
  if (row.spacing) parts.push('letter-spacing: ' + row.spacing);
  // Бейдж кроку плану — кружечок із цифрою, як на карті (кольори з легенди).
  if (row.background) {
    parts.push('background: ' + row.background, 'border-radius: 50%', 'padding: 0 5px');
  }
  return parts.join('; ');
}

const legend = L.control({ position: 'bottomright' });

// Leaflet віддає в onAdd сам контрол карти; розмітку будуємо з LEGEND_ROWS,
// щоб текст і кольори жили в одному місці (і їх перевіряв UI-тест).
legend.onAdd = () => {
  const div = L.DomUtil.create('div', 'map-legend');
  div.setAttribute('aria-label', 'Легенда карти');
  div.innerHTML = LEGEND_ROWS.map((row) => (
    '<div class="legend-row"><span class="legend-icon" style="' + legendIconStyle(row) + '">' +
    row.icon + '</span> ' + row.text + '</div>'
  )).join('');
  // Клік і свайп по легенді не мають провалюватись у карту (не рухають і не
  // зумлять її) — інакше на телефоні легенду неможливо прочитати.
  L.DomEvent.disableClickPropagation(div);
  return div;
};

// legend.addTo(map);

// Розвантаження при віддаленні: стан рахуємо на кожному 'zoomend' (fitBounds у
// плані теж його кидає) і один раз при старті — карта відкривається на zoom 13,
// тож номери маршрутів видно одразу.
map.on('zoomend', updateZoomState);
updateZoomState();

renderChips();
loadStops();

// Публичный мини-API: отладка и UI-тесты (убедиться, что модуль загрузился и
// получил карту — свою на /ui/emulator.html или общую на /ui/editor.html).
// vehiclePopup вынесен наружу целиком: tools/ui/check_emulator.js строит попап
// в песочнице DOM и проверяет его структуру/экранирование. После упаковки файла
// в IIFE (см. docs/STATUS.md п.20) функция иначе не видна из page.evaluate, и
// проверка падала с «vehiclePopup is not defined».
// lastPlan() — тот же случай для самого плана: UI-проверка сравнивает
// нарисованное на карте с ответом сервера (ожидаемые число ног, промежуточные
// остановки, азимуты шевронов). Пока она читала приватный `state.lastPlan`,
// ожидания молча вырождались в 0/null, и проверки плана падали «сами по себе»
// (см. docs/STATUS.md §5 п.9). Отдаём только чтение — состояние не меняется.
window.Emulator = {
  map, clearLayers, clearVehicles, renderPlan, vehiclePopup,
  lastPlan: () => state.lastPlan,
  // Картки варіантів (поставка 1, §13 брифа): тільки читання — UI-проверка
  // сверяет оффер и активный вариант, не роясь в приватном состоянии.
  variantOffer: () => state.variantOffer,
  activeVariantId: () => state.activeVariantId,
};

})(); // конец IIFE: внутренние имена не текут в глобальную область редактора


