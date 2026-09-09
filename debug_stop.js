const puppeteer = require('puppeteer-extra');
const StealthPlugin = require('puppeteer-extra-plugin-stealth');
puppeteer.use(StealthPlugin());
(async () => {
  const browser = await puppeteer.launch({ headless: 'new', args: ['--no-sandbox'] });
  const page = await browser.newPage();
  await page.goto('https://www.eway.in.ua/ua/cities/chernivtsi/routes/21', { waitUntil: 'networkidle2', timeout: 30000 });
  await page.waitForSelector('.stops-list div[data-stop-id]', { timeout: 8000 });
  const html = await page.evaluate(() => {
    const el = document.querySelector('.stops-list.forward div[data-stop-id]');
    return el ? el.outerHTML : 'NOT FOUND';
  });
  console.log(html.slice(0, 800));
  await browser.close();
})();