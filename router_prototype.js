// Прототип роутера: поиск маршрута с пересадками по данным scraped_data
// Использование: node router_prototype.js "Комарова" "Світанок"
const fs = require('fs');
const path = require('path');

const DIR = path.join(__dirname, 'scraped_data');
const norm = s => s.toLowerCase().replace(/["«»]/g, '').replace(/\s+/g, ' ').trim();

// 1. Загружаем все маршруты
const routes = []; // {num, type, dir, stops:[{name,lat,lon}]}
for (const f of fs.readdirSync(DIR).filter(f => f.endsWith('.json'))) {
  const m = f.match(/^route_(bus|trolley)_(.+)_([AB])\.json$/);
  if (!m) continue;
  try {
    const data = JSON.parse(fs.readFileSync(path.join(DIR, f), 'utf-8'));
    if (!Array.isArray(data) || !data.length) continue;
    routes.push({
      num: m[2], type: m[1] === 'bus' ? 'авт' : 'трол', dir: m[3],
      stops: data.map(s => ({ name: (s.n || s.name || '').trim(), lat: s.lat, lon: s.lon }))
    });
  } catch (e) {}
}

// 2. Индекс: нормализованное имя остановки -> [{route, pos}]
const stopIndex = new Map();
for (const r of routes) {
  r.stops.forEach((s, pos) => {
    if (!s.name) return;
    const key = norm(s.name);
    if (!stopIndex.has(key)) stopIndex.set(key, []);
    stopIndex.get(key).push({ route: r, pos });
  });
}

// 3. Поиск остановок по подстроке
function findStops(q) {
  const nq = norm(q);
  const exact = [], partial = [];
  for (const [key, refs] of stopIndex) {
    if (key === nq) exact.push({ key, refs });
    else if (key.includes(nq)) partial.push({ key, refs });
  }
  return { exact, partial: partial.slice(0, 10) };
}

// 4. Поездка по одному маршруту: BFS по состояниям (stopKey, route)
function findRoute(fromQ, toQ, maxTransfers = 2) {
  const A = findStops(fromQ), B = findStops(toQ);
  if (!A.exact.length && !A.partial.length) return { error: `Остановку "${fromQ}" не знайдено` };
  if (!B.exact.length && !B.partial.length) return { error: `Остановку "${toQ}" не знайдено` };
  const fromKeys = new Set([...A.exact, ...A.partial].map(x => x.key));
  const toKeys = new Set([...B.exact, ...B.partial].map(x => x.key));

  // прямые рейсы без пересадок
  const direct = [];
  for (const r of routes) {
    for (let i = 0; i < r.stops.length; i++) {
      if (!fromKeys.has(norm(r.stops[i].name))) continue;
      for (let j = i + 1; j < r.stops.length; j++) {
        if (toKeys.has(norm(r.stops[j].name))) {
          direct.push({ route: r, from: i, to: j, stops: j - i });
          break;
        }
      }
    }
  }
  if (direct.length) {
    direct.sort((a, b) => a.stops - b.stops);
    return { type: 'direct', options: direct.slice(0, 3) };
  }

  // с пересадками: BFS по маршрутам через общие остановки
  // state: {route, pos, path:[{route, from, to}]}
  let frontier = [];
  for (const r of routes) {
    for (let i = 0; i < r.stops.length; i++) {
      if (fromKeys.has(norm(r.stops[i].name))) frontier.push({ route: r, pos: i, path: [], boarded: i });
    }
  }
  const visitedRoutes = new Set();
  for (let transfer = 0; transfer <= maxTransfers; transfer++) {
    const nextFrontier = [];
    const results = [];
    for (const st of frontier) {
      const key = st.route.num + st.route.type + st.route.dir;
      if (visitedRoutes.has(key + '|' + st.pos)) continue;
      visitedRoutes.add(key + '|' + st.pos);
      // едем по маршруту, ищем цель и точки пересадки
      for (let j = st.pos + 1; j < st.route.stops.length; j++) {
        const sk = norm(st.route.stops[j].name);
        if (toKeys.has(sk)) {
          results.push({ path: [...st.path, { route: st.route, from: st.boarded, to: j }], stops: j - st.boarded });
        }
        // возможные пересадки на этой остановке
        if (transfer < maxTransfers) {
          for (const ref of (stopIndex.get(sk) || [])) {
            if (ref.route === st.route) continue;
            if (st.path.some(p => p.route === ref.route)) continue;
            nextFrontier.push({ route: ref.route, pos: ref.pos, path: [...st.path, { route: st.route, from: st.boarded, to: j }], boarded: ref.pos });
          }
        }
      }
    }
    if (results.length) {
      results.sort((a, b) => a.path.length - b.path.length || a.stops - b.stops);
      return { type: 'transfer', count: results[0].path.length - 1, options: results.slice(0, 3) };
    }
    frontier = nextFrontier;
  }
  return { error: 'Маршрут з пересадками не знайдено (спробуйте інші зупинки)' };
}

// 5. Демо: несколько запросов, результат пишем в файл (UTF-8, минуя консоль)
const demos = process.argv.slice(2).length >= 2
  ? [[process.argv[2], process.argv[3]]]
  : [
      ['Комарова', 'Світанок'],          // ваш пример: Комарова -> Садгора
      ['Комарова', 'Геріатричний'],      // Комарова -> Садгора (13-й)
      ['Соборна', 'Клокучка'],           // через центр
      ['Держуніверситет', 'Аеропорт'],   // длинное плечо
    ];

let out = '';
for (const [from, to] of demos) {
  const res = findRoute(from, to);
  out += `\n🔍 ${from} -> ${to}\n`;
  if (res.error) { out += '❌ ' + res.error + '\n'; continue; }
  const fmtLeg = leg => {
    const s = leg.route.stops;
    return `  • ${leg.route.type} №${leg.route.num} (${leg.route.dir}): "${s[leg.from].name}" -> "${s[leg.to].name}" [${leg.to - leg.from} зупинок]`;
  };
  if (res.type === 'direct') {
    out += '✅ БЕЗ пересадок:\n' + res.options.map(fmtLeg).join('\n') + '\n';
  } else {
    out += `🔄 Найкращий варіант: ${res.count} пересадка(и)\n`;
    res.options.slice(0, 2).forEach(o => {
      out += 'Варіант:\n' + o.path.map(fmtLeg).join('\n') + '\n';
      const last = o.path[o.path.length - 2];
      out += `  Пересадка на зупинці: "${last.route.stops[last.to].name}"\n`;
    });
  }
}
fs.writeFileSync('router_demo_out.txt', out, 'utf-8');
console.log('written to router_demo_out.txt');

