# 🔐 HTTPS + Basic Auth для эмулятора TransGPS

Инструкция для администратора VPS (`169.58.82.105`): как закрыть «голый» порт
`8000` и открыть приложение наружу только через nginx — по HTTPS и с логином.

> **Итог после настройки:** `http://<IP>:8000` снаружи недоступен (порт забинджен
> на `127.0.0.1`), а приложение живёт по `https://<домен>/` под Basic Auth.
> Nginx обойти нельзя: единственный вход — через него.

## 0.1 Фактическое состояние (сделано 2026-09-23, домен через nip.io)

Стенд **уже работает по HTTPS** — без покупки домена, через публичный DNS-сервис
`nip.io` (любое имя вида `<IP>.nip.io` резолвится в этот IP, и Let's Encrypt
выдаёт на такое имя сертификат):

* адрес: **`https://169.58.82.105.nip.io/`** (`:80` → 301 → `:443`, Basic Auth,
  редирект корня на `/ui/editor.html`);
* бэкенд: `api_router` слушает только `127.0.0.1:8000` (биндинг в
  `docker-compose.yml`), прямой `http://<IP>:8000` снаружи закрыт;
* сертификат: `/etc/letsencrypt/live/169.58.82.105.nip.io/` (ECDSA, Let's
  Encrypt, до 2026-12-22), автопродление — `certbot.timer` + deploy-хук;
* логин/пароль Basic Auth: хеши в `/etc/nginx/.htpasswd_transgps`, значения —
  `/root/https_basic_auth_credentials.txt` на сервере. **В git не коммитим:**
  репозиторий публичный, а адрес стенда и так виден в Certificate Transparency;
* зачем: Web Speech API (голосовой ввод AI-помощника) и Geolocation работают
  только в secure context — по `http://<IP>` браузер блокирует микрофон целиком,
  без системного запроса (`permissions` сразу `denied`, `mediaDevices` вообще
  отсутствует).

### Что выстрелило по пути (пригодится при повторе на другом сервере)

1. **`htpasswd -c` — интерактивная команда.** Если запускать скрипт через
   `ssh … "bash -s" < script`, её промпт читает stdin, то есть скрипт «съедает»
   собственные строки. Лечение: создать файл логинов заранее, неинтерактивно, —
   тогда шаг 3 скрипта увидит готовый файл и пропустит промпт:
   ```bash
   PASS="$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | cut -c1-20)"
   printf 'admin:%s\n' "$(openssl passwd -apr1 "$PASS")" > /etc/nginx/.htpasswd_transgps
   chown root:www-data /etc/nginx/.htpasswd_transgps && chmod 640 /etc/nginx/.htpasswd_transgps
   ```
2. **Права файла логинов.** При `640 root:root` воркеры nginx (под `www-data`)
   не могут его прочитать, и вместо честного `401` приходит **`500`**, а в
   `/var/log/nginx/error.log` — `open() "/etc/nginx/.htpasswd_transgps" failed
   (13: Permission denied)`. Правильно: `chown root:www-data` + `chmod 640`
   (сам `setup-https.sh` это делает — а вот при ручной настройке легко забыть).
3. **Порядок с ufw.** В `setup-https.sh` правило `ufw allow 'Nginx Full'` стоит
   в шаге 8, а `certbot` — в шаге 5. Если ufw активен и порт 80 закрыт,
   сертификат не выпустится. На нашем VPS `ufw` был `inactive` (правил не
   потребовалось), но при включённом ufw сначала `sudo ufw allow 80,443/tcp`.
4. **Проверка ACME до запуска certbot** (быстро снимает риск «упасть в середине
   скрипта»): положить пробный файл в
   `/var/www/certbot/.well-known/acme-challenge/preflight.txt` и запросить его
   снаружи — `curl http://169.58.82.105.nip.io/.well-known/acme-challenge/preflight.txt`.
   Если содержимое отдаётся, `--webroot` пройдёт.
5. **Запускать долгие шаги не в интерактивной ssh-сессии**: при обрыве клиента
   скрипт умирает по SIGPIPE на ближайшем `echo` (пакеты уже поставлены, а
   конфиг — нет). Надёжнее `nohup setsid bash deploy/setup-https.sh … > /root/https_setup.log 2>&1 &`
   и смотреть лог.
6. **nip.io — решение для стенда, не для прода.** Для боевого домена нужна своя
   A-запись на `169.58.82.105` (см. §1) — дальше всё то же: подменить домен в
   конфиге и выпустить сертификат.

## 0. Что за что отвечает (файлы в `deploy/`)

| Файл | Роль |
|---|---|
| `nginx-emulator-http.conf` | **Bootstrap**, только HTTP: нужен один раз, чтобы выпустить сертификат |
| `nginx-emulator.conf` | **Финал**: HTTP→HTTPS редирект + TLS (443) + Basic Auth + reverse proxy |
| `setup-https.sh` | Автоматизация всех шагов ниже (идемпотентный, `sudo bash …`) |
| `README_HTTPS.md` | Этот файл |

Бэкенд — контейнер `api_router` на `127.0.0.1:8000` (см. корневой
`docker-compose.yml`). Nginx стоит **на хосте** и проксирует в него.

## 1. Предпосылки

- Домен с A-записью **на IP этого VPS**: `dig +short <домен>` должен вернуть
  `169.58.82.105`.

  > ⚠️ **Фактическое состояние DNS (проверено 2026-09-20).** Записи
  > `transgps.cv.ua` и `emulator.transgps.cv.ua` указывают **не на наш VPS**, а
  > на `212.58.187.34` — это адрес **мобильного оператора lifecell** (AS34058;
  > обратной DNS-записи нет, флаг «хостинг/датацентр» = false), отвечает только
  > дефолтный nginx на :80 (`403 Forbidden`), порт 443 молчит. Значит: домен
  > **уже занят и ведёт в мобильное/домашнее подключение** (вероятно, интернет
  > друга на lifecell). **Пока запись не перенацелена на `169.58.82.105`,
  > `setup-https.sh` не отработает** — certbot пойдёт проверять домен по чужому
  > адресу и упадёт на ACME-челлендже. Мобильные адреса динамические (часто за
  > NAT) и как адрес хостинга не годятся в принципе: нужен либо перенацеленный
  > поддомен, либо бесплатный `169.58.82.105.nip.io`.
- Код проекта на сервере, контейнер поднят:
  `docker compose up -d --build` в корне репозитория.
- В `docker-compose.yml` порт `8000` должен быть забинджен **на петлю** —
  строка `127.0.0.1:8000:8000`. ⚠️ **Сейчас в репозитории стоит `"8000:8000"`
  (тест-стенд: порт открыт наружу намеренно, см. `docs/STATUS.md`).** Перед
  настройкой nginx обязательно верните `127.0.0.1:8000:8000` и пересоберите
  контейнер (`git pull` → `docker compose up -d --build`): иначе nginx с паролем
  **обходится** прямым заходом на `http://<IP>:8000`, и защита бессмысленна.

## 2. Быстрый путь (рекомендуется): один скрипт

Из **корня репозитория** на сервере:

```bash
git pull
sudo bash deploy/setup-https.sh emulator.transgps.cv.ua admin@transgps.cv.ua
```

Скрипт спросит пароль для Basic Auth (логин по умолчанию `admin`), поставит
пакеты, поднимет nginx, выпустит сертификат Let's Encrypt, включит финальный
конфиг, поставит авто-перезагрузку nginx при продлении и проверит результат.
Финальный ответ должен быть: `https://<домен>/health -> 401` — это значит,
что HTTPS работает и Basic Auth закрывает доступ.

Домен без e-mail (уведомления о проблемах приходить не будут):

```bash
sudo bash deploy/setup-https.sh emulator.transgps.cv.ua
```

## 3. Ручной путь (шаг за шагом)

### 3.1 Пакеты и webroot

```bash
sudo apt update
sudo apt install -y nginx apache2-utils certbot python3-certbot-nginx
sudo mkdir -p /var/www/certbot/.well-known/acme-challenge
```

### 3.2 Файл логинов (htpasswd)

```bash
sudo htpasswd -c /etc/nginx/.htpasswd_transgps admin     # спросит пароль
sudo chmod 640 /etc/nginx/.htpasswd_transgps
sudo chown root:www-data /etc/nginx/.htpasswd_transgps
```

Добавить второго пользователя (без `-c`, иначе файл перезапишется!):

```bash
sudo htpasswd /etc/nginx/.htpasswd_transgps kiril

### 3.3 Bootstrap-конфиг (HTTP) и проверка

На этом шаге TLS ещё нет, поэтому включаем HTTP-конфиг (он тоже под Basic Auth
и уже проксирует приложение; порт `8000` при этом наружу закрыт биндом).

```bash
sudo cp deploy/nginx-emulator-http.conf /etc/nginx/sites-available/transgps
sudo sed -i 's/emulator\.example\.com/emulator.transgps.cv.ua/g' /etc/nginx/sites-available/transgps
sudo ln -sf /etc/nginx/sites-available/transgps /etc/nginx/sites-enabled/transgps
sudo rm -f /etc/nginx/sites-enabled/default          # чтобы дефолт не перехватывал
sudo nginx -t && sudo systemctl reload nginx
```

### 3.4 Сертификат Let's Encrypt (certbot --webroot)

Мы используем `--webroot`, а не `--nginx`: так certbot не правит наши конфиги,
а ACME-челлендж отдаёт nginx из `/var/www/certbot`. Владелец домена
подтверждается автоматически, сертификат кладётся в `/etc/letsencrypt/live/<домен>/`.

```bash
sudo certbot certonly --webroot -w /var/www/certbot \
     -d emulator.transgps.cv.ua \
     --email admin@transgps.cv.ua --agree-tos --non-interactive
```

> **Альтернатива `certbot --nginx`** (как в исходной задаче): тоже работает —
> `sudo certbot --nginx -d <домен>` сам допишет TLS в server-блок на порту 80
> и настроит автопродление с перезагрузкой nginx. Но тогда финальный
> `nginx-emulator.conf` **не** используем: certbot редактирует уже включённый
> bootstrap-конфиг. Мы рекомендуем `--webroot` + наш финальный конфиг —
> так состояние nginx предсказуемо и лежит в git.

### 3.5 Финальный конфиг (HTTP→HTTPS + TLS)

```bash
sudo cp deploy/nginx-emulator.conf /etc/nginx/sites-available/transgps
sudo sed -i 's/emulator\.example\.com/emulator.transgps.cv.ua/g' /etc/nginx/sites-available/transgps
sudo nginx -t && sudo systemctl reload nginx
```

Теперь `http://<домен>/` редиректит на `https://<домен>/`, а 443 отдаёт
приложение под Basic Auth.

### 3.6 Автопродление

`certbot.timer` запускает продление сам, но webroot-плагин **не**
перезагружает nginx — поэтому ставим deploy-хук:

```bash
sudo mkdir -p /etc/letsencrypt/renewal-hooks/deploy
sudo tee /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh >/dev/null <<'EOF'
#!/usr/bin/env bash
systemctl reload nginx
EOF
sudo chmod 755 /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh

sudo certbot renew --dry-run          # проверка, что продление сработает
systemctl list-timers | grep certbot  # когда следующий автозапуск
```

## 4. Проверка результата

```bash
# 1. Порт 8000 снаружи должен быть НЕДОСТУПЕН (ждём отказ/таймаут):
curl -m 5 http://169.58.82.105:8000/health ; echo "exit=$?"

# 2. Локально на сервере контейнер живой:
curl -s http://127.0.0.1:8000/health

# 3. HTTPS закрыт логином (ждём 401 без пароля):
curl -sk -o /dev/null -w '%{http_code}\n' https://emulator.transgps.cv.ua/health

# 4. HTTPS пускает с логином (ждём 200):
curl -s -u admin:ПАРОЛЬ -o /dev/null -w '%{http_code}\n' https://emulator.transgps.cv.ua/health

# 5. HTTP редиректит на HTTPS (ждём 301, Location: https://…):
curl -sk -o /dev/null -w '%{http_code} %{redirect_url}\n' http://emulator.transgps.cv.ua/
```

Ожидаемые коды: (1) не 200, (2) 200, (3) 401, (4) 200, (5) 301 → https.

## 5. Обновление приложения

Инфраструктура не мешает обычному деплою — nginx и сертификаты трогать не надо:

```bash
cd /opt/transgps-emulator      # путь вашего клона
git pull
docker compose up -d --build   # перебиндит 127.0.0.1:8000:8000
```

Правки сленга и логи жалоб лежат в `./data` (том) и не теряются.

## 6. Частые проблемы

| Симптом | Причина и что делать |
|---|---|
| `nginx -t`: `cannot load certificate … fullchain.pem` | Финальный конфиг включили до выпуска сертификата. Вернитесь на шаг 3.3 (bootstrap) → 3.4 (certbot) → 3.5. |
| `certbot`: `Some challenges have failed` | Домен не указывает на этот IP, либо порт 80 закрыт файрволом, либо мешает другой сайт. Проверьте `dig`, `sudo ufw allow 80`, `ls /etc/nginx/sites-enabled/`. |
| Браузер зацикливает редирект | Где-то остался лишний server-блок на 80 (например, дефолтный). `sudo rm -f /etc/nginx/sites-enabled/default` и `nginx -t`. |
| 502 Bad Gateway | Контейнер не поднят: `docker compose up -d --build`, `docker compose logs --tail=100 api_router`. |
| 401 даже с верным паролем | Логин/пароль не из `/etc/nginx/.htpasswd_transgps`. Посмотреть логины: `sudo cut -d: -f1 /etc/nginx/.htpasswd_transgps`. |
| Нужно закрыть доступ на время | `sudo rm /etc/nginx/sites-enabled/transgps && sudo systemctl reload nginx` (вернуть — `sudo ln -sf … && reload`). |

## 7. Замечания по безопасности

- **Порт `8000` больше не публичен.** В `docker-compose.yml` бинд изменён на
  `127.0.0.1:8000:8000`, поэтому единственный внешний вход — nginx. Если у вас
  включён `ufw`, дополнительно держите открытыми только `80` и `443`.
- **Basic Auth — не «шторка».** «Пароль» внутри страницы редактора (в
  `admin.html`) виден в исходнике и защитой не является; реальная защита —
  `auth_basic` в nginx.
- **Ротация секретов**: ключ OpenRouter (`OPENROUTER_API_KEY`) в `.env` — на
  сервере отдельный, не в git; см. `docs/STATUS.md`, задача 2.
- **Логи жалоб и сленг** пишутся в `./data` на хосте — делайте бэкап:
  `tar czf ~/transgps-data-$(date +%F).tgz -C /opt/transgps-emulator data`.
```
