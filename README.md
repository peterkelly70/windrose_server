# Wayward Winds Windrose Server

Management files for the Wayward Winds Windrose dedicated server.

This repository tracks the local control scripts, Docker Compose file, Flask web panel, monitor, and backup helpers. It intentionally does not track live game data, saves, backups, Steam files, or secrets.

## What Is Tracked

- `docker-compose.yml` - runs the Windrose dedicated server container.
- `windrose-server.sh` - command line control wrapper.
- `server_scripts/` - backup, monitor, Discord notification, and startup helpers.
- `panel/` - Flask web panel, templates, static assets, and requirements.

## What Is Not Tracked

These paths are ignored because they are large, secret, or live runtime data:

- `.env`
- `panel/.env`
- `server_scripts/.discord_webhook`
- `server-files/`
- `data/`
- `backups/`
- `steam-home/`
- `panel/.venv/`
- `panel/static/windroseplus/`

Back up world saves separately before rebuilding a host.

## Required Files

Create `/home/windrose/.env` for the Docker server:

```env
CONTAINER_NAME=windrose
IMAGE_REPOSITORY=indifferentbroccoli/windrose-server-docker
IMAGE_TAG=latest
PUID=1023
PGID=1023
UPDATE_ON_START=false
GENERATE_SETTINGS=false
INVITE_CODE=
SERVER_NAME=Wayward Winds
SERVER_PASSWORD=
MAX_PLAYERS=8
P2P_PROXY_ADDRESS=127.0.0.1
```

Create `/home/windrose/panel/.env` for the web panel:

```env
WINDROSE_PANEL_HOST=0.0.0.0
WINDROSE_PANEL_PORT=8091
WINDROSE_PANEL_USER=windrose
WINDROSE_PANEL_PASSWORD=change-this
WINDROSE_PANEL_SECRET=change-this-to-a-long-random-string
WINDROSE_PANEL_LOG_LINES=180
```

Optional Discord notifications use:

```text
/home/windrose/server_scripts/.discord_webhook
```

Put only the webhook URL in that file.

## Common Commands

Run from `/home/windrose`.

```bash
./windrose-server.sh status
./windrose-server.sh start
./windrose-server.sh stop
./windrose-server.sh restart
./windrose-server.sh update-check
./windrose-server.sh update
./windrose-server.sh logs
```

The stop command disables monitor alerts until the server is started again.

## Web Panel

The panel runs with Gunicorn through `windrose-panel.service` and listens on port `8091` by default.

Useful systemd commands:

```bash
sudo systemctl status windrose-panel.service
sudo systemctl restart windrose-panel.service
sudo journalctl -u windrose-panel.service -n 100 --no-pager
```

Panel endpoints:

- `/` - main server panel.
- `/api/status` - status JSON.
- `/api/monitor` - process, disk, DB lag, and P2P delay JSON.
- `/download/world-migration` - zip current world files plus install scripts.
- `/livemap` - public live map, if WindrosePlus data exists.

## Monitor

`windrose-monitor.timer` runs the monitor every minute.

```bash
systemctl list-timers | grep windrose
sudo systemctl status windrose-monitor.timer
sudo systemctl status windrose-monitor.service
```

The monitor watches:

- container state and Docker health
- player joins/leaves
- backend readiness
- broken backend queue spam
- DB commit lag in `R5.log`
- P2P datagram delay in `R5.log`
- Windrose process CPU, memory, and IO
- disk usage and disk IO

It does not restart for performance hiccups. It only sends alerts. The broken backend queue handler waits for players to leave before restarting.

## Backups

Manual world backup:

```bash
/home/windrose/server_scripts/backup_world.sh
```

Spot backups can also be started from the web panel.

Backups are stored under `/home/windrose/backups`, which is ignored by Git.

## Restore Notes

The active server data lives under:

```text
/home/windrose/server-files
```

The current world directory is:

```text
/home/windrose/server-files/R5/Saved/SaveProfiles/Default/RocksDB/0.10.0/Worlds
```

Stop the server before replacing world files:

```bash
./windrose-server.sh stop
```

Then restore the world data, fix ownership if needed, and start again:

```bash
sudo chown -R windrose:windrose /home/windrose/server-files
./windrose-server.sh start
```

## Git Backup

Initial remote:

```bash
git remote add origin git@github.com:peterkelly70/windrose_server.git
git branch -M main
git push -u origin main
```

Normal update:

```bash
git status
git add .gitignore docker-compose.yml windrose-server.sh panel server_scripts
git commit -m "Update Windrose server management files"
git push
```

Do not force-add ignored files unless you are certain they contain no secrets or live save data.

