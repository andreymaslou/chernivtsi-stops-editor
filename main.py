"""
API-сервер голосового помощника транспортного приложения г. Черновцы.

Стек: FastAPI + Uvicorn + OpenAI SDK (клиент подключён к OpenRouter) +
RapidFuzz (нечёткий поиск) + math (формула гаверсинуса).

Логика работы:
    1. Пользователь присылает текстовую фразу (на укр. языке / суржике).
    2. LLM (через OpenRouter) извлекает из фразы две "сырые" локации:
       "from" (откуда) и "to" (куда) — просто как названия, без ID.
    3. Каждая локация прогоняется через модуль Locator, который:
       - Уровень 1: ищет совпадение среди остановок (name + aliases).
       - Уровень 2: если совпадение среди остановок слабое — ищет совпадение
         среди улиц (streets.json), а затем находит ближайшую к этой улице
         остановку по формуле гаверсинуса.
    4. Результат — ID двух остановок + debug_info с типом найденного совпадения.
"""

import json
import logging
import math
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from openai import OpenAI
from pydantic import BaseModel
from rapidfuzz import fuzz, process

# ---------------------------------------------------------------------------
# Инициализация окружения и логирования
# ---------------------------------------------------------------------------

load_dotenv()  # подтягиваем переменные из .env (в первую очередь OPENROUTER_API_KEY)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("transgps-voice-api")

BASE_DIR = Path(__file__).resolve().parent
STOPS_PATH = BASE_DIR / "stops.json"
STREETS_PATH = BASE_DIR / "streets.json"

# Порог уверенности (в процентах) для срабатывания Уровня 1 (остановки).
# Если совпадение по остановкам ниже этого порога — алгоритм переходит
# на Уровень 2 (поиск по улицам + гаверсинус).
STOP_MATCH_THRESHOLD = 75.0


# ---------------------------------------------------------------------------
# Моковые заглушки на случай, если файлов stops.json / streets.json
# пока нет в директории проекта (например, при первом запуске стенда).
# ---------------------------------------------------------------------------

MOCK_STOPS: List[Dict] = [
    {
        "id": 1,
        "name": "Театральна площа",
        "lat": 48.291455,
        "lon": 25.935182,
        "aliases": ["Театралка", "Театр"],
    },
    {
        "id": 2,
        "name": "Калинівський ринок",
        "lat": 48.246873,
        "lon": 25.963101,
        "aliases": ["Калинка", "Ринок"],
    },
    {
        "id": 3,
        "name": "Держуніверситет",
        "lat": 48.294586,
        "lon": 25.938954,
        "aliases": ["Універ", "ЧНУ", "Держунівер"],
    },
    {
        "id": 4,
        "name": "Соборна площа",
        "lat": 48.291728,
        "lon": 25.934639,
        "aliases": ["Соборка"],
    },
    {
        "id": 5,
        "name": "Завод \"Гравітон\"",
        "lat": 48.279214,
        "lon": 25.916673,
        "aliases": ["Гравітон"],
    },
]

MOCK_STREETS: List[Dict] = [
    {"name": "вулиця Головна", "lat": 48.291200, "lon": 25.935500},
    {"name": "вулиця Кобилянської", "lat": 48.290700, "lon": 25.936200},
    {"name": "вулиця Руська", "lat": 48.291900, "lon": 25.934000},
]


# ---------------------------------------------------------------------------
# Загрузка данных при старте сервера
# ---------------------------------------------------------------------------

def load_stops(path: Path) -> List[Dict]:
    """
    Загружает остановки из stops.json.

    Ожидаемый формат файла: список объектов
        {"id": int, "name": str, "lat": float, "lon": float, "aliases": list[str]}

    Если файла нет — используется моковый набор MOCK_STOPS, чтобы сервер
    можно было поднять и протестировать даже без реальных данных.
    """
    if not path.exists():
        logger.warning(
            "Файл %s не найден — использую моковые данные остановок (%d шт.).",
            path.name,
            len(MOCK_STOPS),
        )
        return MOCK_STOPS

    with path.open(encoding="utf-8") as f:
        data = json.load(f)

    # Подстраховка: если в реальных данных где-то забыли добавить aliases —
    # не даём Locator упасть на KeyError.
    for stop in data:
        stop.setdefault("aliases", [])

    logger.info("Загружено %d остановок из %s.", len(data), path.name)
    return data


def load_streets_geojson(path: Path) -> List[Dict]:
    """
    Парсит streets.json в формате GeoJSON (FeatureCollection).

    Из всех features достаём только те, у которых в properties есть
    непустой ключ "name". Сохраняем в память компактный список:
        {"name": str, "lat": float, "lon": float}

    ВАЖНО: в GeoJSON координаты хранятся в порядке [lon, lat] —
    порядок обратный привычному (lat, lon)! Это учтено ниже.

    Если файла нет — используется моковый набор MOCK_STREETS.
    """
    if not path.exists():
        logger.warning(
            "Файл %s не найден — использую моковые данные улиц (%d шт.).",
            path.name,
            len(MOCK_STREETS),
        )
        return MOCK_STREETS

    with path.open(encoding="utf-8") as f:
        geojson = json.load(f)

    streets: List[Dict] = []
    for feature in geojson.get("features", []):
        properties = feature.get("properties") or {}
        name = properties.get("name")
        if not name:
            # Пропускаем безымянные объекты (по условию задачи нужны только
            # те features, у которых в properties реально есть ключ "name")
            continue

        geometry = feature.get("geometry") or {}
        coordinates = geometry.get("coordinates")
        if not coordinates:
            continue

        # На случай, если в файле встретятся не только Point, но и
        # LineString/Polygon — аккуратно "разворачиваем" вложенные списки
        # координат и берём первую точку геометрии как приближение позиции
        # улицы. Для обычного Point (наш основной случай) это просто
        # coordinates = [lon, lat].
        coords = coordinates
        while isinstance(coords[0], list):
            coords = coords[0]

        lon, lat = coords[0], coords[1]  # GeoJSON хранит [lon, lat]!
        streets.append({"name": name, "lat": lat, "lon": lon})

    logger.info("Загружено %d улиц (с непустым name) из %s.", len(streets), path.name)
    return streets


# ---------------------------------------------------------------------------
# Формула гаверсинуса — расчёт расстояния между двумя точками на сфере
# ---------------------------------------------------------------------------

EARTH_RADIUS_M = 6_371_000  # средний радиус Земли, метры


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Возвращает расстояние (в метрах) между двумя точками на поверхности
    Земли, заданными в градусах широты/долготы, по формуле гаверсинуса.
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    c = 2 * math.asin(math.sqrt(a))
    return EARTH_RADIUS_M * c


# ---------------------------------------------------------------------------
# Locator — многоуровневый алгоритм поиска остановки по названию локации
# ---------------------------------------------------------------------------

class Locator:
    """
    Инкапсулирует двухуровневый алгоритм сопоставления "название локации,
    полученное от LLM" -> "ID реальной остановки".

    Уровень 1 (остановки):
        Ищем нечёткое совпадение запроса с полем name ИЛИ одним из aliases
        каждой остановки (используем rapidfuzz.fuzz.WRatio — он хорошо
        работает с частичными и переставленными по порядку словами,
        что важно для суржика / разговорных формулировок).
        Если score >= STOP_MATCH_THRESHOLD (75) — сразу возвращаем ID
        этой остановки, тип совпадения "stop".

    Уровень 2 (улицы + геометрия):
        Если совпадение по остановкам оказалось слабым (< 75%), ищем
        нечёткое совпадение запроса с названиями улиц из streets.json.
        Если улица найдена — берём её координаты и находим АБСОЛЮТНО
        ближайшую к ней остановку (по формуле гаверсинуса) — это и есть
        наш "разумный" ответ на случай, когда пользователь называет
        улицу/район, а не конкретную остановку. Тип совпадения
        "street_fallback".

    Если ни на одном уровне ничего вменяемого не найдено — возвращаем
    (None, "not_found").
    """

    def __init__(self, stops: List[Dict], streets: List[Dict]):
        self.stops = stops
        self.streets = streets

        # --- Подготовка "плоского" списка кандидатов для rapidfuzz по остановкам ---
        # Каждая остановка даёт несколько вариантов текста для сравнения:
        # основное название + все её алиасы (народные названия).
        # Храним параллельно список текстов (для process.extractOne) и
        # список самих объектов-остановок с тем же индексом.
        self._stop_search_texts: List[str] = []
        self._stop_search_objects: List[Dict] = []
        for stop in stops:
            self._stop_search_texts.append(stop["name"])
            self._stop_search_objects.append(stop)
            for alias in stop.get("aliases", []):
                self._stop_search_texts.append(alias)
                self._stop_search_objects.append(stop)

        # --- Подготовка списка названий улиц для rapidfuzz ---
        self._street_search_texts: List[str] = [s["name"] for s in streets]

    # -- Уровень 1 --------------------------------------------------------

    def _match_stop(self, query: str) -> Optional[Tuple[Dict, float]]:
        """Ищет лучшее нечёткое совпадение query среди name/aliases остановок."""
        if not self._stop_search_texts:
            return None

        result = process.extractOne(
            query, self._stop_search_texts, scorer=fuzz.WRatio
        )
        if result is None:
            return None

        _matched_text, score, index = result
        stop = self._stop_search_objects[index]
        return stop, score

    # -- Уровень 2 --------------------------------------------------------

    def _match_street(self, query: str) -> Optional[Tuple[Dict, float]]:
        """Ищет лучшее нечёткое совпадение query среди названий улиц."""
        if not self._street_search_texts:
            return None

        result = process.extractOne(
            query, self._street_search_texts, scorer=fuzz.WRatio
        )
        if result is None:
            return None

        _matched_text, score, index = result
        street = self.streets[index]
        return street, score

    def _find_nearest_stop(self, lat: float, lon: float) -> Optional[int]:
        """
        Линейный проход по всем остановкам с расчётом расстояния по
        гаверсинусу — находит ID самой близкой к (lat, lon) остановки.

        При объёме данных в масштабе одного города (сотни остановок)
        этого достаточно; строить пространственный индекс (KD-tree)
        избыточно.
        """
        if not self.stops:
            return None

        nearest_id: Optional[int] = None
        nearest_distance = math.inf

        for stop in self.stops:
            distance = haversine_distance(lat, lon, stop["lat"], stop["lon"])
            if distance < nearest_distance:
                nearest_distance = distance
                nearest_id = stop["id"]

        return nearest_id

    # -- Публичный метод: полный многоуровневый поиск --------------------

    def locate(self, query: Optional[str]) -> Tuple[Optional[int], str]:
        """
        Основной метод. Принимает "сырое" название локации (то, что вернула
        LLM) и возвращает (stop_id, match_type).

        match_type может быть:
            "stop"            — найдено уверенное совпадение среди остановок
            "street_fallback" — остановка не найдена уверенно, но найдена
                                улица, и по ней подобрана ближайшая остановка
            "low_confidence"  — совпадение по остановкам есть, но ниже порога,
                                и подходящей улицы не нашлось — возвращаем
                                лучшее, что есть, как запасной вариант
            "not_found"       — вообще ничего подходящего не найдено
        """
        if not query:
            return None, "not_specified"

        # --- Уровень 1: остановки ---
        stop_match = self._match_stop(query)
        if stop_match is not None:
            stop, score = stop_match
            if score >= STOP_MATCH_THRESHOLD:
                return stop["id"], "stop"

        # --- Уровень 2: улицы + гаверсинус ---
        street_match = self._match_street(query)
        if street_match is not None:
            street, street_score = street_match
            if street_score >= STOP_MATCH_THRESHOLD:
                nearest_id = self._find_nearest_stop(street["lat"], street["lon"])
                if nearest_id is not None:
                    return nearest_id, "street_fallback"

        # --- Ничего уверенного не нашли: отдаём лучшее совпадение по
        #     остановкам, если оно вообще было (лучше приблизительный
        #     ответ, чем совсем никакой) ---
        if stop_match is not None:
            return stop_match[0]["id"], "low_confidence"

        return None, "not_found"


# ---------------------------------------------------------------------------
# LLM-интеграция: вызов модели через OpenRouter (OpenAI-совместимый API)
# ---------------------------------------------------------------------------

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "qwen/qwen-2.5-72b-instruct")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

if not OPENROUTER_API_KEY:
    logger.warning(
        "OPENROUTER_API_KEY не задан в .env — сервер запустится, но реальные "
        "вызовы LLM будут завершаться ошибкой 502, пока ключ не будет указан."
    )

# Клиент создаём один раз при старте модуля. Если .env ещё не настроен,
# используем плейсхолдер вместо ключа: свежие версии openai SDK требуют
# непустую строку при инициализации клиента, а реальный запрос всё равно
# провалится на этапе HTTP-вызова (и будет корректно обработан как 502).
llm_client = OpenAI(
    base_url=OPENROUTER_BASE_URL,
    api_key=OPENROUTER_API_KEY or "not-set",
)

# Системный промпт для LLM. Жёстко требуем ТОЛЬКО JSON без каких-либо
# пояснений, чтобы результат можно было безопасно распарсить.
SYSTEM_PROMPT = """\
Ти — модуль розбору тексту для транспортного застосунку міста Чернівці.
Користувач пише запит українською мовою або суржиком (розмовна українсько-\
російська мова), наприклад: "як доїхати з калинки до універу", \
"мені треба з театральної на соборну", "з вулиці головної до ринку".

Твоє єдине завдання: витягнути з тексту дві локації — звідки їде користувач \
("from") і куди він їде ("to"). Локація може бути назвою зупинки, вулиці, \
площі, ринку, закладу, районною народною назвою тощо. Не виправляй, не \
перекладай і не нормалізуй назву — передавай її так, як розпізнав із тексту \
(можеш прибрати прийменники "з", "до", "на", "від", "у" та зайві слова типу \
"доїхати", "проїхати", "маршрут").

Якщо в тексті вказано лише одну локацію (наприклад, тільки "куди"), інше \
поле поверни як порожній рядок "".

СУВОРО дотримуйся формату відповіді: поверни ТІЛЬКИ чистий JSON-об'єкт без \
жодних пояснень, markdown-розмітки чи додаткового тексту, точно такого вигляду:
{"from": "назва локації", "to": "назва локації"}
"""


def call_llm_extract_locations(user_text: str) -> Dict[str, str]:
    """
    Отправляет текст пользователя в LLM (через OpenRouter) и возвращает
    разобранный JSON вида {"from": "...", "to": "..."}.

    В случае любой ошибки (сеть, невалидный JSON от модели, отсутствие
    API-ключа) выбрасывает HTTPException(502), чтобы вызывающий эндпоинт
    мог корректно сообщить об этом клиенту.
    """
    try:
        response = llm_client.chat.completions.create(
            model=OPENROUTER_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
            temperature=0.0,  # детерминированный разбор, без "творчества"
        )
        raw_content = response.choices[0].message.content or ""
    except Exception as exc:  # сетевые ошибки, ошибки авторизации и т.п.
        logger.error("Ошибка вызова LLM (OpenRouter): %s", exc)
        raise HTTPException(status_code=502, detail=f"LLM request failed: {exc}") from exc

    parsed = _parse_llm_json(raw_content)
    if parsed is None:
        logger.error("LLM вернула не-JSON ответ: %r", raw_content)
        raise HTTPException(
            status_code=502,
            detail="LLM returned a response that could not be parsed as JSON",
        )

    return {
        "from": str(parsed.get("from") or "").strip(),
        "to": str(parsed.get("to") or "").strip(),
    }


def _parse_llm_json(raw_content: str) -> Optional[Dict]:
    """
    Максимально терпимо разбирает ответ LLM в JSON.

    Модели иногда оборачивают JSON в ```markdown code fences``` или
    добавляют лишние пробелы/переводы строк вокруг — эта функция
    вычищает такие обёртки перед json.loads.
    """
    text = raw_content.strip()

    # Убираем markdown code fence, если модель всё же его добавила.
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1)
    else:
        # Иначе просто вырезаем первый попавшийся {...} блок.
        brace_match = re.search(r"\{.*\}", text, re.DOTALL)
        if brace_match:
            text = brace_match.group(0)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# FastAPI приложение
# ---------------------------------------------------------------------------

# Глобальное хранилище "загруженных при старте" данных. Инициализируется
# в lifespan-обработчике ниже, чтобы файлы читались один раз при поднятии
# сервера, а не на каждый запрос.
app_state: Dict[str, object] = {"locator": None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Выполняется один раз при старте сервера: читает stops.json и
    streets.json (или подставляет моки, если файлов нет) и создаёт
    единственный экземпляр Locator на всё время жизни приложения.
    """
    logger.info("Инициализация сервера: загрузка stops.json и streets.json...")
    stops = load_stops(STOPS_PATH)
    streets = load_streets_geojson(STREETS_PATH)
    app_state["locator"] = Locator(stops=stops, streets=streets)
    logger.info(
        "Locator готов: %d остановок, %d улиц.", len(stops), len(streets)
    )
    yield
    logger.info("Остановка сервера.")


app = FastAPI(
    title="TransGPS Chernivtsi — Voice Assistant API",
    description="API для голосового помощника транспортного приложения г. Черновцы",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Pydantic-схемы запроса/ответа
# ---------------------------------------------------------------------------

class RouteRequest(BaseModel):
    text: str


class DebugInfo(BaseModel):
    from_type: str
    to_type: str
    from_query: str
    to_query: str


class RouteResponse(BaseModel):
    from_stop_id: Optional[int]
    to_stop_id: Optional[int]
    debug_info: DebugInfo


# ---------------------------------------------------------------------------
# Эндпоинты
# ---------------------------------------------------------------------------

@app.get("/health")
def health_check():
    """Простой health-check: подтверждает, что данные успешно загружены."""
    locator: Locator = app_state["locator"]
    return {
        "status": "ok",
        "stops_loaded": len(locator.stops) if locator else 0,
        "streets_loaded": len(locator.streets) if locator else 0,
    }


@app.post("/api/route", response_model=RouteResponse)
def get_route(request: RouteRequest):
    """
    Основной эндпоинт голосового помощника.

    Принимает текст пользователя на украинском языке/суржике, например:
        {"text": "Як доїхати з калинки до універу?"}

    Пайплайн обработки:
        1. Текст отправляется в LLM (OpenRouter) — она извлекает
           "сырые" названия точек "from" и "to".
        2. Каждое название прогоняется через Locator.locate(), который
           последовательно пытается найти совпадение среди остановок
           (Уровень 1), а при неудаче — среди улиц с последующим поиском
           ближайшей остановки по гаверсинусу (Уровень 2).
        3. Возвращается пара ID остановок + подробная debug_info,
           объясняющая, каким способом был найден каждый ID.
    """
    locator: Optional[Locator] = app_state.get("locator")
    if locator is None:
        # Теоретически невозможно при штатном старте через lifespan,
        # но проверка защищает от гонок при hot-reload/тестах.
        raise HTTPException(status_code=503, detail="Locator is not initialized yet")

    if not request.text or not request.text.strip():
        raise HTTPException(status_code=400, detail="Field 'text' must not be empty")

    # Шаг 1: LLM извлекает названия точек "откуда" и "куда" из свободного текста.
    locations = call_llm_extract_locations(request.text)
    from_query = locations["from"]
    to_query = locations["to"]

    # Шаг 2: многоуровневый геопоиск для каждой точки.
    from_stop_id, from_type = locator.locate(from_query)
    to_stop_id, to_type = locator.locate(to_query)

    logger.info(
        "Запрос: %r -> from=%r(%s, id=%s), to=%r(%s, id=%s)",
        request.text, from_query, from_type, from_stop_id,
        to_query, to_type, to_stop_id,
    )

    return RouteResponse(
        from_stop_id=from_stop_id,
        to_stop_id=to_stop_id,
        debug_info=DebugInfo(
            from_type=from_type,
            to_type=to_type,
            from_query=from_query,
            to_query=to_query,
        ),
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
