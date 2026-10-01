#!/usr/bin/env bash
# First-time setup on the production box.  Idempotent: safe to re-run.
#
#   sudo mkdir -p /opt/polyfin && sudo chown deploy: /opt/polyfin
#   git clone <repo> /opt/polyfin && cd /opt/polyfin && deploy/bootstrap.sh
#
# Needs: docker with the compose plugin, git, curl, sudo for systemd.
# Starts the database, recorder, paper trader and daily refit.  Does NOT start
# live trading - see deploy/README.md.
set -euo pipefail
cd "$(dirname "$0")/.."
APP=$(pwd)

command -v docker >/dev/null || { echo "install docker + compose plugin first"; exit 1; }
command -v uv >/dev/null || { curl -LsSf https://astral.sh/uv/install.sh | sh; export PATH="$HOME/.local/bin:$PATH"; }

if [ ! -f .env ]; then
  PW=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
  sed "s/^FIN_PGPASSWORD=.*/FIN_PGPASSWORD=$PW/" .env.example > .env
  chmod 600 .env
  echo "created .env with a generated DB password (wallet fields still CHANGE_ME)"
fi

set -a; . ./.env; set +a
docker compose up -d
for _ in $(seq 1 30); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' polyfin-timescaledb 2>/dev/null)" = healthy ] && break
  sleep 2
done

uv venv -q --python 3.11 .venv 2>/dev/null || true
uv pip install -q --python .venv/bin/python -r requirements.txt
.venv/bin/python -m polyfin.db

sudo cp deploy/polyfin-*.service deploy/polyfin-*.timer /etc/systemd/system/
sudo sed -i "s#/opt/polyfin#$APP#g; s#^User=deploy#User=$(id -un)#" \
  /etc/systemd/system/polyfin-*.service
sudo systemctl daemon-reload
# polyfin-weekly is NOT enabled here: weekly recording/backtests run on the laptop
# (server disk is shared and limited, 2026-10-01)
sudo systemctl enable --now polyfin-recorder polyfin-paper polyfin-refit.timer

echo
echo "running: recorder, paper trader, daily refit.  The recorder backfills 7 days"
echo "on its first pass (~4 min); then fit stage 2 once:"
echo "    .venv/bin/python -m polyfin.stage2"
echo "logs:  journalctl -fu polyfin-recorder -u polyfin-paper"
