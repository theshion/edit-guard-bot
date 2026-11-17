#!/bin/sh
# Pull the latest code and restart the bot, keeping the same token, keys and data.
# Extra settings (e.g. ANTHROPIC_API_KEY=...) can be put in a .env file next to this script.
set -e
cd "$(dirname "$0")"
umask 077
ENVFILE=$(mktemp)
trap 'rm -f "$ENVFILE"' EXIT
docker inspect editguard --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | grep -E '^(BOT_TOKEN|ANTHROPIC_API_KEY|AI_MODEL|NSFW_THRESHOLD)=' > "$ENVFILE" || true
[ -f .env ] && cat .env >> "$ENVFILE"
grep -q '^BOT_TOKEN=' "$ENVFILE" || { echo "BOT_TOKEN not found in the running container or .env"; exit 1; }
git pull
docker build -t edit-guard-bot .
docker rm -f editguard
docker run -d --name editguard --restart unless-stopped --env-file "$ENVFILE" -v editguard:/data edit-guard-bot
echo "Updated. Logs: docker logs -f editguard"
