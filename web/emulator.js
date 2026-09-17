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
 * Стрелка направления (тот же приём, что в редакторе остановок): группа <g>
 * поворачивается за курсом, круг и подпись остаются прямыми. 0° — на север.
 */
function vehicleIcon(vehicle) {
  const heading = Number(vehicle.heading_deg);
  const angle = Number.isFinite(heading) ? heading.toFixed(1) : '0.0';
  const live = !!vehicle.is_live;
  const colour = vehicle.route_colour_hex || '#4f8cff';
  const fill = live ? colour : '#8b9096';
  const label = String(vehicle.route_label || '').slice(0, 4);
  const stopped = Number(vehicle.speed_kmh) < 3;
  const html = `
    <div class="veh-wrap${live ? ' live' : ' planned'}${stopped ? ' stopped' : ''}" data-heading="${angle}">
      <svg width="34" height="34" viewBox="0 0 34 34">
        <g transform="rotate(${angle} 17 17)">
          <path d="M 17 1 L 26 15 L 8 15 Z" fill="${fill}" stroke="#ffffff" stroke-width="1.8" stroke-linejoin="round"/>
        </g>
        <circle cx="17" cy="17" r="9" fill="${live ? '#111318' : '#4a4f57'}" stroke="#ffffff" stroke-width="1.8"/>
        <text x="17" y="20.5" text-anchor="middle" font-family="sans-serif" font-size="9" font-weight="700" fill="#ffffff">${label}</text>
      </svg>
    </div>`;
  return L.divIcon({
    className: 'veh-marker' + (live ? ' is-live' : ' is-planned'),
    html,
    iconSize: [34, 34],
    iconAnchor: [17, 17],
  });
}

function vehiclePopup(vehicle) {
  const live = !!vehicle.is_live;
  const parts = [
    (live ? '🟢 живий GPS' : ' за розкладом') + ' — ' + (vehicle.route_label || '?') +
    (vehicle.board_number ? ' (борт ' + vehicle.board_number + ')' : ''),
  ];
  if (Number.isFinite(Number(vehicle.speed_kmh))) parts.push('швидкість: ' + vehicle.speed_kmh + ' км/год');
  if (Number.isFinite(Number(vehicle.heading_deg))) parts.push('курс: ' + Math.round(vehicle.heading_deg) + '°');
  if (vehicle.gpstime) {
    parts.push('GPS: ' + vehicle.gpstime +
      (vehicle.age_seconds ? ' (' + Math.round(vehicle.age_seconds) + ' с тому)' : ''));
  }
  if (vehicle.progress !== undefined && vehicle.progress !== null) {
    parts.push('рейс виконано на ' + Math.round(vehicle.progress * 100) + '%');
  }
  return parts.join('<br>');
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
  el.textContent =
    'ТС на карті: ' + live + ' живих' + (planned ? ' + ' + planned + ' за розкладом' : '') +
    (counts && counts.total ? ' (у парку ' + counts.total + ')' : '') +
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
      lines.push(icon + name + walkNote + waitNote);
    }
  });

  if (Array.isArray(plan.vehicles)) {
    renderVehicles(plan.vehicles, bounds);
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
function renderVehicles(vehicles, bounds) {
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

renderChips();
loadStops();


