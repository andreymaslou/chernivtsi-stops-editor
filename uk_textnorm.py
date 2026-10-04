"""Українська текстова нормалізація (TN) чисел для озвучки (TTS).

Навіщо: рухачі TTS (ElevenLabs, OpenAI) нормалізують цифри по-своєму —
часто за російськими правилами або без узгодження роду («тридцять два
хвилини» замість «тридцять дві хвилини»). Тому числа розкриваються
словами ДО відправки в рухач: збирачі промовного тексту (main.py) і
підстраховка в tts_layer.py використовують тільки цей модуль.

Правила (українська граматика):
    1 хвилина, 2 хвилини, 5 хвилин   — жіночий рід
    1 гривня, 2 гривні, 5 гривень    — жіночий рід
    «два/дві», «один/одна» узгоджуються з родом численого іменника

Карточка UI цифри не втрачає: словами замінюється лише промовний текст.
"""

import re
from typing import List, Tuple

_UNITS_M = ("", "один", "два", "три", "чотири", "п'ять",
            "шість", "сім", "вісім", "дев'ять")
_UNITS_F = ("", "одна", "дві", "три", "чотири", "п'ять",
            "шість", "сім", "вісім", "дев'ять")
_TEENS = ("десять", "одинадцять", "дванадцять", "тринадцять",
          "чотирнадцять", "п'ятнадцять", "шістнадцять",
          "сімнадцять", "вісімнадцять", "дев'ятнадцять")
_TENS = ("", "десять", "двадцять", "тридцять", "сорок", "п'ятдесят",
         "шістдесят", "сімдесят", "вісімдесят", "дев'яносто")
_HUNDREDS = ("", "сто", "двісті", "триста", "чотириста", "п'ятсот",
             "шістсот", "сімсот", "вісімсот", "дев'ятсот")

# Порядкові форми годин (жіночий рід): 14:41 -> «чотирнадцята».
_HOUR_ORDINALS_F = (
    "нульова", "перша", "друга", "третя", "четверта", "п'ята",
    "шоста", "сьома", "восьма", "дев'ята", "десята",
    "одинадцята", "дванадцята", "тринадцята", "чотирнадцята",
    "п'ятнадцята", "шістнадцята", "сімнадцята", "вісімнадцята",
    "дев'ятнадцята", "двадцята",
)
_ORDINAL_UNITS_F = ("", "перша", "друга", "третя", "четверта",
                    "п'ята", "шоста", "сьома", "восьма", "дев'ята")

# Латинські літери суфіксів маршруту (8A, 9A, 10A, 15K) — українською.
_ROUTE_LETTERS = {"a": "а", "b": "б", "c": "в", "d": "д", "e": "е", "k": "к"}

# Підстраховка (expand_digits): скорочення одиниць і час «HH:MM».
_MINUTES_ABBREV = re.compile(r"(?<!\d)(\d+)\s*хв\b", re.IGNORECASE)
_MONEY_ABBREV = re.compile(r"(?<!\d)(\d+)\s*грн\b", re.IGNORECASE)
_CLOCK = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)")
# Одинока цифра: не зчіплена з літерами («8A» — лейбл, його нормалізує
# route_number на рівні збирача промови) і не з цифрами.
_STANDALONE_NUMBER = re.compile(r"(?<!\w)(\d+)(?!\w)")


def plural(number: int, one: str, few: str, many: str) -> str:
    """Українське відмінювання: 1 хвилина, 2 хвилини, 5 хвилин."""
    n = abs(int(number))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _hundreds_tens_units(n: int, gender: str) -> str:
    """Число 1..999 прописью з урахуванням роду одиниць."""
    parts: List[str] = []
    hundreds = n // 100
    if hundreds:
        parts.append(_HUNDREDS[hundreds])
    tens = (n % 100) // 10
    units = n % 10
    if tens == 1:
        parts.append(_TEENS[units])
    else:
        if tens:
            parts.append(_TENS[tens])
        if units:
            table = _UNITS_F if gender == "f" else _UNITS_M
            parts.append(table[units])
    return " ".join(parts)


def cardinal(number: int, gender: str = "m") -> str:
    """Ціле число прописью: 32 -> «тридцять два» / «тридцять дві» (f).

    gender: «m» — чоловічий («два»), «f» — жіночий («дві»).
    """
    n = int(number)
    if n < 0:
        return "мінус " + cardinal(-n, gender)
    if n == 0:
        return "нуль"
    parts: List[str] = []
    thousands = n // 1000
    if thousands:
        # «тисяча» — жіночий рід: 1 тисяча, 2 тисячі, 5 тисяч.
        head = _hundreds_tens_units(thousands % 1000, "f")
        parts.append(f"{head} {plural(thousands, 'тисяча', 'тисячі', 'тисяч')}")
    rest = n % 1000
    if rest:
        parts.append(_hundreds_tens_units(rest, gender))
    return " ".join(part for part in parts if part)


def minutes(number: float) -> str:
    """«34» -> «тридцять чотири хвилини» (хвилина — жіночий рід)."""
    n = int(round(float(number)))
    return f"{cardinal(n, 'f')} {plural(n, 'хвилина', 'хвилини', 'хвилин')}"


def money(number: float) -> str:
    """«20» -> «двадцять гривень» (гривня — жіночий рід)."""
    n = int(round(float(number)))
    return f"{cardinal(n, 'f')} {plural(n, 'гривня', 'гривні', 'гривень')}"


def route_number(label: str) -> str:
    """Номер маршруту для озвучки: «9A» -> «дев'ять а».

    Цифра і літера читаються окремо, а латинська літера (A, B, C, D, E, K —
    так записані 8A, 9A, 10A, 15K) произноситься українською буквою:
    інакше TTS читать «номер 15 k» по-англійськи. Номер — базова форма
    (називний відмінок): «номер дев'ять», «маршрут п'ятнадцять».
    """
    text = str(label or "").strip()
    if not text:
        return ""
    digits = "".join(ch for ch in text if ch.isdigit())
    letters = "".join(ch for ch in text if ch.isalpha())
    parts: List[str] = []
    if digits:
        parts.append(cardinal(int(digits)))
    if letters:
        parts.append(_ROUTE_LETTERS.get(letters.lower(), letters.lower()))
    return " ".join(parts) or text


def clock(hour: int, minute: int) -> str:
    """Час для озвучки: 14:41 -> «чотирнадцята сорок одна».

    Години — порядкове числительство жіночого роду, хвилини —
    кількісне. Профілактична функція: жоден шаблон промови зараз
    не несе годин, але ETA-фрази («прибуття о 14:41») плануються.
    """
    h = int(hour) % 24
    m = int(minute)
    if h <= 20:
        hour_word = _HOUR_ORDINALS_F[h]
    else:  # 21..23
        hour_word = "двадцять " + _ORDINAL_UNITS_F[h - 20]
    if m == 0:
        return hour_word
    return f"{hour_word} {cardinal(m, 'f')}"


def _has_digits(text: str) -> bool:
    return any(ch.isdigit() for ch in text)


def expand_digits(text: str) -> str:
    """Підстраховка: цифри в довільному тексті -> слова.

    Консервативно: «32хв» -> «тридцять дві хвилини», «20грн» ->
    «двадцять гривень», «14:41» -> «чотирнадцята сорок одна»,
    одинокі цифри -> базова форма («номер 9» -> «номер дев'ять»).
    Змішані «8A» не чіпаємо: біля літери це лейбл маршруту, і його
    нормалізує route_number на рівні збирача промови. Використовується
    в tts_layer як остання лінія оборони — якщо цифра потрапила в
    текст мімо шаблонів main.py.
    """
    if not text or not _has_digits(text):
        return text
    text = _MINUTES_ABBREV.sub(lambda m: minutes(int(m.group(1))), text)
    text = _MONEY_ABBREV.sub(lambda m: money(int(m.group(1))), text)
    text = _CLOCK.sub(
        lambda m: clock(int(m.group(1)), int(m.group(2))), text
    )
    text = _STANDALONE_NUMBER.sub(lambda m: cardinal(int(m.group(1))), text)
    return text


def digits_left(text: str) -> List[str]:
    """Залишкові цифрові групи в тексті (для тестів і діагностики)."""
    return [m.group(0) for m in _STANDALONE_NUMBER.finditer(text)]

