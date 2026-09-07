const https = require('https');
const fs = require('fs');
const path = require('path');

const query = `[out:json][timeout:60];
area[name="Чернівці"]->.a;
(
  node[highway=bus_stop](area.a);
  node[public_transport=platform](area.a);
  node[public_transport=stop_position](area.a);
);
out body;`;

const data = 'data=' + encodeURIComponent(query);

const options = {
    hostname: 'overpass-api.de',
    path: '/api/interpreter',
    method: 'POST',
    headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Content-Length': Buffer.byteLength(data),
        'User-Agent': 'chernivtsi-stops-editor/1.0 (transport data research)',
    },
};

console.log('Запитую зупинки з OpenStreetMap для Чернівців...');

const req = https.request(options, (res) => {
    let body = '';
    res.on('data', (chunk) => (body += chunk));
    res.on('end', () => {
        try {
            const json = JSON.parse(body);
            if (!json.elements) {
                console.error('Неочікувана відповідь:', body.substring(0, 500));
                return;
            }
            const stops = json.elements
                .filter((el) => el.type === 'node' && el.lat && el.lon)
                .map((el) => {
                    const tags = el.tags || {};
                    const name = tags.name || tags['name:uk'] || '';
                    return {
                        osm_id: el.id,
                        name: name || '(без назви)',
                        lat: el.lat,
                        lon: el.lon,
                    };
                })
                .filter((s) => s.name && s.name !== '(без назви)');
            const outFile = path.join(__dirname, 'osm_stops.json');
            fs.writeFileSync(outFile, JSON.stringify(stops, null, 2), 'utf-8');
            console.log(`✅ Збережено ${stops.length} зупинок у ${outFile}`);
            console.log('Приклад перших 3:', JSON.stringify(stops.slice(0, 3), null, 2));
        } catch (e) {
            console.error('Помилка парсингу:', e.message);
            console.error('Сирі дані (перші 1000):', body.substring(0, 1000));
        }
    });
});
req.on('error', (e) => console.error('Помилка запиту:', e.message));
req.write(data);
req.end();