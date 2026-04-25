#!/usr/bin/env python3
import json
import subprocess
from datetime import datetime
from pathlib import Path

ROOT = Path("/home/windrose")
SERVER_JSON = ROOT / "server-files" / "R5" / "ServerDescription.json"
SCHEDULE_FILE = ROOT / "data" / "world_schedule.json"
SCRIPT = ROOT / "windrose-server.sh"
WORLDS_DIR = ROOT / "server-files" / "R5" / "Saved" / "SaveProfiles" / "Default" / "RocksDB" / "0.10.0" / "Worlds"
STATE_FILE = ROOT / "server_scripts" / ".last_world_schedule_state"
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def run(cmd, timeout=900):
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    return proc.returncode == 0, proc.stdout.strip(), proc.stderr.strip()


def docker_running():
    ok, out, _err = run(["docker", "inspect", "windrose", "--format", "{{.State.Status}}"], timeout=10)
    return ok and out.strip() == "running"


def current_world_id():
    data = read_json(SERVER_JSON, {})
    return data.get("ServerDescription_Persistent", {}).get("WorldIslandId", "")


def set_world_id(world_id):
    data = read_json(SERVER_JSON, {})
    data.setdefault("ServerDescription_Persistent", {})
    data["ServerDescription_Persistent"]["WorldIslandId"] = world_id
    write_json(SERVER_JSON, data)


def choose_target(schedule):
    if not schedule.get("enabled"):
        return "", "disabled"

    now = datetime.now()
    weekday = WEEKDAYS[now.weekday()]
    current_hhmm = now.strftime("%H:%M")

    active_entries = []
    for entry in schedule.get("entries", []):
        if not entry.get("enabled"):
            continue
        if weekday not in (entry.get("days") or []):
            continue
        start = entry.get("start", "")
        end = entry.get("end", "")
        if start and end and start <= current_hhmm < end:
            active_entries.append(entry)

    active_entries.sort(key=lambda item: (item.get("start", ""), item.get("name", "")))
    if active_entries:
        entry = active_entries[0]
        return entry.get("world_id", ""), f"window:{entry.get('name', entry.get('world_id', 'scheduled'))}"

    return schedule.get("default_world_id", ""), "default"


def main():
    schedule = read_json(SCHEDULE_FILE, {"enabled": False, "default_world_id": "", "entries": []})
    target_world, reason = choose_target(schedule)
    if not target_world:
      print("No scheduled target world.")
      return 0

    if not (WORLDS_DIR / target_world).is_dir():
        print(f"Scheduled world missing: {target_world}")
        return 1

    current = current_world_id()
    state = read_json(STATE_FILE, {})
    if current == target_world and state.get("target_world") == target_world:
        print(f"World already set to {target_world}.")
        return 0

    set_world_id(target_world)
    if docker_running():
        ok, out, err = run(["bash", str(SCRIPT), "stop"], timeout=180)
        if not ok:
            print(err or out or "Failed to stop server.")
            return 1
        ok, out, err = run(["bash", str(SCRIPT), "start"], timeout=900)
        if not ok:
            print(err or out or "Failed to start server.")
            return 1
        print(f"Switched running server to world {target_world} via {reason}.")
    else:
        print(f"Updated configured world to {target_world} via {reason}. Server was stopped.")

    write_json(STATE_FILE, {
        "switched_at": datetime.now().isoformat(),
        "target_world": target_world,
        "reason": reason,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
