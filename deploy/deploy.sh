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

echo "== backup"
mkdir -p "$BACKUP_DIR"
docker compose exec -T db pg_dump -U uniq -Fc uniq > "$BACKUP_DIR/uniq-$(date +%Y%m%d-%H%M%S).dump"
ls -1t "$BACKUP_DIR"/uniq-*.dump | tail -n +$((KEEP_BACKUPS + 1)) | xargs -r rm --

echo "== update code"
git fetch --quiet origin main
target=${1:-origin/main}
before=$(git rev-parse --short HEAD)
git reset --hard --quiet "$target"
echo "$before -> $(git rev-parse --short HEAD)"

echo "== build and restart"
docker compose up -d --build --remove-orphans 2>&1 | grep -v '^time=' || true

echo "== health"
for _ in $(seq 1 30); do
  if curl -fs --max-time 5 "$HEALTH_URL" >/dev/null; then
    docker compose ps --format 'table {{.Name}}\t{{.Status}}'
    docker image prune -f >/dev/null
    echo "deployed $(git rev-parse --short HEAD)"
    exit 0
  fi
  sleep 2
done
echo "health check failed"; docker compose logs --tail 50 bot worker; exit 1
