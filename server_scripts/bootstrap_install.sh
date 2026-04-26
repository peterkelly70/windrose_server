#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/windrose"
PANEL_DIR="$ROOT/panel"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root, or allow the panel user to run this script with sudo."
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y ca-certificates curl gnupg jq python3 python3-venv rsync sysstat

if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

getent group docker >/dev/null || groupadd docker
usermod -aG docker windrose || true
if [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER}" != "root" ] && [ "${SUDO_USER}" != "windrose" ]; then
  usermod -aG docker "${SUDO_USER}" || true
fi

mkdir -p "$ROOT/server-files" "$ROOT/backups" "$ROOT/server_scripts"
chown -R windrose:windrose "$ROOT/server-files" "$ROOT/backups"

if [ ! -d "$PANEL_DIR/.venv" ]; then
  python3 -m venv "$PANEL_DIR/.venv"
fi
"$PANEL_DIR/.venv/bin/pip" install --upgrade pip
"$PANEL_DIR/.venv/bin/pip" install -r "$PANEL_DIR/requirements.txt"

if [ ! -f "$PANEL_DIR/.env" ]; then
  cat > "$PANEL_DIR/.env" <<'ENV'
WINDROSE_PANEL_HOST=0.0.0.0
WINDROSE_PANEL_PORT=8091
WINDROSE_PANEL_USER=windrose
WINDROSE_PANEL_PASSWORD=change-this
WINDROSE_PANEL_SECRET=change-this-to-a-long-random-string
WINDROSE_PANEL_LOG_LINES=180
ENV
  chown windrose:windrose "$PANEL_DIR/.env"
  chmod 600 "$PANEL_DIR/.env"
fi

if [ ! -f "$ROOT/.env" ]; then
  cat > "$ROOT/.env" <<'ENV'
CONTAINER_NAME=windrose
IMAGE_REPOSITORY=indifferentbroccoli/windrose-server-docker
IMAGE_TAG=latest
PUID=1023
PGID=1023
UPDATE_ON_START=false
GENERATE_SETTINGS=true
INVITE_CODE=
SERVER_NAME=Wayward Winds
SERVER_PASSWORD=
MAX_PLAYERS=8
P2P_PROXY_ADDRESS=127.0.0.1
ENV
  chown windrose:windrose "$ROOT/.env"
  chmod 640 "$ROOT/.env"
fi

cat > /etc/systemd/system/windrose-panel.service <<'UNIT'
[Unit]
Description=Windrose Dedicated Server Web Panel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=windrose
Group=windrose
SupplementaryGroups=docker
WorkingDirectory=/home/windrose/panel
EnvironmentFile=/home/windrose/panel/.env
ExecStart=/home/windrose/panel/.venv/bin/gunicorn --workers 2 --bind 0.0.0.0:8091 --access-logfile - --error-logfile - app:app
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/windrose-monitor.service <<'UNIT'
[Unit]
Description=Monitor Wayward Winds server health and recover broken backend queue
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=windrose
Group=windrose
SupplementaryGroups=docker
WorkingDirectory=/home/windrose
ExecStart=/home/windrose/server_scripts/monitor_server.sh
TimeoutStartSec=900
UNIT

cat > /etc/systemd/system/windrose-monitor.timer <<'UNIT'
[Unit]
Description=Run Wayward Winds monitor every minute

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
AccuracySec=15s
Persistent=true

[Install]
WantedBy=timers.target
UNIT

cat > /etc/systemd/system/windrose-world-scheduler.service <<'UNIT'
[Unit]
Description=Apply scheduled Windrose world rotation
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=windrose
Group=windrose
SupplementaryGroups=docker
WorkingDirectory=/home/windrose
ExecStart=/home/windrose/server_scripts/world_scheduler.py
TimeoutStartSec=900
UNIT

cat > /etc/systemd/system/windrose-world-scheduler.timer <<'UNIT'
[Unit]
Description=Check Windrose world schedule every minute

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
AccuracySec=15s
Persistent=true

[Install]
WantedBy=timers.target
UNIT

cat > /etc/systemd/system/windrose-instance-scheduler.service <<'UNIT'
[Unit]
Description=Apply scheduled Windrose instance activation
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=windrose
Group=windrose
SupplementaryGroups=docker
WorkingDirectory=/home/windrose
ExecStart=/home/windrose/server_scripts/instance_scheduler.py
TimeoutStartSec=900
UNIT

cat > /etc/systemd/system/windrose-instance-scheduler.timer <<'UNIT'
[Unit]
Description=Check Windrose instance schedule every minute

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
AccuracySec=15s
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now windrose-panel.service
systemctl enable --now windrose-monitor.timer
systemctl enable --now windrose-instance-scheduler.timer

docker compose -f "$ROOT/docker-compose.yml" pull windrose

echo "Bootstrap complete. Review /home/windrose/.env and /home/windrose/panel/.env before starting a public server."
echo "Legacy world-scheduler units were installed but are not enabled by default. Use the instance scheduler timer for multi-instance scheduling."
