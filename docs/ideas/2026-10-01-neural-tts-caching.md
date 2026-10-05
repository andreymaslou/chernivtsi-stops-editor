# Идея: Серверная нейронная озвучка (Neural TTS) с кешированием

> **Статус: реализовано 2026-10-03.** Движок — ElevenLabs (`eleven_multilingual_v2`),
> резерв — Azure Speech / OpenAI `tts-1`; локальный Silero удалён. Кэш —
> **Вариант A** (хеш всей фразы → файл) в `data/tts_cache/<engine>/<sha1>.{mp3,json}`;
> ключ = `sha1(текст + профиль голоса + модель)`. Попадания видно в логах
> контейнера (`TTS: cache HIT` / `... MISS -> сгенерировано`) и в полях
> `engine`/`cache` ответа `/api/tts`. Кэш «по кусочкам» (числа/цены/шаблоны,
> ниже в «Механике») — **следующий шаг**, если hit-rate на сводках планов
> окажется низким. Реализация — `tts_layer.py`.

## Проблема
Текущая реализация голосовых ответов использует встроенный в браузер `window.speechSynthesis` (Web Speech API).
- На многих Android-устройствах по умолчанию может быть установлен русский язык системы, из-за чего украинский текст (`uk-UA`) читается с сильным акцентом или искажениями.
- При "холодном старте" (если системный TTS выгружен из памяти) возникает задержка в 1-2 секунды перед началом озвучки.
- Качество встроенных голосов сильно разнится в зависимости от ОС (iOS vs Android) и вендора (Google TTS vs Samsung TTS).

## Решение
Использовать серверное API (например, OpenAI TTS `tts-1`) для генерации красивой, естественной речи и отдавать на фронтенд готовые `.mp3` файлы.

### Механика (Lazy Caching)
Так как тексты для карточек маршрутов формируются детерминированно из ограниченного набора комбинаций (маршрут + остановка А + остановка Б):
1. Фронтенд больше не использует `speechSynthesis`, а играет полученный от сервера аудиофайл через `new Audio(url).play()`.
2. Когда бекенд формирует ответ для маршрута, он вычисляет хеш (например, MD5) от строки `speech.text`.
3. Бекенд проверяет, есть ли на диске/в сторадже уже сгенерированный файл `cache/{hash}.mp3`.
4. Если **есть**: моментально прикрепляет URL к ответу. Затраты на API — $0.
5. Если **нет**: делает запрос к OpenAI TTS, скачивает MP3, сохраняет его в кеш и отдает пользователю.
6. Самые частые статические фразы ("Маршрут не знайдено", "Запит поза темою") генерируются единоразово и зашиваются в статику.

## Преимущества
1. **Премиальный UX**: Идеальный дикторский голос, одинаковый на всех устройствах, независимо от системного языка.
2. **Моментальный отклик**: Аудиофайлы весят ~20-40 КБ и загружаются быстрее, чем успевает проиграть UI-анимация. Отсутствует лаг "холодного старта" системного TTS на Android.
3. **Экономия**: За счет высокой повторяемости маршрутов, после первоначального "прогрева" кеша количество платных обращений к API стремится к нулю.
## Резерв: Azure Speech (Free F0)

Основной движок — ElevenLabs, но он один и платный: если ключ отвалится
(права/квота/сеть), озвучка молча уйдёт на системный голос браузера. Второй движок —
Azure Speech с **родными украинскими** нейронными голосами `uk-UA-OstapNeural`
(муж.) и `uk-UA-PolinaNeural` (жен.): профили daniel/adam/george → Ostap,
alice/sarah → Polina.

Тариф **Free (F0)**: **0.5 млн символов в месяц** бесплатно, дальше Azure отвечает
`429` — автоматического перехода на платный S0 нет, поэтому счёт молча не растёт.
С кэшем Варианта A повторов в этот лимит почти не попадает.

### Как получить ключ

1. Аккаунт: <https://azure.microsoft.com/free/>.
2. Создать ресурс Speech:
   <https://portal.azure.com/#create/Microsoft.CognitiveServicesSpeechServices>
   — Pricing tier **Free F0**, Region **North Europe** (рабочий вариант;
   «своего» региона для uk-UA у Azure нет). West Europe может отказать новым
   подпискам с `LocationIneligible` — тогда берём North Europe, Sweden Central,
   Germany West Central или Poland Central. Голоса uk-UA есть во всех регионах.
3. Keys and Endpoint:
   <https://portal.azure.com/#view/Microsoft_Azure_ProjectOxford/CognitiveServicesHub/~/SpeechServices>
   → **KEY 1** в `AZURE_SPEECH_KEY`, **Location/Region** (`northeurope`) в
   `AZURE_SPEECH_REGION`. Нужен именно регион, не Endpoint: ключ регион-скоупный,
   чужой регион даёт `401` (код умеет вырезать регион из Endpoint, но лучше сразу
   копировать правильную строку).
4. Прослушать голоса без кода: <https://speech.microsoft.com/portal/voicegallery>.

### Справочники

| Что | Ссылка |
| --- | --- |
| REST text-to-speech (заголовки, SSML, форматы) | <https://learn.microsoft.com/azure/ai-services/speech-service/rest-text-to-speech> |
| Языки и голоса (uk-UA) | <https://learn.microsoft.com/azure/ai-services/speech-service/language-support?tabs=tts> |
| Квоты и лимиты (F0 не повышается) | <https://learn.microsoft.com/azure/ai-services/speech-service/speech-services-quotas-and-limits> |
| Регионы | <https://learn.microsoft.com/azure/ai-services/speech-service/regions> |
| Цены (F0 = 0.5 млн символов/мес, S0 — по прайсу) | <https://azure.microsoft.com/pricing/details/cognitive-services/speech-services/> |

### Проверка

```powershell
# локально (ключи из .env)
python _tts_azure_check.py

# на сервере (ключи берутся из env_file уже запущенного контейнера)
scp _tts_azure_check.py root@169.58.82.105:/tmp/
ssh root@169.58.82.105 "docker cp /tmp/_tts_azure_check.py api_router:/tmp/ && docker exec api_router python /tmp/_tts_azure_check.py"
```

Скрипт печатает маску ключа, список голосов `uk-UA` этого региона и синтезирует
пробную фразу в `data/tts_samples/azure_*.mp3` — та же фраза, что у сэмплов
ElevenLabs, поэтому голоса сравниваются «на слух» один в один.
