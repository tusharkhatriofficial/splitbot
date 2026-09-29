#!/usr/bin/env bash
set -Eeuo pipefail
# Run on your Mac in the folder with bot.py, .env, compose.yaml and Dockerfile.
# Usage: ./deploy.sh user@host [ssh-port]
if [[ $# -lt 1 || $# -gt 2 ]]; then echo "Usage: $0 user@host [ssh-port]" >&2; exit 2; fi
TARGET="$1"; PORT="${2:-22}"
if [[ ! "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then echo "Invalid port" >&2; exit 2; fi
for f in bot.py .env compose.yaml Dockerfile; do
  [[ -f "$f" ]] || { echo "Missing $f in current folder" >&2; exit 2; }
done
if ! grep -Eq 'BASE[[:space:]]*/[[:space:]]*"data"[[:space:]]*/[[:space:]]*"expenses\.db"' bot.py; then
  echo 'bot.py must use BASE / "data" / "expenses.db" for persistence; refusing unsafe deploy.' >&2
  exit 2
fi
read -r -p "Stop local bot.py first. Is it stopped? [y/N] " OK
[[ "$OK" =~ ^[Yy]$ ]] || exit 1
# SSH/SCP prompt interactively for password; no password is written to disk or shown in ps output.
ssh -p "$PORT" "$TARGET" 'mkdir -p ~/homeexpensebot/data && chmod 700 ~/homeexpensebot ~/homeexpensebot/data'
scp -P "$PORT" bot.py .env compose.yaml Dockerfile "$TARGET:homeexpensebot/"
ssh -p "$PORT" "$TARGET" 'cd ~/homeexpensebot && chmod 600 .env && docker compose -p homeexpensebot config -q && docker compose -p homeexpensebot up -d --build --no-deps bot && docker compose -p homeexpensebot ps && docker compose -p homeexpensebot logs --tail=30 bot'
echo 'Deploy finished. Check /balance in Telegram; fresh DB should show zero.'