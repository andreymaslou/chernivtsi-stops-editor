import json
from pathlib import Path
import sys

def main():
    base_dir = Path(__file__).resolve().parent.parent
    graph_path = base_dir / "graph.json"
    manifest_path = base_dir / "data" / "routes_manifest.json"

    if not graph_path.exists() or not manifest_path.exists():
        print("Error: graph.json or routes_manifest.json not found")
        sys.exit(1)

    with open(graph_path, "r", encoding="utf-8") as f:
        graph = json.load(f)

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    # 1. Сбор всех разрешенных ID из манифеста
    allowed_ids = set()
    for route in manifest.get("bus", []):
        allowed_ids.add(route["id"])
    for route in manifest.get("trolley", []):
        allowed_ids.add(route["id"])

    # 2. Проверка каждого маршрута в графе
    errors = []
    routes_in_graph = set()
    directions_in_graph = set()

    for key, route in graph.get("routes", {}).items():
        base_id = key.rsplit(":", 1)[0]
        
        # 1. Отсутствует в манифесте?
        if base_id not in allowed_ids:
            errors.append(f"Route {key} not in manifest")

        # 2. Неизвестный транспорт?
        if route.get("vehicle_type") not in ("bus", "trolley"):
            errors.append(f"Route {key} has unknown vehicle_type: {route.get('vehicle_type')}")
            
        # 3. Исторический 11/3
        if "11" in key and "3" in key:
            errors.append(f"Found historical route in key {key}")

        # 4. Потеряны path или full_geom?
        # В нашем случае это stops и shape.
        if not route.get("stops"):
            errors.append(f"Route {key} is missing stops")
        if not route.get("segments") or not route["segments"][0].get("shape"):
            errors.append(f"Route {key} is missing shapes")

        routes_in_graph.add(base_id)
        directions_in_graph.add(key)

    # Проверка на наличие A/B направлений для каждого маршрута
    for base_id in routes_in_graph:
        if f"{base_id}:A" not in directions_in_graph:
            errors.append(f"Route {base_id} is missing direction A")
        if f"{base_id}:B" not in directions_in_graph:
            errors.append(f"Route {base_id} is missing direction B")

    # Проверка live_route_names
    trolley_3_a = graph["routes"].get("trolley:3:A")
    if trolley_3_a:
        live_names = trolley_3_a.get("live_route_names", [])
        if "3/3a" not in live_names or "3T" not in live_names:
            errors.append(f"Trolley 3 has wrong live_route_names: {live_names}")

    if errors:
        print("Graph validation failed:")
        for err in errors:
            print(f" - {err}")
        sys.exit(1)

    print("Graph validation passed successfully!")
    sys.exit(0)

if __name__ == "__main__":
    main()
