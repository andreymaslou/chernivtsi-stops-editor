// Локальный сервер объединённой страницы (редактор маршрутов + панель эмулятора).
//
// Зачем прокси: страница web/editor.html ходит в /api/* — в проде это делает
// FastAPI (тот же origin), а локально serve.js проксирует запросы на uvicorn
// (по умолчанию http://127.0.0.1:8000). Статику отдаём из web/, но исходники
// EasyWay (scraped_data/) и osm_stops.json лежат в корне репозитория — для них
// отдельные пути (их пишут пайплайны graph_layer / overpass_test.js).
//
// Запуск: node serve.js   → http://localhost:8080/  (API должен быть на :8000)
const http = require('http');
const fs = require('fs');
const path = require('path');

const ROOT = __dirname;
const WEB_ROOT = path.join(ROOT, 'web');
const API_HOST = '127.0.0.1';
const API_PORT = Number(process.env.API_PORT || 8000);
const PORT = Number(process.env.PORT || 8080);

const MIME = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8',
  '.png': 'image/png', '.jpg': 'image/jpeg', '.svg': 'image/svg+xml', '.ico': 'image/x-icon'
};

function sendFile(res, file) {
  fs.readFile(file, (err, data) => {
    if (err) { res.writeHead(404); return res.end('Not found: ' + file); }
    res.writeHead(200, { 'Content-Type': MIME[path.extname(file).toLowerCase()] || 'application/octet-stream' });
    res.end(data);
  });
}

// /api/*, /docs, /openapi.json — на FastAPI: страница и API должны быть на
// одном origin, иначе fetch('/api/...') уйдёт в 404 статики.
function proxyApi(req, res) {
  const upstream = http.request(
    { host: API_HOST, port: API_PORT, path: req.url, method: req.method, headers: req.headers },
    (answer) => { res.writeHead(answer.statusCode, answer.headers); answer.pipe(res); }
  );
  upstream.on('error', (err) => {
    res.writeHead(502, { 'Content-Type': 'application/json; charset=utf-8' });
    res.end(JSON.stringify({ detail: 'uvicorn недоступен (' + API_HOST + ':' + API_PORT + '): ' + err.message }));
  });
  req.pipe(upstream);
}

http.createServer((req, res) => {
  const urlPath = decodeURIComponent(req.url.split('?')[0]);

  if (urlPath.startsWith('/api/') || urlPath === '/docs' || urlPath === '/openapi.json') {
    return proxyApi(req, res);
  }

  if (urlPath.startsWith('/scraped_data/') || urlPath === '/osm_stops.json') {
    const file = path.join(ROOT, urlPath);
    if (!file.startsWith(ROOT)) { res.writeHead(403); return res.end('Forbidden'); }
    return sendFile(res, file);
  }

  // Статика: / → editor.html (объединённая страница), остальное — как есть.
  const relative = urlPath === '/' ? '/editor.html' : urlPath;
  const file = path.join(WEB_ROOT, relative);
  if (!file.startsWith(WEB_ROOT)) { res.writeHead(403); return res.end('Forbidden'); }
  sendFile(res, file);
}).listen(PORT, () => {
  console.log('Editor+emulator: http://localhost:' + PORT + '/ (API -> http://' + API_HOST + ':' + API_PORT + ')');
});

