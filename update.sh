#!/usr/bin/env bash
set -e
cd /root/vpn_bot/vpntg
if [[ -d .git ]]; then
  git pull origin main || true
fi
source venv/bin/activate
pip install -r requirements.txt --quiet
systemctl restart vpnbot
echo "✅ Бот успешно обновлен и перезапущен!"
