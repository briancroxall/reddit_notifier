#!/usr/bin/env bash
# Update reddit-notifier on the droplet to the latest code on GitHub.
# Run as root on the droplet:   bash /opt/reddit-notifier/deploy/update.sh
set -euo pipefail

APP=/opt/reddit-notifier
UV=/home/fragdash/.local/bin/uv

cd "$APP"
# The files belong to fragdash, so run git and uv as that user.
sudo -u fragdash git pull --ff-only
sudo -u fragdash "$UV" sync --frozen

# Pick up any changes to the service files themselves.
cp deploy/reddit-notifier.service deploy/reddit-notifier-web.service /etc/systemd/system/
systemctl daemon-reload
systemctl restart reddit-notifier reddit-notifier-web

sleep 3
systemctl --no-pager --lines=5 status reddit-notifier reddit-notifier-web
