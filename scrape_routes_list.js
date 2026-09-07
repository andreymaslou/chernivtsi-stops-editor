// Строит официальную карту маршрутов eway: id -> номер -> тип -> описание
// Перехватывает AJAX-ответ /ajax/ua/chernivtsi/routes со страницы списка маршрутов
const puppeteer = require('puppeteer-extra');
const StealthPlugin = require('puppeteer-extra-plugin-stealth');
puppeteer.use(StealthPlugin());
const fs = require('fs');
(async () => {
  const browser = await puppeteer.launch({ headless: 'new', args: ['--no-sandbox'] });
  const page = await browser.newPage();
  await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36');
  await page.setExtraHTTPHeaders({ 'Accept-Language': 'uk' });
  let saved = false;
  page.on('response', async (resp) => {
    try {
      if (saved) return;
      const url = resp.url();
      if (url.includes('/ajax/ua/chernivtsi/routes') && !url.includes('Directions')) {
        const body = await resp.text();
        saved = true;
        const data = JSON.parse(body);
        const routes = data.routes, T = data.transports;
        const typeOf = id => T['Автобус'].includes(+id) ? 'BUS'
          : T['Тролейбус'].includes(+id) ? 'TROL' : 'INTERCITY';
        let out = 'eway_id | type | number | description\n';
        for (const [id, v] of Object.entries(routes)) {
          out += `${id} | ${typeOf(id)} | ${v.rn} | ${v.rd}\n`;
        }
        fs.writeFileSync('route_map.txt', out, 'utf-8');
        console.log('DONE. Routes:', Object.keys(routes).length, '-> route_map.txt');
      }
    } catch (e) {}
  });
  await page.goto('https://www.eway.in.ua/ua/cities/chernivtsi/routes', { waitUntil: 'networkidle2', timeout: 30000 });
  await new Promise(r => setTimeout(r, 5000));
  if (!saved) console.log('WARN: route list AJAX not captured');
  await browser.close();
})();

