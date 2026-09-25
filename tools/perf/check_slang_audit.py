# -*- coding: utf-8 -*-
"""Аудит сленговых псевдонимов: где текст ведёт не на ту остановку.

    python tools/perf/check_slang_audit.py

`Locator` строит плоский индекс «текст → остановка» (родное имя + все псевдонимы)
и берёт лучший скор через `process.extractOne`; при равном скоре побеждает
**первый** индекс. Значит любой текст, встречающийся у нескольких остановок, —
лотерея. Скрипт ищет три класса:

* **A** — один псевдоним приписан двум и более остановкам. Чаще всего это норма
  («соборка» → 4 платформы пл. Соборна, «калинка» → 7 остановок рынка), поэтому
  класс информационный;
* **B** — псевдоним совпадает с **родным именем другой** остановки. Это ошибка:
  запрос уходит не туда, и родное имя не выигрывает. Именно так «дріжзавод»
  уезжал на #79 из-за записи в #80 (см. docs/STATUS.md, грабли №28);
* **C** — дублируются родные имена (для справки: два направления, две платформы).

Инвариант класса B проверяется машинно в `tests/test_slang_overrides_data.py`,
здесь — только отчёт для человека. Файлы: `out/slang_audit.json` (машиночитаемо)
и `out/slang_audit_report.txt` (глазами).
"""
import json
from collections import defaultdict

from common import load_data, out_path, use_utf8_stdout

import main as app_main  # noqa: E402  (common уже добавил корень репозитория в sys.path)


def collect():
    """Собирает три класса конфликтов по данным проекта."""
    _, _, _, stops = load_data()
    by_id = {str(stop["id"]): stop for stop in stops}

    alias_owners = defaultdict(set)
    name_owners = defaultdict(set)
    for stop_id, stop in by_id.items():
        name_owners[(stop.get("name") or "").strip().lower()].add(stop_id)
        for alias in stop.get("aliases") or []:
            alias_owners[alias.strip().lower()].add(stop_id)

    def describe(ids):
        return [{"id": stop_id, "name": by_id[stop_id]["name"]} for stop_id in sorted(ids)]

    class_a = [
        {"text": text, "stops": describe(ids), "is_native_name_too": text in name_owners}
        for text, ids in sorted(alias_owners.items()) if len(ids) >= 2
    ]
    class_b = [
        {"text": text, "alias_of": describe(ids), "native_of": describe(name_owners[text] - ids)}
        for text, ids in sorted(alias_owners.items()) if name_owners.get(text, set()) - ids
    ]
    class_c = [
        {"text": text, "stops": describe(ids)}
        for text, ids in sorted(name_owners.items()) if len(ids) > 1
    ]
    return by_id, class_a, class_b, class_c


def main() -> None:
    use_utf8_stdout()
    by_id, class_a, class_b, class_c = collect()
    overrides = app_main.load_overrides()["stops"]

    report = {
        "merged_stops": len(by_id),
        "local_overrides": len(overrides),
        "local_overrides_with_name": sum(1 for entry in overrides.values() if entry.get("name")),
        "class_a_same_alias_several_stops": class_a,
        "class_b_alias_shadows_other_name": class_b,
        "class_c_native_name_duplicates": class_c,
    }
    out_path("slang_audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    lines = [
        "остановок в справочнике: %d | правок сленга: %d (с переименованием: %d)"
        % (len(by_id), len(overrides), report["local_overrides_with_name"]),
        "",
        "A (%d) — один псевдоним у нескольких остановок:" % len(class_a),
    ]
    for row in class_a:
        lines.append("  «%s» → %s%s" % (
            row["text"],
            ", ".join("#%s %s" % (stop["id"], stop["name"]) for stop in row["stops"]),
            "   [это ещё и чьё-то родное имя]" if row["is_native_name_too"] else "",
        ))
    lines += ["", "B (%d) — псевдоним перекрывает чужое родное имя (ошибка):" % len(class_b)]
    for row in class_b:
        lines.append("  «%s»: псевдоним у %s, а родное имя — у %s" % (
            row["text"],
            ", ".join("#%s" % stop["id"] for stop in row["alias_of"]),
            ", ".join("#%s %s" % (stop["id"], stop["name"]) for stop in row["native_of"]),
        ))
    lines += ["", "C (%d) — родные имена дублируются (для справки, первые 12):" % len(class_c)]
    for row in class_c[:12]:
        lines.append("  «%s» → %s" % (row["text"], ", ".join("#%s" % stop["id"] for stop in row["stops"])))

    out_path("slang_audit_report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("A=%d B=%d C=%d" % (len(class_a), len(class_b), len(class_c)))
    print("отчёт: tools/perf/out/slang_audit_report.txt")
    if class_b:
        print("⚠ класс B не пуст — это ошибка данных, посмотри отчёт")


if __name__ == "__main__":
    main()
