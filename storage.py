"""
Общее файловое хранилище эмулятора.

Всё изменяемое живёт в одной папке DATA_DIR:
    data/slang_overrides.json      — сленговые псевдонимы и переименования остановок
    data/feedback/<дата>/<id>.json — по одному JSON на жалобу
    data/feedback/<дата>.jsonl     — те же жалобы потоком (удобно смотреть списком)

Почему так: в Docker папка монтируется томом (./data:/app/data), поэтому
правки сленга и жалобы переживают пересборку контейнера. Путь можно
переопределить переменной окружения TRANSGPS_DATA_DIR.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger("transgps-storage")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("TRANSGPS_DATA_DIR", str(BASE_DIR / "data")))

FEEDBACK_DIR = DATA_DIR / "feedback"
SLANG_PATH = DATA_DIR / "slang_overrides.json"


def ensure_data_dir() -> Path:
    """Создаёт папки данных, если их ещё нет (первый запуск или чистый том)."""
    FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


def read_json(path: Path, default: Any) -> Any:
    """Читает JSON, возвращая default, если файла нет или он битый."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Хранилище: %s не прочитан (%s) — беру значение по умолчанию", path, exc)
        return default


def write_json_atomic(path: Path, payload: Any) -> Path:
    """
    Пишет JSON атомарно: сначала .tmp, затем подмена файла.

    Без этого в момент записи (например, при правке из браузера) можно
    получить обрезанный JSON и потерять все псевдонимы сразу.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp_path.replace(path)
    return path


def append_jsonl(path: Path, payload: Dict[str, Any]) -> Path:
    """Дописывает одну строку в JSONL-поток (журнал жалоб)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return path
