#!/usr/bin/env bash
# deploy/notify.sh "text" — message to ADMIN_IDS straight through the local telegram-bot-api
# container (works even when the bot container is down). Never fails the caller.
set -uo pipefail
cd /opt/video-uniq 2>/dev/null || exit 0

env_value() { grep -E "^$1=" .env | head -1 | cut -d= -f2- | sed 's/#.*//' | tr -d '\r" '; }
token=$(env_value BOT_TOKEN)
admins=$(env_value ADMIN_IDS)
ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' video_uniq_tg_api 2>/dev/null | awk '{print $1}')
[ -n "$token" ] && [ -n "$admins" ] && [ -n "$ip" ] || exit 0

IFS=',' read -ra ids <<< "$admins"
for id in "${ids[@]}"; do
  curl -fsS -m 10 "http://$ip:8081/bot$token/sendMessage" \
    --data-urlencode "chat_id=$id" --data-urlencode "text=$1" \
    --data-urlencode "disable_notification=${2:-false}" >/dev/null 2>&1 || true
done
exit 0
