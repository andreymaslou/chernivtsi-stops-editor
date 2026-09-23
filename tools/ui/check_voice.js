/*
 * Smoke-перевірка голосового введення (Web Speech API) на /ui/editor.html.
 * Компаніон до tools/ui/check_emulator.js: той прогонить усю сторінку, цей —
 * саме кнопку мікрофона (#ai-voice-btn) і її стани. Легенду карти НЕ чіпає
 * (її навмисно сховано комітом be3c6d5, а check_emulator.js ще чекає її —
 * див. падіння «немає .map-legend» — це окремий pre-existing беклог).
 *
 * Запуск з корня репозиторію (потрібен сервер: python main.py):
 *   node tools/ui/check_voice.js
 *
 * П'ять сценаріїв:
 *   A. Мок Web Speech + дозвіл 'granted': клік → «(Слухаю...)» + .recording →
 *      текст у #ask-text → клік #ask-btn (помічено POST /api/plan) → onend →
 *      базовий стан + CSS .recording; шторка дозволу НЕ показується.
 *   B. Браузер «без Web Speech» (як Firefox): кнопка мікрофона прихована.
 *   C. Реальний Web Speech Chromium: старт/стоп без «залипання» стану; якщо
 *      випала шторка дозволу — тест натискає у ній первинну кнопку.
 *   D. Дозвіл 'prompt' (перший клік): шторка-пояснення → «Дозволити» → старт,
 *      прапорець у localStorage → ДРУГИЙ клік уже без шторки.
 *   E. Дозвіл 'denied': шторка-інструкція (заголовок, кроки, «Зрозуміло»,
 *      без «Пізніше»), розпізнавання НЕ стартує; повторний клік — знову вона.
 *
 * Реальний сервіс розпізнавання у headless непередбачуваний (мережа, тиша,
 * not-allowed), а реальний стан permissions у свіжому профілі — теж, тому
 * тести задають його стабом navigator.permissions (evaluateOnNewDocument),
 * головна логіка — моком (A/D/E), а C лише переконується, що стан завжди
 * прибирається.
 */
const puppeteer = require('puppeteer-extra');
const StealthPlugin = require('puppeteer-extra-plugin-stealth');

puppeteer.use(StealthPlugin());

const args = process.argv.slice(2);
function argValue(name, fallback) {
  const index = args.indexOf(name);
  return index >= 0 && args[index + 1] ? args[index + 1] : fallback;
}

const URL = argValue('--url', 'http://127.0.0.1:8000/ui/editor.html');
const HINT_IDLE = '(AI-ЗАПИТАЙ МЕНЕ)';
const HINT_HEARING = '(Слухаю...)';
const TRANSCRIPT = 'з Калинки до Універу';

let failed = 0;

function check(name, value, note) {
  if (!value) failed += 1;
  console.log((value ? '  OK   ' : '  FAIL ') + name + (note ? ' — ' + note : ''));
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Опитує сторінку, поки predicate не поверне truthy або не вичерпається час
 *  (тоді — останнє значення, хиба/порожнє, тобто FAIL). */
async function waitFor(page, predicate, timeout, step = 50) {
  const started = Date.now();
  let last = null;
  while (Date.now() - started < timeout) {
    last = await page.evaluate(predicate);
    if (last) return last;
    await sleep(step);
  }
  return last;
}

// ---------------------------------------------------------------------------
// A. Мок Web Speech: повний цикл «клік → слухаю → текст → запит → скидання»
// ---------------------------------------------------------------------------
async function scenarioMock(browser) {
  console.log('\n--- A. Мок Web Speech ---');
  const page = await browser.newPage();
  const errors = [];
  let planRequests = 0;
  page.on('pageerror', (err) => errors.push(String(err)));
  page.on('request', (req) => {
    if (req.method() === 'POST' && req.url().includes('/api/plan')) planRequests += 1;
  });
  await page.setViewport({ width: 1400, height: 900 });
  // Headless за замовчуванням віддає prefers-reduced-motion: reduce — для
  // перевірки пульса просимо «як у звичайного користувача» (docs/STATUS.md §5).
  await page.emulateMediaFeatures([{ name: 'prefers-reduced-motion', value: 'no-preference' }]);
  // Підміна конструктора ще ДО завантаження скриптів сторінки.
  await page.evaluateOnNewDocument((text) => {
    class FakeRecognition {
      start() {
        setTimeout(() => this.onstart && this.onstart(), 100);
        setTimeout(() => {
          if (this.onresult) this.onresult({ results: [[{ transcript: text }]] });
          setTimeout(() => this.onend && this.onend(), 300);
        }, 700);
      }
      stop() {
        if (this.onend) this.onend();
      }
    }
    window.SpeechRecognition = FakeRecognition;
    window.webkitSpeechRecognition = FakeRecognition;
  }, TRANSCRIPT);
  // Стан дозволу — 'granted': у цьому сценарії шторка не має втручатись,
  // старт має йти напряму (її перевірки — окремими check нижче).
  await page.evaluateOnNewDocument(() => {
    try {
      Object.defineProperty(navigator, 'permissions', {
        configurable: true,
        value: { query: async () => ({ state: 'granted', onchange: null }) },
      });
    } catch (err) { /* не вийшло — спрацює реальний стан браузера */ }
  });

  await page.goto(URL, { waitUntil: 'networkidle2', timeout: 45000 });
  await page.waitForSelector('#ai-voice-btn', { timeout: 10000 });

  const idle = await page.evaluate(() => {
    const btn = document.getElementById('ai-voice-btn');
    const span = document.querySelector('#ai-voice-hint span');
    return {
      hint: span ? span.textContent : '(немає хинта)',
      display: btn ? getComputedStyle(btn).display : '(немає кнопки)',
    };
  });
  const hasApi = await page.evaluate(() =>
    !!(window.SpeechRecognition || window.webkitSpeechRecognition));
  check('A: стартовий хинт ' + HINT_IDLE, idle.hint === HINT_IDLE, idle.hint);
  check('A: SpeechRecognition визначений', hasApi);
  check('A: кнопка видима', idle.display !== 'none', idle.display);

  await page.click('#ai-voice-btn');

  const heard = await waitFor(page, () => {
    const btn = document.getElementById('ai-voice-btn');
    const span = document.querySelector('#ai-voice-hint span');
    return btn && span && span.textContent === '(Слухаю...)' &&
      btn.classList.contains('recording') ? true : null;
  }, 4000, 25);
  check('A: стан «' + HINT_HEARING + '» і клас .recording', !!heard);

  // При granted шторка дозволу не сміє втручатись — ні під час, ні після старту.
  const modalHidden = await page.evaluate(() => {
    const m = document.getElementById('voice-perm-modal');
    return !m || m.classList.contains('hidden');
  });
  check('A: шторка дозволу НЕ показується при granted', modalHidden);

  // Читаємо саме фактичне значення (аргумент у page.evaluate не проброшується
  // через waitFor), воно ж іде в note для діагностики.
  const typed = await waitFor(page, () => {
    const input = document.getElementById('ask-text');
    return input && input.value ? input.value : null;
  }, 4000, 50);
  check('A: розпізнаний текст у #ask-text', typed === TRANSCRIPT, String(typed));

  await sleep(2000);   // даємо ask() відправити запит, а onend — відпрацювати
  check('A: #ask-btn запустив POST /api/plan', planRequests > 0, 'запитів: ' + planRequests);

  const settled = await waitFor(page, () => {
    const btn = document.getElementById('ai-voice-btn');
    const span = document.querySelector('#ai-voice-hint span');
    return btn && span && span.textContent === '(AI-ЗАПИТАЙ МЕНЕ)' &&
      !btn.classList.contains('recording') ? true : null;
  }, 5000, 50);
  check('A: після onend хинт і .recording повернулись', !!settled);

  const css = await page.evaluate(() => {
    const btn = document.getElementById('ai-voice-btn');
    btn.classList.add('recording');
    const out = {
      fill: getComputedStyle(btn.querySelector('svg')).fill,
      animation: getComputedStyle(btn).animationName,
    };
    btn.classList.remove('recording');
    return out;
  });
  check('A: CSS .recording svg fill = #ef4444', css.fill === 'rgb(239, 68, 68)', css.fill);
  check('A: CSS пульс emu-voice-pulse', css.animation === 'emu-voice-pulse', css.animation);

  check('A: сторінка без необроблених помилок', errors.length === 0, errors.join(' | '));
  await page.close();
}

// ---------------------------------------------------------------------------
// B. Браузер без Web Speech API: кнопка має бути прихована
// ---------------------------------------------------------------------------
async function scenarioNoApi(browser) {
  console.log('\n--- B. Браузер без SpeechRecognition ---');
  const page = await browser.newPage();
  await page.evaluateOnNewDocument(() => {
    try { window.SpeechRecognition = undefined; } catch (err) { /* нема чого міняти */ }
    try { window.webkitSpeechRecognition = undefined; } catch (err) { /* нема чого міняти */ }
  });
  await page.goto(URL, { waitUntil: 'networkidle2', timeout: 45000 });
  await page.waitForSelector('#ai-voice-btn', { timeout: 10000 });

  const state = await page.evaluate(() => {
    const btn = document.getElementById('ai-voice-btn');
    return {
      hasApi: !!(window.SpeechRecognition || window.webkitSpeechRecognition),
      display: btn ? getComputedStyle(btn).display : '(немає кнопки)',
    };
  });
  check('B: конструктор Web Speech вимкнено у моку', !state.hasApi, 'hasApi=' + state.hasApi);
  check('B: без SpeechRecognition кнопка прихована', state.display === 'none', state.display);
  await page.close();
}

// ---------------------------------------------------------------------------
// C. Реальний Web Speech: стан має прибратись, помилок сторінки бути не має
// ---------------------------------------------------------------------------
async function scenarioReal(browser) {
  console.log('\n--- C. Реальний Web Speech ---');
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', (err) => errors.push(String(err)));
  await page.setViewport({ width: 1400, height: 900 });
  await page.goto(URL, { waitUntil: 'networkidle2', timeout: 45000 });
  await page.waitForSelector('#ai-voice-btn', { timeout: 10000 });

  const hasApi = await page.evaluate(() =>
    !!(window.SpeechRecognition || window.webkitSpeechRecognition));
  check('C: реальний Web Speech у Chromium', hasApi);

  await page.click('#ai-voice-btn');

  // Можлива шторка дозволу (стан 'prompt' у свіжому профілі): натискаємо у ній
  // первинну кнопку — у режимі ask вона стартує розпізнавання, у denied лише
  // закриється (тоді recording не зʼявиться, і C це чесно відмітить).
  const modalUp = await waitFor(page, () => {
    const m = document.getElementById('voice-perm-modal');
    return m && !m.classList.contains('hidden') ? true : null;
  }, 4000, 50);
  if (modalUp) {
    console.log('        (випала шторка дозволу — натиснули первинну кнопку)');
    await page.click('#voice-perm-ok');
  }

  // Реальний сервіс: або onstart (стан мелькає), або швидка помилка
  // (not-allowed/network) — у будь-якому разі onerror/onend має прибрати стан.
  const seenRecording = !!(await waitFor(page, () => {
    const btn = document.getElementById('ai-voice-btn');
    return btn && btn.classList.contains('recording') ? true : null;
  }, 5000, 25));
  if (seenRecording) {
    // Другий клік під час запису = toggle-стоп (швидше, ніж чекати тишу).
    await sleep(500);
    await page.click('#ai-voice-btn');
  }
  const settled = await waitFor(page, () => {
    const btn = document.getElementById('ai-voice-btn');
    const span = document.querySelector('#ai-voice-hint span');
    return btn && span && span.textContent === '(AI-ЗАПИТАЙ МЕНЕ)' &&
      !btn.classList.contains('recording') ? true : null;
  }, 15000, 200);
  check('C: стан не «залип» — хинт базовий, .recording знято', !!settled,
    seenRecording ? 'recording бачили, toggle-стоп відпрацював' : 'recording не зʼявлявся (помилка сервісу)');
  check('C: сторінка без необроблених помилок', errors.length === 0, errors.join(' | '));
  await page.close();
}

// ---------------------------------------------------------------------------
// D. Стан 'prompt': перший клік показує шторку-пояснення, «Дозволити» стартує
//    розпізнавання (прапорець у localStorage), другий клік — уже без шторки
// ---------------------------------------------------------------------------
async function scenarioFirstAsk(browser) {
  console.log('\n--- D. Дозвіл prompt: шторка-пояснення ---');
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', (err) => errors.push(String(err)));
  await page.setViewport({ width: 1400, height: 900 });
  // Мок Web Speech + стан 'prompt' — обидва ДО скриптів сторінки.
  await page.evaluateOnNewDocument((text) => {
    class FakeRecognition {
      start() {
        setTimeout(() => this.onstart && this.onstart(), 100);
        setTimeout(() => {
          if (this.onresult) this.onresult({ results: [[{ transcript: text }]] });
          setTimeout(() => this.onend && this.onend(), 300);
        }, 700);
      }
      stop() {
        if (this.onend) this.onend();
      }
    }
    window.SpeechRecognition = FakeRecognition;
    window.webkitSpeechRecognition = FakeRecognition;
    try {
      Object.defineProperty(navigator, 'permissions', {
        configurable: true,
        value: { query: async () => ({ state: 'prompt', onchange: null }) },
      });
    } catch (err) { /* не вийшло — спрацює реальний стан браузера */ }
  }, TRANSCRIPT);

  await page.goto(URL, { waitUntil: 'networkidle2', timeout: 45000 });
  await page.waitForSelector('#ai-voice-btn', { timeout: 10000 });
  // Прапорець міг лишитись від попередніх сценаріїв (спільний профіль) — чистимо,
  // бо саме він вирішує, показувати шторку чи ні.
  await page.evaluate(() => {
    try { localStorage.removeItem('transgps_voice_asked_mic'); } catch (err) { /* приватний режим */ }
  });

  await page.click('#ai-voice-btn');

  const modal = await waitFor(page, () => {
    const m = document.getElementById('voice-perm-modal');
    if (!m || m.classList.contains('hidden')) return null;
    return {
      title: (document.getElementById('voice-perm-title') || {}).textContent || '',
      ok: (document.getElementById('voice-perm-ok') || {}).textContent || '',
      cancelDisplay: (document.getElementById('voice-perm-cancel') || {}).style.display,
      body: (document.getElementById('voice-perm-body') || {}).innerHTML || '',
    };
  }, 4000, 50);
  check('D: шторка дозволу відкрилась', !!modal, JSON.stringify(modal));
  if (modal) {
    check('D: заголовок «Доступ до мікрофона»', modal.title === '🎤 Доступ до мікрофона', modal.title);
    check('D: кнопка «Дозволити», «Пізніше» видима',
      modal.ok === 'Дозволити' && modal.cancelDisplay !== 'none',
      modal.ok + ' / display=' + (modal.cancelDisplay || '""'));
    check('D: тіло пояснює навіщо мікрофон',
      modal.body.indexOf('потрібен мікрофон') !== -1, modal.body.slice(0, 80));
  }

  // До натискання «Дозволити» розпізнавання не стартує — лише шторка.
  const before = await page.evaluate(() => {
    const btn = document.getElementById('ai-voice-btn');
    const span = document.querySelector('#ai-voice-hint span');
    return { hint: span && span.textContent, rec: !!(btn && btn.classList.contains('recording')) };
  });
  check('D: до «Дозволити» стан спокійний', before.hint === HINT_IDLE && !before.rec,
    before.hint + ' / recording=' + before.rec);

  await page.click('#voice-perm-ok');

  const heard = await waitFor(page, () => {
    const btn = document.getElementById('ai-voice-btn');
    const span = document.querySelector('#ai-voice-hint span');
    return btn && span && span.textContent === '(Слухаю...)' &&
      btn.classList.contains('recording') ? true : null;
  }, 4000, 25);
  check('D: «Дозволити» → «(Слухаю...)» + .recording', !!heard);

  const after = await page.evaluate(() => {
    const m = document.getElementById('voice-perm-modal');
    let flag = null;
    try { flag = localStorage.getItem('transgps_voice_asked_mic'); } catch (err) { /* ignore */ }
    return { closed: !m || m.classList.contains('hidden'), flag: flag };
  });
  check('D: шторка закрилась після «Дозволити»', after.closed);
  check('D: прапорець у localStorage', after.flag === '1', String(after.flag));

  const settled = await waitFor(page, () => {
    const btn = document.getElementById('ai-voice-btn');
    const span = document.querySelector('#ai-voice-hint span');
    return btn && span && span.textContent === '(AI-ЗАПИТАЙ МЕНЕ)' &&
      !btn.classList.contains('recording') ? true : null;
  }, 5000, 50);
  check('D: після onend базовий стан', !!settled);

  // Другий клік: прапорець є — старт БЕЗ шторки (стан ще 'prompt').
  await page.click('#ai-voice-btn');
  const second = await waitFor(page, () => {
    const btn = document.getElementById('ai-voice-btn');
    const span = document.querySelector('#ai-voice-hint span');
    const m = document.getElementById('voice-perm-modal');
    if (m && !m.classList.contains('hidden')) return 'modal';
    if (btn && span && span.textContent === '(Слухаю...)' && btn.classList.contains('recording')) {
      return 'recording';
    }
    return null;
  }, 4000, 25);
  check('D: другий клік — одразу запис, без шторки', second === 'recording', String(second));
  if (second === 'recording') {
    await sleep(300);
    await page.click('#ai-voice-btn');  // toggle-стоп
  }
  check('D: сторінка без необроблених помилок', errors.length === 0, errors.join(' | '));
  await page.close();
}

// ---------------------------------------------------------------------------
// E. Стан 'denied': шторка-інструкція замість тоста; розпізнавання не стартує,
//    повторний клік — знову інструкція (відмова не лікується сама)
// ---------------------------------------------------------------------------
async function scenarioDenied(browser) {
  console.log('\n--- E. Дозвіл denied: шторка-інструкція ---');
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', (err) => errors.push(String(err)));
  await page.setViewport({ width: 1400, height: 900 });
  await page.evaluateOnNewDocument(() => {
    try {
      Object.defineProperty(navigator, 'permissions', {
        configurable: true,
        value: { query: async () => ({ state: 'denied', onchange: null }) },
      });
    } catch (err) { /* не вийшло — спрацює реальний стан браузера */ }
  });

  await page.goto(URL, { waitUntil: 'networkidle2', timeout: 45000 });
  await page.waitForSelector('#ai-voice-btn', { timeout: 10000 });

  await page.click('#ai-voice-btn');
  const modal = await waitFor(page, () => {
    const m = document.getElementById('voice-perm-modal');
    if (!m || m.classList.contains('hidden')) return null;
    return {
      title: (document.getElementById('voice-perm-title') || {}).textContent || '',
      ok: (document.getElementById('voice-perm-ok') || {}).textContent || '',
      cancelDisplay: (document.getElementById('voice-perm-cancel') || {}).style.display,
      body: (document.getElementById('voice-perm-body') || {}).innerHTML || '',
    };
  }, 4000, 50);
  check('E: шторка-інструкція відкрилась', !!modal, JSON.stringify(modal));
  if (modal) {
    check('E: заголовок «Мікрофон заблоковано»',
      modal.title === '🔇 Мікрофон заблоковано', modal.title);
    check('E: кнопка «Зрозуміло», «Пізніше» сховано',
      modal.ok === 'Зрозуміло' && modal.cancelDisplay === 'none',
      modal.ok + ' / display=' + (modal.cancelDisplay || '""'));
    check('E: тіло — покрокова інструкція (ol + kbd)',
      modal.body.indexOf('<ol>') !== -1 && modal.body.indexOf('Налаштування сайту') !== -1,
      modal.body.slice(0, 120));
  }

  // Головне: попри denied розпізнавання не стартувало, хинт не чіпався.
  const idle = await page.evaluate(() => {
    const btn = document.getElementById('ai-voice-btn');
    const span = document.querySelector('#ai-voice-hint span');
    return { hint: span && span.textContent, rec: !!(btn && btn.classList.contains('recording')) };
  });
  check('E: розпізнавання НЕ стартувало', idle.hint === HINT_IDLE && !idle.rec,
    idle.hint + ' / recording=' + idle.rec);

  await page.click('#voice-perm-ok');
  const closed = await page.evaluate(() => {
    const m = document.getElementById('voice-perm-modal');
    return !m || m.classList.contains('hidden');
  });
  check('E: «Зрозуміло» закриває шторку', closed);

  // Відмова не лікується сама — повторний клік знову показує інструкцію.
  await page.click('#ai-voice-btn');
  const again = await waitFor(page, () => {
    const m = document.getElementById('voice-perm-modal');
    return m && !m.classList.contains('hidden') ? true : null;
  }, 4000, 50);
  check('E: повторний клік — знову інструкція', !!again);
  check('E: сторінка без необроблених помилок', errors.length === 0, errors.join(' | '));
  await page.close();
}

// ---------------------------------------------------------------------------

(async () => {
  const browser = await puppeteer.launch({
    headless: 'new',
    args: [
      '--no-sandbox',
      // Фейковий мікрофон + авто-дозвіл: сценарій C не має застряти на
      // prompt доступу, який у headless ніхто не натисне.
      '--use-fake-ui-for-media-stream',
      '--use-fake-device-for-media-stream',
    ],
  });
  try {
    await scenarioMock(browser);
    await scenarioNoApi(browser);
    await scenarioReal(browser);
    await scenarioFirstAsk(browser);
    await scenarioDenied(browser);
  } finally {
    await browser.close();
  }
  if (failed) {
    console.log('\nCHECK_VOICE_FAILED: ' + failed);
    process.exit(1);
  }
  console.log('\nCHECK_VOICE_OK');
})().catch((error) => {
  console.error('ошибка проверки: ' + error.message);
  process.exit(1);
});
