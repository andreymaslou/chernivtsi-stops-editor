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
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from time_utils import as_kyiv, format_kyiv, now_kyiv

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from pydantic import BaseModel
from rapidfuzz import fuzz, process

from feedback_store import feedback_stats, list_feedback, save_feedback
from live_layer import LiveTracker
from router_layer import TransitRouter
from sim_layer import SimLayer
from slang_store import (
    apply_overrides,
    delete_stop,
    load_overrides,
    overrides_stats,
    upsert_stop,
)
from storage import append_jsonl

# ---------------------------------------------------------------------------
# Инициализация окружения и логирования
# ---------------------------------------------------------------------------

load_dotenv()  # подтягиваем переменные из .env (в первую очередь OPENROUTER_API_KEY)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("transgps-voice-api")

BASE_DIR = Path(__file__).resolve().parent
STOPS_PATH = BASE_DIR / "stops.json"
STREETS_PATH = BASE_DIR / "streets.json"
GRAPH_PATH = BASE_DIR / "graph.json"
SCHEDULE_PATH = BASE_DIR / "routes_schedule.json"

# Журнал выбора варианта плана (поставка 1, §13.3 брифа): одна JSON-строка на
# запрос. Анализ предпочтений идёт по этому потоку, поэтому кладём его рядом с
# остальными данными, в отдельной папке logs (в git не попадает).
TELEMETRY_LOG_PATH = BASE_DIR / "logs" / "telemetry_plan_choices.jsonl"

# FastAPI выполняет синхронные эндпоинты в пуле потоков, поэтому запросы могут
# приходить параллельно. Один lock на процесс гарантирует, что две строки не
# «вклинятся» друг в друга при конкурентной записи (O_APPEND этого не обещает
# на Windows). Нагрузка стенда мизерная, блокировка не узкое место.
_TELEMETRY_LOCK = threading.Lock()

# ``/api/plan`` — обычный sync-endpoint, поэтому FastAPI выполняет запросы в
# threadpool. Роутер хранит последний fleet и его кэши, поэтому нельзя
# разрешать двум запросам одновременно менять этот объект. Lock намеренно
# короткий: он защищает только CPU-расчёт, не сетевой LLM и не live-опрос.
_ROUTER_LOCK = threading.Lock()


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
OPENROUTER_MODELS_RAW = os.getenv("OPENROUTER_MODEL", "qwen/qwen-2.5-72b-instruct:free,google/gemma-2-27b-it:free,openai/gpt-4o-mini")
OPENROUTER_MODELS = [m.strip() for m in OPENROUTER_MODELS_RAW.split(",") if m.strip()]
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Тестовый режим (для локальной обкатки; в проде переменные не задаём):
#   ASSUME_IN_SERVICE=1 — считаем все маршруты в работе независимо от времени
#                         суток (ночные запросы ведут себя как дневные);
#   GPS_SIMULATOR=1     — УСТАРЕЛ, аналог PARK_SOURCE=sim (см. ниже).
#                         Оставлен для обратной совместимости со старым .env.
def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


ASSUME_IN_SERVICE = _env_flag("ASSUME_IN_SERVICE")
GPS_SIMULATOR = _env_flag("GPS_SIMULATOR")

# Источник парка машин (бриф §3, «Идея A» — приоритет реального GPS, дыры
# заполняются симулятором, чтобы демо не было пустым):
#   auto — опросить оба источника и слить (merge_fleet): маршрут
#          (тип ТС + подпись), на котором есть хоть одна свежая реальная
#          машина вне депо, целиком берём из трекера, остальные — из
#          симулятора. Каждой машине добавляется поле source: "real"|"sim";
#   gps  — только реальный трекер;
#   sim  — только виртуальный парк (детерминированные тесты и ночной стенд).
PARK_SOURCE = os.getenv("PARK_SOURCE", "auto").strip().lower()
if GPS_SIMULATOR and PARK_SOURCE == "auto":
    # Обратная совместимость: старый ключ означал «весь парк виртуальный».
    # Явно заданный PARK_SOURCE приоритетнее — новый ключ точнее старого.
    PARK_SOURCE = "sim"
    logger.warning(
        "GPS_SIMULATOR=1 устарел и работает как PARK_SOURCE=sim "
        "(приоритет источников парка выключен). Перейдите на PARK_SOURCE."
    )
if PARK_SOURCE not in ("auto", "gps", "sim"):
    logger.warning("PARK_SOURCE=%r не поддерживается, использую 'auto'.", PARK_SOURCE)
    PARK_SOURCE = "auto"

if not OPENROUTER_API_KEY:
    logger.warning(
        "OPENROUTER_API_KEY не задан в .env — сервер запустится, но реальные "
        "вызовы LLM будут завершаться ошибкой 502, пока ключ не будет указан."
    )

# Клиент создаём один раз при старте модуля. Если .env ещё не настроен,
# используем плейсхолдер вместо ключа: свежие версии openai SDK требуют
# непустую строку при инициализации клиента, а реальный запрос всё равно
# провалится на этапе HTTP-вызова (и будет корректно обработан как 502).
# Таймаут и max_retries ставим явно: дефолт openai SDK — 10 минут и 2 повтора,
# а для живого голосового ассистента запрос дольше ~15 секунд бесполезен.
llm_client = OpenAI(
    base_url=OPENROUTER_BASE_URL,
    api_key=OPENROUTER_API_KEY or "not-set",
    timeout=30.0,
    max_retries=1,
)

# Системный промпт для LLM. Жёстко требуем ТОЛЬКО JSON без каких-либо
# пояснений, чтобы результат можно было безопасно распарсить.
SYSTEM_PROMPT = """\
Ти — розумний помічник для транспортного застосунку міста Чернівці.
Користувач пише запит українською мовою або суржиком (наприклад: "як доїхати з калинки до універу", "яка сьогодні погода", "рецепт борщу").

Твоє завдання — класифікувати запит і повернути СУВОРО чистий JSON.

Якщо користувач запитує про маршрут або проїзд:
1. Витягни дві локації: звідки ("from") і куди ("to").
2. Локація може бути зупинкою, вулицею, площею, закладом тощо. Не виправляй і не перекладай її.
3. Поверни JSON формату:
{"type": "route", "from": "назва", "to": "назва"}
Якщо вказано лише одну локацію, іншу залиш порожньою ("").

Якщо запит НЕ стосується пошуку маршрутів, зупинок або громадського транспорту Чернівців (наприклад, погода, рецепти, загальні питання):
1. Поверни JSON формату:
{"type": "off_topic"}

Поверни ТІЛЬКИ JSON, без markdown-розмітки чи додаткових слів.
"""


# Небольшой кэш разбора фраз в памяти процесса: одна и та же фраза не должна
# бить по OpenRouter (и по балансу $0.20) на каждый чих — Locator/роутер
# каждый раз считаются заново, но LLM отвечает одинаково (temperature=0).
_llm_cache: Dict[str, Dict[str, str]] = {}
LLM_CACHE_MAX = 512


def _normalize_cache_key(user_text: str) -> str:
    return " ".join(user_text.lower().split())


def call_llm_extract_locations(user_text: str) -> Dict[str, str]:
    """
    Отправляет текст пользователя в LLM (через OpenRouter) и возвращает
    разобранный JSON вида {"from": "...", "to": "..."}.

    Ответы кэшируются по нормализованной фразе (LRU-подобный сброс, простой
    cutoff при переполнении). В случае любой ошибки (сеть, невалидный JSON от
    модели, отсутствие API-ключа) выбрасывает HTTPException(502), чтобы
    вызывающий эндпоинт мог корректно сообщить об этом клиенту.
    """
    cache_key = _normalize_cache_key(user_text)
    cached = _llm_cache.get(cache_key)
    if cached is not None:
        return dict(cached)

    last_exc = None
    raw_content = ""
    for model_name in OPENROUTER_MODELS:
        try:
            response = llm_client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_text},
                ],
                temperature=0.0,  # детерминированный разбор, без "творчества"
                max_tokens=250,   # хватит на {"type": "off_topic", "message": "..."}
            )
            raw_content = response.choices[0].message.content or ""
            break  # Успешный вызов, прерываем цикл фоллбэка
        except Exception as exc:  # сетевые ошибки, ошибки авторизации, платные модели и т.п.
            logger.warning("Ошибка вызова LLM (OpenRouter) для модели %s: %s", model_name, exc)
            last_exc = exc

    if not raw_content and last_exc:
        logger.error("Все модели из списка фоллбэка упали. Последняя ошибка: %s", last_exc)
        return {"type": "error"}

    parsed = _parse_llm_json(raw_content)
    if parsed is None:
        logger.error("LLM вернула не-JSON ответ: %r", raw_content)
        return {"type": "error"}

    result = {
        "type": str(parsed.get("type", "route")).strip(),
        "from": str(parsed.get("from") or "").strip(),
        "to": str(parsed.get("to") or "").strip(),
    }
    # Простейший LRU-подобный сброс при переполнении.
    if cache_key not in _llm_cache and len(_llm_cache) >= LLM_CACHE_MAX:
        _llm_cache.pop(next(iter(_llm_cache)))
    _llm_cache[cache_key] = result
    return dict(result)


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
app_state: Dict[str, object] = {"locator": None, "live": None, "router": None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Выполняется один раз при старте сервера: читает stops.json и
    streets.json (или подставляет моки, если файлов нет) и создаёт
    единственный экземпляр Locator на всё время жизни приложения.
    """
    logger.info("Инициализация сервера: загрузка stops.json и streets.json...")
    streets = load_streets_geojson(STREETS_PATH)
    # Сленговые правки (data/slang_overrides.json) применяем сразу: псевдонимы
    # вида «Соборка»/«Тралка» и переименование безымянных остановок должны
    # работать и в /api/route, и в эмуляторе, и после перезапуска контейнера.
    stops = apply_overrides(load_stops(STOPS_PATH))
    app_state["streets"] = streets
    app_state["locator"] = Locator(stops=stops, streets=streets)
    logger.info(
        "Locator готов: %d остановок (после правок сленга), %d улиц.", len(stops), len(streets)
    )

    # Граф маршрутов и расписание — для роутера (/api/plan).
    # graph.json собирается скриптом graph_layer (коммитится в репозиторий),
    # поэтому тут только читаем его. Падение файлов не роняет сервер:
    # /api/plan вернёт 503, а остальные эндпоинты продолжат работать.
    router: Optional[TransitRouter] = None
    try:
        graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Роутер не готов: graph.json не прочитан (%s).", exc)
        graph = None
    if graph is not None:
        try:
            schedule = json.loads(SCHEDULE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Расписание не прочитано (%s) — роутер без расписания.", exc)
            schedule = {}
        router = TransitRouter(
            graph, schedule, 
            stops=app_state["locator"].stops,
            assume_in_service=ASSUME_IN_SERVICE
        )
        logger.info(
            "Роутер готов: %d направлений, %d узлов графа.",
            len(router.routes), len(router.nodes),
        )
    app_state["router"] = router

    # Источник данных о машинах (бриф §3): симулятор и/или реальный трекер.
    # В режиме auto опрашиваются оба, а сливаются они в merge_fleet() при
    # каждом запросе — тогда на маршрутах со свежим GPS видны реальные
    # машины, а пустые направления заполняются виртуальными.
    sim_layer: Optional[SimLayer] = None
    if PARK_SOURCE in ("auto", "sim") and graph is not None:
        sim_layer = SimLayer(graph, schedule, assume_in_service=ASSUME_IN_SERVICE)
        app_state["sim_layer"] = sim_layer
        logger.info("Подключен виртуальный парк (PARK_SOURCE=%s).", PARK_SOURCE)

    tracker: Optional[LiveTracker] = None
    if PARK_SOURCE in ("auto", "gps"):
        tracker = LiveTracker()
        # LiveTracker.start() поднимает фоновый поллер трекера: при
        # PARK_SOURCE=sim он не запускается вовсе, чтобы тесты не лезли в сеть.
        await tracker.start()
        app_state["tracker"] = tracker
        logger.info("Подключен реальный GPS-трекер (PARK_SOURCE=%s).", PARK_SOURCE)

    # app_state["live"] — основной источник для health-check и совместимости:
    # в режиме auto это трекер (он приоритетный, сборка среза идёт в merge).
    app_state["live"] = tracker if tracker is not None else sim_layer

    yield

    live: Optional[Any] = app_state.get("live")
    if live is not None and hasattr(live, "stop"):
        await live.stop()
    logger.info("Остановка сервера.")


app = FastAPI(
    title="TransGPS Chernivtsi — Voice Assistant API",
    description="API для голосового помощника транспортного приложения г. Черновцы",
    version="1.0.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def ui_static_no_cache(request: Request, call_next):
    """Статика /ui/* — заставляем браузер ревалидировать, а не отдавать кэш.

    Зачем: после деплоя адаптива телефон продолжал рисовать СТАРЫЙ CSS (панель
    45vh, схлопнутая карта), хотя сервер уже отдавал новый файл — проверяли
    curl-ом. Причина: Chrome закэшировал style.css без версии в URL.
    `no-cache` не запрещает кэш, а требует ревалидацию: файл берётся из кэша
    только при совпадении ETag/Last-Modified, поэтому после сборки сразу
    прилетает свежий. В HTML к этому добавлены ?v=... у css/js (см.
    web/editor.html — их нужно поднимать при правках этих файлов).
    """
    response = await call_next(request)
    if request.url.path.startswith("/ui/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


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
    mode: Optional[str] = None
    message: Optional[str] = None
    note: Optional[str] = None
    from_stop_id: Optional[int] = None
    to_stop_id: Optional[int] = None
    debug_info: Optional[DebugInfo] = None


# ---------------------------------------------------------------------------
# Эндпоинты
# ---------------------------------------------------------------------------

@app.get("/health")
def health_check():
    """Простой health-check: данные локатора + состояние живого GPS-слоя."""
    locator: Locator = app_state["locator"]
    live: Optional[LiveTracker] = app_state.get("live")
    return {
        "status": "ok",
        "stops_loaded": len(locator.stops) if locator else 0,
        "streets_loaded": len(locator.streets) if locator else 0,
        "live_routes_loaded": live.route_count if live else 0,
        "live_polls_done": live.poll_count if live else 0,
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
    
    if locations.get("type") == "error":
        return RouteResponse(
            mode="clarify",
            note="Не вдалося зрозуміти запит. Спробуйте назвати звідки і куди потрібно доїхати."
        )

    if locations.get("type") == "off_topic":
        return RouteResponse(
            mode="off_topic",
            message="Я можу допомогти знайти маршрут, пересадки, час у дорозі та вартість поїздки. Звідки і куди потрібно доїхати?"
        )
        
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


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Сборка парка машин: приоритет реального GPS над симулятором (бриф §3)
# ---------------------------------------------------------------------------

def _route_key(vehicle: Dict[str, Any]) -> Tuple[str, str]:
    """
    Ключ приоритета источника: (тип ТС, нормализованная подпись маршрута).

    Тип ТС обязателен: «5» есть и у автобусов, и у троллейбусов, это два
    разных маршрута — по голой подписи они склеились бы в один. Нормализация
    та же, что в роутере, иначе ключи слияния и поиска бортов разъедутся.
    """
    vtype = str(vehicle.get("vehicle_type") or "").strip().lower()
    label = str(vehicle.get("route_label") or vehicle.get("route_name") or "")
    return vtype, TransitRouter.normalize_label(label)


def merge_fleet(
    real_fleet: List[Dict[str, Any]],
    sim_fleet: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Сливает реальный и виртуальный парки по правилу приоритета (§3).

    Если на маршруте (тип ТС + подпись) есть хоть одна СВЕЖАЯ реальная
    машина вне депо — берём для этого маршрута только реальные машины;
    если свежих нет — берём виртуальные. Правило маршрутом, а не машиной:
    смешивать оба источника на одном маршруте нельзя — у них разные поля
    (направление есть только у симулятора) и разные эпохи среза, поэтому
    строгость фильтров и ETA отличались бы (§3.2).

    Свежесть и депо проверяются до слияния (это делает live_layer при
    only_fresh=True), поэтому stale-машина не может «прикрыть» направление
    и оставить нас без парка (§3.3 п.2): она не формирует приоритет.

    Каждой машине добавляется поле `source: "real" | "sim"` — роутер
    прокидывает его в ногу плана, а UI рисует бейдж «SIM». Без этой
    пометки приоритет превращается в подмену (§3.3 п.3-4).
    """
    real_keys: set = set()
    real_vehicles: List[Dict[str, Any]] = []
    for vehicle in real_fleet:
        real_vehicles.append(dict(vehicle, source="real"))
        # Право забрать маршрут у симулятора — только у свежей машины не из
        # депо: старый трек и машины на смене — это дыра, её заполняем симом.
        if vehicle.get("is_live") and not vehicle.get("in_depo"):
            key = _route_key(vehicle)
            if key[1]:
                real_keys.add(key)

    sim_vehicles: List[Dict[str, Any]] = []
    for vehicle in sim_fleet:
        if _route_key(vehicle) in real_keys:
            continue  # маршрут уже занят свежим реальным парком
        sim_vehicles.append(dict(vehicle, source="sim"))

    merged = real_vehicles + sim_vehicles
    # Единый порядок для UI: трекер и симулятор сортируют срезы по-разному
    # (route_id vs route_label), а в смешанном ответе должен быть стабильный
    # порядок, иначе маркеры «прыгают» между опросами.
    merged.sort(
        key=lambda v: (
            str(v.get("vehicle_type") or ""),
            str(v.get("route_label") or v.get("route_name") or ""),
            str(v.get("board_number") or ""),
        )
    )
    return merged


def _collect_fleet(
    plan_now: Optional[datetime] = None,
) -> Tuple[List[Dict[str, Any]], Optional[datetime], str]:
    """
    Срез парка для /api/plan и /api/live с учётом PARK_SOURCE.

    Возвращает (vehicles, snapshot_at, fleet_source):
      * snapshot_at — момент, на который построен парк. Роутер сверяет с
        ним ETA: симулятор умеет строить парк на любое время, реальный
        трекер — только «сейчас», поэтому при наличии обоих источников
        (PARK_SOURCE=auto) берём plan_now либо фактическое «сейчас»;
      * fleet_source — источник среза для ног плана и телеметрии: "real"
        или "sim" в мономоде; "auto" в смешанном режиме — тогда роутер
        берёт source каждой отдельной машины (см. _transit_leg).
    """
    sim_layer: Optional[SimLayer] = app_state.get("sim_layer")
    tracker: Optional[LiveTracker] = app_state.get("tracker")

    if PARK_SOURCE == "sim" or (PARK_SOURCE == "auto" and tracker is None):
        # Только симулятор (тестовый стенд) либо трекер недоступен —
        # вырожденный auto, который не во что сливать.
        if sim_layer is None:
            return [], None, "sim"
        snapshot = sim_layer.snapshot(now=plan_now)
        # Симулятор строит парк на plan_now; если его нет — на фактическое
        # «сейчас», как и раньше (роутер сверяет ETA с моментом среза).
        snapshot_at = plan_now if plan_now is not None else now_kyiv()
        return snapshot.get("vehicles", []), snapshot_at, "sim"

    if PARK_SOURCE == "gps":
        # Только реальный трекер: он умеет отдавать только «сейчас».
        if tracker is None:
            return [], None, "real"
        snapshot = tracker.snapshot(only_fresh=True)
        return snapshot.get("vehicles", []), now_kyiv(), "real"

    # PARK_SOURCE == "auto": опрашиваем оба источника и сливаем.
    sim_vehicles: List[Dict[str, Any]] = []
    if sim_layer is not None:
        try:
            sim_vehicles = sim_layer.snapshot(now=plan_now).get("vehicles", [])
        except Exception as exc:  # симулятор не должен ронять план
            logger.warning("Срез симулятора не получен (%s).", exc)

    real_vehicles: List[Dict[str, Any]] = []
    if tracker is not None:
        try:
            real_vehicles = tracker.snapshot(only_fresh=True).get("vehicles", [])
        except Exception as exc:  # сеть перевозчика иногда лежит — выкатимся на симе
            logger.warning("Срез реального трекера не получен (%s).", exc)

    merged = merge_fleet(real_vehicles, sim_vehicles)
    logger.info(
        "Парк собран: %d реальных + %d виртуальных машин.",
        sum(1 for v in merged if v.get("source") == "real"),
        sum(1 for v in merged if v.get("source") == "sim"),
    )
    snapshot_at = plan_now if plan_now is not None else now_kyiv()
    return merged, snapshot_at, "auto"



# Маршрутизация: полный план поездки (роутер на графе остановок)
# ---------------------------------------------------------------------------


def _calculate_plan_locked(
    router: TransitRouter,
    from_stop_id: int,
    to_stop_id: int,
    fleet: List[Dict[str, Any]],
    snapshot_at: Optional[datetime],
    fleet_source: str,
    plan_now: datetime,
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], Optional[str]]:
    """Рассчитать план и варианты под одним short-lived router lock.

    ``set_live`` и ``build_variants`` меняют/читают состояние общего
    TransitRouter. Нельзя разнести их по разным секциям: параллельный запрос
    успеет подменить fleet между вызовом. Сетевой LLM уже завершён до этого
    блока, поэтому lock не держит соединение и не блокирует event loop.
    """
    with _ROUTER_LOCK:
        try:
            router.set_live(fleet, snapshot_at=snapshot_at, source=fleet_source)
        except Exception:
            # План строится и без парка (ожидание по расписанию), поэтому
            # survivability важнее: теряем live-ETA, но не ответ.
            router.set_live([], snapshot_at=snapshot_at, source=fleet_source)

        plan = router.plan(from_stop_id, to_stop_id, now=plan_now)
        if plan is None:
            return None, [], None

        variants, variants_note = router.build_variants(
            from_stop_id, to_stop_id, now=plan_now, default_plan=plan
        )
        return plan, variants, variants_note

class PlanRequest(BaseModel):
    text: str
    # Необязательное «модельное время» для тестов/демо: пересчитать план так,
    # как будто сейчас это время (ISO-8601). Пользователь из приложения это
    # поле не шлёт — маршрут всегда считается по фактическому времени.
    now: Optional[str] = None


@app.post("/api/plan")
def get_plan(request: PlanRequest):
    """
    Полный план поездки: разбор фразы LLM + геопоиск + маршрутизация.

    Ответ — один из режимов:
        "plan"     — маршрут построен: legs, total_min, price_grn, vehicles;
        "clarify"  — геопоиск неуверен (low_confidence) либо точки не найдены —
                     нужно уточнить у пользователя (reask=true);
        "no_route" — точки найдены, но в пределах двух пересадок маршрут не
                     строится (обычно ночью, когда маршруты не ходят).

    Эмулятор уже умеет рисовать "plan"-режим (legs c path и ТС), а clarify
    показывается подсказкой с просьбой переформулировать фразу.
    """
    locator: Optional[Locator] = app_state.get("locator")
    if locator is None:
        raise HTTPException(status_code=503, detail="Locator is not initialized yet")

    if not request.text or not request.text.strip():
        raise HTTPException(status_code=400, detail="Field 'text' must not be empty")

    # Шаг 1 и 2 — ровно как в /api/route: LLM вытаскивает точки, Locator
    # превращает их в stop_id.
    locations = call_llm_extract_locations(request.text)
    
    if locations.get("type") == "error":
        return {
            "mode": "clarify",
            "note": "Не вдалося зрозуміти запит. Спробуйте назвати звідки і куди потрібно доїхати.",
            "user_text": request.text
        }
        
    if locations.get("type") == "off_topic":
        return {
            "mode": "off_topic",
            "message": "Я можу допомогти знайти маршрут, пересадки, час у дорозі та вартість поїздки. Звідки і куди потрібно доїхати?",
            "user_text": request.text
        }
        
    from_query, to_query = locations["from"], locations["to"]
    from_stop_id, from_type = locator.locate(from_query)
    to_stop_id, to_type = locator.locate(to_query)

    logger.info(
        "План: %r -> from=%r(%s,id=%s) to=%r(%s,id=%s)",
        request.text, from_query, from_type, from_stop_id,
        to_query, to_type, to_stop_id,
    )

    debug = {
        "from_type": from_type,
        "to_type": to_type,
        "from_query": from_query,
        "to_query": to_query,
    }

    # Locator подтвердил проблему из реальных кейсов: он возвращает какое-то
    # совпадение даже на «абракадабру» (low_confidence). Глупо предлагать
    # маршрут от случайной остановки — лучше переспросить.
    if from_stop_id is None or to_stop_id is None or "low_confidence" in (from_type, to_type):
        return _clarify_response(request, debug, from_stop_id, to_stop_id)

    router: Optional[TransitRouter] = app_state.get("router")
    if router is None:
        raise HTTPException(
            status_code=503,
            detail="Router is not initialized (graph.json не загружен)",
        )

    # «Модельное время» умеет только сервер (тесты/демо); приложение не шлёт.
    # Разбираем его ДО живого среза и сразу приводим к Europe/Kyiv. В частности,
    # ISO с `Z`/offset нельзя просто «срезать» timezone: это сдвинет расчёт.
    if request.now:
        try:
            raw_now = request.now.replace("Z", "+00:00")
            plan_now = as_kyiv(datetime.fromisoformat(raw_now))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Field 'now' is not ISO-8601: {exc}")
    else:
        # Один и тот же момент для обоих прогонов (дефолт и вариант «≤1
        # пересадка») и для среза парка: иначе цифры карточек не сойдутся.
        plan_now = now_kyiv()

    # Парк машин: реальный трекер и/или симулятор, по PARK_SOURCE (§3 брифа).
    # Срез собирается до lock: сетевой источник не должен удерживать общий
    # роутер. В lock попадает только set_live → plan → build_variants.
    fleet, snapshot_at, fleet_source = _collect_fleet(plan_now)
    plan, variants, variants_note = _calculate_plan_locked(
        router,
        from_stop_id,
        to_stop_id,
        fleet,
        snapshot_at,
        fleet_source,
        plan_now,
    )

    if plan is None:
        stop_by_id = {str(stop["id"]): stop for stop in locator.stops}
        from_name = stop_by_id.get(str(from_stop_id), {}).get("name")
        to_name = stop_by_id.get(str(to_stop_id), {}).get("name")

        # Вночі все стоїть: чесно кажемо, коли перший рейс (за розкладом).
        firsts = [
            router.earliest_service_minutes(from_stop_id),
            router.earliest_service_minutes(to_stop_id),
        ]
        firsts = [value for value in firsts if value is not None]
        if firsts:
            next_minutes = min(firsts)
            first_hour, first_minute = divmod(int(next_minutes), 60)
            note = f"На цьому напрямку зараз нічого не їде — перший рейс орієнтовно о {first_hour:02d}:{first_minute:02d}."
        else:
            note = "На цьому напрямку зараз нічого не їде — спробуйте пізніше."

        return {
            "mode": "no_route",
            "user_text": request.text,
            "debug_info": debug,
            "from_stop_id": from_stop_id,
            "to_stop_id": to_stop_id,
            "from_name": from_name,
            "to_name": to_name,
            "note": note,
        }

    # Варианты уже собраны под lock; корневой ответ остаётся совместимым.
    plan["variants"] = variants
    plan["variants_note"] = variants_note
    plan["user_text"] = request.text
    plan["debug_info"] = debug
    plan["reask"] = False
    return plan


def _clarify_response(
    request: PlanRequest,
    debug: Dict[str, str],
    from_stop_id: Optional[int],
    to_stop_id: Optional[int],
) -> Dict[str, Any]:
    """
    Ответ-переспрос, когда фраза распознана неуверенно/не полностью.

    Неуверенность Locator кодирует в match_type "low_confidence" — в этом
    случае stop_id по-прежнему может быть заполнен, но доверять ему нельзя.
    """
    locator: Locator = app_state["locator"]
    stop_by_id = {str(stop["id"]): stop for stop in locator.stops}
    from_name = stop_by_id.get(str(from_stop_id), {}).get("name") if from_stop_id else None
    to_name = stop_by_id.get(str(to_stop_id), {}).get("name") if to_stop_id else None
    low_confidence = debug.get("from_type") == "low_confidence" or debug.get("to_type") == "low_confidence"

    if low_confidence:
        note = "Уточніть, будь ласка, звідки і куди ви їдете — я не до кінця зрозумів назви."
    else:
        note = "Я не зрозумів, звідки/куди ви їдете. Назвіть зупинку чи вулицю."
    return {
        "mode": "clarify",
        "user_text": request.text,
        "debug_info": debug,
        "from_stop_id": from_stop_id,
        "to_stop_id": to_stop_id,
        "from_name": from_name,
        "to_name": to_name,
        "reask": low_confidence,
        "note": note,
    }


# ---------------------------------------------------------------------------
# Справочник остановок для UI и будущего RN-клиента
# ---------------------------------------------------------------------------

@app.get("/api/stops")
def get_stops(q: Optional[str] = Query(None, description="Подстрока названия или псевдонима")):
    """
    Отдаёт остановки: id, название, координаты, псевдонимы.

    Названия и псевдонимы — уже ПОСЛЕ правок сленга, чтобы эмулятор рисовал
    и искал ровно то же, что возвращает Locator (иначе «Соборка» в подсказке
    и «пл. Соборна» на карте выглядели бы как разные места).
    """
    locator: Optional[Locator] = app_state.get("locator")
    if locator is None:
        raise HTTPException(status_code=503, detail="Locator is not initialized yet")

    stops = locator.stops
    if q:
        needle = q.strip().lower()
        stops = [
            stop for stop in stops
            if needle in str(stop.get("name", "")).lower()
            or any(needle in str(alias).lower() for alias in (stop.get("aliases") or []))
        ]

    return {
        "count": len(stops),
        "stops": [
            {
                "id": stop["id"],
                "name": stop["name"],
                "lat": stop["lat"],
                "lon": stop["lon"],
                "aliases": stop.get("aliases") or [],
            }
            for stop in stops
        ],
    }


# ---------------------------------------------------------------------------
# Телеметрия выбора варианта плана (поставка 1, §13.3 брифа)
# ---------------------------------------------------------------------------

class PlanChoiceTelemetry(BaseModel):
    """
    Один клик по карточке варианта — событие для анализа предпочтений.

    Поля по замороженному контракту лога (§13.3): оффер целиком (включая
    `source` каждого варианта — без него ночной стенд на симуляторе не
    отделить от предпочтений реальных пассажиров), порядок карточек
    (позиционный bias: первая карточка притягивает клики), выбранный вариант
    (`null` — карточки показали, но выбора не сделали; эта метрика обязательна)
    и анонимный `device_id` клиента.
    """

    ts: str
    from_stop_id: Union[int, str]
    to_stop_id: Union[int, str]
    offer: List[Dict[str, Any]]
    default_variant_id: str
    variant_order: List[str]
    chosen_variant_id: Optional[str] = None
    device_id: str
    client: str


@app.post("/api/telemetry/plan_choice")
def log_plan_choice(request: PlanChoiceTelemetry):
    """
    Записывает выбор варианта плана в JSONL-поток `logs/telemetry_plan_choices.jsonl`.

    Эндпоинт ничего не возвращает клиенту, кроме подтверждения — это «пожарная
    и забыть» запись: UI шлёт её после клика по карточке и не ждет ответа.
    Запись не должна ронять приложение: если файл недоступен, логируем ошибку
    и отдаём 500, но сервер продолжает работать.
    """
    payload = request.model_dump()
    # Серверная метка приёма: час клиента может гулять, а для анализа нужна
    # достоверная хронология. Контрактные поля клиента не затираем.
    payload["received_at"] = format_kyiv()

    try:
        with _TELEMETRY_LOCK:
            append_jsonl(TELEMETRY_LOG_PATH, payload)
    except OSError as exc:
        logger.warning("Телеметрия выбора не записана: %s", exc)
        raise HTTPException(status_code=500, detail="Не удалось записать лог телеметрии")

    logger.info(
        "Телеметрия выбора: %s — выбран %r из %d карточек (%s)",
        payload["device_id"], payload["chosen_variant_id"],
        len(payload["variant_order"]), payload["client"],
    )
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Живой GPS-слой (trans-gps.cv.ua)
# ---------------------------------------------------------------------------

def _parse_id_list(raw: Optional[str]) -> Optional[List[int]]:
    """
    Разбирает список routeId из query-параметра.

    Формат совместим с сайтом перевозчика (id через подчёркивание):
        ?routes=6_9_12   |   ?routes=6,9,12
    None или пустая строка означают «без ограничения по маршрутам».
    """
    if not raw:
        return None
    ids = [int(part) for part in re.split(r"[^0-9]+", raw) if part]
    return ids or None


def _parse_type_list(raw: Optional[str]) -> Optional[List[str]]:
    """Разбирает список типов ТС: ?vehicle_types=bus,trolley"""
    if not raw:
        return None
    types = [part.strip().lower() for part in re.split(r"[^A-Za-z]+", raw) if part.strip()]
    return types or None


@app.get("/api/live")
def get_live_vehicles(
    only_fresh: bool = Query(
        True,
        description="Только ТС со свежим GPS-треком (<= 5 мин) и не в депо",
    ),
    include_depo: bool = Query(False, description="Включать ТС, стоящие в депо"),
    routes: Optional[str] = Query(
        None, description="Фильтр по routeId источника, напр. 6_9_12 или 6,9,12"
    ),
    vehicle_types: Optional[str] = Query(
        None, description="Фильтр по типу ТС: bus, trolley"
    ),
    now: Optional[str] = Query(
        None,
        description="«Модельное время» ISO-8601: отдать парк на этот момент (только симулятор)",
    ),
):
    """
    Живой GPS-слой: текущее положение транспорта Черновцов.

    Параметр now работает только для виртуального парка (PARK_SOURCE=sim):
    он позволяет запросить парк на произвольный момент — проверить ночь,
    утро или конец смены, не переводя часы на сервере. Реальный трекер
    умеет отдавать только «сейчас» (§3.1).

    Данные берутся с открытого сайта перевозчика trans-gps.cv.ua
    (/map/tracker/), опрашиваются фоновой задачей раз в 5 секунд и
    отдаются уже нормализованными:

        speed / orientation -> float (в источнике это строки "000.0");
        gpstime             -> плюс age_seconds и status (live/stale/depo);
        routeId             -> подпись и цвет маршрута из /map/routes/1|2;
        routeColour         -> CSS-имя, продублировано в hex для RN-клиента.

    Поле counts считается ДО фильтров, поэтому клиент может показать
    «живих: 12 із 26 машин». Промежуточного кэша нет: срез уже лежит
    в памяти процесса и обновляется фоновым поллером.

    При PARK_SOURCE=auto опрашиваются оба источника и срез сливается
    (merge_fleet, §3): маршрут со свежей реальной машиной целиком берётся
    из трекера, остальные — из симулятора. В этом случае source="mixed",
    а каждая машина помечена своим источником в поле source — UI рисует
    бейдж «SIM», чтобы виртуальные машины не выдавались за живой GPS.
    """
    sim_layer: Optional[SimLayer] = app_state.get("sim_layer")
    tracker: Optional[LiveTracker] = app_state.get("tracker")
    if sim_layer is None and tracker is None:
        raise HTTPException(status_code=503, detail="Live layer is not initialized yet")

    query_now: Optional[datetime] = None
    if now:
        try:
            raw_now = now.replace("Z", "+00:00")
            query_now = as_kyiv(datetime.fromisoformat(raw_now))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Query 'now' is not ISO-8601: {exc}")

    snapshot_kwargs: Dict[str, Any] = {
        "only_fresh": only_fresh,
        "include_depo": include_depo,
        "route_ids": _parse_id_list(routes),
        "vehicle_types": _parse_type_list(vehicle_types),
    }
    sim_kwargs: Dict[str, Any] = dict(snapshot_kwargs)
    if query_now is not None and sim_layer is not None:
        # Симулятор умеет отдать парк на произвольный момент («машина времени»),
        # реальный трекер — только «сейчас» (§3.1: у источников разные эпохи).
        sim_kwargs["now"] = query_now

    # Моно-режим отдаёт срез своего слоя как есть — формат тот же.
    if PARK_SOURCE == "gps":
        if tracker is None:
            raise HTTPException(status_code=503, detail="Live tracker is not initialized yet")
        return tracker.snapshot(**snapshot_kwargs)
    if tracker is None:
        # Тестовый стенд/деградация: трекер не поднят — отдаём виртуальный парк.
        return sim_layer.snapshot(**sim_kwargs)

    # PARK_SOURCE == "auto": опросили оба источника, сливаем с приоритетом
    # реального GPS. Свежесть и депо проверяются внутри слоёв (only_fresh),
    # поэтому в слияние уже не попадут stale-машины, маскирующие дыры.
    sim_snap = sim_layer.snapshot(**sim_kwargs)
    real_snap = tracker.snapshot(**snapshot_kwargs)
    vehicles = merge_fleet(
        real_snap.get("vehicles", []),
        sim_snap.get("vehicles", []),
    )

    return {
        "source": "mixed",
        "generated_at": real_snap.get("generated_at") or sim_snap.get("generated_at"),
        "last_success_at": real_snap.get("last_success_at"),
        "last_error": real_snap.get("last_error"),
        "poll_interval_seconds": real_snap.get("poll_interval_seconds"),
        "fresh_max_age_seconds": real_snap.get("fresh_max_age_seconds"),
        "counts": {
            "total": len(vehicles),
            "live": sum(1 for v in vehicles if v.get("is_live")),
            "stale": sum(1 for v in vehicles if v.get("status") == "stale"),
            "in_depo": sum(1 for v in vehicles if v.get("in_depo")),
            "unknown_gpstime": sum(1 for v in vehicles if v.get("status") == "unknown"),
        },
        "returned": len(vehicles),
        # Кто остался в срезе после приоритета: пользователь видит, что
        # «маршрутов в N раз больше, чем живых машин» — дыры закрыты симом.
        "by_source": {
            "real": sum(1 for v in vehicles if v.get("source") == "real"),
            "sim": sum(1 for v in vehicles if v.get("source") == "sim"),
        },
        "sources": {
            "real": {"counts": real_snap.get("counts"), "last_error": real_snap.get("last_error")},
            "sim": {"counts": sim_snap.get("counts"), "generated_at": sim_snap.get("generated_at")},
        },
        # Реальные маршруты приоритетнее на пересечениях, симовские
        # добавляют те, которых трекер сейчас не видит.
        "routes": {
            **(sim_snap.get("routes") or {}),
            **(real_snap.get("routes") or {}),
        },
        "vehicles": vehicles,
    }


# ---------------------------------------------------------------------------
# Сленг: псевдонимы и переименование остановок (админка)
# ---------------------------------------------------------------------------

class SlangStopRequest(BaseModel):
    """Правка одной остановки. None = «это поле не трогать»."""

    stop_id: int
    aliases: Optional[List[str]] = None
    name: Optional[str] = None
    generic: Optional[bool] = None
    comment: Optional[str] = None


def _rebuild_locator() -> Locator:
    """
    Пересобирает Locator после правок сленга.

    Перечитываем stops.json и применяем свежие правки — это десятки
    миллисекунд, зато изменения из админки действуют сразу, без перезапуска
    контейнера (на телефоне это важно: правишь сленг и тут же проверяешь).
    """
    streets = app_state.get("streets") or []
    stops = apply_overrides(load_stops(STOPS_PATH))
    locator = Locator(stops=stops, streets=streets)
    app_state["locator"] = locator
    logger.info("Locator пересобран после правок сленга: %d остановок.", len(stops))
    return locator


@app.get("/api/slang")
def get_slang(
    include_stops: bool = Query(True, description="Отдать полный список остановок с псевдонимами"),
):
    """
    Отдаёт правки сленга и (по желанию) полный список остановок.

    Админке нужен именно полный список: чтобы дописать псевдоним, надо видеть
    названия и текущие alias'ы всех остановок города.
    """
    payload: Dict[str, Any] = {
        "overrides": load_overrides(),
        "stats": overrides_stats(),
    }
    if include_stops:
        payload["stops"] = [
            {
                "id": stop["id"],
                "name": stop["name"],
                "lat": stop.get("lat"),
                "lon": stop.get("lon"),
                "aliases": stop.get("aliases") or [],
                "generic": bool(stop.get("generic")),
            }
            for stop in apply_overrides(load_stops(STOPS_PATH), keep_generic=True)
        ]
    return payload


@app.post("/api/slang")
def save_slang(request: SlangStopRequest):
    """Добавляет или обновляет правку остановки и сразу пересобирает Locator."""
    raw_stops = load_stops(STOPS_PATH)
    if not any(str(stop.get("id")) == str(request.stop_id) for stop in raw_stops):
        raise HTTPException(
            status_code=404,
            detail=f"Остановка {request.stop_id} не найдена в stops.json",
        )

    entry = upsert_stop(
        request.stop_id,
        aliases=request.aliases,
        name=request.name,
        generic=request.generic,
        comment=request.comment,
    )
    _rebuild_locator()
    return {"saved": entry, "stop_id": request.stop_id, "stats": overrides_stats()}


@app.delete("/api/slang/{stop_id}")
def remove_slang(stop_id: int):
    """Убирает правку остановки (возврат к данным stops.json)."""
    removed = delete_stop(stop_id)
    _rebuild_locator()
    return {"removed": removed, "stop_id": stop_id, "stats": overrides_stats()}


# ---------------------------------------------------------------------------
# Жалобы на ответы эмулятора («це бред») — на разбор
# ---------------------------------------------------------------------------

class FeedbackRequest(BaseModel):
    """Жалоба из эмулятора: что спросили, что показали, что не так."""

    kind: str = "other"
    comment: Optional[str] = None
    user_text: Optional[str] = None
    response: Optional[Dict[str, Any]] = None
    client: Optional[Dict[str, Any]] = None


@app.post("/api/feedback")
def create_feedback(request: FeedbackRequest):
    """
    Принимает жалобу и складывает её в data/feedback.

    Пишем и одиночный JSON (удобно открыть один кейс), и строку в дневном
    JSONL (удобно посмотреть списком) — детали в feedback_store.
    """
    if not (request.comment or request.user_text):
        raise HTTPException(
            status_code=400,
            detail="Нужен хотя бы комментарий или исходный текст запроса",
        )

    payload = request.model_dump() if hasattr(request, "model_dump") else request.dict()
    record = save_feedback(payload)
    return {
        "saved": True,
        "id": record["id"],
        "created_at": record["created_at"],
        "kind": record["kind"],
    }


@app.get("/api/feedback")
def get_feedback(
    limit: int = Query(50, ge=1, le=500),
    day: Optional[str] = Query(None, description="Только за конкретный день, формат YYYY-MM-DD"),
):
    """Отдаёт последние жалобы и сводку по типам (для админки)."""
    return {"stats": feedback_stats(), "items": list_feedback(limit=limit, day=day)}


# ---------------------------------------------------------------------------
# UI эмулятора отдаётся тем же контейнером
# ---------------------------------------------------------------------------

# Наружу открываем ТОЛЬКО папку web: в корне репозитория лежит .env, и монтаж
# корня как статики выставил бы его в открытый доступ.
#
# Порядок монтажей важен: конкретные пути (/ui/scraped_data) регистрируем ДО
# общего /ui — иначе их перехватит монтаж папки web (Starlette матчит префиксы
# в порядке регистрации).
WEB_DIR = BASE_DIR / "web"

# Редактор маршрутов (web/editor.html) читает исходники EasyWay из
# scraped_data/ и osm_stops.json. Оба лежат в корне (их пишут пайплайны
# graph_layer / overpass_test.js), поэтому отдаём их точечно, а не корень целиком.
SCRAPED_DIR = BASE_DIR / "scraped_data"
if SCRAPED_DIR.is_dir():
    app.mount("/ui/scraped_data", StaticFiles(directory=SCRAPED_DIR), name="scraped_data")
else:
    logger.warning("Папка %s не найдена — редактор не увидит исходники маршрутов", SCRAPED_DIR)

OSM_STOPS_PATH = BASE_DIR / "osm_stops.json"


@app.get("/ui/osm_stops.json", include_in_schema=False)
def editor_osm_stops() -> FileResponse:
    """Файл OSM-остановок для редактора (лежит в корне репозитория)."""
    if not OSM_STOPS_PATH.is_file():
        raise HTTPException(status_code=404, detail="osm_stops.json не найден")
    return FileResponse(OSM_STOPS_PATH, media_type="application/json")


if WEB_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=WEB_DIR, html=True), name="ui")
else:
    logger.warning("Папка %s не найдена — UI эмулятора будет недоступен", WEB_DIR)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
