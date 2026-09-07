// Исправление: автобус 8 (id 21, Садова-Клокучка) + 7А (id 55 forward, Садова-Світанок)
const puppeteer = require('puppeteer-extra');
const StealthPlugin = require('puppeteer-extra-plugin-stealth');
puppeteer.use(StealthPlugin());
const fs = require('fs');
const path = require('path');
const sleep = ms => new Promise(r => setTimeout(r, ms));

const globalStops = JSON.parse(fs.readFileSync(path.join(__dirname, 'global_stops.json'), 'utf-8'));
// id: eway id, num: номер маршрута в вашей системе
const targets = [
  { id: '21', num: '8' },   // BUS №8: Садова - Клокучка (НЕ троллейбус 75!)
];

(async () => {
  const browser = await puppeteer.launch({ headless: 'new', args: ['--no-sandbox'] });
  const page = await browser.newPage();
  await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36');

  for (const t of targets) {
    console.log('Route', t.num, 'eway id', t.id);
    try {
      await page.goto('https://www.eway.in.ua/ua/cities/chernivtsi/routes/' + t.id, { waitUntil: 'networkidle2', timeout: 30000 });
      await page.waitForSelector('.stops-list div[data-stop-id]', { timeout: 10000 });

      const extracted = await page.evaluate(() => {
        const result = { forward: [], backward: [] };
        ['forward', 'backward'].forEach(dir => {
          document.querySelectorAll('.stops-list.' + dir + ' div[data-stop-id]').forEach(el => {
            const id = el.getAttribute('data-stop-id');
            let name = '';
            const nameEl = el.querySelector('.stop-name');
            if (nameEl && nameEl.textContent.trim()) name = nameEl.textContent;
            else if (nameEl) name = nameEl.getAttribute('title') || '';
            else name = el.textContent || '';
            name = name.replace(/^\[\d+\]\s*/, '').replace(/\s+/g, ' ').trim();
            result[dir].push({ i: parseInt(id), n: name });
          });
        });
        return result;
      });

      console.log('forward:', extracted.forward.length, 'backward:', extracted.backward.length);

      ['A', 'B'].forEach((suf, k) => {
        const list = k === 0 ? extracted.forward : extracted.backward;
        if (!list.length) { console.log('WARN: direction', suf, 'empty'); return; }
        const mapped = list.map((s, idx) => {
          const g = globalStops[s.i] || [0, 0];
          return { order: idx + 1, name: s.n, lat: g[0] / 1000000, lon: g[1] / 1000000, route: t.num, direction: k + 1 };
        });
        fs.writeFileSync(
          path.join(__dirname, 'scraped_data', 'route_bus_' + t.num + '_' + suf + '.json'),
          JSON.stringify(mapped, null, 2), 'utf-8'
        );
        console.log('saved', suf, mapped.length, 'stops |', mapped[0].name, '->', mapped[mapped.length - 1].name);
      });
    } catch (e) { console.log('ERR route', t.num, e.message); }
    await sleep(2000);
  }

  // === Маршрут 7А: из проверенного _verify (id 55 forward: Садова -> Світанок) ===
  try {
    const f7 = JSON.parse(fs.readFileSync(path.join(__dirname, '_verify', 'route_7_forward.json'), 'utf-8'));
    const a7 = f7.map(s => ({ order: s.order, name: s.name, lat: s.lat, lon: s.lon, route: '7', direction: 1 }));
    fs.writeFileSync(path.join(__dirname, 'scraped_data', 'route_bus_7_A.json'), JSON.stringify(a7, null, 2), 'utf-8');
    fs.writeFileSync(path.join(__dirname, 'scraped_data', 'route_7_A.json'), JSON.stringify(a7, null, 2), 'utf-8');
    console.log('saved 7A:', a7.length, 'stops |', a7[0].name, '->', a7[a7.length - 1].name);
  } catch (e) { console.log('ERR 7A:', e.message); }

  await browser.close();
  console.log('DONE');
})();

