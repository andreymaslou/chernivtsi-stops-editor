# 🚀 Деплой эмулятора на VPS (браузер, не локалка)

> **Цель:** открыть эмулятор и админку с телефона в любой момент, без своего компьютера.
> **Где:** тот же VPS Contabo `169.58.82.105`, где уже крутится digital-signage (порт 3001, PM2).
> **Конфликта нет:** контейнер эмулятора слушает `8000`, наружу отдаём через nginx с логином и HTTPS.

---

## 1. Что появится после деплоя

| Адрес | Что это |
|---|---|
| `https://<домен>/ui/emulator.html` | эмулятор: фраза → маршрут на карте + кнопка «🚩 Це бред» |
| `https://<домен>/ui/admin.html` | админка: **сленговый редактор** + логи жалоб |
| `https://<домен>/docs` | автогенерация API (Swagger) |
| `https://<домен>/api/stops`, `/api/slang`, `/api/feedback`, `/api/route`, `/api/live` | API |

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

### nginx + логин + HTTPS

```bash
sudo apt install -y nginx apache2-utils
sudo htpasswd -c /etc/nginx/.htpasswd_transgps admin     # логин/пароль для входа

sudo cp deploy/nginx-emulator.conf /etc/nginx/sites-available/transgps
sudo nano /etc/nginx/sites-available/transgps            # вписать server_name (домен)
sudo ln -s /etc/nginx/sites-available/transgps /etc/nginx/sites-enabled/transgps
sudo nginx -t && sudo systemctl reload nginx

sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d <домен>                          # HTTPS + автопродление
```

Нет домена? Можно быстро проверить по IP: открой `http://169.58.82.105:8000/ui/emulator.html`
(но так нет HTTPS и логина — только как временный тест, не для постоянной работы).

---

## 3. Обновление версии

```bash
cd /opt/transgps-emulator
git pull
docker compose up -d --build
```

Правки сленга и логи жалоб при этом **не теряются** — они лежат в `./data` на хосте.

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

- **Пароль в `index.html` редактора** (`btoa('15201520')`) — это «шторка», а не защита: он виден в исходнике страницы. Настоящая защита — `auth_basic` в nginx (см. выше).
- **DNS/домен** и `certbot` настраиваются один раз руками; после этого всё обновляется одной командой `docker compose up -d --build`.
- **Логи жалоб пишутся на диск** контейнера/хоста, чистку делать вручную (`data/feedback/`), автопродление и ротация не настроены — на объёмах «сотни мелких JSON» это не проблема.
- **Я (ассистент) не имею SSH-доступа** к VPS: все команды выше выполняются тобой. Если дашь доступ — могу деплоить и обновлять сам.
