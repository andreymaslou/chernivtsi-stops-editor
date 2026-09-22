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
 * Три сценарії:
 *   A. Мок Web Speech (підміна конструктора ДО скриптів сторінки): клік →
 *      «(Слухаю...)» + .recording → текст у #ask-text → клік #ask-btn
 *      (помічено POST /api/plan) → onend → базовий стан + CSS .recording.
 *   B. Браузер «без Web Speech» (як Firefox): кнопка мікрофона прихована.
 *   C. Реальний Web Speech Chromium: старт/стоп без «залипання» стану.
 *
 * Реальний сервіс розпізнавання у headless непередбачуваний (мережа, тиша,
 * not-allowed), тому головна логіка перевіряється моком у сценарії A, а C лише
 * переконується, що стан завжди прибирається.
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
