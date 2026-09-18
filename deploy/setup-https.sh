#!/usr/bin/env bash
#
# setup-https.sh — разовая (идемпотентная) настройка доступа к эмулятору
# TransGPS: nginx reverse proxy + Basic Auth + HTTPS (Let's Encrypt).
#
# Что делает по шагам:
#   1. ставит nginx, apache2-utils (htpasswd), certbot (+ nginx-плагин для хука);
#   2. создаёт webroot для ACME-челленджа (/var/www/certbot);
#   3. если нет /etc/nginx/.htpasswd_transgps — предлагает создать (спросит пароль);
#   4. поднимает bootstrap-конфиг nginx (HTTP) и перезагружает nginx;
#   5. выпускает сертификат Let's Encrypt (certbot --webroot);
#   6. включает финальный конфиг (HTTP→HTTPS + TLS + Basic Auth) и перезагружает;
#   7. ставит deploy-хук, чтобы nginx перечитывал сертификат после автопродления;
#   8. по возможности открывает в ufw только 80/443 и проверяет результат.
#
# Использование:
#   sudo bash deploy/setup-https.sh <домен> [email]
#
# Примеры:
#   sudo bash deploy/setup-https.sh emulator.transgps.cv.ua admin@transgps.cv.ua
#   sudo bash deploy/setup-https.sh emulator.transgps.cv.ua            # без email
#
# ВАЖНО:
#   * Запускать из КОРНЯ репозитория (иначе не найдёт deploy/*.conf).
#   * Домен уже должен указывать A-записью на IP этого сервера (169.58.82.105).
#   * Порт 8000 наружу закрыт биндом 127.0.0.1 в docker-compose.yml — обход nginx
#     невозможен; контейнер должен быть поднят ДО запуска скрипта.
#   * Скрипт идемпотентен: повторный запуск обновит конфиг и продлит сертификат.
set -euo pipefail

DOMAIN="${1:-}"
EMAIL="${2:-}"

SITE_NAME="transgps"
SITE_AVAILABLE="/etc/nginx/sites-available/${SITE_NAME}"
SITE_ENABLED="/etc/nginx/sites-enabled/${SITE_NAME}"
HTPASSWD_FILE="/etc/nginx/.htpasswd_transgps"
ACME_WEBROOT="/var/www/certbot"
HOOK_DIR="/etc/letsencrypt/renewal-hooks/deploy"
HOOK_FILE="${HOOK_DIR}/reload-nginx.sh"

say()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\n\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31m[x] %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Запустите через sudo: sudo bash deploy/setup-https.sh <домен> [email]"
[ -n "$DOMAIN" ] || die "Не указан домен. Пример: sudo bash deploy/setup-https.sh emulator.transgps.cv.ua admin@example.com"
[ -f "docker-compose.yml" ] || die "Запускайте из корня репозитория (нет docker-compose.yml в текущей папке)."
[ -f "deploy/nginx-emulator.conf" ] || die "Не найден deploy/nginx-emulator.conf — проверьте, что работаете из корня репозитория."
[ -f "deploy/nginx-emulator-http.conf" ] || die "Не найден deploy/nginx-emulator-http.conf."

# Подсказка, если домен не резолвится в этот хост (не блокирует, но полезно).
if ! getent hosts "$DOMAIN" >/dev/null 2>&1; then
    warn "DNS: домен ${DOMAIN} не резолвится. Проверьте A-запись → IP сервера, иначе certbot упадёт."
fi

# --- 1. Пакеты -------------------------------------------------------------
say "Ставлю пакеты (nginx, apache2-utils, certbot)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq nginx apache2-utils certbot python3-certbot-nginx

# --- 2. Webroot для ACME ---------------------------------------------------
say "Готовлю webroot для ACME: ${ACME_WEBROOT}"
mkdir -p "${ACME_WEBROOT}/.well-known/acme-challenge"

# --- 3. htpasswd -----------------------------------------------------------
if [ ! -f "${HTPASSWD_FILE}" ]; then
    say "Создаю файл паролей Basic Auth: ${HTPASSWD_FILE}"
    echo "Сейчас htpasswd спросит пароль для логина 'admin' (можно ввести свой логин, поменяв 'admin')."
    htpasswd -c "${HTPASSWD_FILE}" admin
    chmod 640 "${HTPASSWD_FILE}"
    chown root:www-data "${HTPASSWD_FILE}"
else
    say "Файл паролей уже есть: ${HTPASSWD_FILE} (не перезаписываю)"
    echo "  добавить пользователя: sudo htpasswd ${HTPASSWD_FILE} <логин>"
fi

# --- 4. Bootstrap-конфиг (HTTP) --------------------------------------------
say "Включаю bootstrap-конфиг nginx (HTTP) для выпуска сертификата"
sed "s/emulator\.example\.com/${DOMAIN}/g" deploy/nginx-emulator-http.conf > "${SITE_AVAILABLE}"
ln -sf "${SITE_AVAILABLE}" "${SITE_ENABLED}"

# Отключаем дефолтный сайт, чтобы он не перехватывал запросы.
# (if, а не `[ -e ] && rm`: под set -e отсутствие файла уронило бы скрипт)
if [ -e /etc/nginx/sites-enabled/default ]; then
    rm -f /etc/nginx/sites-enabled/default
fi

nginx -t
systemctl reload nginx
systemctl enable nginx >/dev/null 2>&1 || true

# --- 5. Сертификат ---------------------------------------------------------
say "Выпускаю сертификат Let's Encrypt для ${DOMAIN}"
CERTBOT_ARGS=(certonly --webroot -w "${ACME_WEBROOT}" -d "${DOMAIN}"
              --non-interactive --agree-tos --keep-until-expiring)
if [ -n "${EMAIL}" ]; then
    CERTBOT_ARGS+=(--email "${EMAIL}")
else
    CERTBOT_ARGS+=(--register-unsafely-without-email)
    warn "Email не указан: регистрирую без email (продление работает, но уведомления о проблемах не придут)."
fi
certbot "${CERTBOT_ARGS[@]}"

CERT_DIR="/etc/letsencrypt/live/${DOMAIN}"
[ -f "${CERT_DIR}/fullchain.pem" ] || die "Сертификат не найден: ${CERT_DIR}/fullchain.pem"

# --- 6. Финальный конфиг (HTTPS) -------------------------------------------
say "Включаю финальный конфиг (HTTP→HTTPS + TLS + Basic Auth)"
sed "s/emulator\.example\.com/${DOMAIN}/g" deploy/nginx-emulator.conf > "${SITE_AVAILABLE}"
nginx -t
systemctl reload nginx

# --- 7. Хук автопродления --------------------------------------------------
say "Ставлю хук перезагрузки nginx после автопродления"
mkdir -p "${HOOK_DIR}"
cat > "${HOOK_FILE}" <<'HOOK'
#!/usr/bin/env bash
# Перечитываем nginx после успешного продления сертификата (webroot-плагин
# сам не перезагружает nginx, в отличие от --nginx).
systemctl reload nginx
HOOK
chmod 755 "${HOOK_FILE}"
# Проверяем автопродление без реального выпуска.
if certbot renew --dry-run --no-random-sleep-on-renew >/dev/null 2>&1; then
    echo "  dry-run продления: OK"
else
    warn "dry-run продления не прошёл — проверьте вручную: sudo certbot renew --dry-run"
fi

# --- 8. Firewall и проверка ------------------------------------------------
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
    say "Открываю в ufw только 80/443 (порт 8000 наружу не открыт)"
    ufw allow 'Nginx Full' >/dev/null 2>&1 || { ufw allow 80/tcp; ufw allow 443/tcp; }
else
    warn "ufw не активен — пропускаю настройку файрвола (порт 8000 всё равно закрыт биндом 127.0.0.1)."
fi

say "Проверка HTTPS (после Basic Auth ожидается 401 — это норма)"
code="$(curl -sk -o /dev/null -w '%{http_code}' "https://${DOMAIN}/health" || echo 000)"
case "${code}" in
    401) echo "  https://${DOMAIN}/health -> 401 (Basic Auth работает ✔)" ;;
    200) warn "  https://${DOMAIN}/health -> 200 (проверьте, что auth_basic включён!)" ;;
    000) warn "  HTTPS не ответил — проверьте DNS, сертификат и логи: journalctl -u nginx -n 50" ;;
    *)   warn "  https://${DOMAIN}/health -> HTTP ${code} (ожидался 401)" ;;
esac

say "Готово. Открывайте: https://${DOMAIN}/ (редирект на /ui/emulator.html)"
echo "  логин/пароль — из ${HTPASSWD_FILE}"
echo "  сертификаты продлеваются автоматически (certbot.timer); проверка: systemctl list-timers | grep certbot"
