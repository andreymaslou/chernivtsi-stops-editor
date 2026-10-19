# VEO 3.1 — SUPER PROMPTER & PRO DIRECTOR (Мастер-скилл)

## 🏛️ 6-СЛОЙНАЯ АРХИТЕКТУРА ПРОМПТА (по убыванию внимания модели)
Первые 25–30 слов несут до 60% веса. Строгий порядок блоков:

```
[СЛОЙ 1: CAMERA & OPTICS]        → Тип кадра, оптика, угол, движение камеры. СРАЗУ в начале.
[СЛОЙ 2: VISUAL LOCK & BOUNDS]   → Жёсткая привязка к Start/End кадрам. Что заморожено на 100%.
[СЛОЙ 3: KINETIC CHOREOGRAPHY]   → Посекундные действия [00:00-00:03] с физическими глаголами.
[СЛОЙ 4: LIGHTING & ATMOSPHERE]  → Кельвины, отражения, объёмный свет.
[СЛОЙ 5: PHYSICS & MATERIALS]    → Гравитация, динамика ткани, инерция массы, ambient occlusion.
[СЛОЙ 6: NATIVE SOUNDSCAPE]      → SFX и аудио в самом конце.
```

## 🛡️ 7 ЗАЩИТНЫХ ЭКРАНОВ ОТ ГАЛЛЮЦИНАЦИЙ

| Экран | Правило | Пример |
|-------|---------|--------|
| **Strict Visual Lock** | Архитектура, мебель, материалы, свет — 100% идентичны референсам | `Strict visual lock to Start Frame and End Frame: cream walls, arched windows, marble floor remain 100% stationary.` |
| **Anti-Crossfade** | Запрет `transforms`, `becomes`, `magically appears`. Только физические глаголы | `workers physically carry in the sofa` |
| **I2V Noise Protection** | Не описывай статику, которая уже на фото (зерно/мерцание) | Не пиши `cream walls` если они на Start Frame |
| **Positive Absence** | Нет отрицаний-существительных (активируют объект). Позитивная пустота | `completely empty open hall` вместо `no furniture` |
| **Axis Lock X=332/540** | Оба кадра приведены к одной вертикальной оси (720px → X=332, 1080px → X=540) | `Central window mullion locked at X=540.` |
| **No Quote Bug** | Речь без кавычек — только через двоеточие | `She speaks in a calm voice: ...` |
## ⏱️ ТАЙМКОД-ХОРЕОГРАФИЯ (8 сек = 3 беата)

Всегда разбивай на 3 блока по 2–3 сек:

| Беат | Таймкод | Назначение | Формулировка |
|------|---------|------------|--------------|
| Beat 1 | `[00:00-00:02]` | Вход, начало, установка | `[ПЕРСОНАЖ] [ГЛАГОЛ] [ОБЪЕКТ] [МЕСТО]` |
| Beat 2 | `[00:02-00:06]` | Пик действия, монтаж | `[ПЕРСОНАЖ] [ГЛАГОЛ] [ОБЪЕКТ] [РЕЗУЛЬТАТ]` |
| Beat 3 | `[00:06-00:08]` | Завершение, выход, финал | `[ПЕРСОНАЖ] [ГЛАГОЛ] [ФИНАЛЬНОЕ СОСТОЯНИЕ]` |

**Физические глаголы (разрешено):** `carry`, `lower`, `roll`, `mount`, `press`, `glide`, `slide`, `align`, `walk`, `sit`, `enter`, `exit`, `adjust`.

**Запрещено:** `transforms into`, `becomes`, `changes to`, `magically appears`, `appears out of nowhere`.

## 🎥 ОПЕРАТОРСКИЙ СЛОВАРЬ (первые 25–30 слов)

| Задача | Операторская формулировка Veo 3.1 |
|--------|-----------------------------------|
| Неподвижная камера | `Static locked-off tripod vertical shot, zero camera shake, fixed eye-level perspective.` |
| Оптика интерьера | `24mm wide-angle architectural lens, straight verticals.` |
| Привязка референса | `Strict visual lock to Start Frame and End Frame: ...` |
| Медленный наезд | `Slow cinematic dolly-in push toward subject at constant speed.` |
## ⚡ ФИЗИЧЕСКИЕ ТОКЕНЫ (обязательны в Слое 5)
`physically accurate gravity and cloth dynamics, proper weight and momentum conservation, ambient occlusion in fabric folds.`

## 🔊 ЗВУК (Слой 6, строго в конце)
```
SFX: [конкретные физические звуки, синхронные с действием].
STRICTLY NO background music, NO robotic hum, NO spoken subtitles, NO on-screen text.
```

## 🎯 ПРОТОКОЛ FAST → QUALITY

| Режим | Использование |
|-------|--------------|
| `veo-3.1-fast` | Итерации, черновики, тест промпта (< 60 сек, ~в 2 раза дешевле) |
| `veo-3.1-generate-preview` | Финальные клипы, сложная ткань/физика (~90+ сек, базовая цена) |

**Правило:** Один и тот же текст промпта для обоих режимов. Менять можно только детали физики, если Fast сдал.

## ✅ ЧЕК-ЛИСТ ПЕРЕД ЗАПУСКОМ (60 секунд)
- [ ] Первые 3 слова = кадр + оптика (Слой 1)
- [ ] `Strict visual lock` конкретен и Namens все статические элементы
- [ ] Все действия — физические глаголы с таймкодами `[00:00-00:XX]`
- [ ] Нет повторного описания статики с фото (I2V Noise Protection)
- [ ] Нет кавычек, нет отрицаний-существительных (Positive Absence)
- [ ] SFX-блок в самом конце, звук синхронен с действием
- [ ] Emotion: одна метка на клип
- [ ] Ось X выровнена (X=332 для 720p, X=540 для 1080p)
- [ ] Длина промпта 80–180 слов (Fast оптимум)

## 📽️ ПРИМЕНЕНИЕ К СЦЕНЕ 4 (видео Movers_arranging_rug_and_sofa_20261019030529.mp4)

**Фактологический разбор клипа (8 сек, 720×1280, 24 fps):**
- t=0–2с: зал пуст, ковёр уже лежит на мраморе
- t=3–7с: трое рабочих в серой форме несут изогнутый диван букле слева и ставят его на ковёр у правой стены
- t=7–8с: поправляют подушки, уходят налево, остаётся пустой зал с диваном

**Start Frame:** пустой зал, ковёр расстелен, дивана нет (убрать из кадра)
**End Frame:** изогнутый диван букле на ковре, рабочие ушли, пусто

### Финальный промпт v7 (валидатор: 100/100, 147 слов)

```
Static locked-off tripod vertical shot, fixed eye-level angle, 24mm architectural lens, arched window centered on axis X=540. Strict visual lock to Start and End Frame: cream walls, ceiling coffers, sphere chandelier, black-framed windows, oak slat wall with LED lines, side drapes and marble reflections remain 100% stationary. [00:00-00:02] The greige rug lies flat on the marble floor; nothing moves yet. [00:02-00:07] Three movers in grey uniforms carry the complete curved cream boucle sofa into the hall from the left and set it down on the rug along the slat wall. [00:07-00:08] They adjust the cushions, step back, and walk out to the left, leaving the hall empty with the sofa centered on the rug. physically accurate gravity and weight distribution, fabric dynamics, soft ambient occlusion in couch folds. Photorealistic bright daylight. SFX: sofa being set down on rug, faint footsteps, fabric rustle, receding footsteps. Emotion: Calm, precise.
```

**Что применено из всех скиллов:**
- Слой 1: камера + оптика + ось X=540 в первых 12 словах ✅
- Слой 2: Strict Visual Lock на все архитектурные элементы ✅
- Слой 3: 3 беата с физическими глаголами (carry → set down → adjust → exit) ✅
- Слой 4: bright daylight, Photorealistic ✅
- Слой 5: gravity, weight distribution, fabric dynamics, ambient occlusion ✅
- Слой 6: SFX блок в конце, без кавычек ✅
- Positive Absence: `leaving the hall empty` вместо `no people` ✅
- I2V Noise Protection: статика не описана повторно ✅
- Entity Persistence: трое рабочих видны от входа до выхода ✅
- Conservation of Props: только диван перемещается, ничего не спавнится ✅
- Spatial Grid Blocking: from the left → along the slat wall (center-right zone) ✅
- Causal Vector Chain: carry → set down → adjust → exit ✅
- Чек-лист валидатора: 100/100, 147 слов ✅

**Запуск:** Fast mode для итерации → Quality 1080p для финала. Промпт не меняется между режимами.

## 📁 Файлы проекта
- Промпт Scene 4: `work/scene_4_prompt.md`
- Валидатор: `validate_veo_prompt.py`
- Проверка сцены: `check_scene_4.py`
- Скиллы: `.agents/skills/veo_31_*/SKILL.md`
- Сценарий: `SCENARIO.md`
- Правила производства: `.agents/rules/veo_production_rules.md`
