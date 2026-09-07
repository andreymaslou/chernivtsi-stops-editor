const puppeteer = require('puppeteer-extra');
const StealthPlugin = require('puppeteer-extra-plugin-stealth');
puppeteer.use(StealthPlugin());
const fs = require('fs');
const path = require('path');
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const globalStops = JSON.parse(fs.readFileSync(path.join(__dirname, 'global_stops.json'), 'utf-8'));
  const browser = await puppeteer.launch({ headless: 'new', args: ['--no-sandbox'] });
  const page = await browser.newPage();
  await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36');
  const targets = [{ id: '21', num: '8' }, { id: '26', num: '15' }];
  for (const t of targets) {
    console.log('Route', t.num, 'id', t.id);
    try {
      await page.goto('https://www.eway.in.ua/ua/cities/chernivtsi/routes/' + t.id, { waitUntil: 'networkidle2', timeout: 30000 });
      await page.waitForSelector('.stops-list div[data-stop-id]', { timeout: 8000 });
      const extracted = await page.evaluate(() => {
        const result = { forward: [], backward: [] };
        ['forward', 'backward'].forEach(dir => {
          document.querySelectorAll('.stops-list.' + dir + ' div[data-stop-id]').forEach(el => {
            const id = el.getAttribute('data-stop-id');
            const nameEl = el.querySelector('.stop-name');
            const name = nameEl ? nameEl.textContent : '';
            result[dir].push({ i: parseInt(id), n: name.split('[')[0].trim() });
          });
        });
        return result;
      });
      console.log('forward:', extracted.forward.length, 'backward:', extracted.backward.length);
      ['A', 'B'].forEach((suf, k) => {
        const list = k === 0 ? extracted.forward : extracted.backward;
        if (!list.length) return;
        const mapped = list.map((s, idx) => {
          const g = globalStops[s.i] || [0, 0];
          return { order: idx + 1, name: s.n, lat: g[0] / 1000000, lon: g[1] / 1000000, route: t.num, direction: k + 1 };
        });
        fs.writeFileSync(path.join(__dirname, 'scraped_data', 'route_bus_' + t.num + '_' + suf + '.json'), JSON.stringify(mapped, null, 2), 'utf-8');
        console.log('saved', suf, mapped.length, 'stops |', mapped[0].name, '->', mapped[mapped.length - 1].name);
      });
    } catch (e) { console.log('ERR route', t.num, e.message); }
    await sleep(1500);
  }
  await browser.close();
  console.log('DONE');
})();