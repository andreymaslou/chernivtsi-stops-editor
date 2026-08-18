const fs = require('fs');
const data = JSON.parse(fs.readFileSync('stops.txt', 'utf8'));
const rn = data.general.rn;

function mapStops(stopsList, dirNum) {
    return stopsList.map((s, i) => ({
        order: i + 1,
        name: s.n,
        lat: s.x,
        lon: s.y,
        route: rn,
        direction: dirNum
    }));
}

const forward = mapStops(data.stops.forward, 1);
const backward = mapStops(data.stops.backward, 2);

fs.writeFileSync('route_' + rn + '_A.json', JSON.stringify(forward, null, 2));
fs.writeFileSync('route_' + rn + '_B.json', JSON.stringify(backward, null, 2));
console.log('Generated route_' + rn + '_A.json and route_' + rn + '_B.json!');
