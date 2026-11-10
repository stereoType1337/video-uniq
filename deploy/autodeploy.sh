#!/usr/bin/env bash
# Pull-based CD: run every minute by video-uniq-autodeploy.timer. Deploys a new commit on
# origin/main only after its GitHub Actions "test" job succeeded. No tokens, no inbound access:
# the repo is public and the check-runs API needs no auth (called only for new commits).
set -euo pipefail

APP_DIR=/opt/video-uniq
REPO=stereoType1337/video-uniq
STATE=/var/lib/video-uniq-autodeploy
mkdir -p "$STATE"
cd "$APP_DIR"

git fetch --quiet origin main
target=$(git rev-parse origin/main)
[ "$target" = "$(git rev-parse HEAD)" ] && exit 0
[ "$(cat "$STATE/skipped" 2>/dev/null)" = "$target" ] && exit 0   # tests failed for it

verdict=$(curl -fsS --max-time 10 "https://api.github.com/repos/$REPO/commits/$target/check-runs" |
  python3 -c '
import json, sys
runs = [r for r in json.load(sys.stdin)["check_runs"] if r["name"] == "test"]
if not runs: print("pending")
elif any(r["status"] != "completed" for r in runs): print("pending")
elif all(r["conclusion"] == "success" for r in runs): print("success")
else: print("failure")')

case "$verdict" in
  pending) echo "${target:0:7}: tests not finished yet" ;;
  failure) echo "${target:0:7}: tests failed, not deploying"; echo "$target" > "$STATE/skipped"
           "$APP_DIR/deploy/notify.sh" "❌ ${target:0:7}: тесты на GitHub упали, в прод не выкатываю" ;;
  success) echo "${target:0:7}: tests passed, deploying"; exec "$APP_DIR/deploy/deploy.sh" "$target" ;;
esac
