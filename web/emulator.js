/*
 * Логіка AI-емулятора: питаємо бекенд, малюємо план, скаржимось на дурний результат.
 *
 * Принцип: сторінка НЕ вважає маршрут сама — вона показує те, що порахував
 * сервер. Кнопка «🚩 Це бред» відправляє кейс у data/feedback, щоб потім
 * розібрати його без здогадок («що саме тоді повернув сервер»).
 */

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
  fleetOn: false,
  playing: false,
  modelNow: null,      // «машина часу»: ISO без секунд, null = реальное время
  fleetTimer: null,
  animFrame: null,
};

// ---------------------------------------------------------------------------
// Карта
// ---------------------------------------------------------------------------

const map = L.map('map', { zoomControl: true }).setView([48.2921, 25.9358], 13);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
  attribution: '© OpenStreetMap',
}).addTo(map);
const layerGroup = L.layerGroup().addTo(map);

// Слой живых ТС — отдельно от плана: маршрут можно перерисовать, а парк при
// этом продолжал бы ехать. Машины из плана попадают сюда же (дублей нет:
// ключ «маршрут|борт», см. upsertVehicle).
const vehicleLayer = L.layerGroup().addTo(map);

// На телефоне карта сначала может быть нулевой высоты — просим пересчитать.
window.addEventListener('load', () => setTimeout(() => map.invalidateSize(), 200));
window.addEventListener('resize', () => map.invalidateSize());

function clearLayers() {
  layerGroup.clearLayers();
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
  return String(vehicle.route_label || '?') + '|' + String(vehicle.board_number || '?');
}

/**
 * Маркер ТС (спецификация дизайна Gemini, docs/BRIEF-emulator-vehicles-visual.md):
 * круг радиусом 11px с номером маршрута + выступающий сверху треугольник-стрелка.
 * Стрелка повёрнута за курсом (0° — на север), круг и подпись — прямые.
 *
 * Состояния — классами на обёртке: live / planned (приглушён), stopped
 * (стрілка ховається, обводка пульсує) / target (золоте пульсування цілі).
 */
function vehicleIcon(vehicle) {
  const heading = Number(vehicle.heading_deg);
  const angle = Number.isFinite(heading) ? heading.toFixed(1) : '0.0';
  const live = !!vehicle.is_live;
  const colour = vehicle.route_colour_hex || '#4f8cff';
  // Підпис і борт теж приходять з API — обидва через esc() (див. vehiclePopup).
  const label = esc(String(vehicle.route_label || '').slice(0, 4));
  const stopped = Number(vehicle.speed_kmh) < 3;
  const isTarget = state.targetBoards.has(boardKey(vehicle));

  let wrapClass = 'veh-wrap' + (live ? ' live' : ' planned');
  if (stopped) wrapClass += ' stopped';
  if (isTarget) wrapClass += ' target';

  const html = `
    <div class="${wrapClass}" data-heading="${angle}" data-board="${esc(vehicle.board_number)}">
      <svg width="36" height="36" viewBox="0 0 36 36">
        <g class="veh-arrow-group" transform="rotate(${angle} 18 18)">
          <path class="veh-arrow" d="M 18 2 L 24 10 L 12 10 Z" fill="${colour}" stroke="#14161a" stroke-width="1.5" stroke-linejoin="round"/>
        </g>
        <circle cx="18" cy="18" r="11" fill="#1e2126" stroke="${colour}" stroke-width="2.5"/>
        <text x="18" y="21.5" text-anchor="middle" font-family="sans-serif" font-size="10" font-weight="700" fill="#ffffff">${label}</text>
      </svg>
    </div>`;

  return L.divIcon({
    className: 'veh-marker' + (isTarget ? ' is-target' : ''),
    html,
    iconSize: [36, 36],
    iconAnchor: [18, 18],
    popupAnchor: [0, -18],
  });
}

/** Борт-номер как он приходит и в /api/live, и в leg.live_bus (одно и то же поле). */
function boardKey(vehicle) {
  return String(vehicle.board_number === undefined || vehicle.board_number === null
    ? '' : vehicle.board_number);
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
  const boards = new Set();
  (Array.isArray(legs) ? legs : []).forEach((leg) => {
    const board = String(leg && leg.live_bus ? leg.live_bus : '').trim();
    if (board && board !== '?') boards.add(board);
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

  return '<div class="veh-popup">' +
    '<div class="veh-popup-head">' +
      '<span class="badge ' + (live ? 'live' : 'plan') + '">' + (live ? 'GPS' : 'Розклад') + '</span>' +
      '<strong>Маршрут ' + route + '</strong>' +
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
  state.vehicles.forEach((entry) => {
    if (entry.vehicle.is_live) live += 1;
    else planned += 1;
  });
  const time = state.modelNow ? state.modelNow.replace('T', ' ') : 'зараз';
  const targets = [...state.targetBoards];
  el.textContent =
    'ТС на карті: ' + live + ' живих' + (planned ? ' + ' + planned + ' за розкладом' : '') +
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
  document.getElementById('answer').style.display = 'block';
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

/** Основной режим: сервер посчитал маршрут (возможно, с пересадкой). */
function renderPlan(plan) {
  const bounds = [];
  const lines = [];

  plan.legs.forEach((leg, index) => {
    if (leg.type === 'transit') {
      const path = Array.isArray(leg.path) ? leg.path : [];
      if (path.length > 1) {
        L.polyline(path, { color: leg.color || '#4f8cff', weight: 6, opacity: .9 }).addTo(layerGroup);
        path.forEach((point) => bounds.push(point));
      }
      const isFirst = index === 0;
      lines.push(
        (isFirst ? '1️⃣ ' : '2️⃣ ') + (leg.vehicle === 'trolley' ? '🚎' : '🚌') + ' ' + leg.route +
        ': «' + leg.from + '» → «' + leg.to + '», ' + leg.travel_min + ' хв у дорозі, чекати ~' + leg.wait_min + ' хв'
      );

      // Перший потрібний ТС: те, у що сідати в цій нозі.
      if (leg.live_bus || leg.eta) {
        lines.push('   ↳ сідати: ' + (leg.live_bus || 'ТЗ') + (leg.eta ? ', буде ~' + leg.eta : '') +
          (leg.vehicle_state ? ' [' + leg.vehicle_state + ']' : ''));
      }
    } else if (leg.type === 'transfer') {
      const isWalk = leg.kind === 'walk';
      const icon = isWalk ? '🚶' : '⇄';
      const name = isWalk ? ' йдемо до «' + leg.at + '»' : ' пересадка на «' + leg.at + '»';
      const walkNote = leg.walk_min ? ' (' + leg.walk_min + ' хв пішки)' : '';
      const waitNote = leg.wait_min ? ', чекати ~' + leg.wait_min + ' хв' : '';
      // Пішохідна частина: коли роутер почне віддавати геометрію пересадки
      // (leg.path), малюємо її «як у Google Maps» — пунктир із кружечків.
      const walkPath = isWalk && Array.isArray(leg.path) ? leg.path : [];
      if (walkPath.length > 1) {
        L.polyline(walkPath, {
          color: '#808080', weight: 5, dashArray: '1, 10',
          lineCap: 'round', lineJoin: 'round',
        }).addTo(layerGroup);
        walkPath.forEach((point) => bounds.push(point));
      }
      lines.push(icon + name + walkNote + waitNote);
    }
  });

  if (Array.isArray(plan.vehicles)) {
    renderVehicles(plan.vehicles, bounds, plan.legs);
  }

  if (bounds.length) map.fitBounds(bounds, { padding: [40, 40] });

  const summary = [];
  if (plan.total_min) summary.push('усього ~' + plan.total_min + ' хв');
  if (plan.price_grn !== undefined) summary.push('вартість ~' + plan.price_grn + ' грн');
  if (plan.transfers !== undefined) summary.push('пересадок: ' + plan.transfers);

  document.getElementById('answer').innerHTML =
    '<span class="badge plan">план маршруту</span> ' + summary.join(' · ') + '\n' + lines.join('\n');
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
  { icon: '--', spacing: '2px', text: 'Пішки' },
];

function legendIconStyle(row) {
  const parts = ['color: ' + (row.colour || 'inherit')];
  if (row.glow) parts.push('text-shadow: 0 0 5px ' + row.glow);
  if (row.spacing) parts.push('letter-spacing: ' + row.spacing);
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

legend.addTo(map);

// Розвантаження при віддаленні: стан рахуємо на кожному 'zoomend' (fitBounds у
// плані теж його кидає) і один раз при старті — карта відкривається на zoom 13,
// тож номери маршрутів видно одразу.
map.on('zoomend', updateZoomState);
updateZoomState();

renderChips();
loadStops();


