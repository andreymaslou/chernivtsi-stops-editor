/* Контекстный редактор сленга для выбранной остановки. */
(() => {
  'use strict';

  const panelState = { stops: [], overrides: {}, current: null, aliases: [], loadPromise: null };
  const $ = (id) => document.getElementById(id);
  const norm = (value) => String(value || '').trim().toLocaleLowerCase('uk-UA');
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch]));

  async function loadCatalog() {
    if (panelState.loadPromise) return panelState.loadPromise;
    panelState.loadPromise = fetch('/api/slang?include_stops=true', { cache: 'no-store' })
      .then(async (response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        panelState.stops = data.stops || [];
        panelState.overrides = (data.overrides || {}).stops || {};
        return panelState.stops;
      })
      .catch((error) => { panelState.loadPromise = null; throw error; });
    return panelState.loadPromise;
  }

  function findCatalogStop(routeStop) {
    if (!routeStop) return null;
    let best = null;
    let bestDistance = Infinity;
    for (const stop of panelState.stops) {
      const lat = Number(stop.lat);
      const lon = Number(stop.lon);
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
      const distance = (lat - Number(routeStop.lat)) ** 2 + (lon - Number(routeStop.lon)) ** 2;
      if (distance < bestDistance) { bestDistance = distance; best = stop; }
    }
    // Для одинаковых названий выбираем конкретную физическую остановку.
    return bestDistance <= 0.000002 ? best : null;
  }

  function currentOverride(stop) {
    return stop ? (panelState.overrides[String(stop.id)] || {}) : {};
  }

  function renderAliases() {
    const container = $('slang-panel-aliases');
    if (!container) return;
    container.innerHTML = '';
    if (!panelState.aliases.length) {
      const empty = document.createElement('div');
      empty.className = 'slang-alias-empty';
      empty.textContent = 'Пока нет вариантов — добавьте, как говорят пассажиры.';
      container.appendChild(empty);
      return;
    }
    panelState.aliases.forEach((alias, index) => {
      const row = document.createElement('div');
      row.className = 'slang-alias-row';
      row.innerHTML = `<input type="text" class="input-field slang-alias-input" value="${esc(alias)}" aria-label="Псевдоним ${index + 1}" /><button type="button" class="slang-alias-remove" title="Удалить" aria-label="Удалить псевдоним">✕</button>`;
      row.querySelector('input').addEventListener('input', (event) => {
        panelState.aliases[index] = event.target.value;
        renderDuplicates();
      });
      row.querySelector('button').addEventListener('click', () => {
        panelState.aliases.splice(index, 1);
        renderAliases();
        renderDuplicates();
      });
      container.appendChild(row);
    });
  }

  function duplicateRows() {
    const stop = panelState.current;
    if (!stop) return [];
    const ownAliases = new Set(panelState.aliases.map(norm).filter(Boolean));
    const rows = [];
    for (const other of panelState.stops) {
      if (String(other.id) === String(stop.id)) continue;
      const aliases = (other.aliases || []).filter((alias) => ownAliases.has(norm(alias)));
      if (aliases.length) rows.push({ stop: other, aliases });
    }
    return rows;
  }

  function renderDuplicates() {
    const box = $('slang-panel-duplicates');
    if (!box) return;
    const rows = duplicateRows();
    box.hidden = !rows.length;
    if (!rows.length) return;
    box.innerHTML = '<div class="slang-duplicates-title">⚠ Такі слова вже є в інших зупинках</div>' +
      rows.map((row) => `<button type="button" class="slang-duplicate-row" data-stop-id="${esc(row.stop.id)}"><span>${esc(row.aliases.join(', '))}</span><b>${esc(row.stop.name)}</b><small>#${esc(row.stop.id)}</small></button>`).join('');
    box.querySelectorAll('[data-stop-id]').forEach((button) => {
      button.addEventListener('click', () => {
        const target = panelState.stops.find((stop) => String(stop.id) === button.dataset.stopId);
        if (target && window.TRANSPORT_MAP) window.TRANSPORT_MAP.flyTo([target.lat, target.lon], 17, { duration: 0.6 });
      });
    });
  }

  function open(routeStop) {
    const panel = $('slang-panel');
    if (!panel) return;
    panel.hidden = false;
    panel.classList.add('open');
    $('slang-panel-status').textContent = 'Завантаження…';
    $('slang-panel-save').disabled = true;
    loadCatalog().then(() => {
      panelState.current = findCatalogStop(routeStop);
      const stop = panelState.current;
      if (!stop) {
        $('slang-panel-title').textContent = routeStop.name || 'Зупинка';
        $('slang-panel-id').textContent = 'Не знайдено в довіднику за координатами';
        $('slang-panel-context').hidden = false;
        $('slang-panel-context').textContent = 'Цю зупинку ще немає в stops.json — сленг зберегти неможливо.';
        $('slang-panel-name').value = '';
        panelState.aliases = [];
        renderAliases();
        renderDuplicates();
        $('slang-panel-status').textContent = 'Додай зупинку в каталог перед редагуванням сленгу.';
        return;
      }
      const override = currentOverride(stop);
      $('slang-panel-title').textContent = stop.name;
      $('slang-panel-id').textContent = `#${stop.id} · ${Number(stop.lat).toFixed(6)}, ${Number(stop.lon).toFixed(6)}`;
      $('slang-panel-context').hidden = false;
      $('slang-panel-context').textContent = `Назва в маршруті: ${routeStop.name || '—'}${Number(routeStop.lat).toFixed(6) === Number(stop.lat).toFixed(6) ? '' : ' · координати підібрано з довідника'}`;
      $('slang-panel-name').value = override.name || '';
      panelState.aliases = Array.isArray(override.aliases) ? override.aliases.slice() : (stop.aliases || []).slice();
      renderAliases();
      renderDuplicates();
      $('slang-panel-save').disabled = false;
      $('slang-panel-status').textContent = 'Зміни збережуться одразу для розпізнавання фраз.';
    }).catch((error) => { $('slang-panel-status').textContent = `Не вдалося завантажити сленг: ${error.message}`; });
  }

  function close() {
    const panel = $('slang-panel');
    if (panel) { panel.hidden = true; panel.classList.remove('open'); }
  }

  function addAlias() {
    panelState.aliases.push('');
    renderAliases();
    const inputs = document.querySelectorAll('#slang-panel-aliases input');
    if (inputs.length) inputs[inputs.length - 1].focus();
  }

  async function save() {
    const stop = panelState.current;
    if (!stop || $('slang-panel-save').disabled) return;
    const aliases = panelState.aliases.map((value) => value.trim()).filter(Boolean);
    const unique = aliases.filter((value, index) => aliases.findIndex((item) => norm(item) === norm(value)) === index);
    $('slang-panel-save').disabled = true;
    $('slang-panel-status').textContent = 'Збереження…';
    try {
      const response = await fetch('/api/slang', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ stop_id: stop.id, aliases: unique, name: $('slang-panel-name').value.trim() }) });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      panelState.aliases = unique;
      panelState.overrides[String(stop.id)] = { ...(panelState.overrides[String(stop.id)] || {}), aliases: unique, name: $('slang-panel-name').value.trim() };
      renderAliases();
      renderDuplicates();
      $('slang-panel-status').textContent = duplicateRows().length ? 'Збережено. Є дублікати — перевір їх на карті.' : '✅ Збережено';
      if (window.showToast) window.showToast('✅ Сленг остановки збережено', 'success');
    } catch (error) { $('slang-panel-status').textContent = `Не збережено: ${error.message}`; }
    finally { $('slang-panel-save').disabled = false; }
  }

  document.addEventListener('DOMContentLoaded', () => {
    $('slang-panel-close')?.addEventListener('click', close);
    $('slang-panel-cancel')?.addEventListener('click', close);
    $('slang-panel-save')?.addEventListener('click', save);
    $('slang-panel-add')?.addEventListener('click', addAlias);
    document.addEventListener('keydown', (event) => { if (event.key === 'Escape') close(); });
  });

  window.SlangPanel = { open, close };
})();
