#!/bin/sh
# Pull the latest code and restart the bot, keeping the same token and data.
set -e
cd "$(dirname "$0")"
TOKEN=$(docker inspect editguard --format '{{range .Config.Env}}{{println .}}{{end}}' | grep '^BOT_TOKEN=')
[ -n "$TOKEN" ] || { echo "Could not read BOT_TOKEN from the running container."; exit 1; }
git pull
docker build -t edit-guard-bot .
docker rm -f editguard
docker run -d --name editguard --restart unless-stopped -e "$TOKEN" -v editguard:/data edit-guard-bot
echo "Updated. Logs: docker logs -f editguard"
