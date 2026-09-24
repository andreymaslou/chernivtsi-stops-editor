import json
from pathlib import Path
from typing import Dict, List, Any, Optional

class RouteManifest:
    def __init__(self, path: Path):
        self.path = path
        with open(path, "r", encoding="utf-8") as f:
            self.data = json.load(f)
        
        self.bus_routes = {r["id"]: r for r in self.data.get("bus", [])}
        self.trolley_routes = {r["id"]: r for r in self.data.get("trolley", [])}
        
    def get_route(self, route_id: str) -> Optional[Dict[str, Any]]:
        """Возвращает информацию о маршруте по его внутреннему ID (например, 'trolley:3')."""
        if route_id.startswith("bus:"):
            return self.bus_routes.get(route_id)
        elif route_id.startswith("trolley:"):
            return self.trolley_routes.get(route_id)
        return None
        
    def get_display_name(self, route_id: str) -> Optional[str]:
        route = self.get_route(route_id)
        return route["display_name"] if route else None

    def get_live_names(self, route_id: str) -> List[str]:
        route = self.get_route(route_id)
        return route["live_names"] if route else []

    def _normalize_id(self, vehicle_type: str, route_name: str) -> str:
        # Normalize Cyrillic А, К to Latin A, K
        normalized = route_name.replace("А", "A").replace("К", "K")
        return f"{vehicle_type}:{normalized}"

    def is_active(self, vehicle_type: str, route_name: str) -> bool:
        """Проверяет, разрешен ли маршрут в манифесте."""
        route_id = self._normalize_id(vehicle_type, route_name)
        return self.get_route(route_id) is not None

def load_manifest(path: Path) -> RouteManifest:
    return RouteManifest(path)
