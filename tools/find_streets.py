import json
import math
import sys

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi/2.0)**2 + \
        math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda/2.0)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

with open('stops.json', 'r', encoding='utf-8') as f:
    stops_data = json.load(f)

with open('streets.json', 'r', encoding='utf-8') as f:
    streets_data = json.load(f)

streets = streets_data.get('features', [])

on_demand_stops = [s for s in stops_data if s.get('name') and 'вимогу' in s['name'].lower()]

results = []
for stop in on_demand_stops:
    min_dist = float('inf')
    best_street = "Невідомо"
    
    for street in streets:
        geom = street.get('geometry')
        if not geom or geom.get('type') != 'Point':
            continue
            
        lon, lat = geom['coordinates']
        dist = haversine(stop['lat'], stop['lon'], lat, lon)
        
        if dist < min_dist:
            min_dist = dist
            props = street.get('properties', {})
            best_street = props.get('name') or props.get('name:uk') or "Без назви"
            
    results.append({
        'id': stop['id'],
        'name': stop['name'],
        'street': best_street,
        'dist': min_dist
    })

results.sort(key=lambda x: x['dist'])

with open('on_demand_stops_analysis.md', 'w', encoding='utf-8') as out_f:
    out_f.write(f"Знайдено {len(on_demand_stops)} зупинок «на вимогу».\n\n")
    out_f.write("| ID | Поточна назва | Найближча вулиця | Відстань |\n")
    out_f.write("|---|---|---|---|\n")

    for r in results:
        dist_str = f"{r['dist']:.0f} м"
        out_f.write(f"| {r['id']} | {r['name']} | {r['street']} | {dist_str} |\n")
