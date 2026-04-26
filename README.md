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

## Users And Ownership

Recommended model:

- `windrose` is the runtime owner for the game server, panel service, monitor, scheduler, live configs, and live server data.
- a separate operator/admin account can be used for code edits and host administration
- the operator/admin account can be added to the `windrose` group for read access and optional shared editing where explicitly enabled
- Docker access should be granted to both `windrose` and the chosen operator/admin account through the `docker` group

Current service model:

- `windrose-panel.service` runs as `windrose`
- `windrose-monitor.service` runs as `windrose`
- `windrose-world-scheduler.service` runs as `windrose`
- `windrose-instance-scheduler.service` runs as `windrose`

This is intentional. Running the panel and scheduled jobs as `windrose` avoids mismatches where the web panel can read live server files but cannot update them.

Suggested ownership split:

- repo and templates: editable by the operator/admin account
- live server data under `instances/*/server-files`: owned by `windrose`
- secrets and live env files: owned by `windrose`

If you want group-based manual editing of live files, use the `windrose` group and grant group write on those paths explicitly. The safer default is runtime ownership by `windrose` with limited shared-write only where needed.

## Web Panel

The panel runs with Gunicorn through `windrose-panel.service` and listens on port `8091` by default. It is independent from the game container: stopping, restarting, or updating the game server should not restart the web panel.

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
- `/api/logs` - recent Docker logs as text.
- `/download/world-migration` - zip current world files plus install scripts.
- `/livemap` - public live map, if WindrosePlus data exists.

The panel has tabs for Overview, Setup, Instance Schedule, Monitor, Players, Logs, and Report Bug. Only Overview, Monitor, Players, and Logs refresh live data in the background. Setup and Instance Schedule stay still so forms do not get overwritten while editing.

The Setup tab includes a Bootstrap Install action. It installs Docker, the compose plugin, sysstat, the panel Python environment, systemd services, and pulls the Windrose image. It does not start or restart the game server. For the button to work from the web panel, the `windrose` user must be allowed to run `/home/windrose/server_scripts/bootstrap_install.sh` with passwordless sudo.

The Setup tab can update:

- server name
- invite code
- max players
- server password
- Discord webhook URL
- P2P proxy address
- direct connection settings
- active world scaling values
- creature health and damage scaling
- ship health and damage scaling
- boarding difficulty scaling
- player and ship stat scaling

The Setup tab also lists current world folders and includes a guarded Create New World action. That action makes a spot backup, stops the server, archives current world folders under `/home/windrose/backups/archived-worlds`, clears the active world ID, and starts the server again.

The Worlds section also supports:

- manual switching to another existing world
- a default world used outside schedule windows
- scheduled world windows by weekday and time

`windrose-world-scheduler.timer` checks the legacy world schedule every minute. If the target world changes, it updates `WorldIslandId` and restarts the game server only when the server is already running. If the server is intentionally stopped, it only updates the configured target world.

The Instance Schedule tab edits `config/instances.json` and stores:

- scheduler timezone
- scheduler default instance
- whether the default instance stays up during scheduled windows
- scheduler behavior
- per-instance mode
- per-instance schedule windows

`windrose-instance-scheduler.timer` is the scheduler for multi-instance mode. It reads `config/instances.json`, selects the active instance for the current time, and in `exclusive` mode starts the target instance and stops the other managed instances.

The instance scheduler also sends Discord notifications when the webhook is configured:

- a 15-minute warning before a scheduled stop
- a 15-minute warning before a scheduled start
- a start notification when an instance starts
- a stop notification when an instance stops

If you are using instance scheduling, disable the legacy world scheduler timer:

```bash
sudo systemctl disable --now windrose-world-scheduler.timer
sudo systemctl enable --now windrose-instance-scheduler.timer
```

## Multi-Instance Direction

For safer operation with distinct public worlds, the intended next architecture is separate instances rather than one server rotating between worlds.

Tracked planning files:

- `config/instances.example.json`
- `docs/multi-instance-architecture.md`

Target pair:

- `Wayward Winds`
- `Waylaid Wanderers`

This lets the scheduler start and stop isolated instances instead of changing `WorldIslandId` inside one shared install. The repo can be refactored toward that model without touching the currently running production server until a later test window.

The intended long-term model is:

- instance scheduling controls which server instance is active
- the legacy world scheduler is only kept for the older single-instance world-rotation path
- the panel, scheduler, and monitor all continue to run as `windrose`

## Monitor

`windrose-monitor.timer` runs the monitor every minute.

```bash
systemctl list-timers | grep windrose
sudo systemctl status windrose-monitor.timer
sudo systemctl status windrose-monitor.service
sudo systemctl status windrose-instance-scheduler.timer
sudo systemctl status windrose-world-scheduler.timer
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

DB/P2P hiccups are appended to `/home/windrose/server_scripts/hiccups.log` and displayed under Monitor performance. The monitor does not restart for performance hiccups. It only sends alerts. The broken backend queue handler waits for players to leave before restarting.

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
