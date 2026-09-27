# 🚀 Деплой эмулятора на VPS (браузер, не локалка)

> **Цель:** открыть эмулятор и админку с телефона в любой момент, без своего компьютера.
> **Где:** тот же VPS Contabo `169.58.82.105`, где уже крутится digital-signage (порт 3001, PM2).
> **Конфликта нет:** контейнер эмулятора слушает `8000`, наружу отдаём через nginx с логином и HTTPS.

---

## 1. Что появится после деплоя

| Адрес | Что это |
|---|---|
| `https://<домен>/ui/editor.html` | **объединённая страница**: редактор маршрутов (EasyWay) + панель AI-эмулятора справа на одной карте |
| `https://<домен>/ui/emulator.html` | эмулятор отдельной страницей (оставлен: на него нацелен `tools/ui/check_emulator.js`) |
| `https://<домен>/ui/admin.html` | админка: **сленговый редактор** + логи жалоб |
| `https://<домен>/docs` | автогенерация API (Swagger) |
| `https://<домен>/api/stops`, `/api/slang`, `/api/feedback`, `/api/route`, `/api/live`, `/api/manifest`, `/api/fleet/stream` | API (поток `/api/fleet/stream` — SSE: живой парк без клиентского поллинга) |

Один контейнер отдаёт и API, и UI (статику берёт из папки `web/`).
**Внимание:** наружу отдаётся только `web/` — корень репозитория не публикуется, иначе `.env` оказался бы в открытом доступе.

---

## 2. Установка (выполнять на сервере, по шагам)

```bash
# 1. Код
cd /opt                                  # или ваша папка проектов
git clone <repo-url> transgps-emulator   # если ещё не клонирован
cd transgps-emulator
git pull

# 2. Переменные окружения (ключ LLM; без него /api/route вернёт 502)
cp .env.example .env 2>/dev/null || nano .env
#   OPENROUTER_API_KEY=...
#   OPENROUTER_MODEL=qwen/qwen-2.5-72b-instruct

# 3. Папка данных: сленг и жалобы (монтируется томом, переживает пересборку)
mkdir -p data

# 4. Запуск
docker compose up -d --build
docker compose logs -f api_router        # ждём «Живой слой запущен...»
curl -s localhost:8000/health            # {"status":"ok", ...}
```

### nginx + логин + HTTPS (обязательно)

Контейнер `api_router` слушает только `127.0.0.1:8000` (см. `docker-compose.yml`),
поэтому наружу приложение отдаёт **только nginx** — уже с логином и HTTPS.
Один скрипт делает всё сам (пакеты → htpasswd → nginx → certbot → автопродление):

```bash
cd /opt/transgps-emulator        # корень репозитория
git pull
sudo bash deploy/setup-https.sh <домен> <email>     # напр. emulator.transgps.cv.ua admin@transgps.cv.ua
```

Дальше открывай `https://<домен>/` (редирект на `/ui/emulator.html`).

**Полная инструкция** (ручной путь, проверки, частые ошибки, firewall):
[`deploy/README_HTTPS.md`](deploy/README_HTTPS.md).

Нет домена? HTTPS нельзя (Let's Encrypt требует домен), а «голый» порт `8000`
теперь закрыт снаружи специально. Временный доступ без HTTPS возможен только
с самого сервера: `curl -s http://127.0.0.1:8000/health`.

---

## 3. Обновление версии

```bash
cd /opt/transgps-emulator
git pull
docker compose up -d --build
```

Правки сленга и логи жалоб при этом **не теряются** — они лежат в `./data` на хосте.

### Если версия первая «с потоком парка» — обновите конфиг nginx

Поток парка (`/api/fleet/stream`, SSE) обязан идти без буферизации: с ней nginx
копит кадры, и браузер не получает ни одного — снаружи это выглядит как «карта
не обновляется». В конфиге репозитория для этого есть отдельный
`location = /api/fleet/stream` с `proxy_buffering off`.

⚠️ **Не копируйте `deploy/nginx-emulator.conf` на сервер целиком** (`cp`): в
репозитории это шаблон с доменом `emulator.example.com` и путями к его
сертификату. На VPS домен другой (`169.58.82.105.nip.io` или свой) — копия
затрёт `server_name` и `ssl_certificate`, и HTTPS перестанет подниматься
(`nginx -t` упадёт: нет `/etc/letsencrypt/live/emulator.example.com/…`).
Правильно — подставить домен из ЖИВОГО конфига (скрипт `deploy/setup-https.sh`
делает то же самое, строки 81 и 111):

```bash
cd /opt/transgps-emulator
git pull

# 1. Домен — из уже стоящего конфига (менять руками ничего не нужно)
DOMAIN=$(sudo grep -m1 -oP 'server_name\s+\K[^;]+' /etc/nginx/sites-available/transgps)
echo "домен стенда: ${DOMAIN}"

# 2. Бэкап + подстановка домена в свежий шаблон
sudo cp /etc/nginx/sites-available/transgps /etc/nginx/sites-available/transgps.bak-$(date +%F)
sudo sed "s/emulator\.example\.com/${DOMAIN}/g" deploy/nginx-emulator.conf \
  | sudo tee /etc/nginx/sites-available/transgps >/dev/null

# 3. Проверка и мягкая перезагрузка (без простоя)
sudo nginx -t && sudo systemctl reload nginx
```

Идемпотентная альтернатива одной командой (сама подставит домен, перезапустит
nginx и проверит продление сертификата): `sudo bash deploy/setup-https.sh ${DOMAIN}`.

Проверка с любого компьютера (пароль Basic Auth — свой):

```bash
curl -N -u admin:<пароль> "https://<домен>/api/fleet/stream?once=true"  # один кадр SSE
curl -u  admin:<пароль> "https://<домен>/api/manifest" | head -c 300    # 38 маршрутов
```

### Проверка, что поток реально не буферизуется

`once=true` даёт один кадр и закрывает соединение — по нему буферизацию **не
видно** (при закрытии nginx обязан отдать всё). Признак «карта не обновляется»
ловится только счётчиком кадров за время:

```bash
# За 5 с при interval=1 ждём >= 3 кадра. 0 при HTTP 200 = буферизация осталась.
curl -s -u admin:<пароль> -N --max-time 5 \
  "https://<домен>/api/fleet/stream?source=sim&interval=1" | grep -c '^event: snapshot'
```

`source=sim` берём намеренно: симулятор не зависит от трекера перевозчика и
отдаёт машины даже вечером, когда живой парк стоит в депо. `--max-time`
завершает curl сам (код выхода `28` — это норма, на счёт не влияет), поэтому
обёртка `timeout 3 curl …` тут не нужна: с ней bash печатает `Terminated`
прямо в вывод пайпа и результат теряется.

Если счётчик `0` при статусе `200` — проверьте, не перекрывает ли этот
location другой конфиг: `sudo nginx -T | grep -n 'fleet/stream'` (файлы из
`sites-enabled/` и `conf.d/`).

#### Пароля под рукой нет: временный юзер для замера

Поток за Basic Auth меряется и без пароля владельца — временной записью в тот же
файл, с бэкапом и откатом (проверено на стенде: `5` кадров за 5 с через nginx):

```bash
sudo cp /etc/nginx/.htpasswd_transgps /root/htpasswd.bak
# -b: пароль аргументом, без TTY; -B: bcrypt (как остальные записи файла)
sudo htpasswd -bB /etc/nginx/.htpasswd_transgps _sse_probe 'SseProbe-9f2c'
curl -s -N --max-time 5 -u '_sse_probe:SseProbe-9f2c' \
  "https://<домен>/api/fleet/stream?interval=1" | grep -c '^event: snapshot'   # ждём >= 3
sudo cp /root/htpasswd.bak /etc/nginx/.htpasswd_transgps                       # ВЕРНУТЬ обязательно
wc -l < /etc/nginx/.htpasswd_transgps                       # 1 — как было
grep -c _sse_probe /etc/nginx/.htpasswd_transgps            # 0 — временного нет
```

Перезагрузка nginx после правки файла паролей не нужна: он читается на каждый
запрос. Откат — именно `cp` бэкапа, а не `htpasswd -D`: так файл побитово
прежний, и пароль владельца не задевается.

#### Грабли: живой конфиг на сервере может быть старой ревизии

`location = /api/fleet/stream` есть только в `deploy/nginx-emulator.conf`
(шаблон) — на стенде его ставит отдельный шаг из §3. Если на VPS обновляли
только образ (`docker compose up -d --build`), nginx остаётся **доревизионным**,
и SSE идёт на одном `X-Accel-Buffering: no` от приложения, без страховки.
Признак: `sudo nginx -T | grep -c 'proxy_buffering off'` → `0` (в свежем файле
счётчик `2`: директива + упоминание в комментарии). Смотреть надо файл, а не
только вывод `-T`:

```bash
grep -n 'fleet/stream' /etc/nginx/sites-available/transgps      # пусто = блока нет
diff <(sed 's|/etc/letsencrypt/live/[^/]*|LIVE|g' /etc/nginx/sites-available/transgps) \
     <(sed 's|/etc/letsencrypt/live/[^/]*|LIVE|g; s/server_name .*;/server_name D;/' deploy/nginx-emulator.conf)
# ожидаемые отличия: комментарии + сам блок location = /api/fleet/stream
```

Обновлять — подстановкой домена (§3, шаг «обновите конфиг nginx»), не `cp`
шаблона: иначе затрутся `server_name`/`ssl_certificate`. Перед `cp` бэкап
живого файла, после — `nginx -t` и `systemctl reload nginx` (без простоя).

### Грабли ввода пароля (тут легко потерять полчаса)

* `<пароль>` в примерах — **заполнитель**, а не пароль. Скопированная команда
  с незаменённым плейсхолдером даёт `401`, а разбор ответа в `python3 -c
  "json.load(...)"` — `JSONDecodeError: Expecting value: line 1 column 1`:
  тело ответа — HTML от nginx, а не JSON.
* `read -rsp 'пароль: ' P` **нельзя вставлять одной пастой** с остальными
  командами: `read` читает из того же буфера ввода, что и bash, и «съедает»
  следующую вставленную строку как пароль (та же грабля, что у `htpasswd -c`,
  см. `docs/STATUS.md`). Интерактив — отдельной вставкой, либо так, чтобы
  `read` и проверка стояли в ОДНОЙ строке через `;` (bash разбирает строку
  целиком до выполнения, и остаток `read` уже не достаётся):

  ```bash
  read -rsp 'пароль: ' P; echo "длина: ${#P}"; curl -s -u "admin:$P" -o /dev/null \
    -w 'manifest: %{http_code}\n' "https://<домен>/api/manifest"
  ```
  `длина: 0` — значит ввод не попал в переменную, и дальше смысла нет.
* Смена пароля без «невидимого» двойного ввода (и без `password verification
  error` из-за сбитой раскладки) — `htpasswd -i`, он читает пароль из stdin:

  ```bash
  read -rsp 'новый пароль: ' P; printf '%s\n' "$P" | sudo htpasswd -i /etc/nginx/.htpasswd_transgps admin; unset P
  ```
  После смены пароля браузер продолжит слать старый из кэша Basic Auth и
  получит `401` — проверяйте в приватном окне.

---

## 4. Данные и бэкап

```
./data/slang_overrides.json        — сленговые псевдонимы и переименования остановок
./data/feedback/<дата>/<id>.json   — по одному файлу на жалобу (открыть и починить)
./data/feedback/<дата>.jsonl       — те же жалобы потоком (посмотреть списком, grep)
```

```bash
tar czf ~/transgps-data-$(date +%F).tgz -C /opt/transgps-emulator data   # бэкап
docker compose logs --tail=200 api_router                                # посмотреть логи
```

---

## 5. Что смотреть при разборе жалобы

1. Открыть `https://<домен>/ui/admin.html` → вкладка **«Скарги»**.
2. В карточке видно: фразу пользователя, комментарий, что именно вернул сервер (JSON) и данные клиента.
3. Если проблема в распознавании названия — идём во вкладку **«Сленг»**, находим остановку и дописываем псевдоним.
   Правка действует сразу (сервер пересобирает Locator без перезапуска).
4. Если проблема в маршруте — кейс из жалобы переносится в тест (JSON ответа уже есть).

---

## 6. Ограничения, о которых надо знать

- **Защита настроена через Nginx.** Порт бэкенда в `docker-compose.yml` проброшен безопасно (`127.0.0.1:8000:8000`), так что он не торчит наружу. Внешний доступ идёт только через Nginx с настроенным Basic Auth, что закрывает доступ случайным пользователям. При переносе в React Native эту авторизацию потребуется заменить на токены. В конфиге nginx есть отдельный `location = /api/fleet/stream` с `proxy_buffering off` — без него SSE-поток живого парка до браузера не доходит.
- **Ключ OpenRouter тратится API.** `/api/route` вызывает LLM и расходует `OPENROUTER_API_KEY`, а API доступен любому, кто знает адрес: держите лимит расхода в кабинете OpenRouter (или `docker compose stop`, когда не тестируете).
- **DNS/домен** и `certbot` настраиваются один раз руками; после этого всё обновляется одной командой `docker compose up -d --build`.
- **Логи жалоб пишутся на диск** контейнера/хоста, чистку делать вручную (`data/feedback/`), автопродление и ротация не настроены — на объёмах «сотни мелких JSON» это не проблема.
- **Я (ассистент) не имею SSH-доступа** к VPS: все команды выше выполняются тобой. Если дашь доступ — могу деплоить и обновлять сам.
