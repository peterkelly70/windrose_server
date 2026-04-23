#!/usr/bin/env python3
import json
import os
import re
import shutil
import subprocess
import secrets
import threading
import zipfile
from io import BytesIO
from datetime import datetime, timezone, timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, Response, flash, redirect, render_template, request, send_file, session, url_for

try:
    from map_data import get_map_data
except ModuleNotFoundError:
    from panel.map_data import get_map_data


ROOT = Path("/home/windrose")
SCRIPT = ROOT / "windrose-server.sh"
SERVER_FILES = ROOT / "server-files"
SERVER_JSON = SERVER_FILES / "R5" / "ServerDescription.json"
ENV_FILE = ROOT / ".env"
GAME_LOG = SERVER_FILES / "R5" / "Saved" / "Logs" / "R5.log"
STEAM_MANIFEST = SERVER_FILES / "steamapps" / "appmanifest_4129620.acf"
BACKUP_DIR = ROOT / "backups"
COMPOSE_DIR = ROOT
WINDROSE_PLUS_DATA = ROOT / "windrose_plus_data"
PUBLIC_LIVEMAP = ROOT / "panel" / "static" / "windroseplus" / "livemap" / "index.html"
WORLDS_DIR = SERVER_FILES / "R5" / "Saved" / "SaveProfiles" / "Default" / "RocksDB" / "0.10.0" / "Worlds"
MIGRATION_WORLD_TARGET = Path("server-files") / "R5" / "Saved" / "SaveProfiles" / "Default" / "RocksDB" / "0.10.0" / "Worlds"
BROCCOLI_WORLD_TARGET = Path("server-files") / "R5" / "Saved" / "SaveProfiles" / "Default" / "RocksDB" / "0.10.0" / "Worlds"
WORLD_SETTING_LABELS = {
    "WDS.Parameter.MobHealthMultiplier": "Creature Health",
    "WDS.Parameter.MobDamageMultiplier": "Creature Damage",
    "WDS.Parameter.ShipsHealthMultiplier": "Ship Health",
    "WDS.Parameter.ShipsDamageMultiplier": "Ship Damage",
    "WDS.Parameter.BoardingDifficultyMultiplier": "Boarding Difficulty",
    "WDS.Parameter.Coop.StatsCorrectionModifier": "Player Stat Scaling",
    "WDS.Parameter.Coop.ShipStatsCorrectionModifier": "Ship Stat Scaling",
}

APP_USER = os.environ.get("WINDROSE_PANEL_USER", "")
APP_PASSWORD = os.environ.get("WINDROSE_PANEL_PASSWORD", "")
LOG_LINES = int(os.environ.get("WINDROSE_PANEL_LOG_LINES", "180"))
GAME_LOG_BYTES = int(os.environ.get("WINDROSE_PANEL_GAME_LOG_BYTES", str(4 * 1024 * 1024)))

app = Flask(__name__)
app.secret_key = os.environ.get("WINDROSE_PANEL_SECRET") or secrets.token_hex(32)


TIMESTAMP_RE = re.compile(r"\[(\d{4}\.\d{2}\.\d{2}-\d{2}\.\d{2}\.\d{2}:\d{3})\]")
READY_RE = re.compile(r"ServerAccount\. AccountName '([^']+)'\. AccountId ([A-F0-9]+)\.")
ACCOUNT_LINE_RE = re.compile(
    r"Name '([^']+)'\. AccountId '([A-F0-9]+)'\. State '([^']+)'.*?"
    r"TimeInGame ([+0-9:.]+).*?TimeOnServer ([+0-9:.]+).*?FarewellReason\s*(.*)$"
)
DISCONNECT_RE = re.compile(r"(?:Account disconnected\. AccountId|Disconnect AccountId) ([A-F0-9]+)")
STATE_RE = re.compile(r"Server\. Change state .*?=>\s*([^\s]+)")
SESSION_RE = re.compile(r"BLSessionId ([A-Za-z0-9]+)")
LOG_TIME_RE = re.compile(r"^\[(\d{4})\.(\d{2})\.(\d{2})-(\d{2})\.(\d{2})\.(\d{2})")
P2P_DELAY_RE = re.compile(r"(?:Send|Read|Receive|Call receive|Pending data check|Pending data receive):? (\d+) msec")
GAME_VERSION_RE = re.compile(
    r"GameVersion (?P<game>\S+).*?ReleaseVersion (?P<release>\S+).*?DeploymentId (?P<deployment>\S+)"
)
STEAM_BUILD_RE = re.compile(r'"buildid"\s+"([^"]+)"')
STEAM_TARGET_BUILD_RE = re.compile(r'"TargetBuildID"\s+"([^"]+)"')


def run_command(args, timeout=30):
    proc = subprocess.run(
        args,
        cwd=COMPOSE_DIR,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    return {
        "ok": proc.returncode == 0,
        "code": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def run_command_ok(args, timeout=10):
    try:
        return run_command(args, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": 124, "stdout": "", "stderr": "Command timed out."}


def log_datetime(line):
    match = LOG_TIME_RE.match(line)
    if not match:
        return None
    year, month, day, hour, minute, second = map(int, match.groups())
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


def backup_files():
    patterns = ("windrose-*.tar.gz", "wayward-winds-*.tar.gz")
    backups = []
    for pattern in patterns:
        backups.extend(BACKUP_DIR.glob(pattern))
    return sorted(set(backups), key=lambda p: p.stat().st_mtime, reverse=True)


def create_spot_backup(label="spot"):
    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    archive = BACKUP_DIR / f"windrose-{label}-{timestamp}.tar.gz"
    tmp_archive = archive.with_suffix(archive.suffix + ".tmp")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    result = run_command(["tar", "-czf", str(tmp_archive), "-C", str(SERVER_FILES), "R5"], timeout=900)
    if result["ok"]:
        tmp_archive.replace(archive)
    else:
        tmp_archive.unlink(missing_ok=True)
    return result


def restore_latest_backup():
    backups = backup_files()
    if not backups:
        return {"ok": False, "stderr": "No backups found.", "stdout": "", "code": 1}

    latest = backups[0]
    safety = create_spot_backup("pre-restore")
    if not safety["ok"]:
        return safety

    stopped = docker_compose("stop", "windrose", timeout=120)
    if not stopped["ok"]:
        return stopped

    target = SERVER_FILES / "R5"
    if target.exists():
        shutil.rmtree(target)

    restored = run_command(["tar", "-xzf", str(latest), "-C", str(SERVER_FILES)], timeout=900)
    if not restored["ok"]:
        docker_compose("start", "windrose", timeout=120)
        return restored

    started = docker_compose("start", "windrose", timeout=120)
    if not started["ok"]:
        return started

    return {
        "ok": True,
        "code": 0,
        "stdout": f"Restored {latest.name}. A pre-restore backup was created first.",
        "stderr": "",
    }


def run_background(action_name, target):
    def worker():
        try:
            result = target()
            level = "OK" if result["ok"] else "FAILED"
            detail = result["stdout"] or result["stderr"]
        except Exception as exc:
            level = "FAILED"
            detail = str(exc)
        app.logger.warning("%s %s: %s", action_name, level, detail)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()


def docker_compose(*args, timeout=30):
    return run_command(["docker", "compose", *args], timeout=timeout)


def read_tail(path, max_bytes):
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()
            return fh.read().decode("utf-8", "replace")
    except FileNotFoundError:
        return ""
    except OSError as exc:
        return f"Unable to read {path}: {exc}"


def read_json_file(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        return {"error": f"Unable to parse {path.name}: {exc}"}
    except OSError as exc:
        return {"error": f"Unable to read {path.name}: {exc}"}


def line_time(line):
    match = TIMESTAMP_RE.search(line)
    if not match:
        return ""
    return match.group(1).replace(".", ":", 2).replace("-", " ")


def read_config():
    try:
        data = json.loads(SERVER_JSON.read_text())
        desc = data.get("ServerDescription_Persistent", {})
    except Exception as exc:
        return {"error": str(exc)}

    return {
        "server_name": desc.get("ServerName", ""),
        "invite_code": desc.get("InviteCode", ""),
        "password_protected": desc.get("IsPasswordProtected", False),
        "password": desc.get("Password", ""),
        "max_players": desc.get("MaxPlayerCount", ""),
        "world_id": desc.get("WorldIslandId", ""),
        "p2p_proxy": desc.get("P2pProxyAddress", ""),
        "use_direct_connection": desc.get("UseDirectConnection", False),
        "direct_connection_server_address": desc.get("DirectConnectionServerAddress", ""),
        "direct_connection_server_port": desc.get("DirectConnectionServerPort", 7777),
        "direct_connection_proxy_address": desc.get("DirectConnectionProxyAddress", "0.0.0.0"),
    }


def read_env_file(path):
    values = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return values
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key] = value
    return values


def write_env_file(path, updates):
    existing_lines = []
    try:
        existing_lines = path.read_text().splitlines()
    except OSError:
        pass

    seen = set()
    output = []
    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            output.append(line)
            continue
        key, _ = stripped.split("=", 1)
        if key in updates:
            output.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            output.append(line)

    for key, value in updates.items():
        if key not in seen:
            output.append(f"{key}={value}")

    path.write_text("\n".join(output) + "\n")


def load_server_description():
    data = read_json_file(SERVER_JSON)
    if data is None or "error" in data:
        raise ValueError(data.get("error", f"Unable to read {SERVER_JSON}") if isinstance(data, dict) else f"Unable to read {SERVER_JSON}")
    data.setdefault("ServerDescription_Persistent", {})
    return data


def save_server_description(data):
    tmp = SERVER_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent="\t") + "\n")
    tmp.replace(SERVER_JSON)


def active_world_description_path(world_id=None):
    world_id = world_id or read_config().get("world_id", "")
    if not world_id:
        return None
    return WORLDS_DIR / world_id / "WorldDescription.json"


def world_setting_key(tag_name):
    return json.dumps({"TagName": tag_name}, separators=(", ", ": "))


def read_world_settings():
    path = active_world_description_path()
    if path is None:
        return {"error": "No active world ID is configured.", "float_parameters": []}
    data = read_json_file(path)
    if data is None or "error" in data:
        return {"error": data.get("error", f"Unable to read {path}") if isinstance(data, dict) else f"Unable to read {path}", "float_parameters": []}

    params = data.get("WorldDescription", {}).get("WorldSettings", {}).get("FloatParameters", {})
    values = []
    for tag_name, label in WORLD_SETTING_LABELS.items():
        key = world_setting_key(tag_name)
        values.append({
            "tag": tag_name,
            "field": "world_float_" + re.sub(r"[^A-Za-z0-9_]", "_", tag_name),
            "key": key,
            "label": label,
            "value": params.get(key, 1),
        })
    return {"error": "", "path": str(path), "float_parameters": values}


def update_world_settings(form):
    path = active_world_description_path()
    if path is None:
        raise ValueError("No active world ID is configured.")
    data = read_json_file(path)
    if data is None or "error" in data:
        raise ValueError(data.get("error", f"Unable to read {path}") if isinstance(data, dict) else f"Unable to read {path}")

    world = data.setdefault("WorldDescription", {})
    settings = world.setdefault("WorldSettings", {})
    params = settings.setdefault("FloatParameters", {})

    for tag_name in WORLD_SETTING_LABELS:
        field = "world_float_" + re.sub(r"[^A-Za-z0-9_]", "_", tag_name)
        raw = form.get(field)
        if raw is None or raw == "":
            continue
        try:
            value = float(raw)
        except ValueError as exc:
            raise ValueError(f"{WORLD_SETTING_LABELS[tag_name]} must be a number.") from exc
        if value < 0 or value > 20:
            raise ValueError(f"{WORLD_SETTING_LABELS[tag_name]} must be between 0 and 20.")
        params[world_setting_key(tag_name)] = value

    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent="\t") + "\n")
    tmp.replace(path)


def update_server_settings(form):
    data = load_server_description()
    desc = data["ServerDescription_Persistent"]

    server_name = form.get("server_name", "").strip() or "Wayward Winds"
    invite_code = form.get("invite_code", "").strip()
    password = form.get("password", "")
    p2p_proxy = form.get("p2p_proxy", "").strip() or "127.0.0.1"
    direct_address = form.get("direct_connection_server_address", "").strip()
    direct_proxy = form.get("direct_connection_proxy_address", "").strip() or "0.0.0.0"

    try:
        max_players = max(1, min(32, int(form.get("max_players", "8"))))
    except ValueError as exc:
        raise ValueError("Max players must be a number.") from exc

    try:
        direct_port = max(1, min(65535, int(form.get("direct_connection_server_port", "7777"))))
    except ValueError as exc:
        raise ValueError("Direct connection port must be a number.") from exc

    use_direct = form.get("use_direct_connection") == "on"

    desc["ServerName"] = server_name
    desc["InviteCode"] = invite_code
    desc["IsPasswordProtected"] = bool(password)
    desc["Password"] = password
    desc["MaxPlayerCount"] = max_players
    desc["P2pProxyAddress"] = p2p_proxy
    desc["UseDirectConnection"] = use_direct
    desc["DirectConnectionServerAddress"] = direct_address
    desc["DirectConnectionServerPort"] = direct_port
    desc["DirectConnectionProxyAddress"] = direct_proxy

    save_server_description(data)
    update_world_settings(form)
    write_env_file(ENV_FILE, {
        "SERVER_NAME": server_name,
        "INVITE_CODE": invite_code,
        "SERVER_PASSWORD": password,
        "MAX_PLAYERS": str(max_players),
        "P2P_PROXY_ADDRESS": p2p_proxy,
        "GENERATE_SETTINGS": "false",
    })

    return {
        "ok": True,
        "code": 0,
        "stdout": "Settings saved. Restart the server for all changes to apply.",
        "stderr": "",
    }


def world_summary():
    worlds = []
    if WORLDS_DIR.is_dir():
        for path in sorted(WORLDS_DIR.iterdir()):
            if not path.is_dir():
                continue
            desc = read_json_file(path / "WorldDescription.json") or {}
            world_desc = desc.get("WorldDescription", {}) if isinstance(desc, dict) else {}
            worlds.append({
                "id": path.name,
                "size": directory_size(path),
                "mtime": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "preset": world_desc.get("WorldPresetType", "unknown"),
                "name": world_desc.get("WorldName", ""),
            })
    return worlds


def directory_size(path):
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    if total >= 1024 * 1024:
        return f"{total / 1024 / 1024:.1f} MB"
    return f"{total / 1024:.0f} KB"


def create_new_world():
    safety = create_spot_backup("pre-new-world")
    if not safety["ok"]:
        return safety

    stopped = run_command(["bash", str(SCRIPT), "stop"], timeout=180)
    if not stopped["ok"]:
        return stopped

    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    archive_root = BACKUP_DIR / "archived-worlds" / timestamp
    archive_root.mkdir(parents=True, exist_ok=True)

    moved = []
    if WORLDS_DIR.is_dir():
        for path in sorted(WORLDS_DIR.iterdir()):
            if path.is_dir():
                shutil.move(str(path), str(archive_root / path.name))
                moved.append(path.name)

    data = load_server_description()
    desc = data["ServerDescription_Persistent"]
    desc["WorldIslandId"] = ""
    save_server_description(data)
    write_env_file(ENV_FILE, {"GENERATE_SETTINGS": "false"})

    started = run_command(["bash", str(SCRIPT), "start"], timeout=900)
    if not started["ok"]:
        return {
            "ok": False,
            "code": started["code"],
            "stdout": f"Archived worlds to {archive_root}. Server start failed.",
            "stderr": started["stderr"] or started["stdout"],
        }

    return {
        "ok": True,
        "code": 0,
        "stdout": f"Archived {len(moved)} world folder(s) to {archive_root} and started the server. Check logs for the newly generated world ID.",
        "stderr": "",
    }


def version_summary():
    text = read_tail(GAME_LOG, GAME_LOG_BYTES)
    versions = {
        "game_version": "unknown",
        "release_version": "unknown",
        "deployment_id": "unknown",
        "steam_build": "unknown",
        "steam_target_build": "unknown",
    }

    for line in text.splitlines():
        match = GAME_VERSION_RE.search(line)
        if match:
            versions["game_version"] = match.group("game")
            versions["release_version"] = match.group("release")
            versions["deployment_id"] = match.group("deployment")

    try:
        manifest = STEAM_MANIFEST.read_text()
    except OSError:
        manifest = ""

    build = STEAM_BUILD_RE.search(manifest)
    target = STEAM_TARGET_BUILD_RE.search(manifest)
    if build:
        versions["steam_build"] = build.group(1)
    if target:
        versions["steam_target_build"] = target.group(1)

    return versions


def docker_status():
    inspect = run_command(
        [
            "docker",
            "inspect",
            "windrose",
            "--format",
            "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}no-health{{end}}|{{.State.StartedAt}}|{{.HostConfig.RestartPolicy.Name}}",
        ],
        timeout=10,
    )
    ps = docker_compose("ps", timeout=10)

    if not inspect["ok"]:
        return {
            "container": "missing",
            "health": "unknown",
            "started_at": "",
            "restart_policy": "",
            "ps": ps["stdout"] or ps["stderr"],
        }

    fields = inspect["stdout"].split("|")
    return {
        "container": fields[0] if len(fields) > 0 else "unknown",
        "health": fields[1] if len(fields) > 1 else "unknown",
        "started_at": fields[2] if len(fields) > 2 else "",
        "restart_policy": fields[3] if len(fields) > 3 else "",
        "ps": ps["stdout"] or ps["stderr"],
    }


def docker_logs():
    result = docker_compose("logs", "--tail", str(LOG_LINES), "windrose", timeout=15)
    return result["stdout"] or result["stderr"]


def game_summary():
    text = read_tail(GAME_LOG, GAME_LOG_BYTES)
    players = {}
    disconnected = {}
    latest_errors = []
    last_server_state = "unknown"
    last_session = ""
    last_terrain = ""
    last_ready = ""
    last_log_time = ""

    for line in text.splitlines():
        stamp = line_time(line)
        if stamp:
            last_log_time = stamp

        session = SESSION_RE.search(line)
        if session and "Server registration finished successfully" in line:
            last_session = session.group(1)

        state = STATE_RE.search(line)
        if state:
            last_server_state = state.group(1)

        if "Start Terrain Generation" in line:
            last_terrain = f"Started {stamp}" if stamp else "Started"
        elif "Generate all Terrains took" in line:
            detail = line.strip().split("Generate all Terrains took", 1)[-1].split("[")[0].strip()
            last_terrain = f"Generated in {detail}"

        ready = READY_RE.search(line)
        if ready:
            name, account_id = ready.groups()
            players[account_id] = {
                "name": name,
                "account_id": account_id,
                "state": "ReadyToPlay",
                "time_in_game": "",
                "time_on_server": "",
                "last_seen": stamp,
            }
            disconnected.pop(account_id, None)
            last_ready = stamp

        account_line = ACCOUNT_LINE_RE.search(line)
        if account_line:
            name, account_id, state_name, time_in_game, time_on_server, farewell = account_line.groups()
            if state_name == "ReadyToPlay" and not farewell.strip():
                players[account_id] = {
                    "name": name,
                    "account_id": account_id,
                    "state": state_name,
                    "time_in_game": time_in_game,
                    "time_on_server": time_on_server,
                    "last_seen": stamp,
                }
                disconnected.pop(account_id, None)
            else:
                disconnected[account_id] = name
                players.pop(account_id, None)

        disconnect = DISCONNECT_RE.search(line)
        if disconnect:
            account_id = disconnect.group(1)
            name = players.get(account_id, {}).get("name", account_id)
            disconnected[account_id] = name
            players.pop(account_id, None)

        if any(token in line for token in ("Fatal error", "GsStream is broken", "GcStream is broken", "Server Authorization failed", "ResponseCode 503")):
            latest_errors.append(f"{stamp} {line.strip()}".strip())
            latest_errors = latest_errors[-6:]

    player_list = sorted(players.values(), key=lambda item: item["name"].lower())
    return {
        "players": player_list,
        "online_count": len(player_list),
        "last_server_state": last_server_state,
        "last_session": last_session,
        "last_terrain": last_terrain or "unknown",
        "last_ready": last_ready,
        "last_log_time": last_log_time,
        "latest_errors": latest_errors,
    }


def backup_summary():
    backups = backup_files()
    if not backups:
        return {"count": 0, "latest": "none", "latest_size": ""}
    latest = backups[0]
    return {
        "count": len(backups),
        "latest": latest.name,
        "latest_size": f"{latest.stat().st_size / 1024:.0f} KB",
    }


def windrose_pid():
    result = run_command_ok(["ps", "-eo", "pid=,stat=,cmd="], timeout=5)
    if not result["ok"] or not result["stdout"]:
        return ""

    fallbacks = []
    for line in result["stdout"].splitlines():
        parts = line.strip().split(maxsplit=2)
        if len(parts) < 3:
            continue
        pid, stat, cmd = parts
        if stat.startswith("Z"):
            continue
        if "WindroseServer-Win64-Shipping.exe" not in cmd and "WindroseServer-Linux" not in cmd:
            continue
        if "xvfb-run" in cmd or cmd.startswith("start.exe"):
            fallbacks.append(pid)
            continue
        return pid
    if fallbacks:
        return fallbacks[0]
    return ""


def process_monitor(pid):
    if not pid:
        return {"running": False, "pid": "", "summary": "not running", "io": ""}

    ps = run_command_ok(["ps", "-p", pid, "-o", "pid=,stat=,etimes=,%cpu=,%mem=,rss=,cmd="], timeout=5)
    io = run_command_ok(["pidstat", "-d", "-p", pid, "1", "1"], timeout=5)
    io_line = ""
    if io["ok"]:
        for line in io["stdout"].splitlines():
            if pid in line and "UID" not in line:
                io_line = " ".join(line.split())

    summary = " ".join(ps["stdout"].split()) if ps["ok"] and ps["stdout"] else "unknown"
    return {
        "running": bool(ps["ok"] and ps["stdout"]),
        "pid": pid,
        "summary": summary,
        "io": io_line,
    }


def disk_monitor():
    df = run_command_ok(["df", "-h", "/home/windrose"], timeout=5)
    usage = "unknown"
    if df["ok"] and df["stdout"]:
        lines = df["stdout"].splitlines()
        if len(lines) >= 2:
            parts = lines[-1].split()
            if len(parts) >= 5:
                usage = f"{parts[2]} used / {parts[1]} total ({parts[4]}), {parts[3]} free"

    iostat = run_command_ok(["iostat", "-xz", "1", "2"], timeout=5)
    device = ""
    if iostat["ok"]:
        capture = False
        for line in iostat["stdout"].splitlines():
            if line.startswith("Device"):
                capture = True
                continue
            if capture and re.match(r"^(sd|nvme|vd)", line):
                device = " ".join(line.split())

    return {"usage": usage, "device_io": device}


def log_performance_summary(window_seconds=300):
    text = read_tail(GAME_LOG, max(GAME_LOG_BYTES, 6 * 1024 * 1024))
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=window_seconds)
    db_slow = 0
    db_extreme = 0
    p2p_delay_lines = 0
    max_p2p_delay_ms = 0
    latest_lines = []

    for line in text.splitlines():
        stamp = log_datetime(line)
        if stamp is None or stamp < cutoff:
            continue

        if "R5BLDalAsyncQueue::DetectProblems" in line and "commitT" in line:
            if "Slow task" in line or "EXTREMELY slow task" in line:
                db_slow += 1
                latest_lines.append(line.strip())
            if "EXTREMELY slow task" in line:
                db_extreme += 1

        if "Delay between datagrams" in line:
            delays = [int(value) for value in P2P_DELAY_RE.findall(line)]
            line_max = max(delays) if delays else 0
            max_p2p_delay_ms = max(max_p2p_delay_ms, line_max)
            if line_max >= 30000:
                p2p_delay_lines += 1
                latest_lines.append(line.strip())

    status = "good"
    if db_extreme >= 3 or db_slow >= 20 or p2p_delay_lines >= 3:
        status = "bad"
    elif db_extreme or db_slow >= 5 or p2p_delay_lines:
        status = "warn"

    return {
        "window_seconds": window_seconds,
        "status": status,
        "db_slow": db_slow,
        "db_extreme": db_extreme,
        "p2p_delay_lines": p2p_delay_lines,
        "max_p2p_delay_ms": max_p2p_delay_ms,
        "latest_lines": latest_lines[-8:],
    }


def monitor_summary():
    pid = windrose_pid()
    return {
        "process": process_monitor(pid),
        "disk": disk_monitor(),
        "performance": log_performance_summary(),
    }


def migration_install_sh():
    target = MIGRATION_WORLD_TARGET.as_posix()
    broccoli_target = BROCCOLI_WORLD_TARGET.as_posix()
    return f"""#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SERVER_ROOT=${{1:-"$SCRIPT_DIR"}}
SOURCE="$SCRIPT_DIR/migration/Worlds"

if [ -f "$SERVER_ROOT/docker-compose.yml" ] || [ -f "$SERVER_ROOT/compose.yml" ] || [ -f "$SERVER_ROOT/compose.yaml" ]; then
  if grep -R "server-files:/home/steam/server-files\\|indifferentbroccoli/windrose-server-docker" "$SERVER_ROOT/docker-compose.yml" "$SERVER_ROOT/compose.yml" "$SERVER_ROOT/compose.yaml" >/dev/null 2>&1; then
    TARGET="$SERVER_ROOT/{broccoli_target}"
  else
    TARGET="$SERVER_ROOT/{target}"
  fi
else
  TARGET="$SERVER_ROOT/{target}"
fi

if [ ! -d "$SOURCE" ]; then
  echo "Missing $SOURCE"
  exit 1
fi

if command -v docker >/dev/null 2>&1 && docker ps --format '{{{{.Names}}}}' | grep -qx windrose; then
  echo "The windrose Docker container is running. Stop it before installing world files."
  exit 1
fi

mkdir -p "$TARGET"
if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete "$SOURCE"/ "$TARGET"/
else
  find "$TARGET" -mindepth 1 -maxdepth 1 -exec rm -rf {{}} +
  cp -a "$SOURCE"/. "$TARGET"/
fi

echo "World files installed to $TARGET"
"""


def migration_install_bat():
    target = str(MIGRATION_WORLD_TARGET).replace("/", "\\")
    broccoli_target = str(BROCCOLI_WORLD_TARGET).replace("/", "\\")
    return f"""@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
if "%~1"=="" (
  set "SERVER_ROOT=%SCRIPT_DIR:~0,-1%"
) else (
  set "SERVER_ROOT=%~1"
)

set "SOURCE=%SCRIPT_DIR%migration\\Worlds"
set "TARGET=%SERVER_ROOT%\\{target}"

if exist "%SERVER_ROOT%\\docker-compose.yml" (
  findstr /i /c:"server-files:/home/steam/server-files" /c:"indifferentbroccoli/windrose-server-docker" "%SERVER_ROOT%\\docker-compose.yml" >nul 2>nul
  if not errorlevel 1 set "TARGET=%SERVER_ROOT%\\{broccoli_target}"
)

if not exist "%SOURCE%\\" (
  echo Missing "%SOURCE%"
  exit /b 1
)

where docker >nul 2>nul
if not errorlevel 1 (
  for /f "delims=" %%C in ('docker ps --format "{{{{.Names}}}}" 2^>nul') do (
    if /i "%%C"=="windrose" (
      echo The windrose Docker container is running. Stop it before installing world files.
      exit /b 1
    )
  )
)

mkdir "%TARGET%" 2>nul
robocopy "%SOURCE%" "%TARGET%" /MIR
if errorlevel 8 exit /b %errorlevel%

echo World files installed to "%TARGET%"
exit /b 0
"""


def build_world_migration_zip():
    if not WORLDS_DIR.is_dir():
        return None, f"Worlds directory not found: {WORLDS_DIR}"

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr("README.txt", "\n".join([
            "Windrose world migration bundle",
            "",
            "Extract this zip into the target Windrose server root, then run:",
            "  Linux/macOS: sh install-world.sh",
            "  Windows: install-world.bat",
            "",
            "You can also pass the server root as the first argument:",
            "  sh install-world.sh /path/to/windrose",
            "  install-world.bat C:\\path\\to\\windrose",
            "",
            "The installer supports two Docker layouts:",
            f"  Current panel layout: {MIGRATION_WORLD_TARGET.as_posix()}",
            f"  indifferentbroccoli Docker layout: {BROCCOLI_WORLD_TARGET.as_posix()}",
            "",
            "Stop the game server before installing world files.",
            "",
        ]))
        archive.writestr("install-world.sh", migration_install_sh())
        archive.writestr("install-world.bat", migration_install_bat())

        for path in sorted(WORLDS_DIR.rglob("*")):
            if path.is_file():
                rel = path.relative_to(WORLDS_DIR)
                archive.write(path, Path("migration") / "Worlds" / rel)

    buffer.seek(0)
    return buffer, ""


def check_auth(username, password):
    return secrets.compare_digest(username, APP_USER) and secrets.compare_digest(password, APP_PASSWORD)


def require_login(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


@app.get("/login")
def login():
    if session.get("authenticated"):
        return redirect(url_for("index"))
    return render_template("login.html")


@app.post("/login")
def login_post():
    username = request.form.get("username", "")
    password = request.form.get("password", "")
    next_url = request.args.get("next") or url_for("index")

    if check_auth(username, password):
        session.clear()
        session["authenticated"] = True
        session["username"] = username
        flash("Logged in.", "good")
        return redirect(next_url)

    flash("Invalid username or password.", "bad")
    return redirect(url_for("login"))


@app.post("/logout")
@require_login
def logout():
    session.clear()
    flash("Logged out.", "good")
    return redirect(url_for("login"))


@app.route("/")
@require_login
def index():
    return render_template(
        "index.html",
        config=read_config(),
        status=docker_status(),
        game=game_summary(),
        version=version_summary(),
        backups=backup_summary(),
        monitor=monitor_summary(),
        worlds=world_summary(),
        world_settings=read_world_settings(),
        env=read_env_file(ENV_FILE),
        logs=docker_logs(),
        checked_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


@app.post("/action/<action>")
@require_login
def action(action):
    allowed = {
        "start": ["bash", str(SCRIPT), "start"],
        "stop": ["bash", str(SCRIPT), "stop"],
        "restart": ["bash", str(SCRIPT), "restart"],
        "update-check": ["bash", str(SCRIPT), "update-check"],
        "update": ["bash", str(SCRIPT), "update"],
        "notify-test": ["bash", str(SCRIPT), "notify-test"],
    }

    special_actions = {
        "spot-backup": create_spot_backup,
        "spot-restore": restore_latest_backup,
        "new-world": create_new_world,
    }

    if action in special_actions:
        run_background(action, special_actions[action])
        flash(f"{action} started. Refresh in a minute or two to see the latest backup.", "good")
        return redirect(url_for("index"))

    if action not in allowed:
        flash(f"Unknown action: {action}", "bad")
        return redirect(url_for("index"))

    timeout = 900 if action in {"update", "update-check"} else 60
    result = run_command(allowed[action], timeout=timeout)
    if result["ok"]:
        flash(f"{action} completed.", "good")
    else:
        flash(f"{action} failed: {result['stderr'] or result['stdout']}", "bad")
    return redirect(url_for("index"))


@app.post("/settings")
@require_login
def settings():
    try:
        result = update_server_settings(request.form)
    except Exception as exc:
        flash(f"Settings update failed: {exc}", "bad")
        return redirect(url_for("index", tab="setup"))

    flash(result["stdout"], "good")
    return redirect(url_for("index", tab="setup"))


@app.get("/api/status")
@require_login
def api_status():
    body = {
        "config": read_config(),
        "status": docker_status(),
        "game": game_summary(),
        "version": version_summary(),
        "backups": backup_summary(),
        "monitor": monitor_summary(),
        "worlds": world_summary(),
        "world_settings": read_world_settings(),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    return app.response_class(json.dumps(body, indent=2), mimetype="application/json")


@app.get("/api/monitor")
@require_login
def api_monitor():
    return app.response_class(json.dumps(monitor_summary(), indent=2), mimetype="application/json")


@app.get("/api/logs")
@require_login
def api_logs():
    return Response(docker_logs(), mimetype="text/plain")


@app.get("/download/world-migration")
@require_login
def download_world_migration():
    bundle, error = build_world_migration_zip()
    if bundle is None:
        flash(error, "bad")
        return redirect(url_for("index"))

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return send_file(
        bundle,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"windrose-world-migration-{timestamp}.zip",
    )


@app.get("/map")
def public_livemap():
    return send_file(PUBLIC_LIVEMAP)


@app.get("/livemap")
def public_livemap_alias():
    return send_file(PUBLIC_LIVEMAP)


@app.get("/chart")
def static_server_map():
    return render_template("static_map.html")


@app.get("/api/static-map")
def api_static_map():
    return app.response_class(json.dumps(get_map_data()), mimetype="application/json")


@app.get("/api/mapinfo")
def public_mapinfo():
    data = read_json_file(WINDROSE_PLUS_DATA / "map_coords.json")
    if data is None:
        data = {"error": "Map not ready yet. WindrosePlus has not generated windrose_plus_data/map_coords.json."}
    return app.response_class(json.dumps(data), mimetype="application/json")


@app.get("/api/livemap")
def public_livemap_data():
    data = read_json_file(WINDROSE_PLUS_DATA / "livemap_data.json")
    if data is None:
        data = {"error": "No livemap data yet.", "players": [], "mobs": []}
    return app.response_class(json.dumps(data), mimetype="application/json")


@app.get("/livemap/tiles/<int:zoom>/<tile_name>")
def public_livemap_tile(zoom, tile_name):
    if not re.fullmatch(r"\d+-\d+\.png", tile_name):
        return Response("Invalid tile", status=400, mimetype="text/plain")

    tile_path = WINDROSE_PLUS_DATA / "map_tiles" / str(zoom) / tile_name
    if not tile_path.is_file():
        return Response("", status=404)
    return send_file(tile_path, mimetype="image/png")


if __name__ == "__main__":
    host = os.environ.get("WINDROSE_PANEL_HOST", "0.0.0.0")
    port = int(os.environ.get("WINDROSE_PANEL_PORT", "8091"))
    app.run(host=host, port=port)
