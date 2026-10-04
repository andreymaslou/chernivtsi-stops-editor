"""Текстова нормалізація чисел для озвучки (uk_textnorm).

Табличні тести: кожна форма, яку читає голос, має бути впевненою
українською — без reliance на TN рухача TTS (ElevenLabs/OpenAI
нормалізують цифри по-своєму, часто без узгодження роду).
"""

import uk_textnorm as tn


def test_cardinal_masculine():
    assert tn.cardinal(0) == "нуль"
    assert tn.cardinal(1) == "один"
    assert tn.cardinal(2) == "два"
    assert tn.cardinal(5) == "п'ять"
    assert tn.cardinal(9) == "дев'ять"
    assert tn.cardinal(10) == "десять"
    assert tn.cardinal(11) == "одинадцять"
    assert tn.cardinal(20) == "двадцять"
    assert tn.cardinal(21) == "двадцять один"
    assert tn.cardinal(32) == "тридцять два"
    assert tn.cardinal(41) == "сорок один"
    assert tn.cardinal(99) == "дев'яносто дев'ять"
    assert tn.cardinal(100) == "сто"
    assert tn.cardinal(101) == "сто один"
    assert tn.cardinal(112) == "сто дванадцять"
    assert tn.cardinal(200) == "двісті"
    assert tn.cardinal(999) == "дев'ятсот дев'яносто дев'ять"


def test_cardinal_feminine_gender_agreement():
    """«два/дві», «один/одна» узгоджуються з родом численого іменника."""
    assert tn.cardinal(1, "f") == "одна"
    assert tn.cardinal(2, "f") == "дві"
    assert tn.cardinal(3, "f") == "три"  # незмінно
    assert tn.cardinal(32, "f") == "тридцять дві"
    assert tn.cardinal(41, "f") == "сорок одна"
    assert tn.cardinal(21, "f") == "двадцять одна"


def test_cardinal_thousands():
    assert tn.cardinal(1000) == "одна тисяча"
    assert tn.cardinal(2000) == "дві тисячі"
    assert tn.cardinal(5000) == "п'ять тисяч"
    assert tn.cardinal(1001) == "одна тисяча один"
    assert tn.cardinal(1234) == "одна тисяча двісті тридцять чотири"


def test_cardinal_negative():
    assert tn.cardinal(-5) == "мінус п'ять"


def test_plural_forms():
    assert tn.plural(1, "хвилина", "хвилини", "хвилин") == "хвилина"
    assert tn.plural(2, "хвилина", "хвилини", "хвилин") == "хвилини"
    assert tn.plural(4, "хвилина", "хвилини", "хвилин") == "хвилини"
    assert tn.plural(5, "хвилина", "хвилини", "хвилин") == "хвилин"
    assert tn.plural(11, "хвилина", "хвилини", "хвилин") == "хвилин"
    assert tn.plural(12, "хвилина", "хвилини", "хвилин") == "хвилин"
    assert tn.plural(21, "хвилина", "хвилини", "хвилин") == "хвилина"
    assert tn.plural(22, "хвилина", "хвилини", "хвилин") == "хвилини"


def test_minutes():
    assert tn.minutes(1) == "одна хвилина"
    assert tn.minutes(2) == "дві хвилини"
    assert tn.minutes(5) == "п'ять хвилин"
    assert tn.minutes(21) == "двадцять одна хвилина"
    assert tn.minutes(32) == "тридцять дві хвилини"
    assert tn.minutes(34) == "тридцять чотири хвилини"
    assert tn.minutes(112) == "сто дванадцять хвилин"


def test_money():
    assert tn.money(1) == "одна гривня"
    assert tn.money(2) == "дві гривні"
    assert tn.money(5) == "п'ять гривень"
    assert tn.money(20) == "двадцять гривень"
    assert tn.money(40) == "сорок гривень"


def test_route_number():
    assert tn.route_number("9") == "дев'ять"
    assert tn.route_number("42") == "сорок два"
    assert tn.route_number("9A") == "дев'ять а"
    assert tn.route_number("15K") == "п'ятнадцять к"
    assert tn.route_number("10A") == "десять а"
    assert tn.route_number("") == ""
    assert tn.route_number("abc") == "abc"


def test_clock():
    assert tn.clock(14, 41) == "чотирнадцята сорок одна"
    assert tn.clock(9, 5) == "дев'ята п'ять"
    assert tn.clock(23, 0) == "двадцять третя"
    assert tn.clock(0, 0) == "нульова"
    assert tn.clock(21, 15) == "двадцять перша п'ятнадцять"


def test_expand_digits_abbreviations():
    assert tn.expand_digits("32хв") == "тридцять дві хвилини"
    assert tn.expand_digits("20грн") == "двадцять гривень"
    assert tn.expand_digits("14:41") == "чотирнадцята сорок одна"


def test_expand_digits_standalone_numbers():
    assert tn.expand_digits("номер 9") == "номер дев'ять"
    assert tn.expand_digits("маршрути 9 та 10") == "маршрути дев'ять та десять"


def test_expand_digits_keeps_route_labels():
    """«8A» — лейбл маршруту: буква поруч, не чіпаємо (normalizer main.py)."""
    assert tn.expand_digits("8A") == "8A"


def test_expand_digits_clean_text_untouched():
    text = "Поїздка автобусом номер дев'ять, приблизно тридцять чотири хвилини."
    assert tn.expand_digits(text) == text


def test_expand_digits_empty():
    assert tn.expand_digits("") == ""


def test_digits_left():
    assert tn.digits_left("чисто слова") == []
    assert tn.digits_left("номер 9") == ["9"]
    assert tn.digits_left("8A") == []
