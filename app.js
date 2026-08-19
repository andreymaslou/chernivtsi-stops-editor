// App State
let state = {
  currentTransportType: 'bus',
  currentRouteNumber: '',
  currentDirection: '',
  routes: {},
  pendingMarker: null,
  stopMarkers: [],
  dragIndex: null,
  exportData: { content: '', filename: '' }
};

const AVAILABLE_TROLLEYS = ["1", "2", "3", "4", "5", "6", "6A", "8"];
const AVAILABLE_BUSES = ["1", "2", "3", "4", "5", "6", "7", "8", "8А", "9", "9A", "10", "10A", "13", "14", "15", "15К", "19", "20", "21", "23", "24", "25", "26", "27", "29", "30", "31", "32", "33", "34", "35", "35A", "36", "37", "39", "41", "43"];

// DOM Elements
const els = {
  routeNumber: document.getElementById('route-number'),
  direction: document.getElementById('direction')
};

// Global polyline ref
let routePolyline = null;

// ===== Init Map =====
const map = L.map('map', {
  center: [48.2921, 25.9358],
  zoom: 14,
  zoomControl: true,
});

L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
  maxZoom: 19,
}).addTo(map);

// ===== Helpers =====
function getRouteKey(dir) {
  return `${state.currentTransportType}-${state.currentRouteNumber}-${dir}`;
}

function getCurrentStops() {
  const key = getRouteKey(state.currentDirection);
  if (!state.routes[key]) state.routes[key] = [];
  return state.routes[key];
}

function getRouteBearing(lat1, lon1, lat2, lon2) {
  const toRad = deg => deg * Math.PI / 180;
  const toDeg = rad => rad * 180 / Math.PI;
  const dLon = toRad(lon2 - lon1);
  lat1 = toRad(lat1);
  lat2 = toRad(lat2);
  const y = Math.sin(dLon) * Math.cos(lat2);
  const x = Math.cos(lat1) * Math.sin(lat2) - Math.sin(lat1) * Math.cos(lat2) * Math.cos(dLon);
  const bearing = (toDeg(Math.atan2(y, x)) + 360) % 360;
  return Math.round(bearing);
}

function recalculateAllAngles() {
  const stops = getCurrentStops();
  if (!stops || stops.length === 0) return;
  for (let i = 0; i < stops.length; i++) {
    if (stops[i].customAngle) continue;
    let p1 = stops[i];
    let p2 = stops[i + 1];
    if (p2) {
      stops[i].angle = getRouteBearing(p1.lat, p1.lon, p2.lat, p2.lon);
    } else if (i > 0) {
      let prev = stops[i - 1];
      stops[i].angle = getRouteBearing(prev.lat, prev.lon, p1.lat, p1.lon);
    } else {
      stops[i].angle = 0;
    }
  }
}

function showToast(msg, type = 'default') {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = `toast ${type}`;
  t.classList.remove('hidden');
  setTimeout(() => t.classList.add('hidden'), 2500);
}

function updateRouteLabel() {
  const typeText = state.currentTransportType === 'bus' ? 'Автобус' : 'Тролейбус';
  document.getElementById('route-label').textContent = `${typeText} ${state.currentRouteNumber} / ${state.currentDirection}`;
}

// Populate Route Dropdown
function populateRouteDropdown(type) {
  const arr = type === 'trolley' ? AVAILABLE_TROLLEYS : AVAILABLE_BUSES;
  els.routeNumber.innerHTML = '';
  arr.forEach(num => {
    const opt = document.createElement('option');
    opt.value = num;
    opt.textContent = num;
    els.routeNumber.appendChild(opt);
  });
}

// Function to handle Transport Type change via UI buttons
window.setTransportType = function(type) {
  state.currentTransportType = type;
  document.getElementById('btn-bus').classList.remove('active');
  document.getElementById('btn-trolley').classList.remove('active');
  document.getElementById('btn-' + type).classList.add('active');
  populateRouteDropdown(type);
  window.handleRouteChange();
};

// ===== Map Click =====
map.on('click', function (e) {
  const { lat, lng } = e.latlng;
  document.getElementById('stop-lat').value = lat.toFixed(6);
  document.getElementById('stop-lon').value = lng.toFixed(6);

  const stops = getCurrentStops();
  const badge = document.getElementById('current_stop_order');
  badge.textContent = stops.length + 1;
  badge.style.display = 'flex';
  badge.style.background = '#4ade80';

  // Place/move temp marker
  if (state.pendingMarker) {
    state.pendingMarker.setLatLng([lat, lng]);
  } else {
    state.pendingMarker = L.circleMarker([lat, lng], {
      radius: 11,
      color: '#ffffff',
      fillColor: '#8b85ff',
      fillOpacity: 1,
      weight: 2.5,
    }).addTo(map);
    state.pendingMarker.bindTooltip('Нова зупинка', {
      permanent: true, direction: 'top', className: 'stop-marker-label',
    }).openTooltip();
  }

  // Try reverse geocode for auto-name suggestion
  reverseGeocode(lat, lng);
});

async function reverseGeocode(lat, lng) {
  try {
    const url = `https://nominatim.openstreetmap.org/reverse?lat=${lat}&lon=${lng}&format=json&accept-language=uk`;
    const res = await fetch(url, { headers: { 'Accept-Language': 'uk' } });
    const data = await res.json();

    let suggestion = '';
    const addr = data.address;
    if (addr) {
      const road = addr.road || addr.pedestrian || addr.footway || '';
      const hn = addr.house_number || '';
      if (road) suggestion = road + (hn ? ' ' + hn : '');
    }

    const nameInput = document.getElementById('stop-name');
    if (suggestion && !nameInput.value) {
      nameInput.value = suggestion;
      nameInput.style.borderColor = '#6c63ff';
      setTimeout(() => nameInput.style.borderColor = '', 1200);
    }
  } catch (e) { /* silent */ }
}

// ===== Search =====
document.getElementById('search-btn').addEventListener('click', doSearch);
document.getElementById('search-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') doSearch();
});

async function doSearch() {
  const q = document.getElementById('search-input').value.trim();
  if (!q) return;

  const container = document.getElementById('search-results');
  container.innerHTML = '<div class="search-item">⏳ Пошук...</div>';
  container.classList.remove('hidden');

  try {
    const url = `https://nominatim.openstreetmap.org/search?q=${encodeURIComponent(q + ', Чернівці')}&format=json&limit=6&accept-language=uk&addressdetails=1`;
    const res = await fetch(url, { headers: { 'Accept-Language': 'uk' } });
    const results = await res.json();

    if (!results.length) {
      container.innerHTML = '<div class="search-item">Нічого не знайдено</div>';
      return;
    }

    container.innerHTML = '';
    results.forEach(r => {
      const div = document.createElement('div');
      div.className = 'search-item';

      const name = r.display_name.split(',').slice(0, 2).join(',');
      div.innerHTML = `<strong>📍</strong> ${name}`;
      div.title = r.display_name;

      div.addEventListener('click', () => {
        const lat = parseFloat(r.lat);
        const lng = parseFloat(r.lon);

        document.getElementById('stop-lat').value = lat.toFixed(6);
        document.getElementById('stop-lon').value = lng.toFixed(6);

        const stops = getCurrentStops();
        const badge = document.getElementById('current_stop_order');
        badge.textContent = stops.length + 1;
        badge.style.display = 'flex';
        badge.style.background = '#4ade80';

        // Suggest name
        const addr = r.address;
        let suggestion = '';
        if (addr) {
          const road = addr.road || addr.pedestrian || addr.footway || '';
          const hn = addr.house_number || '';
          if (road) suggestion = road + (hn ? ' ' + hn : '');
        }
        if (suggestion) document.getElementById('stop-name').value = suggestion;

        // Move map
        map.setView([lat, lng], 17, { animate: true });

        // Move temp marker
        if (state.pendingMarker) {
          state.pendingMarker.setLatLng([lat, lng]);
        } else {
          state.pendingMarker = L.circleMarker([lat, lng], {
            radius: 11, color: '#ffffff', fillColor: '#8b85ff',
            fillOpacity: 1, weight: 2.5,
          }).addTo(map);
          state.pendingMarker.bindTooltip('Нова зупинка', {
            permanent: true, direction: 'top', className: 'stop-marker-label',
          }).openTooltip();
        }

        container.classList.add('hidden');
      });

      container.appendChild(div);
    });
  } catch (err) {
    container.innerHTML = '<div class="search-item">Помилка пошуку</div>';
  }
}

// Hide search results on outside click
document.addEventListener('click', e => {
  if (!e.target.closest('.search-wrap') && !e.target.closest('.search-results')) {
    document.getElementById('search-results').classList.add('hidden');
  }
});

// ===== Add Stop =====
document.getElementById('add-stop-btn').addEventListener('click', addStop);
document.getElementById('stop-name').addEventListener('keydown', e => {
  if (e.key === 'Enter') addStop();
});

function addStop() {
  const name = document.getElementById('stop-name').value.trim();
  const lat = parseFloat(document.getElementById('stop-lat').value);
  const lon = parseFloat(document.getElementById('stop-lon').value);
  const angleInput = document.getElementById('stop-angle').value;

  if (!name) { showToast('⚠ Введіть назву зупинки', 'error'); return; }
  if (isNaN(lat) || isNaN(lon)) { showToast('⚠ Клікніть на карті або знайдіть через пошук', 'error'); return; }

  const stops = getCurrentStops();
  const badgeText = document.getElementById('current_stop_order').textContent;
  
  let newObj = { name, lat, lon };
  
  // Angle parsing
  if (angleInput.trim() !== '') {
    newObj.angle = parseInt(angleInput);
    newObj.customAngle = true;
  } else {
    newObj.angle = 0;
    newObj.customAngle = false;
  }

  if (badgeText !== '+' && !isNaN(parseInt(badgeText))) {
    // Modify existing stop
    const idx = parseInt(badgeText) - 1;
    stops[idx] = { ...stops[idx], ...newObj };
    showToast(`✅ Зупинку "${name}" оновлено`, 'success');
  } else {
    // Add new stop
    stops.push(newObj);
    showToast(`✅ Зупинку "${name}" додано`, 'success');
  }

  // Clear pending marker and place permanent
  if (state.pendingMarker) {
    map.removeLayer(state.pendingMarker);
    state.pendingMarker = null;
  }
  
  recalculateAllAngles();
  redrawAllMarkers();
  renderStopsList();

  // Reset inputs
  document.getElementById('stop-name').value = '';
  document.getElementById('stop-lat').value = '';
  document.getElementById('stop-lon').value = '';
  document.getElementById('stop-angle').value = '';
  const badge = document.getElementById('current_stop_order');
  badge.textContent = '+';
  badge.style.background = '#323232';
}

function placeStopMarker(stop, index) {
  const angle = stop.angle !== undefined ? Math.round(stop.angle) : 0;
  const displayAngle = stop.angle !== undefined ? Math.round(stop.angle) : '-';
  
  // The group <g> rotates the arrow, while circle and text stay upright
  const svgHTML = `
    <div style="width: 48px; height: 48px; filter: drop-shadow(0 2px 5px rgba(0,0,0,0.4));">
      <svg width="48" height="48" viewBox="0 0 48 48">
        <g style="transform: rotate(${angle}deg); transform-origin: 24px 24px;">
          <path d="M 24 2 L 34 18 L 14 18 Z" fill="#ffffff" stroke="#ef4444" stroke-width="2.5" stroke-linejoin="round"/>
        </g>
        <circle cx="24" cy="24" r="14" fill="#ffffff" stroke="#ef4444" stroke-width="3" />
        <text x="24" y="28" font-family="sans-serif" font-size="11" font-weight="bold" fill="#000" text-anchor="middle">${displayAngle}</text>
      </svg>
    </div>
  `;

  const marker = L.marker([stop.lat, stop.lon], {
    icon: L.divIcon({
      className: 'dir-marker',
      html: svgHTML,
      iconSize: [48, 48],
      iconAnchor: [24, 24]
    })
  }).addTo(map);

  marker.bindTooltip(`${index}. ${stop.name}`, {
    permanent: false, direction: 'top', className: 'stop-marker-label',
  });

  marker.on('click', () => {
    document.getElementById('stop-name').value = stop.name;
    document.getElementById('stop-lat').value = stop.lat.toFixed(6);
    document.getElementById('stop-lon').value = stop.lon.toFixed(6);
    document.getElementById('stop-angle').value = stop.angle !== undefined ? stop.angle : '';
    
    const badge = document.getElementById('current_stop_order');
    badge.textContent = index;
    badge.style.display = 'flex';
    badge.style.background = 'linear-gradient(135deg, #6c63ff 0%, #8b85ff 100%)';
    
    if (state.pendingMarker) {
      state.pendingMarker.setLatLng([stop.lat, stop.lon]);
    } else {
      state.pendingMarker = L.circleMarker([stop.lat, stop.lon], {
        radius: 20, color: '#ffffff', fillColor: '#1e3a8a',
        fillOpacity: 1, weight: 3.5,
      }).addTo(map);
    }
  });

  state.stopMarkers.push(marker);
}

function redrawAllMarkers() {
  state.stopMarkers.forEach(m => map.removeLayer(m));
  state.stopMarkers = [];
  if (routePolyline) map.removeLayer(routePolyline);

  const stops = getCurrentStops();
  const latlngs = stops.map(s => [s.lat, s.lon]);
  
  if (latlngs.length > 1) {
    routePolyline = L.polyline(latlngs, {
      color: '#4ade80',
      weight: 3,
      opacity: 0.6,
      dashArray: '5, 8'
    }).addTo(map);
  }

  stops.forEach((stop, i) => placeStopMarker(stop, i + 1));
}

// ===== Render Stops List =====
function renderStopsList() {
  const stops = getCurrentStops();
  const container = document.getElementById('stops-list');
  document.getElementById('stop-count').textContent = stops.length;

  if (!stops.length) {
    container.innerHTML = '<div class="empty-state">Клікніть на карті щоб додати першу зупинку</div>';
    return;
  }

  container.innerHTML = '';
  stops.forEach((stop, i) => {
    const card = document.createElement('div');
    card.className = 'stop-card';
    card.draggable = true;
    card.dataset.index = i;

    card.innerHTML = `
      <div class="stop-num">${i + 1}</div>
      <div class="stop-info">
        <div class="stop-name-text">${escapeHtml(stop.name)}</div>
        <div class="stop-coords">${stop.lat.toFixed(6)}, ${stop.lon.toFixed(6)} <span style="background:#555; padding:2px 6px; border-radius:4px; margin-left:5px; font-size: 10px;">∠ ${stop.angle !== undefined ? stop.angle : 0}°</span></div>
      </div>
      <button class="stop-del" data-index="${i}" title="Видалити">✕</button>
    `;

    // Fly to and populate inputs on click
    card.addEventListener('click', (e) => {
      if (e.target.classList.contains('stop-del')) return;
      map.flyTo([stop.lat, stop.lon], 17, { animate: true, duration: 0.8 });
      
      // Populate inputs for easy copying to admin panel
      document.getElementById('stop-name').value = stop.name;
      document.getElementById('stop-lat').value = stop.lat.toFixed(6);
      document.getElementById('stop-lon').value = stop.lon.toFixed(6);
      document.getElementById('stop-angle').value = stop.angle !== undefined ? stop.angle : '';
      
      // Move temp pending marker to highlight selection
      if (state.pendingMarker) {
        state.pendingMarker.setLatLng([stop.lat, stop.lon]);
      } else {
        state.pendingMarker = L.circleMarker([stop.lat, stop.lon], {
          radius: 20, color: '#ffffff', fillColor: '#1e3a8a',
          fillOpacity: 1, weight: 3.5,
        }).addTo(map);
      }

      const badge = document.getElementById('current_stop_order');
      badge.textContent = i + 1;
      badge.style.display = 'flex';
      badge.style.background = 'linear-gradient(135deg, #6c63ff 0%, #8b85ff 100%)';
    });

    // Delete
    card.querySelector('.stop-del').addEventListener('click', (e) => {
      e.stopPropagation();
      getCurrentStops().splice(i, 1);
      recalculateAllAngles();
      redrawAllMarkers();
      renderStopsList();
      showToast(`🗑 Зупинку видалено`);
    });

    // Drag & Drop reorder
    card.addEventListener('dragstart', () => {
      state.dragIndex = i;
      card.classList.add('dragging');
    });
    card.addEventListener('dragend', () => {
      card.classList.remove('dragging');
      document.querySelectorAll('.stop-card').forEach(c => c.classList.remove('drag-over'));
    });
    card.addEventListener('dragover', e => {
      e.preventDefault();
      card.classList.add('drag-over');
    });
    card.addEventListener('dragleave', () => card.classList.remove('drag-over'));
    card.addEventListener('drop', e => {
      e.preventDefault();
      card.classList.remove('drag-over');
      const from = state.dragIndex;
      const to = i;
      if (from === null || from === to) return;
      const stops = getCurrentStops();
      const [item] = stops.splice(from, 1);
      stops.splice(to, 0, item);
      state.dragIndex = null;
      recalculateAllAngles();
      redrawAllMarkers();
      renderStopsList();
    });

    container.appendChild(card);
  });
}

function escapeHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ===== Route change =====
window.handleRouteChange = async () => {
  // Clear map markers before loading new route
  state.stopMarkers.forEach(m => map.removeLayer(m));
  state.stopMarkers = [];
  if (routePolyline) map.removeLayer(routePolyline);

  state.currentRouteNumber = els.routeNumber.value;
  state.currentDirection = els.direction.value;
  updateRouteLabel();
  await loadRouteFromScrapedData();
  redrawAllMarkers();
  renderStopsList();
};

async function loadRouteFromScrapedData() {
  const key = getRouteKey(state.currentDirection);
  const stops = getCurrentStops();
  
  // Don't overwrite if it already has stops loaded
  if (stops.length > 0) return;

  try {
    const type = state.currentTransportType;
    const num = state.currentRouteNumber;
    const dir = state.currentDirection;
    const rawNum = num.replace(/[^a-zA-Z0-9А-Яа-яЄєІіЇїҐґ]/g, '');
    let res = await fetch(`scraped_data/route_${type}_${rawNum}_${dir}.json`);
    
    // Fallback to old format
    if (!res.ok) {
      res = await fetch(`scraped_data/route_${rawNum}_${dir}.json`);
    }

    if (!res.ok) return;
    const data = await res.json();
    if (Array.isArray(data) && data.length > 0) {
      state.routes[key] = data.map(s => ({
        name: s.name,
        lat: parseFloat(s.lat),
        lon: parseFloat(s.lon),
        angle: s.angle || 0,
        customAngle: s.angle !== undefined
      }));
      recalculateAllAngles();
      showToast(`✅ Завантажено маршрут ${num} (${dir})`, 'success');
      redrawAllMarkers();
      renderStopsList();
      if (state.routes[key][0]) {
        map.flyTo([state.routes[key][0].lat, state.routes[key][0].lon], 13);
      }
    }
  } catch (e) {
    // silent failure if file doesn't exist
  }
}

// ===== Clear =====
document.getElementById('clear-btn').addEventListener('click', () => {
  if (!getCurrentStops().length) return;
  if (!confirm('Очистити всі зупинки поточного напрямку?')) return;
  const key = getRouteKey();
  state.routes[key] = [];
  redrawAllMarkers();
  renderStopsList();
  showToast('🗑 Список очищено');
});

// ===== Export =====
document.getElementById('export-csv-btn').addEventListener('click', () => {
  const stops = getCurrentStops();
  if (!stops.length) { showToast('⚠ Список зупинок порожній', 'error'); return; }

  const num = document.getElementById('route-number').value.trim();
  const dir = document.getElementById('direction').value;
  const dirNum = dir === 'A' ? 1 : 2;

  let csv = 'order,name,lat,lon,route,direction,angle\n';
  stops.forEach((s, i) => {
    csv += `${i + 1},"${s.name}",${s.lat.toFixed(6)},${s.lon.toFixed(6)},${num},${dirNum},${s.angle || 0}\n`;
  });

  state.exportData = { content: csv, filename: `route_${num}_${dir}.csv`, type: 'text/csv' };
  document.getElementById('modal-title').textContent = `CSV — Маршрут ${num} / ${dir}`;
  document.getElementById('export-content').textContent = csv;
  document.getElementById('export-modal').classList.remove('hidden');
});

document.getElementById('export-json-btn').addEventListener('click', () => {
  const stops = getCurrentStops();
  if (!stops.length) { showToast('⚠ Список зупинок порожній', 'error'); return; }

  const num = document.getElementById('route-number').value.trim();
  const dir = document.getElementById('direction').value;
  const dirNum = dir === 'A' ? 1 : 2;

  const data = stops.map((s, i) => ({
    order: i + 1,
    name: s.name,
    lat: parseFloat(s.lat.toFixed(6)),
    lon: parseFloat(s.lon.toFixed(6)),
    route: num,
    direction: dirNum,
    angle: s.angle || 0
  }));

  const json = JSON.stringify(data, null, 2);
  state.exportData = { content: json, filename: `route_${num}_${dir}.json`, type: 'application/json' };
  document.getElementById('modal-title').textContent = `JSON — Маршрут ${num} / ${dir}`;
  document.getElementById('export-content').textContent = json;
  document.getElementById('export-modal').classList.remove('hidden');
});

// Import JSON
document.getElementById('import-json-btn').addEventListener('click', () => {
  document.getElementById('import-file-input').click();
});

document.getElementById('import-file-input').addEventListener('change', (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = (ev) => {
    try {
      const data = JSON.parse(ev.target.result);
      if (!Array.isArray(data)) throw new Error("JSON format error");
      
      const stops = data.map(s => ({
        name: s.name,
        lat: s.lat,
        lon: s.lon,
        angle: s.angle || 0,
        customAngle: s.angle !== undefined,
      }));
      
      const key = getRouteKey();
      state.routes[key] = stops;
      
      recalculateAllAngles();
      updateRouteLabel();
      redrawAllMarkers();
      renderStopsList();

      if (stops.length > 0) {
          map.flyTo([stops[0].lat, stops[0].lon], 14);
      }

      showToast('✅ Дані успішно імпортовано', 'success');
    } catch (err) {
      showToast('⚠ Помилка імпорту JSON', 'error');
    }
  };
  reader.readAsText(file);
  e.target.value = ''; // Reset input
});

// Modal close
document.getElementById('modal-close').addEventListener('click', () => {
  document.getElementById('export-modal').classList.add('hidden');
});
document.getElementById('export-modal').addEventListener('click', e => {
  if (e.target === document.getElementById('export-modal')) {
    document.getElementById('export-modal').classList.add('hidden');
  }
});

// Initial setup is handled in Init at the bottom

// Copy
document.getElementById('copy-btn').addEventListener('click', () => {
  navigator.clipboard.writeText(state.exportData.content).then(() => {
    showToast('📋 Скопійовано в буфер обміну!', 'success');
  });
});

// Download
document.getElementById('download-btn').addEventListener('click', () => {
  const blob = new Blob([state.exportData.content], { type: state.exportData.type });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = state.exportData.filename;
  a.click();
  URL.revokeObjectURL(url);
  showToast('📥 Файл завантажено', 'success');
});

// ===== Copy Helper =====
window.copyField = function(id) {
  const el = document.getElementById(id);
  if (!el.value) return;
  navigator.clipboard.writeText(el.value).then(() => {
    // Visual feedback
    const originalBg = el.style.backgroundColor;
    el.style.backgroundColor = 'rgba(74, 222, 128, 0.2)';
    setTimeout(() => el.style.backgroundColor = originalBg, 400);
    showToast('📋 Скoпійовано: ' + el.value, 'success');
  });
};

// ===== Init =====
populateRouteDropdown('bus');
setTimeout(window.handleRouteChange, 100);

