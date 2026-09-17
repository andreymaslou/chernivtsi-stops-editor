/*
 * Адмінка: редактор сленгу та перегляд скарг.
 *
 * Сленг: псевдоніми («Соборка», «Тралка») і перейменування зупинок
 * («на вимогу» → найближча вулиця). Правки зберігаються в
 * data/slang_overrides.json і діють одразу — сервер перебудовує Locator
 * на кожному збереженні, без перезапуску контейнера.
 */

const MAX_ROWS = 200;

const state = { stops: [], overrides: {}, query: '' };

async function api(url, options) {
  const res = await fetch(url, { cache: 'no-store', ...(options || {}) });
  if (!res.ok) throw new Error('HTTP ' + res.status + ': ' + (await res.text()));
  return res.json();
}

// ---------------------------------------------------------------------------
// Сленг
// ---------------------------------------------------------------------------

async function loadSlang() {
  try {
    const data = await api('/api/slang');
    state.stops = data.stops || [];
    state.overrides = (data.overrides || {}).stops || {};

    const stats = data.stats || {};
    document.getElementById('slang-stats').innerHTML =
      'зупинок у довіднику: <b>' + state.stops.length + '</b>' +
      ' · правок: <b>' + (stats.stops || 0) + '</b>' +
      ' · з псевдонімами: <b>' + (stats.with_aliases || 0) + '</b>' +
      ' · перейменовано: <b>' + (stats.with_name || 0) + '</b>' +
      ' · сховано з пошуку: <b>' + (stats.marked_generic || 0) + '</b>' +
      '<br>файл правок на сервері: <code>' + (stats.path || '—') + '</code>' +
      ' · оновлено: ' + (stats.updated || 'ще не було правок');
    renderSlangList();
  } catch (err) {
    document.getElementById('slang-stats').textContent = 'помилка завантаження: ' + err.message;
  }
}

function matchesQuery(stop, needle) {
  if (!needle) return true;
  if (String(stop.id) === needle) return true;
  if ((stop.name || '').toLowerCase().includes(needle)) return true;
  return (stop.aliases || []).some((alias) => String(alias).toLowerCase().includes(needle));
}

function stopCard(stop) {
  const override = state.overrides[String(stop.id)] || {};

  const card = document.createElement('div');
  card.className = 'card';

  const grid = document.createElement('div');
  grid.className = 'stop-grid';

  // 1. Назва, id, координати, коли остання правка
  const info = document.createElement('div');
  info.innerHTML =
    '<div class="stop-name">' + stop.name + ' <span class="stop-meta">#' + stop.id + '</span></div>' +
    '<div class="stop-meta">' + 
       '<a href="https://www.google.com/maps?q=' + stop.lat + ',' + stop.lon + '" target="_blank" style="text-decoration:none;" title="Відкрити на карті">🗺️ ' + stop.lat.toFixed(5) + ', ' + stop.lon.toFixed(5) + '</a>' + 
    '</div>' +
    (override.updated ? '<div class="stop-meta">правка: ' + override.updated + '</div>' : '');
  grid.appendChild(info);

  // 2. Псевдоніми через кому
  const aliasesInput = document.createElement('input');
  aliasesInput.type = 'text';
  aliasesInput.placeholder = 'псевдоніми через кому: соборка, тралка';
  aliasesInput.value = (stop.aliases || []).join(', ');
  grid.appendChild(aliasesInput);

  // 3. Перейменування (порожнє = назва з stops.json)
  const nameInput = document.createElement('input');
  nameInput.type = 'text';
  nameInput.placeholder = 'нова назва (необов\'язково)';
  nameInput.value = override.name || '';
  grid.appendChild(nameInput);

  grid.appendChild(actionsFor(stop, override, aliasesInput, nameInput));
  card.appendChild(grid);
  return card;
}

/** Кнопки «зберегти/скинути» + перемикач видимості в пошуку. */
function actionsFor(stop, override, aliasesInput, nameInput) {
  const actions = document.createElement('div');
  actions.className = 'row-actions';

  const searchableLabel = document.createElement('label');
  searchableLabel.className = 'generic';
  const searchableBox = document.createElement('input');
  searchableBox.type = 'checkbox';
  // «у пошуку» = НЕ generic. Якщо правки ще немає — показуємо поточний стан системи.
  searchableBox.checked = override.generic !== undefined ? !override.generic : !stop.generic;
  searchableLabel.appendChild(searchableBox);
  searchableLabel.appendChild(document.createTextNode('у пошуку'));

  const saveButton = document.createElement('button');
  saveButton.textContent = 'Зберегти';

  const resetButton = document.createElement('button');
  resetButton.className = 'secondary';
  resetButton.textContent = 'Скинути';
  resetButton.disabled = !override.updated;

  const status = document.createElement('span');
  status.className = 'saved';

  saveButton.onclick = async () => {
    saveButton.disabled = true;
    status.textContent = '';
    try {
      const aliases = aliasesInput.value.split(',').map((alias) => alias.trim()).filter(Boolean);
      await api('/api/slang', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          stop_id: stop.id,
          aliases,
          name: nameInput.value.trim(),      // порожньо = повернути назву з stops.json
          generic: !searchableBox.checked,   // зняли «у пошуку» → ховаємо з пошуку
        }),
      });
      status.textContent = 'збережено ✓';
      await loadSlang();
    } catch (err) {
      status.textContent = 'помилка: ' + err.message;
    } finally {
      saveButton.disabled = false;
    }
  };

  resetButton.onclick = async () => {
    resetButton.disabled = true;
    try {
      await api('/api/slang/' + stop.id, { method: 'DELETE' });
      await loadSlang();
    } catch (err) {
      status.textContent = 'помилка: ' + err.message;
    }
  };

  actions.appendChild(searchableLabel);
  actions.appendChild(saveButton);
  actions.appendChild(resetButton);
  actions.appendChild(status);
  return actions;
}

function renderSlangList() {
  const needle = state.query.trim().toLowerCase();
  const container = document.getElementById('slang-list');
  container.innerHTML = '';

  const filtered = state.stops.filter((stop) => matchesQuery(stop, needle));
  const shown = filtered.slice(0, MAX_ROWS);

  if (!shown.length) {
    const empty = document.createElement('div');
    empty.className = 'hint';
    empty.textContent = 'нічого не знайдено — змініть запит';
    container.appendChild(empty);
    return;
  }

  if (filtered.length > MAX_ROWS) {
    const note = document.createElement('div');
    note.className = 'hint';
    note.textContent = 'показано ' + MAX_ROWS + ' з ' + filtered.length + ' — уточніть пошук';
    container.appendChild(note);
  }

  shown.forEach((stop) => container.appendChild(stopCard(stop)));
}

// ---------------------------------------------------------------------------
// Скарги «це бред»
// ---------------------------------------------------------------------------

async function loadFeedback() {
  try {
    const data = await api('/api/feedback?limit=100');
    const stats = data.stats || {};
    const kinds = Object.entries(stats.by_kind_labeled || {})
      .map(([label, count]) => label + ': ' + count)
      .join(' · ') || 'порожньо';

    document.getElementById('feedback-stats').innerHTML =
      'усього скарг: <b>' + (stats.total || 0) + '</b> · ' + kinds +
      '<br>дні: ' + ((stats.days || []).join(', ') || '—') +
      ' · тека на сервері: <code>data/feedback/</code>';
    renderFeedback(data.items || []);
  } catch (err) {
    document.getElementById('feedback-stats').textContent = 'помилка завантаження: ' + err.message;
  }
}

function renderFeedback(items) {
  const kind = document.getElementById('feedback-kind').value;
  const container = document.getElementById('feedback-list');
  container.innerHTML = '';

  const filtered = kind ? items.filter((item) => item.kind === kind) : items;
  document.getElementById('feedback-note').textContent = 'показано ' + filtered.length + ' з ' + items.length;

  if (!filtered.length) {
    container.innerHTML = '<div class="hint">скарг поки немає (або фільтр зайвий)</div>';
    return;
  }

  filtered.forEach((item) => {
    const card = document.createElement('div');
    card.className = 'card';

    const query = item.user_text ? '<div>фраза: <b>' + escapeHtml(item.user_text) + '</b></div>' : '';
    const comment = item.comment ? '<div>коментар: ' + escapeHtml(item.comment) + '</div>' : '';

    card.innerHTML =
      '<div><span class="kind' + (item.kind === 'nonsense' ? ' danger' : '') + '">' +
      escapeHtml(item.kind_label || item.kind) + '</span> ' +
      '<span class="stop-meta">' + escapeHtml(item.id) + ' · ' + escapeHtml(item.created_at) + '</span></div>' +
      query + comment +
      '<details><summary>що показав емулятор (JSON)</summary><pre>' +
      escapeHtml(JSON.stringify(item.response, null, 1)) + '</pre></details>' +
      '<details><summary>клієнт</summary><pre>' +
      escapeHtml(JSON.stringify(item.client, null, 1)) + '</pre></details>';

    container.appendChild(card);
  });
}

function escapeHtml(value) {
  return String(value === undefined || value === null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

// ---------------------------------------------------------------------------
// Перемикання вкладок і ініціалізація
// ---------------------------------------------------------------------------

function showView(name) {
  document.getElementById('view-slang').classList.toggle('hidden', name !== 'slang');
  document.getElementById('view-feedback').classList.toggle('hidden', name !== 'feedback');
  document.getElementById('tab-slang').classList.toggle('active', name === 'slang');
  document.getElementById('tab-feedback').classList.toggle('active', name === 'feedback');
  if (name === 'feedback') loadFeedback();
}

let searchTimer = null;
document.getElementById('slang-search').oninput = (event) => {
  const value = event.target.value;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.query = value;
    renderSlangList();
  }, 200);
};

document.getElementById('slang-reload').onclick = loadSlang;
document.getElementById('feedback-reload').onclick = loadFeedback;
document.getElementById('feedback-kind').onchange = loadFeedback;
document.getElementById('tab-slang').onclick = () => showView('slang');
document.getElementById('tab-feedback').onclick = () => showView('feedback');

loadSlang();


