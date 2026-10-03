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

import asyncio
import json
import logging
import math
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple, Union

from time_utils import as_kyiv, format_kyiv, now_kyiv

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
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
import tts_layer

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

# Whitelist активных маршрутов города (30 автобусных + 8 троллейбусных, правило
# .agents/rules/active_routes.md). Отдаётся наружу эндпоинтом /api/manifest:
# кнопки фильтра в эмуляторе должны показывать все городские маршруты, даже
# если сегодня на них нет ни одной машины (трекер пустой, машины в депо).
ROUTES_MANIFEST_PATH = BASE_DIR / "data" / "routes_manifest.json"

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
OPENROUTER_MODELS_RAW = os.getenv(
    "OPENROUTER_MODEL",
    "openai/gpt-4o-mini,google/gemma-4-26b-a4b-it:free,qwen/qwen3.8-27b:free",
)
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
#          (тип ТС + подпись), на котором есть хоть одна реальная машина
#          вне депо с треком не старше ROUTE_FALLBACK_MAX_AGE_SECONDS
#          (час — обеденный перерыв водителя, §3.6), целиком берём из
#          трекера, остальные — из симулятора. Каждой машине добавляется
#          поле source: "real"|"sim";
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
    timeout=10.0,
    max_retries=0,
)

# Системный промпт для LLM. Жёстко требуем ТОЛЬКО JSON без каких-либо
# пояснений, чтобы результат можно было безопасно распарсить.
#
# Список мест (PLACES_HINT) подставляется в промпт динамически: без него модель
# работает вслепую и выдумывает названия — на вопрос «до ринку» отвечала
# from="мені потрібно", а на «мені потрібно київська» придумывала from="південна",
# которой в фразе не было. Сленг из data/slang_overrides.json («форик»,
# «тралка», «соборка») модель тоже не знает, хотя Locator по нему ищет.
SYSTEM_PROMPT_TEMPLATE = """\
Ти — голосовий помічник транспорту міста Чернівці.
Користувач пише або каже українською мовою чи суржиком.

Твоє єдине завдання — зрозуміти запит і повернути СУВОРО чистий JSON.

Відомі місця, які називають мешканці (використовуй ці назви дослівно):
{places}

1. Якщо запит про маршрут, проїзд, зупинку, пересадку, час у дорозі або ціну:
   - "from" — звідки їхати, "to" — куди їхати;
   - короткі назви на кшталт «ринок», «універ», «гравитон», «соборка» —
     це теж місця, а не загальні слова;
   - НІКОЛИ не вигадуй назву, якої немає в запиті користувача;
   - якщо вказано лише одне місце, друге залиш порожнім ("");
   - відповідь: {{"type": "route", "from": "назва", "to": "назва"}}

2. Якщо користувач просить ПОКАЗАТИ транспорт певних маршрутів (де зараз
   машини), а не проїхати з точки в точку — «покажи дев'ятку і десятку»,
   «де зараз 9А», «хочу бачити 5 тролейбус»:
   - назву маршруту ОБОВ'ЯЗКОВО нормалізуй у цифровий рядок так, як він є в
     системі: сленг, числівники й порядкові — це теж номер маршруту:
       «одиничка», «перший» → "1";      «двійка» → "2";
       «трійка» → "3";                  «четвірка» → "4";
       «п'ятірка» → "5";                «шістка» → "6";
       «сімка» → "7";                   «вісімка» → "8";
       «дев'ятка», «дев'ятий» → "9";    «десятка», «десятий» → "10";
       «одинадцятка» → "11";            «дванадцятка» → "12";
       «п'ятнадцятка» → "15";
   - літеру в номері став латиницею у верхньому регістрі: «9а» → "9A",
     «15к» → "15K", «8а» → "8A";
   - НІКОЛИ не вигадуй номер, якого немає в запиті;
   - якщо разом із номером названо звідки й куди — це НЕ моніторинг, а
     маршрут (пункт 1);
   - відповідь: {{"type": "monitor_routes", "routes": ["9", "10"]}}

3. Якщо запит НЕ стосується транспорту Чернівців (погода, рецепти, жарти,
   новини, загальні питання про світ):
   відповідь: {{"type": "off_topic"}}

ВАЖЛИВО: короткий запит на кшталт «до ринку» або «на гравитон» — це
запит про МАРШРУТ, а не off_topic. Не відкидай такі запити.

Поверни ТІЛЬКИ JSON, без markdown-розмітки і без додаткових слів.
"""

# Подсказка с местами. Собирается один раз при старте из stops.json + сленга,
# ограничена по длине: слишком длинный промпт дороже и медленнее.
PLACES_HINT_MAX_CHARS = 1800


def _collect_places_hint(stops: List[Dict]) -> str:
    """
    Собирает компактный список мест, которые реально знает Locator.

    Приоритет — сленг (data/slang_overrides.json): именно эти слова мешканцы
    говорят вслух («форик», «тралка», «соборка»), и именно их модель не знает.
    Затем добираем именованные остановки, пока не упрёмся в лимит длины.
    """
    parts: List[str] = []
    seen = set()

    def _push(value: str) -> bool:
        text = " ".join(str(value or "").split()).strip(" ?.!,;:")
        if not text or len(text) < 3:
            return True
        # Служебные пометки внутри алиасов («маг (зроби сам)») — это мусор для
        # модели, а не место: такие обрезаем до основной части.
        if "(" in text and ")" in text:
            text = text.split("(", 1)[0].strip(" ?.!,;:")
            if len(text) < 3:
                return True
        if not text or not any(ch.isalpha() for ch in text):
            return True
        key = text.lower()
        if key in seen:
            return True
        seen.add(key)
        # Проверяем лимит ДО добавления: если элемент уже не влезает, не
        # добавляем его вовсе — иначе итоговая строка выходит за лимит.
        total = sum(len(p) for p in parts) + 2 * len(parts)
        if total + 2 + len(text) > PLACES_HINT_MAX_CHARS:
            return False
        parts.append(text)
        return True

    # 1) Сленг и псевдонимы — самое важное, идёт первым.
    for stop in stops:
        for alias in stop.get("aliases", []):
            if not _push(alias):
                return ", ".join(parts)

    # 2) Названия остановок: пропускаем безымянные и дубли вида «вул. ...».
    for stop in stops:
        name = str(stop.get("name") or "").strip()
        if not name or name.lower().startswith("вул"):
            continue
        if not _push(name):
            break

    return ", ".join(parts)


def build_system_prompt(places_hint: str) -> str:
    """Подставляет список мест в шаблон промпта."""
    if not places_hint.strip():
        places_hint = "(список порожній — використовуй назви, які є в запиті)"
    return SYSTEM_PROMPT_TEMPLATE.format(places=places_hint)


# Промпт по умолчанию — шаблон с пустым списком мест. Реальный собирается в
# startup(), когда уже загружены stops.json и сленг из data/.
SYSTEM_PROMPT = build_system_prompt("")


# Небольшой кэш разбора фраз в памяти процесса: одна и та же фраза не должна
# бить по OpenRouter (и по балансу $0.20) на каждый чих — Locator/роутер
# каждый раз считаются заново, но LLM отвечает одинаково (temperature=0).
_llm_cache: Dict[str, Dict[str, str]] = {}
LLM_CACHE_MAX = 512


def _normalize_cache_key(user_text: str) -> str:
    return " ".join(user_text.lower().split())


def emergency_extract_locations(user_text: str) -> Dict[str, str]:
    """Последний локальный слой без LLM: разобрать типовую фразу маршрута."""
    text = " ".join(str(user_text or "").split()).strip(" ?.!,;:")
    if not text:
        return {"from": "", "to": ""}

    patterns = (
        re.compile(r"\b(?:з|від)\s+(.+?)\s+(?:до|на)\s+(.+)", re.IGNORECASE),
        re.compile(r"\bдоїхати\s+(?:з|від)\s+(.+?)\s+(?:до|на)\s+(.+)", re.IGNORECASE),
        re.compile(r"\bпоїхати\s+(?:з|від)\s+(.+?)\s+(?:до|на)\s+(.+)", re.IGNORECASE),
        re.compile(
            r"\bя\s+на\s+(.+?),\s*(?:а\s+)?(?:мені\s+)?(?:треба|їду)\s+(?:на|до)\s+(.+)",
            re.IGNORECASE,
        ),
        re.compile(r"\bмені\s+треба\s+(?:з|від)\s+(.+?)\s+(?:до|на)\s+(.+)", re.IGNORECASE),
    )
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            origin = match.group(1).strip(" ?.!,;:")
            destination = match.group(2).strip(" ?.!,;:")
            if origin and destination:
                return {"from": origin, "to": destination}

    arrow_match = re.search(r"\s+(?:->|→|—|–)\s+", text)
    if arrow_match:
        origin = text[:arrow_match.start()].strip(" ?.!,;:")
        destination = text[arrow_match.end():].strip(" ?.!,;:")
        if origin and destination:
            return {"from": origin, "to": destination}
    return {"from": "", "to": ""}


FALLBACK_INPUT_MESSAGE_UA = (
    "Я не зміг автоматично зрозуміти фразу. "
    "Напишіть або скажіть: «з Соборки до Гравітону»."
)
FALLBACK_SERVICE_MESSAGE_UA = (
    "Сервіс розпізнавання зараз відповідає нестабільно. "
    "Напишіть початок і пункт призначення — я спробую побудувати маршрут."
)


def _fallback_message(locations: Dict[str, str]) -> str:
    origin = str(locations.get("from") or "").strip()
    destination = str(locations.get("to") or "").strip()
    if origin and not destination:
        return f"Я почув «{origin}», але не зрозумів, куди потрібно доїхати. Назвіть пункт призначення."
    if destination and not origin:
        return f"Куди ви їдете — «{destination}». А звідки потрібно виїхати?"
    return FALLBACK_INPUT_MESSAGE_UA


def extract_locations_with_fallback(user_text: str) -> Dict[str, Any]:
    """
    Получить разбор фразы через LLM, затем через локальный аварийный слой.

    Возвращает один из интентов: route (from/to), off_topic, monitor_routes
    (номера маршрутов) либо error с текстом-подсказкой.
    """
    locations = call_llm_extract_locations(user_text)
    intent = str(locations.get("type") or "route").strip().lower()
    if intent in ("off_topic", "monitor_routes"):
        return locations
    if intent == "route" and (locations.get("from") or locations.get("to")):
        return locations

    # Локальный слой идёт ВТОРЫМ: модель недоступна или не разобрала фразу.
    emergency = emergency_extract_locations(user_text)
    if emergency["from"] or emergency["to"]:
        return {
            "type": "fallback",
            "from": emergency["from"],
            "to": emergency["to"],
            "message": _fallback_message(emergency),
        }
    # «Покажи дев'ятку і десятку» моніторинг працює і без моделі: номер
    # маршруту назван словом/цифрою, а не місцем — Locator тут не потрібен.
    numbers = parse_route_numbers(user_text)
    if numbers:
        return {"type": "monitor_routes", "routes": numbers}
    return {
        "type": "error",
        "from": "",
        "to": "",
        "message": FALLBACK_SERVICE_MESSAGE_UA,
    }



def call_llm_extract_locations(user_text: str) -> Dict[str, Any]:
    """
    Отправляет текст пользователя в LLM (через OpenRouter) и возвращает
    разобранный JSON: {"type": "route", "from": ..., "to": ...} для поездки
    А→Б, {"type": "off_topic"} для всего остального и
    {"type": "monitor_routes", "routes": ["9", "10"]} для просьбы показать
    машины конкретных маршрутов (интент нормализует номера в цифры).

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
    for model_name in OPENROUTER_MODELS:
        try:
            response = llm_client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_text},
                ],
                temperature=0.0,
                max_tokens=250,
            )
            raw_content = response.choices[0].message.content or ""
            parsed = _parse_llm_json(raw_content)
            if parsed is None:
                raise ValueError(f"Модель {model_name} вернула не-JSON")
            intent = str(parsed.get("type", "route")).strip().lower()
            if intent == "monitor_routes":
                # «Покажи дев'ятку і десятку»: номеров может быть несколько, и
                # словесные формы приводим к цифрам локально — модель это
                # правило знает, но выполняет не всегда.
                routes = _clean_route_list(parsed.get("routes"))
                if not routes:
                    raise ValueError(f"Модель {model_name} не назвала ни одного маршрута")
                result = {"type": "monitor_routes", "routes": routes}
            else:
                result = {
                    "type": intent,
                    "from": str(parsed.get("from") or "").strip(),
                    "to": str(parsed.get("to") or "").strip(),
                }
                if result["type"] not in ("route", "off_topic"):
                    raise ValueError(f"Модель {model_name} вернула неизвестный intent")
            if cache_key not in _llm_cache and len(_llm_cache) >= LLM_CACHE_MAX:
                _llm_cache.pop(next(iter(_llm_cache)))
            _llm_cache[cache_key] = result
            logger.info(
                "Модель OpenRouter %s ответила успешно (intent=%s)",
                model_name,
                result["type"],
            )
            return dict(result)
        except Exception as exc:
            last_exc = exc
            logger.warning("Модель OpenRouter %s не сработала: %s", model_name, exc)

    logger.error("Все модели OpenRouter недоступны. Последняя ошибка: %s", last_exc)
    return {"type": "error", "from": "", "to": ""}


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

    # Промпт собираем после загрузки остановок и сленга: модель должна видеть
    # реальные названия («соборка», «форик», «гравитон»), иначе выдумывает их.
    global SYSTEM_PROMPT
    places_hint = _collect_places_hint(stops)
    SYSTEM_PROMPT = build_system_prompt(places_hint)
    logger.info(
        "Промпт LLM собран: %d символов, в подсказке мест ~%d.",
        len(SYSTEM_PROMPT),
        len(places_hint),
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

    # Прогрев TTS больше не нужен: локальной модели (Silero) нет, а ключи
    # облачных движков читаются в момент синтеза — см. tts_layer.generate_tts.

    # Whitelist активных маршрутов (38) — для фильтра парка в эмуляторе
    # (/api/manifest). Файл маленький и меняется вместе с данными графа,
    # поэтому читаем один раз на старте; если его нет — эндпоинт честно
    # ответит 503, а сервер поднимется (фильтр в UI просто будет пустым).
    try:
        app_state["routes_manifest"] = json.loads(
            ROUTES_MANIFEST_PATH.read_text(encoding="utf-8")
        )
        logger.info(
            "Справочник активных маршрутов прочитан: %d автобусов + %d троллейбусов.",
            len(app_state["routes_manifest"].get("bus", [])),
            len(app_state["routes_manifest"].get("trolley", [])),
        )
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("routes_manifest.json не прочитан (%s) — /api/manifest вернёт 503.", exc)

    # Источник данных о машинах (бриф §3): симулятор и/или реальный трекер.
    # В режиме auto опрашиваются оба, а сливаются они в merge_fleet() при
    # каждом запросе — тогда на маршрутах с реальным GPS (трек не старше
    # ROUTE_FALLBACK_MAX_AGE_SECONDS) видны реальные машины, а пустые
    # направления заполняются виртуальными.
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
    """
    Ответ /api/route: разбор фразы на пару остановок.

    Режим «моніторинг маршрутів» (ідея 2026-09-28) остановок не возвращает
    вовсе: вместо from_stop_id/to_stop_id приходят номера, которые назвал
    пользователь (requested_routes) и разобранные записи активных маршрутов
    (routes: key/type/label — ключ кнопки фильтра парка в UI).
    """

    mode: Optional[str] = None
    message: Optional[str] = None
    note: Optional[str] = None
    from_stop_id: Optional[int] = None
    to_stop_id: Optional[int] = None
    debug_info: Optional[DebugInfo] = None
    requested_routes: Optional[List[str]] = None
    routes: Optional[List[Dict[str, Any]]] = None
    missing_routes: Optional[List[str]] = None
    # Уточнення типу ТС (01.10.2026): номер є і в автобуса, і в тролейбуса —
    # віддаємо варіанти (автобуси / тролейбуси / обидва) з ГОТОВИМИ
    # routes/shapes/speech, щоб клієнт показав картки без повторного розбору.
    ambiguous_routes: Optional[List[str]] = None
    clarify_options: Optional[List[Dict[str, Any]]] = None
    # Готова фраза для озвучки («Показую маршрути 9 та 10.»): у режимі
    # моніторингу голос звучить із цим полем, інакше клієнт збирав би фразу сам.
    speech: Optional[Dict[str, Any]] = None
    # «Показувати нечего, назвіть номер інакше» — той самий флаг, що й у clarify.
    reask: Optional[bool] = None


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

    Отдельный интент — monitor_routes (идея 2026-09-28): «покажи 9 і 10». Тогда
    остановки не ищем, а отвечаем режимом mode="monitor_routes" с номерами
    (requested_routes/routes) — полный план поездки отдаёт /api/plan.
    """
    locator: Optional[Locator] = app_state.get("locator")
    if locator is None:
        # Теоретически невозможно при штатном старте через lifespan,
        # но проверка защищает от гонок при hot-reload/тестах.
        raise HTTPException(status_code=503, detail="Locator is not initialized yet")

    if not request.text or not request.text.strip():
        raise HTTPException(status_code=400, detail="Field 'text' must not be empty")

    # Шаг 1: LLM извлекает названия точек, а локальный слой страхует его отказ.
    locations = extract_locations_with_fallback(request.text)

    if locations.get("type") == "monitor_routes":
        # Тот же ответ, что и у /api/plan: клиент, откатившийся на /api/route
        # (старый сервер без плана), обязан получить и мониторинг.
        monitor = monitor_routes_response(request.text, locations.get("routes") or [])
        return RouteResponse(**monitor)

    if locations.get("type") == "error":
        return RouteResponse(
            mode="manual_input",
            message=locations.get("message") or FALLBACK_INPUT_MESSAGE_UA,
        )

    if locations.get("type") == "off_topic":
        return RouteResponse(
            mode="off_topic",
            message="Я можу допомогти з пошуком оптимального маршруту, часом у дорозі та вартістю поїздки, звідки і куди потрібно доїхати?"
        )

    if locations.get("type") == "fallback" and not (locations.get("from") and locations.get("to")):
        return RouteResponse(
            mode="manual_input",
            message=locations.get("message") or FALLBACK_INPUT_MESSAGE_UA,
        )

    from_query = locations.get("from", "")
    to_query = locations.get("to", "")

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

# Окно, в котором реальный GPS ещё держит маршрут за собой (§3.6).
#
# Это НЕ порог свежести: FRESH_MAX_AGE_SECONDS = 300 (live_layer.py) отвечает
# за «живость» отдельной машины — статус is_live, пульсацию на карте и фразу
# «виходи зараз», и его мы не трогаем. Здесь речь о другом: сколько времени
# маршрут считается реальным, если трек замолчал. Час выбран владельцем из
# жизни — столько длится обеденный перерыв водителя: автобус стоит на
# кінцевій, GPS молчит, и подменять такой маршрут выдуманными машинами нельзя
# (иначе в обед на карте «призраки» симулятора вместо реального борта).
# Мертвее часа — трек уже не улика, маршрут уходит симулятору, а чип в UI
# получает хрестик ❌ (web/emulator.js, ROUTE_DEAD_NOTE).
ROUTE_FALLBACK_MAX_AGE_SECONDS = 3600.0


def _is_trolley(vehicle: Dict[str, Any]) -> bool:
    """Троллейбус: реальный GPS на нём маршрут не держит (§3.5)."""
    return str(vehicle.get("vehicle_type") or "").strip().lower() == "trolley"


def _gps_alive(vehicle: Dict[str, Any]) -> bool:
    """
    Держит ли этот реальный борт маршрут за собой по GPS (§3.6).

    Возраст трека сверяем с окном фоллбека (час), а не с порогом свежести
    (5 минут): водитель на обеде — трек стоит, но маршрут реальный. Приоритет
    не держат машины в депо (они не на линии), троллейбусы (§3.5) и машины с
    неизвестным возрастом трека (битый gpstime в источнике): приоритет отдаём
    только тому, чей возраст мы знаем (§3.3 п.2).
    """
    if _is_trolley(vehicle) or vehicle.get("in_depo"):
        return False
    age = vehicle.get("age_seconds")
    if age is None:
        return False
    try:
        return float(age) <= ROUTE_FALLBACK_MAX_AGE_SECONDS
    except (TypeError, ValueError):
        return False


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
    Сливает реальный и виртуальный парки по правилу приоритета (§3, §3.6).

    Если на маршруте (тип ТС + подпись) есть хоть одна реальная машина вне
    депо, трек которой не старше ROUTE_FALLBACK_MAX_AGE_SECONDS (час), — это
    «живой» маршрут: отдаём его реальные машины, а симулятор для этого
    маршрута отбрасывается целиком. Если реальных машин нет или все они
    старше часа — маршрут «мёртвый» за GPS: его целиком берёт симулятор, а
    призраки старого трека в ответ не попадают. Правило маршрутом, а не
    машиной: смешивать оба источника на одном маршруте нельзя — у них разные
    поля (направление есть только у симулятора) и разные эпохи среза, поэтому
    строгость фильтров и ETA отличались бы (§3.2).

    Час, а не пять минут (§3.6): FRESH_MAX_AGE_SECONDS описывает живость
    ОТДЕЛЬНОЙ машины (is_live, пульсация в UI) и остаётся как есть, но
    приоритет маршрута по нему считать нельзя — водитель на обеде стоит на
    кінцевій, и маршрут подменялся бы выдуманными машинами. Пока треку меньше
    часа, маршрут реальный: машина уезжает в ответ со своим статусом («за
    розкладом»), а симулятор в него не подмешивается.

    Инвариант для клиента: у «мёртвого» маршрута в ответе НЕТ ни одной машины
    с source="real" — по этому признаку UI ставит хрестик ❌ на чип маршрута
    (web/emulator.js, routeSourceStates/syncRouteSourceMarks).

    Депо и неизвестный возраст трека приоритета не дают (§3.3 п.2): такие
    машины не «прикрывают» направление, чтобы дыра осталась видимой. В ответе
    они остаются как есть — это не подмена источника, а факт среза (клиент
    сам решает, показывать ли борта из депо, см. include_depo).

    Каждой машине добавляется поле `source: "real" | "sim"` — роутер
    прокидывает его в ногу плана, а UI рисует бейдж «SIM». Без этой
    пометки приоритет превращается в подмену (§3.3 п.3-4).

    Исключение: троллейбусы никогда не берутся из реального GPS (§3.5).
    Причина: у перевозчика trans-gps.cv.ua реально работает 1 тролл,
    трек нестабильный, охват маршрутов нулевой. Симулятор держит все
    8 троллейбусных маршрутов с правильным интервалом — он лучше.
    Если в будущем GPS-охват троллейбусов вырастет — убрать проверку
    `_is_trolley()` в _gps_alive() и ниже.
    """
    # Живые маршруты считаем ДО отбора машин: иначе первый же призрак решал
    # бы судьбу маршрута сам за себя.
    gps_keys: set = set()
    for vehicle in real_fleet:
        key = _route_key(vehicle)
        if key[1] and _gps_alive(vehicle):
            gps_keys.add(key)

    real_vehicles: List[Dict[str, Any]] = []
    for vehicle in real_fleet:
        key = _route_key(vehicle)
        # Машины без подписи маршрута, борта в депо и троллейбусы (§3.5)
        # маршрут не занимают — но и прятать их нечего: это факт среза.
        on_route = key[1] and not _is_trolley(vehicle) and not vehicle.get("in_depo")
        if on_route and key not in gps_keys:
            continue  # трек старше часа: маршрут честно уходит симулятору
        real_vehicles.append(dict(vehicle, source="real"))

    sim_vehicles: List[Dict[str, Any]] = []
    for vehicle in sim_fleet:
        if _route_key(vehicle) in gps_keys:
            continue  # маршрут держит реальный GPS — симулятор не подмешиваем
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

    В режиме auto срез трекера берём с окном фоллбека (only_fresh=False,
    ROUTE_FALLBACK_MAX_AGE_SECONDS): маршруты решает merge_fleet помаршрутно
    (§3.6), и на «свежих» 5 минутах это решение принимать не на чем. Порог
    свежести отдельной машины при этом не тронут — is_live/status приходят
    из live_layer как были, и роутер, как и раньше, ведёт ETA только по
    живым бортам (остальные помечаются «за розкладом»).
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
            # Окно фоллбека, а не только «свежие» 5 минут (§3.6): решение по
            # маршруту принимает merge_fleet, и ему нужны машины с треком до
            # часа. С is_live/status машины приходят как есть — порог свежести
            # (FRESH_MAX_AGE_SECONDS) не тронут.
            real_vehicles = tracker.snapshot(only_fresh=False).get("vehicles", [])
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
                     строится (обычно ночью, когда маршруты не ходят);
        "monitor_routes" — пользователь просит не маршрут А→Б, а показать
                     машины названных маршрутов («покажи дев'ятку і десятку»):
                     legs пустые, приходят requested_routes/routes/speech, а
                     клиент по key программно фильтрует живой парк.

    Эмулятор уже умеет рисовать "plan"-режим (legs c path и ТС), а clarify
    показывается подсказкой с просьбой переформулировать фразу.
    """
    locator: Optional[Locator] = app_state.get("locator")
    if locator is None:
        raise HTTPException(status_code=503, detail="Locator is not initialized yet")

    if not request.text or not request.text.strip():
        raise HTTPException(status_code=400, detail="Field 'text' must not be empty")

    # Шаг 1 и 2 — LLM + аварийный локальный разбор → Locator.
    locations = extract_locations_with_fallback(request.text)

    if locations.get("type") == "monitor_routes":
        # Ни Locator, ни роутер не нужны: точек нет вовсе, а парк клиент
        # фильтрует сам по ключам маршрутов (renderMonitorRoutes в emulator.js).
        return monitor_routes_response(request.text, locations.get("routes") or [])

    if locations.get("type") == "error":
        return {
            "mode": "manual_input",
            "message": locations.get("message") or FALLBACK_INPUT_MESSAGE_UA,
            "user_text": request.text,
        }

    if locations.get("type") == "off_topic":
        return {
            "mode": "off_topic",
            "message": "Я можу допомогти з пошуком оптимального маршруту, часом у дорозі та вартістю поїздки, звідки і куди потрібно доїхати?",
            "user_text": request.text,
        }

    if locations.get("type") == "fallback" and not (locations.get("from") and locations.get("to")):
        return {
            "mode": "manual_input",
            "message": locations.get("message") or FALLBACK_INPUT_MESSAGE_UA,
            "user_text": request.text,
        }

    from_query, to_query = locations.get("from", ""), locations.get("to", "")

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
    # Голосова фраза для озвучки: «поїздка автобусом номер 9, приблизно 34
    # хвилини, вартість 20 гривень». Фронт читає її, а если поля нет
    # (старый кэш/старый сервер) — собирає короткий варіант из цифр плана.
    speech_text = build_plan_speech(plan)
    if speech_text:
        plan["speech"] = {"text": speech_text, "lang": "uk-UA"}
    # Варіанти — такі самі плани для клієнта (картка + голос), тому фраза
    # собирается і для них: без неї вибір «Дешевий» озвучувався б коротким
    # «План: 36 хвилин, 36 гривень» без номерів маршрутів, а RN-клієнт мав би
    # дозбирати фразу з кореня відповіді.
    for variant in plan["variants"]:
        variant_speech = build_plan_speech(variant)
        if variant_speech:
            variant["speech"] = {"text": variant_speech, "lang": "uk-UA"}
    return plan


def _uk_num(value: float) -> str:
    """Число прописью по-украински — для голоса, который читает цифры."""
    rounded = int(round(float(value)))
    if rounded == rounded // 1:
        return str(rounded)
    return str(rounded)


def _uk_plural(number: int, one: str, few: str, many: str) -> str:
    """Украинское склонение: 1 хвилина, 2 хвилини, 5 хвилин."""
    n = abs(int(number))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _speech_route_label(label: str) -> str:
    """
    Номер маршрута для озвучки: «9A» → «9 а».

    Цифра и литера читаются отдельно, а латинская литера (A, B, C, D, E, K —
    так записаны 8A, 9A, 10A, 15K) произносится украинской буквой: иначе TTS
    читает «номер 15 k» по-английски. Общий помощник для планов и для
    мониторинга маршрутов — чтобы «дев'ятка» и «9A» звучали одинаково.
    """
    text = str(label or "").strip()
    if not text:
        return ""
    digits = "".join(ch for ch in text if ch.isdigit())
    letters = "".join(ch for ch in text if ch.isalpha())
    parts: List[str] = []
    if digits:
        parts.append(digits)
    if letters:
        parts.append({
            "a": "а", "b": "б", "c": "в", "d": "д", "e": "е", "k": "к",
        }.get(letters.lower(), letters.lower()))
    return " ".join(parts) or text


def _speech_route_case(label: str, vehicle: str, case: str = "instr") -> str:
    """
    Название транспорта для озвучки в нужном падеже.

    «поїздка автобусом номер 9» — творительный (instr);
    «з пересадкою на автобус номер 3» — винительный (acc).
    Без этого получалось «на тролейбусом номер 3».
    """
    is_trolley = str(vehicle).lower().startswith("trolley")
    if case == "acc":
        noun = "тролейбус" if is_trolley else "автобус"
    else:
        noun = "тролейбусом" if is_trolley else "автобусом"
    spoken = _speech_route_label(label)
    if not spoken:
        return noun
    return f"{noun} номер {spoken}"


def build_plan_speech(plan: Dict[str, Any]) -> str:
    """
    Голосова фраза для плана: не «34 хвилини, 20 гривень», а «поїздка
    автобусом номер 9, приблизно 34 хвилини, вартість 20 гривень».

    Ожидание в произносимый текст не берём намеренно: фраза должна звучать
    быстро и по делу, детали остаются в карточке. А вот ВСЕ ноги-поездки
    обязаны звучать: план с двумя пересадками содержит три ноги, и «первая +
    последняя» молча теряла средний маршрут (на стенде «Соборка → Гравітон» —
    bus:8A + bus:5 + trolley:2, а голос говорил «8 а … на номер 2»).
    """
    legs = [leg for leg in (plan.get("legs") or []) if leg.get("type") == "transit"]
    if not legs:
        return ""

    parts: List[str] = []
    if len(legs) == 1:
        parts.append(
            "Поїздка "
            + _speech_route_case(legs[0].get("route"), legs[0].get("vehicle", "bus"))
        )
    else:
        # Все ноги кроме последней — «автобусом номер 8 а, потім автобусом
        # номер 5»; последняя идёт после «з пересадкою на», то есть в
        # винительном падеже: «на тролейбус 3», а не «на тролейбусом 3».
        earlier = ", потім ".join(
            _speech_route_case(leg.get("route"), leg.get("vehicle", "bus"))
            for leg in legs[:-1]
        )
        last = _speech_route_case(
            legs[-1].get("route"), legs[-1].get("vehicle", "bus"), case="acc"
        )
        parts.append(f"Поїздка {earlier}, з пересадкою на {last}")

    total_min = plan.get("total_min")
    if isinstance(total_min, (int, float)):
        value = int(round(total_min))
        parts.append(f"приблизно {_uk_num(value)} {_uk_plural(value, 'хвилина', 'хвилини', 'хвилин')}")

    price = plan.get("price_grn")
    if isinstance(price, (int, float)) and price > 0:
        value = int(round(price))
        parts.append(f"вартість {_uk_num(value)} {_uk_plural(value, 'гривня', 'гривні', 'гривень')}")

    return ", ".join(parts) + "."


# ---------------------------------------------------------------------------
# Моніторинг маршрутів голосом: «покажи дев'ятку і десятку»
# (ідея 2026-09-28, docs/ideas/2026-09-28-voice-route-monitoring.md)
# ---------------------------------------------------------------------------
#
# Житель, який знає місто, не потребує маршруту А→Б: йому треба побачити, де
# зараз машини потрібних ліній. Розбір фрази робить LLM (інтент
# "monitor_routes" у промпті), але нормалізацію номерів вона виконує не
# завжди — тому тримаємо локальний словник і локальний розбір: він же працює,
# коли модель недоступна (аварійний шлях без ключа OpenRouter).

# Сколько точек линии отдаём на направление: ланцюг маршруту — это десятки
# остановок, а клиент просит пару линий. Ограничение защищает от случайного
# «нарисуй все 38» (список приходит из запроса).
ROUTE_SHAPE_MAX_POINTS = 200

# Словесные формы номеров, которые реально звучат в микрофон: сленг («дев'ятка»)
# и порядковые («дев'ятий»). Формы даём в падежах — распознавание слышит
# «покажи дев'ятку», а не «дев'ятка», и без этого номер терялся.
# Ключ — как чует распознавання, значение — цифровой рядок в системе.
_ROUTE_WORD_FORMS: Dict[str, Tuple[str, ...]] = {
    "1": ("одиничка", "одиничку", "одинички", "одиниця", "одиницю",
          "перший", "перша", "перше", "першого", "першу"),
    "2": ("двійка", "двійку", "двійки", "двійочка", "двойка",
          "другий", "друга", "друге", "другого", "другу"),
    "3": ("трійка", "трійку", "трійки", "тройка",
          "третій", "третя", "третє", "третього", "третю"),
    "4": ("четвірка", "четвірку", "четвірки", "четверка",
          "четвертий", "четверта", "четвертого", "четверту"),
    "5": ("п'ятірка", "п'ятірку", "п'ятірки", "пятірка",
          "п'ятий", "п'ята", "п'ятого", "п'яту"),
    "6": ("шістка", "шістку", "шістки", "шестірка",
          "шестий", "шоста", "шостого", "шосту"),
    "7": ("сімка", "сімку", "сімки", "сьомий", "сьома", "сьомого", "сьому"),
    "8": ("вісімка", "вісімку", "вісімки", "восьмий", "восьма", "восьмого", "восьму"),
    "9": ("дев'ятка", "дев'ятку", "дев'ятки", "девятка",
          "дев'ятий", "дев'ята", "дев'ятого", "дев'яту"),
    "10": ("десятка", "десятку", "десятки", "десятий", "десята", "десятого", "десяту"),
    "11": ("одинадцятка", "одинадцятку", "одинадцятки", "одинадцятий"),
    "12": ("дванадцятка", "дванадцятку", "дванадцятки", "дванадцятий"),
    "13": ("тринадцятка", "тринадцятку", "тринадцятки", "тринадцятий"),
    "14": ("чотирнадцятка", "чотирнадцятку", "чотирнадцятки", "чотирнадцятий"),
    "15": ("п'ятнадцятка", "п'ятнадцятку", "пятнадцятка", "п'ятнадцятий"),
    "20": ("двадцятка", "двадцятку", "двадцятки", "двадцятий"),
}
ROUTE_WORD_NUMBERS: Dict[str, str] = {
    word: number
    for number, words in _ROUTE_WORD_FORMS.items()
    for word in words
}

# Хвостовая часть сленгового названия в падежах: «десятка → десятку → десяткою»
# — номер один и тот же. Основа + этот хвіст закрывают все бытовые формы, а
# список окончаний намеренно узкий: иначе «п'ятниця» (день недели) стала бы
# маршрутом 5.
_ROUTE_SLANG_STEMS: Dict[str, str] = {
    "одинич": "1", "одиниц": "1",
    "двій": "2", "двой": "2",
    "трій": "3", "трой": "3",
    "четвір": "4", "четвер": "4",
    "п'ятір": "5", "пятір": "5",
    "шіст": "6", "шестір": "6",
    "сім": "7",
    "вісім": "8",
    "дев'ят": "9", "девят": "9",
    "десят": "10",
    "одинадцят": "11",
    "дванадцят": "12",
    "тринадцят": "13",
    "чотирнадцят": "14",
    "п'ятнадцят": "15", "пятнадцят": "15",
    "двадцят": "20",
}
_ROUTE_SLANG_SUFFIXES = ("ка", "ку", "ки", "кою", "кой", "ці", "цю", "чка", "чку", "чки")


def _route_word_number(word: str) -> str:
    """
    Номер маршрута по словесной форме: сначала точные формы, потом основа.

    Точный словарь закрывает сленг и порядковые («дев'ятка», «дев'ятий»), а
    основа со типовыми окончаниями — остальные падежи, которых в нём нет
    («десяткою», «одиничкою»): распознавание слышит именно их.
    """
    exact = ROUTE_WORD_NUMBERS.get(word)
    if exact:
        return exact
    for stem, number in _ROUTE_SLANG_STEMS.items():
        if word.startswith(stem) and word[len(stem):] in _ROUTE_SLANG_SUFFIXES:
            return number
    return ""

# Слова-маркери моніторингу. Потрібні лише для ЦИФРОВИХ номерів: «покажи 9» —
# це маршрут, а «до вулиці 9» — адреса. Словесна форма («дев'ятка») у
# маршрутному контексті однозначна, тому для неї маркер не потрібен.
MONITOR_MARKERS = re.compile(
    r"покаж|показат|монітор|монитор|бачит|бач|вивед|вивод|відфільтр|відфільтру|"
    r"фільтр|увімкн|включ|де\s+зараз|де\s+(їде|iде|їздит|ездит)",
    re.IGNORECASE,
)

# Цифрова форма номера: «9», «10», «9а», «8A», «15К», «3/3a» (так номер зве
# перевізник у live_names). Буква — рівно одна, після цифр або дробу.
ROUTE_NUMBER_RE = re.compile(r"\d{1,2}(?:/\d{1,2})?[A-Za-zА-Яа-яЇїІіЄєҐґ]?")
_WORD_TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЇїІіЄєҐґ'’/]+")


def _route_word(text: str) -> str:
    """Ключ для словаря форм номера: нижний регистр и один апостроф.

    Распознавание и клавиатуры дают разные апострофы (’ ʼ ` ´) — без этого
    «п’ятірка» не находилась бы в словаре рядом с «п'ятірка».
    """
    return (
        str(text or "").strip().lower()
        .replace("’", "'").replace("ʼ", "'").replace("`", "'").replace("´", "'")
    )


def _normalize_route_number(raw: str) -> str:
    """
    Номер маршруту в том виде, в каком он живёт в манифесте: «9а» → «9A».

    Кириллицу сводим к латинице («8А» → «8A», «15К» → «15K») — та же пара
    букв, что и в подписях перевозчика; регистр приводим к верхнему, потому
    что так записан display_name. Слэш не трогаем: «3/3a» — это алиас
    троллейбуса 3 из live_names, и склеивать его нельзя.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    text = text.upper()
    for cyrillic, latin in (("А", "A"), ("Б", "B"), ("В", "V"), ("К", "K")):
        text = text.replace(cyrillic, latin)
    return text


def _clean_route_list(raw: Any) -> List[str]:
    """
    Нормализует список номеров от LLM: слова → цифры, дубликаты — вон.

    Модель получает правило нормализации в промпте, но ошибается (вернёт
    «дев'ятку» словом). Тогда чиню номер локально: пользователь не должен
    увидеть «Показую маршрути 0» из-за каприза модели.
    """
    if isinstance(raw, (str, int, float)):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    numbers: List[str] = []
    for item in raw:
        text = str(item or "").strip()
        if not text:
            continue
        word = _route_word(text)
        number = _route_word_number(word)
        if not number:
            if ROUTE_NUMBER_RE.fullmatch(text):
                number = _normalize_route_number(text)
            else:
                # «маршрут 9», «9-й», «маршрут №10» — вытащим цифры с буквой.
                found = ROUTE_NUMBER_RE.search(text)
                number = _normalize_route_number(found.group(0)) if found else ""
        if number and number not in numbers:
            numbers.append(number)
    return numbers


def parse_route_numbers(text: str) -> List[str]:
    """
    Локальный (без LLM) разбор номеров маршрутов — аварийный слой.

    Возвращает номера в порядке упоминания, без дублей. Правило простое:
      * словесная форма («дев'ятка», «десятка») — это маршрутная сленг-форма,
        спутать её не с чем;
      * цифровая («9», «9А») принимается ТОЛЬКО при слове-маркере моніторингу
        («покажи», «де зараз»): «як доїхати до вулиці 9» — это адрес, а не
        маршрут, и такой запрос обязан уйти в обычный план.
    """
    if not text:
        return []
    has_marker = bool(MONITOR_MARKERS.search(text))
    numbers: List[str] = []
    for token in _WORD_TOKEN_RE.findall(text):
        word = _route_word(token)
        number = _route_word_number(word)
        if not number and has_marker and ROUTE_NUMBER_RE.fullmatch(token):
            number = _normalize_route_number(token)
        if number and number not in numbers:
            numbers.append(number)
    return numbers


def _manifest_route_entries() -> List[Dict[str, Any]]:
    """Активные маршруты города из манифеста (38) — вместе с алиасами."""
    manifest = app_state.get("routes_manifest") or {}
    entries: List[Dict[str, Any]] = []
    for type_name in ("bus", "trolley"):
        for item in manifest.get(type_name) or []:
            label = str(item.get("display_name") or item.get("number") or "").strip()
            if not label:
                continue
            entries.append({
                "type": type_name,
                "label": label,
                "number": str(item.get("number") or label).strip(),
                "aliases": [str(alias) for alias in (item.get("live_names") or [])],
            })
    return entries


# Тип ТС в запите: «покажи 4 автобус» / «де зараз 5 тролейбус». Нужен, чтобы
# строго отсечь одноимённый маршрут другого типа: «4» автобуса — это не «4»
# троллейбуса (у перевозчика тот же борт идёт как «4T», но это другой маршрут).
# Покрываем оба написания: украинское «тролейбус» и русское «троллейбус».
_VEHICLE_TYPE_RE = re.compile(r"(?P<trolley>трол?лейбус)|(?P<bus>автобус)", re.IGNORECASE)


def _vehicle_type_hint(text: str) -> str:
    """
    Тип ТС, названный пользователем: «автобус» → bus, «тролейбус» → trolley,
    оба сразу → both, ничего → "".

    Разница «both» и "" критична для мониторинга: «покажи автобус 4 і тролейбус
    4» — это осознанный выбор обоих типов (показываем без вопросов), а «покажи
    4» — тип не назван, и общий номер 4 надо уточнить (см.
    monitor_routes_response → _monitor_clarify_response).
    """
    found = {match.lastgroup for match in _VEHICLE_TYPE_RE.finditer(str(text or ""))}
    if found == {"trolley"}:
        return "trolley"
    if found == {"bus"}:
        return "bus"
    if found == {"bus", "trolley"}:
        return "both"
    return ""


def _match_manifest_entries(
    number: str,
    entries: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Записи манифеста, чей номер/подпись/алиас совпал с названным номером.

    Сравнение — по нормализованной подписи (TransitRouter.normalize_label):
    «9а» = «9A», «3/3a» — алиас троллейбуса 3. Буква НЕ срезается, поэтому «4»
    не совпадёт с алиасом «4T» — это разные номера.
    """
    if entries is None:
        entries = _manifest_route_entries()
    wanted = TransitRouter.normalize_label(str(number or ""))
    if not wanted:
        return []
    return [
        entry for entry in entries
        if any(
            TransitRouter.normalize_label(candidate) == wanted
            for candidate in [entry["label"], entry["number"], *entry["aliases"]]
        )
    ]


def resolve_requested_routes(
    route_numbers: List[str],
    vehicle_type: str = "",
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Сопоставляет названные номера с активными маршрутами города (манифест).

    Возвращает (найденные, ненайденные). Ключ собирается так же, как ключ кнопки
    фильтра в UI («bus|9A»): клиент по нему программно сужает живой парк.

    Номер есть и у автобусов, и у троллейбусов («5»)? По умолчанию отдаём оба.
    Но если `vehicle_type` — "bus"/"trolley" (тип назван явно: «покажи 4
    автобус»), одноимённый маршрут другого типа не отдаём вовсе. "both"/"" — без
    сужения (мониторинг сам решает, спросить у пользователя или показать всё).
    """
    found: List[Dict[str, Any]] = []
    missing: List[str] = []
    wanted_type = str(vehicle_type or "").strip().lower()
    entries = _manifest_route_entries()
    if wanted_type in ("bus", "trolley"):
        entries = [entry for entry in entries if entry["type"] == wanted_type]
    for number in route_numbers or []:
        matches = _match_manifest_entries(number, entries)
        if not matches:
            if str(number) not in missing:
                missing.append(str(number))
            continue
        for entry in matches:
            key = entry["type"] + "|" + entry["label"]
            if any(item["key"] == key for item in found):
                continue
            found.append({
                "key": key,
                "type": entry["type"],
                "label": entry["label"],
                "number": entry["number"],
                "requested": str(number),
            })
    return found, missing


def _ambiguous_requested_numbers(route_numbers: List[str]) -> List[str]:
    """
    Из названных номеров — те, что есть И у автобуса, И у троллейбуса.

    Только они требуют уточнения типа ТС. У «9» троллейбуса нет — номер
    однозначный, спрашивать нечего; у «4» — есть оба.
    """
    entries = _manifest_route_entries()
    ambiguous: List[str] = []
    for number in route_numbers or []:
        types = {entry["type"] for entry in _match_manifest_entries(number, entries)}
        if types == {"bus", "trolley"} and str(number) not in ambiguous:
            ambiguous.append(str(number))
    return ambiguous


def _resolve_monitor_choice(
    route_numbers: List[str],
    choice: str,
    ambiguous: List[str],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Маршруты для одного варианта уточнения мониторинга.

    `choice` — "bus"/"trolley"/"both". Для НЕоднозначных номеров берём выбранный
    тип, однозначные идут как есть: «покажи 4, 23» в варианте «тролейбус» даст
    trolley|4 + bus|23 — названный 23 без троллейбуса не теряется.
    """
    ambiguous_set = {str(item) for item in (ambiguous or [])}
    found: List[Dict[str, Any]] = []
    missing: List[str] = []
    for number in route_numbers or []:
        matches = _match_manifest_entries(number)
        if str(number) in ambiguous_set and choice in ("bus", "trolley"):
            matches = [entry for entry in matches if entry["type"] == choice]
        if not matches:
            if str(number) not in missing:
                missing.append(str(number))
            continue
        for entry in matches:
            key = entry["type"] + "|" + entry["label"]
            if any(item["key"] == key for item in found):
                continue
            found.append({
                "key": key,
                "type": entry["type"],
                "label": entry["label"],
                "number": entry["number"],
                "requested": str(number),
            })
    return found, missing


def route_shapes_for(routes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Геометрия маршрутов для линий на карте (идея 2026-09-29, п.2).

    Линия рисуется по ОБОИМ направлениям («туди і назад»): у A и B ланцюжки
    слегка расходятся, а пользователь просил «покажи 9» — нарисовать половину
    линии было бы подменой. Координаты берём из уже загруженного графа
    (`TransitRouter.route_coords` — те же цепочки остановок, что уезжают в
    `leg.full_geom` плана): отдельного «osm_routes.json» у клиента нет, а тянуть
    537 КБ `graph.json` в браузер ради двух линий незачем.

    Формат: [{"key": "bus|9", "type": "bus", "label": "9",
              "directions": [{"direction": "A", "coords": [[lat, lon], ...]}]}]
    """
    router: Optional[TransitRouter] = app_state.get("router")
    if router is None or not routes:
        return []

    wanted: set = set()
    for item in routes:
        wanted.add((
            str(item.get("type") or "").strip().lower(),
            TransitRouter.normalize_label(str(item.get("label") or "")),
        ))

    shapes: Dict[str, Dict[str, Any]] = {}
    for route_key, coords in router.route_coords.items():
        parts = str(route_key).split(":")
        if len(parts) < 2:
            continue
        vtype = parts[0].strip().lower()
        route = router.routes.get(route_key) or {}
        labels = [
            route.get("live_route_name"),
            route.get("route_name"),
            *(route.get("live_route_names") or []),
            parts[1],
        ]
        norms = {TransitRouter.normalize_label(str(label)) for label in labels if label}
        if not any((vtype, candidate) in wanted for candidate in norms):
            continue
        # Ключ тот же, что у кнопки фильтра: его отдал resolve_requested_routes,
        # по нему же клиент ставит фильтр парка.
        item = next(
            (
                route_item for route_item in routes
                if str(route_item.get("type") or "").strip().lower() == vtype
                and TransitRouter.normalize_label(str(route_item.get("label") or "")) in norms
            ),
            None,
        )
        if item is None:
            continue
        points = [
            [round(float(lat), 6), round(float(lon), 6)]
            for lat, lon in coords
        ][:ROUTE_SHAPE_MAX_POINTS]
        if len(points) < 2:
            continue
        entry = shapes.setdefault(item["key"], {
            "key": item["key"],
            "type": item["type"],
            "label": item["label"],
            "directions": [],
        })
        entry["directions"].append({
            "direction": router.route_direction.get(route_key) or parts[-1],
            "coords": points,
        })
    return [item for item in shapes.values() if item["directions"]]


_UA_ORDINAL_ONES = {
    1: "перший", 2: "другий", 3: "третій", 4: "четвертий", 5: "п'ятий",
    6: "шостий", 7: "сьомий", 8: "восьмий", 9: "дев'ятий",
}
_UA_ORDINAL_TEENS = {
    10: "десятий", 11: "одинадцятий", 12: "дванадцятий", 13: "тринадцятий",
    14: "чотирнадцятий", 15: "п'ятнадцятий", 16: "шістнадцятий",
    17: "сімнадцятий", 18: "вісімнадцятий", 19: "дев'ятнадцятий",
}
_UA_ORDINAL_TENS_CARD = {2: "двадцять", 3: "тридцять", 4: "сорок"}
_UA_ORDINAL_TENS_FULL = {2: "двадцятий", 3: "тридцятий", 4: "сороковий"}


def _speech_ordinal_masc(number: str) -> str:
    """
    Порядковий числівник чоловічого роду: «4» → «четвертий».

    Потрібен для природної озвучки колізії номера («четвертий автобус та
    четвертий тролейбус»). Працює для чисел активних маршрутів; на «9A» чи
    «3/3a» поверне порожньо — там лишається звична форма «маршрут 9 а».
    """
    text = str(number or "").strip()
    if not text.isdigit():
        return ""
    value = int(text)
    if value in _UA_ORDINAL_ONES:
        return _UA_ORDINAL_ONES[value]
    if value in _UA_ORDINAL_TEENS:
        return _UA_ORDINAL_TEENS[value]
    tens, ones = divmod(value, 10)
    if tens in _UA_ORDINAL_TENS_CARD:
        if ones == 0:
            return _UA_ORDINAL_TENS_FULL[tens]
        if ones in _UA_ORDINAL_ONES:
            return _UA_ORDINAL_TENS_CARD[tens] + " " + _UA_ORDINAL_ONES[ones]
    return ""


def _speech_vehicle_noun(vehicle: str) -> str:
    """«автобус» / «тролейбус» (називний відмінок) за типом маршруту."""
    return "тролейбус" if str(vehicle).lower().startswith("trolley") else "автобус"


def _join_ua(items: List[str]) -> str:
    """Перелічення по-українськи: «A, B та C»."""
    parts = [str(item) for item in items if str(item)]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " та " + parts[-1]


def _monitor_route_names(routes: List[Dict[str, Any]]) -> Tuple[List[str], bool]:
    """
    Назви маршрутів для тексту й озвучки + ознака «номер спільний».

    Звичний випадок — «маршрут 9». Колізія — коли той самий номер є і в
    автобуса, і в тролейбуса («4»): дві однакові цифри злилися б у «чотири та
    чотири». Тоді називаємо тип ТС і порядковий номер: «четвертий автобус»,
    «четвертий тролейбус» (бриф Gemini 2026-10-01).
    """
    items = [item for item in (routes or []) if item]
    counts: Dict[str, int] = {}
    for item in items:
        key = TransitRouter.normalize_label(str(item.get("label") or ""))
        if key:
            counts[key] = counts.get(key, 0) + 1
    collision = any(count > 1 for count in counts.values())
    names: List[str] = []
    for item in items:
        label = str(item.get("label") or "").strip()
        spoken = _speech_route_label(label)
        if not spoken:
            continue
        key = TransitRouter.normalize_label(label)
        if collision and counts.get(key, 0) > 1 and label.isdigit():
            ordinal = _speech_ordinal_masc(label)
            if ordinal:
                names.append(ordinal + " " + _speech_vehicle_noun(item.get("type")))
                continue
        names.append("маршрут " + spoken)
    return names, collision


def build_monitor_speech(routes: List[Dict[str, Any]], missing: List[str]) -> str:
    """
    Голосова фраза моніторингу: «Показую маршрути 9 та 10.»

    Якщо номер спільний для автобуса й тролейбуса («покажи 4»), говоримо тип
    ТС і порядковий номер — «Показую четвертий автобус та четвертий
    тролейбус», а не «4 та 4» (бриф Gemini 2026-10-01).

    Ненайдені номери озвучуємо окремо («На жаль, маршрут 42 зараз не
    працює»): молчание в ответ на названный номер читается как поломка, а на
    самом деле такого маршрута нет среди активных
    (.agents/rules/active_routes.md).
    """
    names, collision = _monitor_route_names(routes)
    parts: List[str] = []
    if collision:
        parts.append("Показую " + _join_ua(names))
    elif len(names) == 1:
        parts.append("Показую " + names[0])
    elif names:
        spoken = [
            name[len("маршрут "):] if name.startswith("маршрут ") else name
            for name in names
        ]
        parts.append("Показую маршрути " + ", ".join(spoken[:-1]) + " та " + spoken[-1])
    if missing:
        numbers = ", ".join(str(item) for item in missing)
        if len(missing) == 1:
            parts.append("На жаль, маршрут " + numbers + " зараз не працює")
        else:
            parts.append("На жаль, маршрути " + numbers + " зараз не працюють")
    if not parts:
        return ""
    return ", ".join(parts) + "."


def _monitor_routes_message(routes: List[Dict[str, Any]], missing: List[str]) -> str:
    """Текст в панели ответа: что показываем и чего не нашли."""
    names, collision = _monitor_route_names(routes)
    labels = [str(item.get("label") or "") for item in routes if item.get("label")]
    if collision:
        # «4» є і в автобуса, і в тролейбуса: у панелі теж називаємо тип ТС,
        # щоб не читалося «4 та 4» (бриф Gemini 2026-10-01).
        text = "Показую " + _join_ua(names) + " — на карті лише їхні машини."
    elif len(labels) == 1:
        text = "Показую маршрут " + labels[0] + " — на карті лише його машини."
    elif labels:
        text = ("Показую маршрути " + ", ".join(labels[:-1]) + " та " + labels[-1]
                + " — на карті лише їхні машини.")
    else:
        text = ("Не знаю такого маршруту. Назвіть номер, "
                "наприклад «покажи дев'ятку і десятку».")
    if missing:
        numbers = ", ".join(str(item) for item in missing)
        if len(missing) == 1:
            text += f" Маршрут {numbers} зараз не працює — у списку активних його немає."
        else:
            text += f" Маршрути {numbers} зараз не працюють — у списку активних їх немає."
    return text


_MONITOR_CHOICES = (
    ("bus", "🚌 Автобуси"),
    ("trolley", "🚎 Тролейбуси"),
    ("both", "🚌🚎 Обидва"),
)


def _monitor_clarify_question(ambiguous: List[str]) -> str:
    """Вопрос в панели ответа: «... — що показати?» (тире — визуально)."""
    joined = _join_ua([str(item) for item in ambiguous])
    noun = "Маршрут" if len(ambiguous) == 1 else "Маршрути"
    return f"{noun} {joined} є і в автобусів, і в тролейбусів — що показати?"


def _monitor_clarify_speech(ambiguous: List[str]) -> str:
    """Тот же вопрос для TTS: точка вместо тире (движки читают «—» неровно)."""
    joined = _join_ua([str(item) for item in ambiguous])
    noun = "Маршрут" if len(ambiguous) == 1 else "Маршрути"
    return f"{noun} {joined} є і в автобусів, і в тролейбусів. Що показати?"


def _monitor_clarify_response(
    text: str,
    numbers: List[str],
    ambiguous: List[str],
) -> Dict[str, Any]:
    """
    Уточнение типа ТС: номер общий, тип не назван (бриф Gemini 01.10.2026).

    Отдаём три готовых варианта (автобусы / троллейбусы / оба): каждый несёт
    routes + shapes + speech + message, чтобы тап по карточке на клиенте сразу
    показывал нужный парк — без повторного разбора фразы и без сессии.
    """
    options: List[Dict[str, Any]] = []
    for choice, label in _MONITOR_CHOICES:
        found, missing = _resolve_monitor_choice(numbers, choice, ambiguous)
        option: Dict[str, Any] = {
            "id": choice,
            "label": label,
            "routes": found,
            "shapes": route_shapes_for(found),
            "missing_routes": missing,
            "message": _monitor_routes_message(found, missing),
        }
        speech = build_monitor_speech(found, missing)
        if speech:
            option["speech"] = {"text": speech, "lang": "uk-UA"}
        options.append(option)
    logger.info(
        "Моніторинг: уточнення типу ТС для %s (спільні номери: %s)",
        numbers, ambiguous,
    )
    return {
        "mode": "monitor_clarify",
        "user_text": text,
        "message": _monitor_clarify_question(ambiguous),
        "requested_routes": numbers,
        "ambiguous_routes": ambiguous,
        "clarify_options": options,
        "reask": True,
        "speech": {"text": _monitor_clarify_speech(ambiguous), "lang": "uk-UA"},
    }


def monitor_routes_response(
    text: str,
    route_numbers: List[str],
    vehicle_type: str = "",
) -> Dict[str, Any]:
    """
    Ответ режима «покажи маршрути» (ідея 2026-09-28): без Locator и роутера.

    Пользователю нужен не маршрут А→Б, а карта: где сейчас машины названных
    линий. Поэтому остановки не ищем вовсе, а отдаём разобранные номера и
    ключи кнопок фильтра (`bus|9A`): клиент по ним программно сужает живой
    парк (web/emulator.js, renderMonitorRoutes), а фразу для TTS собираем
    здесь — на фронт уходит готовый текст. Вместе с ключами едет `shapes` —
    геометрия линий (оба направления), чтобы на карте были видны не только
    машины, но и сам маршрут (идея 2026-09-29, п.2).

    `vehicle_type` («автобус»/«тролейбус» в фразе) сужает выдачу строго до
    названного типа: «покажи 4 автобус» не притащит тролейбус «4». Тип ищем и
    локально по тексту (поле от LLM — лишь подсказка сверху), чтобы работало и
    без ключа OpenRouter.
    """
    raw_numbers = [str(item) for item in (route_numbers or [])]
    # Числа чистим на входе: LLM могла вернуть «дев'ятку» словом (правило в
    # промпте есть, но модель ошибается) — а ответ клиенту обязан нести
    # цифровые номера, иначе фильтр парка искать будет нечего.
    numbers = _clean_route_list(raw_numbers)
    dropped = [item for item in raw_numbers if not _clean_route_list([item])]
    hint = str(vehicle_type or "").strip().lower() or _vehicle_type_hint(text)
    # Тип не назван, а номер есть и у автобуса, и у троллейбуса («покажи 4»)?
    # Это не «покажи автобус 4» — неоднозначность: спросим карточками, что
    # показать (бриф Gemini 01.10.2026), а не угадаем тип молча.
    if hint == "":
        ambiguous = _ambiguous_requested_numbers(numbers)
        if ambiguous:
            return _monitor_clarify_response(text, numbers, ambiguous)
    routes, missing = resolve_requested_routes(numbers, hint)
    shapes = route_shapes_for(routes)
    logger.info(
        "Моніторинг маршрутів: %r -> %s (немає: %s, не розібрано: %s, тип: %s, ліній: %d)",
        text, [item["key"] for item in routes], missing, dropped, hint or "—", len(shapes),
    )
    response: Dict[str, Any] = {
        "mode": "monitor_routes",
        "user_text": text,
        "message": _monitor_routes_message(routes, missing),
        # requested_routes — как назвали (LLM или локальный разбор), routes —
        # что реально есть среди активных: key = ключ кнопки фильтра в UI.
        "requested_routes": numbers,
        "routes": routes,
        "missing_routes": missing,
        # Геометрия линий для карты: у клиента её нет (graph.json ему не грузим).
        "shapes": shapes,
        # reask — «показывать нечего, назовите номер иначе»: тот же флаг, что у
        # clarify, поэтому даже старый клиент не промолчит.
        "reask": not routes,
    }
    speech = build_monitor_speech(routes, missing)
    if speech:
        response["speech"] = {"text": speech, "lang": "uk-UA"}
    return response


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

    Отдельный случай — половину просто НЕ НАЗВАЛИ («до ринку»): Locator
    отвечает "not_specified" на пустой запрос, и тогда в тексте переспроса
    должно быть видно, что вторая половина уже понята.
    """
    locator: Locator = app_state["locator"]
    stop_by_id = {str(stop["id"]): stop for stop in locator.stops}
    from_name = stop_by_id.get(str(from_stop_id), {}).get("name") if from_stop_id else None
    to_name = stop_by_id.get(str(to_stop_id), {}).get("name") if to_stop_id else None
    low_confidence = debug.get("from_type") == "low_confidence" or debug.get("to_type") == "low_confidence"
    # «Не назвали» и «не разобрал название» — разные подсказки: старая фраза
    # «звідки і куди» звинувачувала обидві половини там, де одну уже поняли.
    missing_from = from_stop_id is None or debug.get("from_type") in ("not_specified", "not_found")
    missing_to = to_stop_id is None or debug.get("to_type") in ("not_specified", "not_found")

    if missing_from and to_name:
        note = (
            f"Здається, вам до «{to_name}». А звідки потрібно виїхати?"
            if debug.get("to_type") == "low_confidence"
            else f"Куди ви їдете — «{to_name}». А звідки потрібно виїхати?"
        )
    elif missing_to and from_name:
        note = (
            f"Здається, ви виїжджаєте з «{from_name}». А куди потрібно доїхати?"
            if debug.get("from_type") == "low_confidence"
            else f"Я почув «{from_name}», але не зрозумів, куди потрібно доїхати. Назвіть пункт призначення."
        )
    elif low_confidence:
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
        # reask — «сервер чекає уточнення»: і непевна назва, і неназвана
        # половина фрази. Без цього UI писав «маршрут не знайдено» там, де
        # насправді питали «а звідки?».
        "reask": low_confidence or missing_from or missing_to,
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


# ---------------------------------------------------------------------------
# Срез парка для клиента: /api/live и поток /api/fleet/stream — одна сборка
# ---------------------------------------------------------------------------

# Источники парка, которые можно запросить на один запрос (кнопки «джерело»
# в панели эмулятора). PARK_SOURCE в .env — тот же набор: это режим стенда.
FLEET_SOURCES = ("auto", "gps", "sim")


def _fleet_mode(source: Optional[str]) -> str:
    """
    Источник парка для одного HTTP-запроса: явный source важнее .env.

    Днём владелец смотрит живой GPS перевозчика, вечером, когда машины уже
    в депо, — «як відпрацював симулятор» (там пусто у трекера, но не у
    симулятора). Неизвестное значение — ошибка клиента, а не тихий откат на
    .env: иначе UI показывал бы не то, что просил, и «пустой GPS» выглядел бы
    как поломка сервера.
    """
    if source is None or not str(source).strip():
        return PARK_SOURCE
    mode = str(source).strip().lower()
    if mode not in FLEET_SOURCES:
        raise HTTPException(
            status_code=400,
            detail="Query 'source' must be one of " + ", ".join(FLEET_SOURCES),
        )
    return mode


def _parse_query_now(now: Optional[str]) -> Optional[datetime]:
    """«Машина времени»: ISO-8601 из query → киевское время (иначе 400)."""
    if not now:
        return None
    try:
        return as_kyiv(datetime.fromisoformat(now.replace("Z", "+00:00")))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Query 'now' is not ISO-8601: {exc}")


def build_fleet_snapshot(
    *,
    source: Optional[str] = None,
    plan_now: Optional[datetime] = None,
    only_fresh: bool = True,
    include_depo: bool = False,
    route_ids: Optional[List[int]] = None,
    vehicle_types: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Один срез парка в формате клиента — для /api/live и /api/fleet/stream.

    Сборка жила в теле /api/live; вынесли, чтобы SSE не расходился с
    поллингом: клиент, откатившийся на /api/live (старый браузер, ошибка
    потока), получает ровно тот же JSON. Отличие от _collect_fleet: тот
    собирает парк для /api/plan и возвращает (vehicles, snapshot_at, source),
    здесь — полный ответ клиенту плюс переопределение источника на запрос.

    only_fresh в режиме auto применяется к симулятору, а трекер опрашивается в
    окне фоллбека (§3.6): маршруты решает merge_fleet помаршрутно, и по
    «свежим» 5 минутам решение принимать не на чем. Каждая машина приходит со
    своим is_live/status, поэтому клиент, которому нужны только живые борта,
    фильтрует по is_live — поле не подменяется.

    source: None — режим стенда (PARK_SOURCE), иначе "auto" | "gps" | "sim".
    """
    sim_layer: Optional[SimLayer] = app_state.get("sim_layer")
    tracker: Optional[LiveTracker] = app_state.get("tracker")
    if sim_layer is None and tracker is None:
        raise HTTPException(status_code=503, detail="Live layer is not initialized yet")

    mode = _fleet_mode(source)

    snapshot_kwargs: Dict[str, Any] = {
        "only_fresh": only_fresh,
        "include_depo": include_depo,
        "route_ids": route_ids,
        "vehicle_types": vehicle_types,
    }
    sim_kwargs: Dict[str, Any] = dict(snapshot_kwargs)
    if plan_now is not None and sim_layer is not None:
        # Симулятор умеет отдать парк на произвольный момент («машина времени»),
        # реальный трекер — только «сейчас» (§3.1: у источников разные эпохи).
        sim_kwargs["now"] = plan_now

    # Моно-режим отдаёт срез своего слоя как есть — формат тот же.
    if mode == "gps":
        if tracker is None:
            raise HTTPException(status_code=503, detail="Live tracker is not initialized yet")
        return tracker.snapshot(**snapshot_kwargs)
    if mode == "sim":
        if sim_layer is None:
            raise HTTPException(status_code=503, detail="Simulator is not initialized yet")
        return sim_layer.snapshot(**sim_kwargs)
    if tracker is None:
        # Тестовый стенд/деградация: трекер не поднят — отдаём виртуальный парк.
        return sim_layer.snapshot(**sim_kwargs)

    # mode == "auto": опросили оба источника, сливаем с приоритетом реального
    # GPS. Срез трекера берём с окном фоллбека (only_fresh=False): маршруты
    # решает merge_fleet помаршрутно, а по «свежим» 5 минутам решение о
    # пріоритеті принимать не на чем (§3.6 — обед водителя). Порог свежести
    # отдельной машины не тронут: она приходит со своим is_live/status, и
    # клиент видит «за розкладом» там, где трек остыл.
    sim_snap = sim_layer.snapshot(**sim_kwargs)
    real_snap = tracker.snapshot(**dict(snapshot_kwargs, only_fresh=False))
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
        # Окно, в котором маршрут ещё считается реальным (§3.6). Порог
        # свежести машины выше и меньше — их не путать: 300 с про is_live,
        # 3600 с про «мертвий GPS» маршрута (хрестик ❌ на чипе).
        "route_fallback_max_age_seconds": ROUTE_FALLBACK_MAX_AGE_SECONDS,
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
    source: Optional[str] = Query(
        None,
        description="Источник парка на один запрос: auto | gps | sim "
                    "(по умолчанию — PARK_SOURCE стенда)",
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
    (merge_fleet, §3): маршрут, на котором есть реальная машина вне депо с
    треком не старше часа (ROUTE_FALLBACK_MAX_AGE_SECONDS, §3.6), целиком
    берётся из трекера, остальные — из симулятора. В этом случае
    source="mixed", а каждая машина помечена своим источником в поле source —
    UI рисует бейдж «SIM», чтобы виртуальные машины не выдавались за живой
    GPS. Маршруты без реального GPS получают на чипе хрестик ❌ (в ответе у
    них нет ни одной машины с source="real"); окно, по которому принято это
    решение, отдаётся полем route_fallback_max_age_seconds.

    Параметр source переопределяет режим стенда на один запрос (auto/gps/sim):
    это кнопки «джерело» в панели эмулятора — днём смотрят живой GPS, а
    вечером, когда перевозчик уже в депо, — как отработал симулятор. Тот же
    срез, но потоком (без клиентского поллинга), отдаёт /api/fleet/stream.
    """
    return build_fleet_snapshot(
        source=source,
        plan_now=_parse_query_now(now),
        only_fresh=only_fresh,
        include_depo=include_depo,
        route_ids=_parse_id_list(routes),
        vehicle_types=_parse_type_list(vehicle_types),
    )

# ---------------------------------------------------------------------------
# Поток парка (SSE): сервер сам пушит снимок, клиент держит одно соединение
# ---------------------------------------------------------------------------
#
# Зачем не поллинг: эмулятор в режиме «рух» тянет /api/live каждые 0.9 с, а
# «живий онлайн» днём раньше обновлялся только по клику. SSE (EventSource) —
# одно долгое соединение, сервер отправляет срез в темпе опроса перевозчика
# (5 с), и карта едет сама.
# ⚠️ Прокси обязан отключить буферизацию, иначе кадры копятся в буфере и
# клиент не получает ничего: X-Accel-Buffering: no (заголовок ниже) +
# proxy_buffering off в deploy/nginx-emulator.conf.

FLEET_STREAM_INTERVAL_SECONDS = 5.0
FLEET_STREAM_MIN_INTERVAL = 0.5
FLEET_STREAM_MAX_INTERVAL = 30.0
FLEET_STREAM_RETRY_MS = 5000


def _sse_frame(event: str, payload: Dict[str, Any]) -> str:
    """Кадр SSE: имя события + data одной строкой (JSON без переводов строк)."""
    return "event: " + event + "\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


@app.get("/api/fleet/stream")
async def stream_fleet(
    request: Request,
    source: Optional[str] = Query(
        None, description="Источник парка: auto | gps | sim (по умолчанию PARK_SOURCE)"
    ),
    interval: float = Query(
        FLEET_STREAM_INTERVAL_SECONDS,
        ge=FLEET_STREAM_MIN_INTERVAL,
        le=FLEET_STREAM_MAX_INTERVAL,
        description="Период отправки снимков, сек",
    ),
    only_fresh: bool = Query(
        True, description="Только ТС со свежим GPS-треком (<= 5 мин) и не в депо"
    ),
    include_depo: bool = Query(False, description="Включать ТС, стоящие в депо"),
    routes: Optional[str] = Query(
        None, description="Фильтр по routeId источника: 6_9_12 или 6,9,12"
    ),
    vehicle_types: Optional[str] = Query(None, description="Фильтр по типу ТС: bus, trolley"),
    now: Optional[str] = Query(
        None, description="«Модельное время» ISO-8601: снимки на этот момент (только симулятор)"
    ),
    once: bool = Query(False, description="Отдать один снимок и закрыть поток (curl/тесты)"),
    limit: int = Query(
        0, ge=0, le=1000, description="Сколько кадров отдать (0 — без ограничения)"
    ),
):
    """
    Поток парка: text/event-stream, событие snapshot с тем же JSON, что /api/live.

    Формат кадров:
        retry: 5000                     — один раз в начале (переподключение)
        event: snapshot
        data: {"source": "mixed", "vehicles": [...], "counts": {...}, ...}

    Событие failed приходит, если срез не собрался уже внутри потока (предыдущие
    кадры клиент получил) — имя намеренно не "error": в EventSource под этим
    именем живёт событие разрыва соединения, и клиент не отличил бы одно от
    другого. Ошибки первого снимка (400/503) отдаются обычным JSON ДО начала
    потока — иначе EventSource молча переподключался бы вечно и на экране не
    было бы ни данных, ни причины.
    """
    kwargs: Dict[str, Any] = {
        "source": source,
        "plan_now": _parse_query_now(now),
        "only_fresh": only_fresh,
        "include_depo": include_depo,
        "route_ids": _parse_id_list(routes),
        "vehicle_types": _parse_type_list(vehicle_types),
    }
    # Первый снимок собираем до заголовков: ошибку источника клиент должен
    # увидеть как HTTP-статус, а не как пустой поток.
    first = build_fleet_snapshot(**kwargs)
    frames_total = 1 if once else limit

    async def frames() -> AsyncIterator[str]:
        sent = 0
        payload = first
        try:
            yield f"retry: {FLEET_STREAM_RETRY_MS}\n\n"
            while True:
                yield _sse_frame("snapshot", payload)
                sent += 1
                if frames_total and sent >= frames_total:
                    break
                await asyncio.sleep(interval)
                if await request.is_disconnected():
                    break
                try:
                    # Сбор среза синхронный (индекс в памяти, в сеть не ходим):
                    # уводим его в поток, чтобы не блокировать event loop.
                    payload = await asyncio.to_thread(build_fleet_snapshot, **kwargs)
                except HTTPException as exc:
                    yield _sse_frame("failed", {"status": exc.status_code, "detail": exc.detail})
        finally:
            logger.info("SSE-поток парка завершён: кадров %d.", sent)

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # nginx: не буферизовать этот ответ (иначе SSE не доходит).
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Сленг: псевдонимы и переименование остановок (админка)
# ---------------------------------------------------------------------------

@app.get("/api/manifest")
def get_routes_manifest():
    """
    Whitelist активных маршрутов города (.agents/rules/active_routes.md).

    Нужен фильтру парка в панели эмулятора: кнопки маршрутов должны быть
    стабильными (все 30 автобусных + 8 троллейбусных), а не появляться по мере
    того, как трекер увидит машину на линии. `live_names` — алиасы перевозчика,
    которыми GPS-подписи сопоставляются с внутренним id (например «3/3a» →
    trolley:3); наружу они отдаются как справка, не как id.
    """
    manifest = app_state.get("routes_manifest")
    if manifest is None:
        try:
            manifest = json.loads(ROUTES_MANIFEST_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=503, detail=f"routes_manifest.json не прочитан: {exc}")
    bus = manifest.get("bus") or []
    trolley = manifest.get("trolley") or []
    return {
        "version": manifest.get("version"),
        "source": ROUTES_MANIFEST_PATH.name,
        "counts": {"bus": len(bus), "trolley": len(trolley), "total": len(bus) + len(trolley)},
        "bus": bus,
        "trolley": trolley,
    }


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


@app.get("/api/tts")
def api_tts(text: str, speaker: str = 'daniel'):
    """Синтез речи для эмулятора (ElevenLabs -> Azure -> OpenAI, см. tts_layer).

    Обычный `def`, а НЕ `async def`: синтез — блокирующий внешний HTTP-запрос к
    облаку. В корутине он замораживал бы весь event loop — включая
    /api/fleet/stream и /api/plan. FastAPI уводит обычные `def` в threadpool, как
    и остальные тяжёлые эндпоинты проекта (например /api/plan).

    `mime` отдаём наружу: облака возвращают mp3. Поля `engine` и `cache`
    показывают, какой движок ответил и попали ли мы в дисковый кэш озвучки
    (data/tts_cache/) — по ним видно экономию платных символов.
    """
    audio_base64, mime, meta = tts_layer.generate_tts(text, speaker)
    if not audio_base64:
        raise HTTPException(status_code=503, detail="TTS engine not available")
    return {
        "audio_base64": audio_base64,
        "mime": mime,
        "engine": meta.get("engine"),
        "cache": meta.get("cache"),
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
