// Аудит маршрутов: поиск иногородних остановок и поездных маршрутов
const fs = require('fs');
const path = require('path');
const dir = path.join(__dirname, 'scraped_data');

// Черновицкие топонимы, которые ЛЕГАЛЬНО содержат "городские" слова (false positives)
const LEGAL = /(^вул\.|^пров\.|^бульв\.|^просп\.|київська|деснянська|хмельницького|залізничний вокзал|садгора|магала|клокучка|гравітон|ленина|гагаріна|небесної сотні|соборна|головна|центр)/i;

// Иногородние / поездные маркеры
const SUSPECT = /(київ-|пасажирський|львів|івано-франківськ|тернопіль|ужгород|одеса|харків|дніпро|вінниця|полтава|море|борисполь|жуляни|вокзал-|залізнична станція|пас\s|придніпровськ|десна-)/i;

const files = fs.readdirSync(dir).filter(f => f.endsWith('.json')).sort();
const problems = [];
const summary = [];

for (const f of files) {
  let data;
  try { data = JSON.parse(fs.readFileSync(path.join(dir, f), 'utf-8')); }
  catch (e) { problems.push(`[JSON ERROR] ${f}: ${e.message}`); continue; }
  if (!Array.isArray(data) || !data.length) { problems.push(`[EMPTY] ${f}`); continue; }

  const first = data[0], last = data[data.length - 1];
  const nameOf = s => (s.n || s.name || '').toString().trim();
  summary.push(`${f} | stops:${data.length} | ${nameOf(first)} -> ${nameOf(last)}`);

  // 1) Явные иногородние остановки
  for (const s of data) {
    const n = nameOf(s);
    if (SUSPECT.test(n) && !LEGAL.test(n)) {
      problems.push(`[ALIEN STOP] ${f} :: order ${s.order} :: "${n}"`);
    }
  }
  // 2) Координаты вне Черновицкой агломерации (примерно: lat 48.05–48.45, lon 25.65–26.10)
  //    Киев ~50.4, Львов ~49.8, Ів-Франківськ ~48.9, Дніпро ~48.46
  for (const s of data) {
    const lat = s.lat, lon = s.lon;
    if (typeof lat === 'number' && typeof lon === 'number') {
      if (lat < 47.9 || lat > 48.6 || lon < 25.4 || lon > 26.4) {
        problems.push(`[FAR COORDS] ${f} :: order ${s.order} :: "${nameOf(s)}" lat=${lat} lon=${lon}`);
      }
    } else if (lat === 0 && lon === 0) {
      problems.push(`[ZERO COORDS] ${f} :: order ${s.order} :: "${nameOf(s)}"`);
    }
  }
}

fs.writeFileSync(path.join(__dirname, 'audit_report.txt'), 
  '=== СВОДКА МАРШРУТОВ ===\n' + summary.join('\n') +
  '\n\n=== ПРОБЛЕМЫ (' + problems.length + ') ===\n' + problems.join('\n'), 'utf-8');
console.log('DONE. Summary routes:', summary.length, '| Problems:', problems.length);
