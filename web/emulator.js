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

const state = { stops: {}, last: null };

// ---------------------------------------------------------------------------
// Карта
// ---------------------------------------------------------------------------

const map = L.map('map', { zoomControl: true }).setView([48.2921, 25.9358], 13);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
  attribution: '© OpenStreetMap',
}).addTo(map);
const layerGroup = L.layerGroup().addTo(map);

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

/** Живые и симулированные ТС: зелёный — реальный GPS, серый — за розкладом. */
function renderVehicles(vehicles, bounds) {
  vehicles.forEach((vehicle) => {
    if (vehicle.lat === undefined || vehicle.lon === undefined) return;
    const live = !!vehicle.is_live;
    L.circleMarker([vehicle.lat, vehicle.lon], {
      radius: live ? 6 : 4,
      color: vehicle.route_colour_hex || '#888',
      weight: 2,
      fillColor: live ? '#43c463' : '#9aa0a6',
      fillOpacity: live ? 1 : .5,
    }).bindPopup(
      (live ? '🟢 живий' : '⚪ за розкладом') + ' — ' + (vehicle.route_label || '') +
      ' (борт ' + (vehicle.board_number || '?') + ')'
    ).addTo(layerGroup);
    bounds.push([vehicle.lat, vehicle.lon]);
  });
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
  document.getElementById('answer').style.display = 'none';
  document.getElementById('report-btn').disabled = true;
  state.last = null;
  setStatus('карту очищено');
};

renderChips();
loadStops();


