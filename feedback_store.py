"""
Жалобы на ответы эмулятора («це бред») — на разбор.

Кнопка в эмуляторе отправляет сюда всё, что нужно для разбора кейса:
исходную фразу пользователя, то, что показал эмулятор, тип проблемы,
комментарий и контекст клиента (браузер/экран/версия).

Каждая жалоба сохраняется ДВУМЯ способами:
    data/feedback/<дата>/<id>.json — один файл на случай: удобно открыть и починить;
    data/feedback/<дата>.jsonl     — поток за день: удобно смотреть списком и грепать.

Так надёжнее, чем «просто лог»: один кейс можно посмотреть руками, а по
потоку — посчитать, чего больше всего ломается.
"""

import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from storage import FEEDBACK_DIR, append_jsonl, ensure_data_dir, write_json_atomic

logger = logging.getLogger("transgps-feedback")

# Типы проблем: чтобы в статистике было видно, что именно чинить в первую очередь.
FEEDBACK_KINDS = ("nonsense", "wrong_route", "wrong_stop", "wrong_time", "other")

# Человекочитаемые подписи для админки.
KIND_LABELS = {
    "nonsense": "Повна нісенітниця",
    "wrong_route": "Не той маршрут",
    "wrong_stop": "Не та зупинка",
    "wrong_time": "Не той час/очікування",
    "other": "Інше",
}


def _new_id(now: datetime) -> str:
    """ID кейса: читается глазами (дата-время) и не повторяется."""
    return f"{now.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"


def save_feedback(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Сохраняет жалобу и возвращает запись с присвоенным id.

    Ожидаемые поля payload (обязателен только kind):
        kind      — nonsense | wrong_route | wrong_stop | wrong_time | other
        comment   — что именно не так, словами пользователя
        user_text — фраза, которую он написал или сказал
        response  — то, что показал эмулятор (план, остановки, debug)
        client    — браузер, экран, версия приложения
    """
    ensure_data_dir()
    now = datetime.now()

    kind = str(payload.get("kind") or "other").strip().lower()
    if kind not in FEEDBACK_KINDS:
        kind = "other"

    record = {
        "id": str(payload.get("id") or _new_id(now)),
        "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "kind": kind,
        "kind_label": KIND_LABELS.get(kind, kind),
        "status": "new",
        "comment": str(payload.get("comment") or "").strip(),
        "user_text": str(payload.get("user_text") or "").strip(),
        "response": payload.get("response"),
        "client": payload.get("client") or {},
    }

    day = now.strftime("%Y-%m-%d")
    write_json_atomic(FEEDBACK_DIR / day / f"{record['id']}.json", record)
    append_jsonl(FEEDBACK_DIR / f"{day}.jsonl", record)
    logger.info(
        "Жалоба сохранена: %s (%s) — %s",
        record["id"], record["kind"], (record["comment"] or record["user_text"])[:80],
    )
    return record


def _iter_stream(day: str):
    """Построчный обход JSONL за конкретный день (битые строки пропускаем)."""
    stream = FEEDBACK_DIR / f"{day}.jsonl"
    if not stream.exists():
        return
    for line in stream.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def list_feedback(limit: int = 50, day: Optional[str] = None) -> List[Dict[str, Any]]:
    """Возвращает последние жалобы (свежие — первыми)."""
    ensure_data_dir()
    records: List[Dict[str, Any]] = []

    days = [day] if day else sorted((path.stem for path in FEEDBACK_DIR.glob("*.jsonl")), reverse=True)
    for name in days:
        records.extend(_iter_stream(name))
        if len(records) >= limit:
            break

    records.sort(key=lambda record: record.get("created_at", ""), reverse=True)
    return records[:limit]


def feedback_stats() -> Dict[str, Any]:
    """Сводка для админки: сколько жалоб всего и по каким типам."""
    ensure_data_dir()
    by_kind: Dict[str, int] = {}
    total = 0

    days = sorted((path.stem for path in FEEDBACK_DIR.glob("*.jsonl")), reverse=True)
    for name in days:
        for record in _iter_stream(name):
            total += 1
            kind = record.get("kind", "other")
            by_kind[kind] = by_kind.get(kind, 0) + 1

    return {
        "total": total,
        "by_kind": by_kind,
        "by_kind_labeled": {KIND_LABELS.get(k, k): v for k, v in by_kind.items()},
        "days": days[:30],
    }
