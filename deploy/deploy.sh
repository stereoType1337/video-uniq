#!/usr/bin/env bash
# Server-side deploy: backup, update to a commit (default origin/main), rebuild, health check.
# Called by autodeploy.sh after green CI; can also be run by hand: deploy/deploy.sh [sha].
set -euo pipefail

APP_DIR=/opt/video-uniq
BACKUP_DIR=/opt/backups
KEEP_BACKUPS=14
HEALTH_URL=https://uniq.pulsemarket.eu/health

exec 9>/tmp/video-uniq-deploy.lock
flock -n 9 || { echo "another deploy is running"; exit 1; }
cd "$APP_DIR"

step=start
fail() { "$APP_DIR/deploy/notify.sh" "🚨 Деплой ${target:-?} провален на шаге «$step». journalctl -u video-uniq-autodeploy"; }
trap fail ERR

step=backup; echo "== backup"
mkdir -p "$BACKUP_DIR"
docker compose exec -T db pg_dump -U uniq -Fc uniq > "$BACKUP_DIR/uniq-$(date +%Y%m%d-%H%M%S).dump"
ls -1t "$BACKUP_DIR"/uniq-*.dump | tail -n +$((KEEP_BACKUPS + 1)) | xargs -r rm --

step=update; echo "== update code"
git fetch --quiet origin main
target=${1:-origin/main}
before=$(git rev-parse --short HEAD)
git reset --hard --quiet "$target"
echo "$before -> $(git rev-parse --short HEAD)"

step=build; echo "== build and restart"
docker compose up -d --build --remove-orphans 2>&1 | grep -v '^time=' || true

step=caddy
if ! cmp -s caddy/video-uniq.conf /opt/caddy/conf.d/video-uniq/video-uniq.conf; then
  echo "== caddy config changed, reloading"
  cp caddy/video-uniq.conf /opt/caddy/conf.d/video-uniq/video-uniq.conf
  docker exec caddy caddy reload --config /etc/caddy/Caddyfile
fi

step=health; echo "== health"
for _ in $(seq 1 30); do
  if curl -fs --max-time 5 "$HEALTH_URL" >/dev/null; then
    echo "== webhook delivery"
    if ! docker compose exec -T bot python -m app.scripts.check_webhook; then
      step=webhook; docker compose logs --tail 50 bot tg_api; fail; exit 1
    fi
    docker compose ps --format 'table {{.Name}}\t{{.Status}}'
    docker image prune -f >/dev/null
    echo "deployed $(git rev-parse --short HEAD)"
    "$APP_DIR/deploy/notify.sh" "✅ Выкачено $(git log -1 --format='%h %s')" true
    exit 0
  fi
  sleep 2
done
echo "health check failed"; docker compose logs --tail 50 bot worker; fail; exit 1
