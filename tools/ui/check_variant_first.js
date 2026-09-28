// Ізольована перевірка логіки renderVariantCards без браузера: підміняємо
// DOM/Layerwalk мінімальними заглушками й дивимось, що реально малюється
// і що стає активною карткою. Запуск: node tools/ui/check_variant_first.js
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SRC = fs.readFileSync(path.join(__dirname, '..', '..', 'web', 'emulator.js'), 'utf8');

// Вирізаємо потрібні функції + state: файл цілий не запустимо (Leaflet, fetch).
function grab(name) {
  const start = SRC.indexOf('function ' + name + '(');
  if (start < 0) throw new Error('не знайдено ' + name);
  let depth = 0;
  for (let i = SRC.indexOf('{', start); i < SRC.length; i += 1) {
    if (SRC[i] === '{') depth += 1;
    else if (SRC[i] === '}') {
      depth -= 1;
      if (depth === 0) return SRC.slice(start, i + 1);
    }
  }
  throw new Error('не знайдено кінець ' + name);
}

let drawn = null;      // що останнім змалював renderPlan
let cleared = 0;       // скільки разів чистили шари
const layers = { lines: 0, vehicles: 0 };
const calls = [];

const sandbox = {
  console,
  clearLayers: () => { cleared += 1; },
  renderPlan: (plan) => { drawn = plan; calls.push('renderPlan'); },
  loadFleet: () => { calls.push('loadFleet'); },
  speakPlanSummary: () => { calls.push('speak'); },
  stopVoice: () => { calls.push('stopVoice'); },
  vehicleLayer: { clearLayers: () => { layers.vehicles += 1; } },
  rootVariantId: null,
  renderVariantCards: null,
  hideVariantCards: () => {},
  variantTransportLabel: (v) => String(v.id),
  sendPlanChoice: (id) => { sandbox.lastChoice = id; },
  state: {
    defaultVariantId: null, activeVariantId: null, variantOffer: null,
    vehicles: new Map(), fleetOn: true, playing: false,
  },
  document: {
    getElementById: (id) => (id === 'plan-variants'
      ? { innerHTML: '', hidden: false, appendChild: () => {}, querySelectorAll: () => [] }
      : null),
    createElement: () => ({
      className: '', setAttribute: () => {}, appendChild: () => {}, onclick: null,
    }),
  },
};
sandbox.window = sandbox;
const ctx = vm.createContext(sandbox);
vm.runInContext(grab('rootVariantId') + '\n' + grab('renderVariantCards') + '\n' +
  grab('selectVariant'), ctx);
sandbox.rootVariantId = sandbox.rootVariantId || ctx.rootVariantId;
sandbox.renderVariantCards = ctx.renderVariantCards;

// Сценарій з брифу: ціновий пріоритет поставив «Дешевий» першим, корінь — «Швидкий».
const plan = {
  total_min: 49, price_grn: 56, transfers: 2,
  variants: [
    { id: 'fewer_transfers', tags: ['Дешевий'], total_min: 55, price_grn: 36, transfers: 0,
      legs: [{ type: 'transit', route: '1' }] },
    { id: 'default', tags: ['Швидкий'], total_min: 49, price_grn: 56, transfers: 2,
      legs: [{ type: 'transit', route: '9' }] },
  ],
};

const ok = [];
// vm-контекст має власний realm: рядки звідти не === з нашими, порівнюємо значення.
const eq = (a, b) => typeof a === 'string' && String(a) === String(b);
const check = (name, cond, detail) => {
  ok.push({ name, cond, detail });
  console.log((cond ? '  ok  ' : ' FAIL ') + name + (detail ? ' — ' + detail : ''));
};

const shown = ctx.renderVariantCards(plan);
check('карточки відрендерено', shown === true, 'результат: ' + shown);
check('активна = перша карточка', eq(sandbox.state.activeVariantId, 'fewer_transfers'),
  'active: ' + sandbox.state.activeVariantId);
check('на карті намальовано перший варіант', !!drawn && eq(drawn.id, 'fewer_transfers'),
  'lastPlan.id: ' + (drawn && drawn.id) + ', ціна: ' + (drawn && drawn.price_grn));
check('лінії очищено перед перемальовуванням', cleared === 1, 'clearLayers(): ' + cleared);
check('маркери живого парку очищено перед перемальовуванням', layers.vehicles === 1, 'vehicleLayer.clearLayers(): ' + layers.vehicles);
check('кеш машин скинуто', sandbox.state.vehicles.size === 0, 'записів у state.vehicles: ' + sandbox.state.vehicles.size);
check('цифри саммарі = показаного варіанта', !!drawn && drawn.price_grn === 36 && drawn.total_min === 55,
  drawn ? drawn.total_min + ' хв / ' + drawn.price_grn + ' грн' : 'null');
check('дефолт у телеметрії = показаний', eq(sandbox.state.defaultVariantId, 'fewer_transfers'), 'default: ' + sandbox.state.defaultVariantId);
check('корінь усе ще валідний окремо', eq(ctx.rootVariantId(plan, plan.variants), 'default'),
  'root: ' + ctx.rootVariantId(plan, plan.variants));

// Без цінового пріоритету: перша карточка == корінь → перемальовування не має бути.
drawn = null; cleared = 0;
const plan2 = Object.assign({}, plan, { total_min: 49, price_grn: 56, transfers: 2 });
plan2.variants = [plan.variants[1], plan.variants[0]];
ctx.renderVariantCards(plan2);
check('корінь перший — зайвого перемальовування немає', cleared === 0 && drawn === null,
  'clearLayers(): ' + cleared);

// --- Клік по іншій картці (БАГ 2): маркери ТС старого варіанта не лишаються.
sandbox.state.variantOffer = plan;
sandbox.state.activeVariantId = 'fewer_transfers';
sandbox.state.vehicles.set('bus|1|OLD', { marker: {}, vehicle: {} });
calls.length = 0; layers.vehicles = 0; drawn = null;
ctx.selectVariant(plan.variants[1]);
check('клік: маркери живого парку очищено', layers.vehicles === 1,
  'vehicleLayer.clearLayers(): ' + layers.vehicles);
check('клік: кеш машин скинуто', sandbox.state.vehicles.size === 0,
  'записів у state.vehicles: ' + sandbox.state.vehicles.size);
check('клік: намальовано обраний варіант', !!drawn && eq(drawn.id, 'default'),
  'lastPlan.id: ' + (drawn && drawn.id));
check('клік: парк оновлено одразу, без очікування тіку', calls.indexOf('loadFleet') > 0,
  'порядок викликів: ' + calls.join(' → '));

// Парк вимкнено — /api/live не смикаємо (юзер сам його вимкнув).
sandbox.state.fleetOn = false;
calls.length = 0;
ctx.selectVariant(plan.variants[0]);
check('клік при вимкненому парку не робить запит', calls.indexOf('loadFleet') < 0,
  'порядок викликів: ' + calls.join(' → '));
sandbox.state.fleetOn = true;

const failed = ok.filter((row) => !row.cond).length;
console.log('\n' + (ok.length - failed) + '/' + ok.length + ' перевірок пройдено');
process.exit(failed ? 1 : 0);
