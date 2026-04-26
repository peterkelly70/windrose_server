#!/usr/bin/env python3
import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path("/home/windrose")
CONFIG = ROOT / "config" / "instances.json"
CONFIG_EXAMPLE = ROOT / "config" / "instances.example.json"
STATE_FILE = ROOT / "server_scripts" / ".last_instance_schedule_state"
NOTIFY = ROOT / "server_scripts" / "notify_discord.sh"
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAY_LABELS = {
    "mon": "Mon",
    "tue": "Tue",
    "wed": "Wed",
    "thu": "Thu",
    "fri": "Fri",
    "sat": "Sat",
    "sun": "Sun",
}


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


def load_config():
    path = CONFIG if CONFIG.is_file() else CONFIG_EXAMPLE
    return path, read_json(path, {"instances": [], "scheduler": {}})


def read_env(path):
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


def run(cmd, cwd=None, timeout=900):
    proc = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=timeout)
    return proc.returncode == 0, proc.stdout.strip(), proc.stderr.strip()


def notify(title, message, color="BLUE"):
    if not NOTIFY.is_file():
        return
    run([str(NOTIFY), "-t", title, "-m", message, "-c", color, "-s", "Windrose Scheduler"], timeout=30)


def runtime_root(instance):
    return Path(instance.get("runtime_root", ""))


def env_file(instance):
    return Path(instance.get("env_file", ""))


def service_name(instance):
    return instance.get("service_name", "windrose")


def display_name(instance):
    return instance.get("name", "") or instance.get("id", "unknown")


def container_name(instance):
    env = read_env(env_file(instance))
    return env.get("CONTAINER_NAME", service_name(instance)) or service_name(instance)


def inspect_status(instance):
    name = container_name(instance)
    ok, out, _err = run(["docker", "inspect", name, "--format", "{{.State.Status}}"], timeout=15)
    if not ok:
        return "missing"
    return out.strip() or "unknown"


def start_instance(instance):
    root = runtime_root(instance)
    if not root.is_dir():
        return False, "", f"Runtime root missing: {root}"
    ok, out, err = run(["docker", "compose", "up", "-d", "--remove-orphans"], cwd=root, timeout=900)
    return ok, out, err


def stop_instance(instance):
    root = runtime_root(instance)
    if not root.is_dir():
        return False, "", f"Runtime root missing: {root}"
    name = container_name(instance)
    run(["docker", "update", "--restart=no", name], timeout=30)

    ok, out, err = run(["docker", "compose", "stop", service_name(instance)], cwd=root, timeout=240)
    if ok:
        return ok, out, err

    fallback = [
        "docker",
        "exec",
        name,
        "sh",
        "-lc",
        "pkill -TERM -f 'WindroseServer-Win64-Shipping.exe|wineserver|tail -F /home/steam/server-files/R5/Saved/Logs/R5.log|/bin/bash ./start.sh|/home/steam/server/init.sh' || true; "
        "sleep 5; "
        "pkill -KILL -f 'WindroseServer-Win64-Shipping.exe|wineserver|tail -F /home/steam/server-files/R5/Saved/Logs/R5.log|/bin/bash ./start.sh|/home/steam/server/init.sh' || true",
    ]
    fb_ok, fb_out, fb_err = run(fallback, timeout=60)
    if not fb_ok:
        return False, "\n".join(filter(None, [out, fb_out])), "\n".join(filter(None, [err, fb_err]))

    for _ in range(20):
        status = inspect_status(instance)
        if status in {"exited", "missing"}:
            return True, "\n".join(filter(None, [out, fb_out])), err
        run(["sleep", "1"], timeout=5)

    return False, "\n".join(filter(None, [out, fb_out])), err or "Container did not stop after fallback shutdown."


def timezone_name(scheduler):
    return str(scheduler.get("timezone", "Australia/Perth")).strip() or "Australia/Perth"


def scheduler_now(scheduler):
    return datetime.now(ZoneInfo(timezone_name(scheduler)))


def hhmm(moment):
    return moment.strftime("%H:%M")


def time_label(moment):
    return moment.strftime("%I:%M%p").lstrip("0").lower()


def weekday_key(moment):
    return WEEKDAYS[moment.weekday()]


def instance_schedule(instance):
    value = instance.get("schedule", {})
    return value if isinstance(value, dict) else {}


def default_instance(instances, scheduler):
    default_id = str(scheduler.get("default_instance", "")).strip()
    if default_id:
        for item in instances:
            if item.get("id") == default_id and item.get("enabled", False):
                return item
    for item in instances:
        if item.get("enabled", False) and instance_schedule(item).get("mode", "default_on") == "default_on":
            return item
    return None


def active_window_for(instance, moment):
    schedule = instance_schedule(instance)
    if schedule.get("mode") != "windowed":
        return None

    weekday = weekday_key(moment)
    current = hhmm(moment)
    windows = []
    for window in schedule.get("windows", []):
        if not window.get("enabled", True):
            continue
        if weekday not in (window.get("days") or []):
            continue
        start = str(window.get("start", "")).strip()
        end = str(window.get("end", "")).strip()
        if start and end and start <= current < end:
            windows.append(window)
    windows.sort(key=lambda item: (item.get("start", ""), item.get("id", "")))
    return windows[0] if windows else None


def choose_scheduled_target(instances, moment):
    active = []
    for item in instances:
        if not item.get("enabled", False):
            continue
        window = active_window_for(item, moment)
        if window:
            active.append((window.get("start", ""), item.get("id", ""), item, window))
    active.sort(key=lambda entry: (entry[0], entry[1]))
    if active:
        _start, _id, item, window = active[0]
        return item, window
    return None, None


def desired_running(instances, scheduler, moment):
    desired = []
    scheduled_target, active_window = choose_scheduled_target(instances, moment)
    default_item = default_instance(instances, scheduler)
    default_always = bool(scheduler.get("default_instance_always_on", False))

    if scheduled_target:
        desired.append(scheduled_target)
        if default_item and default_always and default_item.get("id") != scheduled_target.get("id"):
            desired.append(default_item)
        reason = f"window:{scheduled_target.get('id', '')}:{active_window.get('start', '')}-{active_window.get('end', '')}"
    elif default_item and instance_schedule(default_item).get("mode", "default_on") != "off":
        desired.append(default_item)
        reason = f"default:{default_item.get('id', '')}"
    else:
        reason = "no-target"

    seen = set()
    ordered = []
    for item in desired:
        item_id = item.get("id", "")
        if item_id and item_id not in seen:
            seen.add(item_id)
            ordered.append(item)
    return ordered, reason


def schedule_summary(instance):
    schedule = instance_schedule(instance)
    mode = schedule.get("mode", "default_on")
    if mode == "windowed":
        windows = []
        for window in schedule.get("windows", []):
            if not window.get("enabled", True):
                continue
            days = "/".join(WEEKDAY_LABELS.get(day, day) for day in window.get("days", []))
            windows.append(f"{days} {window.get('start', '')}-{window.get('end', '')}")
        return ", ".join(windows) or "windowed"
    if mode == "off":
        return "off"
    return "default"


def find_next_change(instances, scheduler, now, horizon_minutes=15):
    current_ids = [item.get("id", "") for item in desired_running(instances, scheduler, now)[0]]
    for minute in range(1, horizon_minutes + 1):
        future = now + timedelta(minutes=minute)
        future_ids = [item.get("id", "") for item in desired_running(instances, scheduler, future)[0]]
        if future_ids != current_ids:
            current_set = set(current_ids)
            future_set = set(future_ids)
            return {
                "time": future,
                "event_id": future.strftime("%Y-%m-%dT%H:%M"),
                "start_ids": sorted(future_set - current_set),
                "stop_ids": sorted(current_set - future_set),
            }
    return None


def maybe_send_warning(instances, scheduler, state):
    now = scheduler_now(scheduler)
    event = find_next_change(instances, scheduler, now, horizon_minutes=15)
    if not event:
        return None

    if state.get("last_warning_event") == event["event_id"]:
        return event["event_id"]

    by_id = {item.get("id", ""): item for item in instances}
    for item_id in event["stop_ids"]:
        item = by_id.get(item_id)
        if not item:
            continue
        notify(
            f"{display_name(item)} - Game Server will terminate at {time_label(event['time'])}",
            f"{display_name(item)} is scheduled to stop at {time_label(event['time'])}. Please log out before the switch.",
            "YELLOW",
        )

    for item_id in event["start_ids"]:
        item = by_id.get(item_id)
        if not item:
            continue
        notify(
            f"{display_name(item)} - Server will start at {time_label(event['time'])}",
            f"{display_name(item)} is scheduled to start at {time_label(event['time'])}. Schedule: {schedule_summary(item)}.",
            "BLUE",
        )

    return event["event_id"]


def send_change_notifications(instances, started_ids, stopped_ids, reason):
    by_id = {item.get("id", ""): item for item in instances}
    now = datetime.now()
    stamp = time_label(now)

    for item_id in stopped_ids:
        item = by_id.get(item_id)
        if not item:
            continue
        notify(
            f"{display_name(item)} - Server stopped at {stamp}",
            f"{display_name(item)} stopped at {stamp}. Scheduler reason: {reason}.",
            "YELLOW",
        )

    for item_id in started_ids:
        item = by_id.get(item_id)
        if not item:
            continue
        notify(
            f"{display_name(item)} Server Started at {stamp}",
            f"{display_name(item)} started at {stamp}. Scheduler reason: {reason}. Schedule: {schedule_summary(item)}.",
            "GREEN",
        )


def main():
    path, config = load_config()
    instances = config.get("instances", [])
    scheduler = config.get("scheduler", {})
    state = read_json(STATE_FILE, {})

    desired, reason = desired_running(instances, scheduler, scheduler_now(scheduler))
    desired_ids = {item.get("id", "") for item in desired}
    if not desired_ids:
        print(f"No target instance selected from {path}.")
        return 0

    warning_event = maybe_send_warning(instances, scheduler, state)

    changes = []
    failures = []
    started_ids = []
    stopped_ids = []

    for item in instances:
        if not item.get("enabled", False):
            continue

        item_id = item.get("id", "")
        status = inspect_status(item)
        should_run = item_id in desired_ids

        if should_run and status != "running":
            ok, out, err = start_instance(item)
            if ok:
                changes.append(f"started:{item_id}")
                started_ids.append(item_id)
            else:
                failures.append(f"start {item_id}: {err or out or 'unknown error'}")
        elif not should_run and status == "running":
            ok, out, err = stop_instance(item)
            if ok:
                changes.append(f"stopped:{item_id}")
                stopped_ids.append(item_id)
            else:
                failures.append(f"stop {item_id}: {err or out or 'unknown error'}")

    if started_ids or stopped_ids:
        send_change_notifications(instances, started_ids, stopped_ids, reason)

    payload = {
        "checked_at": datetime.now().isoformat(),
        "config_source": str(path),
        "target_instances": sorted(desired_ids),
        "reason": reason,
        "changes": changes,
        "failures": failures,
        "last_warning_event": warning_event or state.get("last_warning_event", ""),
    }
    write_json(STATE_FILE, payload)

    if failures:
        for failure in failures:
            print(failure)
        return 1

    if changes:
        print(f"Applied instance schedule via {reason}: {', '.join(changes)}")
    else:
        print(f"Instance schedule already satisfied via {reason}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
