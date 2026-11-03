#!/usr/bin/env bash
# =============================================================================
# Автоматическая установка VPN-сервера (3x-ui + Telegram-бот) на Ubuntu 22.04/24.04:
#   - Установка 3x-ui (MHSanaei/3x-ui)
#   - Очистка дублей в settings и принудительная установка порта 2053, логина/пароля и SSL
#   - Включение HTTPS для панели (2053) и сервиса подписок (2096)
#   - Автоматическое создание инбаунда VLESS + XTLS-Vision + Reality на 443/tcp (с CSRF)
#   - Автокопирование файлов бота из текущей директории в /root/vpn_bot/vpntg
# =============================================================================
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log()  { echo -e "\n\033[1;32m==> $*\033[0m"; }
warn() { echo -e "\033[1;33m[!] $*\033[0m"; }
die()  { echo -e "\033[1;31m[x] $*\033[0m" >&2; exit 1; }

ask() {
  local var="$1" q="$2" def="${3:-}"
  if [[ -n "${!var:-}" ]]; then return 0; fi
  local val=""
  read -rp "$q${def:+ [$def]}: " val || true
  printf -v "$var" '%s' "${val:-$def}"
}

[[ $EUID -eq 0 ]] || die "Запустите скрипт от имени root (sudo -i)"

# ---------- 1. Сбор параметров -----------------------------------------------
log "Конфигурация сервера (3x-ui + VPN Bot)"
ask DOMAIN            "Домен сервера (A-запись должна указывать на этот VPS)"
ask SERVER_LABEL      "Название локации" "Main"
ask LE_EMAIL          "Email для Let's Encrypt"
ask BOT_TOKEN         "BOT_TOKEN от @BotFather"
ask ADMIN_ID          "Ваш Telegram ID (ADMIN_ID)"
ask BOT_REPO          "Git-URL репозитория бота (оставьте пустым, если запускаете из распакованного архива)" ""
ask SUPPORT_USERNAME  "Username поддержки (без @)" "support"
ask SUPPORT_PHONE     "Номер телефона (СБП)" ""
ask BANK_NAME         "Название банка для перевода" "Т-Банк (СБП)"
ask PRICE_1_MONTH     "Цена подписки за 1 месяц (₽)" "100"
ask TRIAL_DAYS        "Длительность пробного периода (дней)" "3"

[[ -n "$DOMAIN" && -n "$LE_EMAIL" && -n "$BOT_TOKEN" && -n "$ADMIN_ID" ]] \
  || die "Обязательные поля: DOMAIN, LE_EMAIL, BOT_TOKEN, ADMIN_ID"

BOT_DIR="/root/vpn_bot/vpntg"
CRED_FILE="/root/install_credentials.txt"
XUI_PORT="2053"
XUI_SUB_PORT="2096"
XUI_ADMIN_USER="admin"
XUI_ADMIN_PASS="$(openssl rand -hex 12)"
REALITY_SHORT_ID="$(openssl rand -hex 8)"
REALITY_DEST="www.microsoft.com"

# ---------- 2. Проверка DNS --------------------------------------------------
log "Проверка публичного IP и DNS"
PUBLIC_IP="$(curl -s4 https://api.ipify.org || true)"
[[ -n "$PUBLIC_IP" ]] || die "Не удалось определить публичный IPv4 сервера"
RESOLVED_IP="$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}' || true)"
if [[ "$RESOLVED_IP" != "$PUBLIC_IP" ]]; then
  warn "Домен $DOMAIN резолвится в '${RESOLVED_IP:-пусто}', а IP сервера: $PUBLIC_IP."
fi

# ---------- 3. Пакеты --------------------------------------------------------
log "Установка системных пакетов"
apt-get update -y
apt-get install -y curl wget socat ufw certbot git openssl jq sqlite3 python3-venv python3-pip uuid-runtime

# ---------- 4. Firewall (UFW) ------------------------------------------------
log "Настройка файрвола UFW"
ufw allow 22/tcp   comment 'ssh'
ufw allow 80/tcp   comment 'http (certbot)'
ufw allow 443/tcp  comment '3x-ui vless reality'
ufw allow 443/udp  comment '3x-ui hysteria2 / quic'
ufw allow 2053/tcp comment '3x-ui web panel'
ufw allow 2096/tcp comment '3x-ui sub server'
ufw --force enable

# ---------- 5. Let's Encrypt -------------------------------------------------
log "Получение сертификата Let's Encrypt для ${DOMAIN}"
if [[ ! -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]]; then
  systemctl stop x-ui 2>/dev/null || true
  certbot certonly --standalone -d "$DOMAIN" -m "$LE_EMAIL" \
    --agree-tos --non-interactive --keep-until-expiring || warn "Не удалось выпустить сертификат Let's Encrypt"
fi

# ---------- 6. Установка и настройка 3x-ui -----------------------------------
log "Установка 3x-ui"
if [[ ! -x /usr/local/x-ui/x-ui ]]; then
  printf "n\n" | bash <(curl -Ls https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh) || true
fi

systemctl stop x-ui 2>/dev/null || true

# Задаем логин, пароль, порт 2053 и корневой путь через CLI 3x-ui
/usr/local/x-ui/x-ui setting -username "${XUI_ADMIN_USER}" -password "${XUI_ADMIN_PASS}" -port "${XUI_PORT}" -webBasePath "/" >/dev/null 2>&1 || true

# Удаляем старые ключи перед вставкой (в таблице settings колонка key не имеет UNIQUE)
sqlite3 /etc/x-ui/x-ui.db <<SQL
DELETE FROM settings WHERE key IN ('webPort', 'webBasePath', 'webListen', 'subEnable', 'subPort', 'subPath', 'webCertFile', 'webKeyFile', 'subCertFile', 'subKeyFile');
INSERT INTO settings (key, value) VALUES ('webPort', '${XUI_PORT}');
INSERT INTO settings (key, value) VALUES ('webBasePath', '/');
INSERT INTO settings (key, value) VALUES ('subEnable', 'true');
INSERT INTO settings (key, value) VALUES ('subPort', '${XUI_SUB_PORT}');
INSERT INTO settings (key, value) VALUES ('subPath', '/sub/');
SQL

if [[ -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]]; then
  sqlite3 /etc/x-ui/x-ui.db <<SQL
INSERT INTO settings (key, value) VALUES ('webCertFile', '/etc/letsencrypt/live/${DOMAIN}/fullchain.pem');
INSERT INTO settings (key, value) VALUES ('webKeyFile', '/etc/letsencrypt/live/${DOMAIN}/privkey.pem');
INSERT INTO settings (key, value) VALUES ('subCertFile', '/etc/letsencrypt/live/${DOMAIN}/fullchain.pem');
INSERT INTO settings (key, value) VALUES ('subKeyFile', '/etc/letsencrypt/live/${DOMAIN}/privkey.pem');
SQL
fi

systemctl daemon-reload
systemctl enable --now x-ui
systemctl restart x-ui
sleep 4

# ---------- 7. Генерация ключей Reality и создание Inbound #1 ----------------
log "Настройка инбаунда VLESS + Reality в 3x-ui"
XRAY_BIN="$(find /usr/local/x-ui/bin -name 'xray-linux-*' | head -n1)"
REALITY_KEYS="$("$XRAY_BIN" x25519)"
REALITY_PRIV="$(echo "$REALITY_KEYS" | grep -i 'private' | awk -F':' '{print $NF}' | tr -d ' \r')"
REALITY_PUB="$(echo "$REALITY_KEYS" | grep -iE 'public|password' | head -n1 | awk -F':' '{print $NF}' | tr -d ' \r')"

PROTOCOL_SCHEME="http"
if [[ -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]]; then
  PROTOCOL_SCHEME="https"
fi

COOKIE_JAR="$(mktemp)"
CSRF_TOKEN=""
for _ in $(seq 1 10); do
  CSRF_TOKEN="$(curl -s -k -c "$COOKIE_JAR" -b "$COOKIE_JAR" "${PROTOCOL_SCHEME}://127.0.0.1:${XUI_PORT}/csrf-token" | jq -r '.obj // empty' 2>/dev/null || true)"
  if curl -s -k -c "$COOKIE_JAR" -b "$COOKIE_JAR" -X POST "${PROTOCOL_SCHEME}://127.0.0.1:${XUI_PORT}/login" \
    -H "X-CSRF-Token: ${CSRF_TOKEN}" \
    -d "username=${XUI_ADMIN_USER}&password=${XUI_ADMIN_PASS}" | grep -q '"success":true'; then
    break
  fi
  sleep 2
done

EXISTING_INBOUNDS="$(curl -s -k -b "$COOKIE_JAR" "${PROTOCOL_SCHEME}://127.0.0.1:${XUI_PORT}/panel/api/inbounds/list" | jq -r '(.obj | length) // 0' 2>/dev/null || echo "0")"
if [[ ! "$EXISTING_INBOUNDS" =~ ^[0-9]+$ ]]; then
  EXISTING_INBOUNDS=0
fi

if [[ "$EXISTING_INBOUNDS" -eq 0 ]]; then
  SETTINGS_JSON="$(jq -cn '{clients: [], decryption: "none", fallbacks: []}')"
  STREAM_JSON="$(jq -cn \
    --arg dest "${REALITY_DEST}:443" \
    --arg sni "${REALITY_DEST}" \
    --arg priv "${REALITY_PRIV}" \
    --arg pub "${REALITY_PUB}" \
    --arg sid "${REALITY_SHORT_ID}" \
    '{
      network: "tcp",
      security: "reality",
      externalProxy: [],
      realitySettings: {
        show: false,
        xver: 0,
        dest: $dest,
        serverNames: [$sni],
        privateKey: $priv,
        minClient: "",
        maxClient: "",
        maxTimediff: 0,
        shortIds: [$sid],
        settings: {
          publicKey: $pub,
          fingerprint: "chrome",
          serverName: "",
          spiderX: "/"
        }
      },
      tcpSettings: {
        acceptProxyProtocol: false,
        header: {type: "none"}
      }
    }')"
  SNIFFING_JSON="$(jq -cn '{enabled: true, destOverride: ["http", "tls", "quic", "fakedns"], metadataOnly: false, routeOnly: false}')"

  INBOUND_PAYLOAD="$(jq -n \
    --arg remark "${SERVER_LABEL}-VLESS-Reality" \
    --arg settings "$SETTINGS_JSON" \
    --arg streamSettings "$STREAM_JSON" \
    --arg sniffing "$SNIFFING_JSON" \
    '{
      up: 0,
      down: 0,
      total: 0,
      remark: $remark,
      enable: true,
      expiryTime: 0,
      listen: "",
      port: 443,
      protocol: "vless",
      settings: $settings,
      streamSettings: $streamSettings,
      sniffing: $sniffing
    }')"

  curl -s -k -b "$COOKIE_JAR" -X POST "${PROTOCOL_SCHEME}://127.0.0.1:${XUI_PORT}/panel/api/inbounds/add" \
    -H "Content-Type: application/json" \
    -H "X-CSRF-Token: ${CSRF_TOKEN}" \
    -d "$INBOUND_PAYLOAD" >/dev/null || warn "Не удалось создать инбаунд через curl (бот создаст его автоматически при первом запросе)"
  log "Инбаунд VLESS + Reality (порт 443, ID=1) успешно настроен!"
else
  log "Инбаунд в 3x-ui уже существует, пропускаем создание."
fi
rm -f "$COOKIE_JAR"

# ---------- 8. Установка Telegram-бота ---------------------------------------
log "Развертывание Telegram-бота в ${BOT_DIR}"
mkdir -p "$BOT_DIR"

if [[ -f "$SCRIPT_DIR/bot.py" && "$SCRIPT_DIR" != "$BOT_DIR" ]]; then
  cp -r "$SCRIPT_DIR"/. "$BOT_DIR"/
elif [[ -n "$BOT_REPO" && ! -f "$BOT_DIR/bot.py" ]]; then
  git clone "$BOT_REPO" "$BOT_DIR"
fi

[[ -f "$BOT_DIR/requirements.txt" ]] || die "Файлы бота не найдены в $BOT_DIR! Запускайте install_vpn.sh из папки с распакованным архивом."

cd "$BOT_DIR"
[[ -d venv ]] || python3 -m venv venv
venv/bin/pip install --upgrade pip --quiet
venv/bin/pip install -r requirements.txt --quiet

cat > "$BOT_DIR/.env" <<EOF
# ===== BOT =====
BOT_TOKEN=${BOT_TOKEN}
ADMIN_ID=${ADMIN_ID}
ADDITIONAL_ADMINS=

# ===== SUPPORT =====
SUPPORT_USERNAME=${SUPPORT_USERNAME}
SUPPORT_PHONE=${SUPPORT_PHONE}
BANK_NAME=${BANK_NAME}

# ===== 3X-UI =====
XUI_URL=${PROTOCOL_SCHEME}://127.0.0.1:${XUI_PORT}
XUI_USERNAME=${XUI_ADMIN_USER}
XUI_PASSWORD=${XUI_ADMIN_PASS}
XUI_INBOUND_ID=1
XUI_PUBLIC_HOST=${DOMAIN}
XUI_SUB_URL=${PROTOCOL_SCHEME}://${DOMAIN}:${XUI_SUB_PORT}/sub
DEFAULT_DATA_LIMIT_GB=100
LIMIT_IP=0
SERVER_LABEL=${SERVER_LABEL}

# ===== CRYPTOBOT =====
CRYPTOBOT_TOKEN=
CRYPTOBOT_ENABLED=false
CRYPTOBOT_TESTNET=true

# ===== TRIAL & PRICES =====
TRIAL_DAYS=${TRIAL_DAYS}
PRICE_1_MONTH=${PRICE_1_MONTH}
EOF
chmod 600 "$BOT_DIR/.env"

cat > /etc/systemd/system/vpnbot.service <<EOF
[Unit]
Description=VPN Telegram Bot (3x-ui)
After=network-online.target x-ui.service
Wants=network-online.target

[Service]
WorkingDirectory=${BOT_DIR}
ExecStart=${BOT_DIR}/venv/bin/python bot.py
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now vpnbot
systemctl restart vpnbot
sleep 3

# ---------- 9. Проверка и вывод данных ---------------------------------------
log "Проверка сервисов"
systemctl is-active --quiet x-ui   && echo "3x-ui: OK"  || warn "3x-ui не активен: journalctl -u x-ui"
systemctl is-active --quiet vpnbot && echo "vpnbot: OK" || warn "vpnbot не активен: journalctl -u vpnbot"

cat > "$CRED_FILE" <<EOF
Домен сервера:       ${DOMAIN}
IP сервера:          ${PUBLIC_IP}
3x-ui панель:        ${PROTOCOL_SCHEME}://${DOMAIN}:${XUI_PORT}/
Сервис подписок:     ${PROTOCOL_SCHEME}://${DOMAIN}:${XUI_SUB_PORT}/sub/
3x-ui логин:         ${XUI_ADMIN_USER}
3x-ui пароль:        ${XUI_ADMIN_PASS}
3x-ui Inbound ID:    1
Reality PublicKey:   ${REALITY_PUB}
Reality ShortId:     ${REALITY_SHORT_ID}
Директория бота:     ${BOT_DIR} (конфиг: ${BOT_DIR}/.env)
EOF
chmod 600 "$CRED_FILE"

log "Установка завершена!"
echo "Учетные данные сохранены в $CRED_FILE (chmod 600)."
echo
echo "Для входа в веб-панель 3x-ui откройте в браузере:"
echo "  ${PROTOCOL_SCHEME}://${DOMAIN}:${XUI_PORT}/"
