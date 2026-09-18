import os
import sys

# Ensure slang_store can be imported
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

import slang_store


# We can reuse the street data parsing from find_streets
import json
import math

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def run():
    with open('stops.json', 'r', encoding='utf-8') as f:
        stops_data = json.load(f)
    
    on_demand_stops = [s for s in stops_data if s.get('name') and 'вимогу' in s['name'].lower()]
    
    with open('streets.json', 'r', encoding='utf-8') as f:
        streets_data = json.load(f)
    streets = streets_data.get('features', [])
    
    updated_count = 0
    
    for stop in on_demand_stops:
        min_dist = float('inf')
        best_street = None
        best_street_ru = None
        
        for street in streets:
            geom = street.get('geometry')
            if not geom or geom.get('type') != 'Point':
                continue
                
            lon, lat = geom['coordinates']
            dist = haversine(stop['lat'], stop['lon'], lat, lon)
            
            if dist < min_dist:
                min_dist = dist
                props = street.get('properties', {})
                name_uk = props.get('name') or props.get('name:uk')
                name_ru = props.get('name:ru')
                
                if name_uk:
                    best_street = name_uk
                    best_street_ru = name_ru
        
        if best_street:
            aliases_to_add = [best_street]
            if best_street_ru:
                aliases_to_add.append(best_street_ru)
                
            slang_store.upsert_stop(
                stop_id=stop['id'], 
                aliases=aliases_to_add, 
                generic=False,
                comment="Автоматично прив'язано до найближчої вулиці"
            )
            updated_count += 1
            print(f"[{stop['id']}] Додано аліаси: {aliases_to_add}")
            
    print(f"\nГотово! Оновлено зупинок: {updated_count}")

if __name__ == '__main__':
    run()
