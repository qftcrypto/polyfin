#!/usr/bin/env bash
# Ship a new version:  git push (laptop), then on the box:  deploy/update.sh
# Fast-forward only, refuses a dirty tree, reinstalls deps only when
# requirements.txt changed, applies the schema, restarts what is running.
set -euo pipefail
cd "$(dirname "$0")/.."

git diff --quiet && git diff --cached --quiet || { echo "working tree is dirty - refusing"; exit 1; }
before=$(sha1sum requirements.txt | cut -d' ' -f1)
git pull --ff-only
after=$(sha1sum requirements.txt | cut -d' ' -f1)
if [ "$before" != "$after" ]; then
  uv pip install -q --python .venv/bin/python -r requirements.txt
fi
.venv/bin/python -m polyfin.db
sudo cp deploy/polyfin-*.service deploy/polyfin-*.timer /etc/systemd/system/
sudo sed -i "s#/opt/polyfin#$(pwd)#g; s#^User=deploy#User=$(id -un)#" \
  /etc/systemd/system/polyfin-*.service
sudo systemctl daemon-reload
for u in polyfin-recorder polyfin-paper polyfin-live; do
  if systemctl is-active --quiet "$u"; then sudo systemctl restart "$u"; echo "restarted $u"; fi
done
git log --oneline -1
