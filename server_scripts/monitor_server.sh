#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/windrose"
SERVER_FILES="$ROOT/server-files"
LOG_FILE="$SERVER_FILES/R5/Saved/Logs/R5.log"
NOTIFY="$ROOT/server_scripts/notify_discord.sh"
STATE_FILE="$ROOT/server_scripts/.last_monitor_state"
MONITOR_ENABLED_FILE="$ROOT/server_scripts/.monitor_enabled"
BROKEN_QUEUE_FILE="$ROOT/server_scripts/.broken_queue_pending_restart"
REMINDER_FILE="$ROOT/server_scripts/.broken_queue_last_reminder"
READY_SESSION_FILE="$ROOT/server_scripts/.last_ready_session"
PLAYER_STATE_FILE="$ROOT/server_scripts/.last_player_state"
PERF_STATE_FILE="$ROOT/server_scripts/.last_perf_alert"
PERF_HISTORY_STATE_FILE="$ROOT/server_scripts/.last_perf_history"
HICCUP_LOG="$ROOT/server_scripts/hiccups.log"
LOCK_FILE="$ROOT/server_scripts/.monitor_server.lock"
DISK_THRESHOLD="${DISK_THRESHOLD:-95}"
BROKEN_QUEUE_THRESHOLD="${BROKEN_QUEUE_THRESHOLD:-100}"
BROKEN_QUEUE_MAX_AGE="${BROKEN_QUEUE_MAX_AGE:-180}"
DB_SLOW_WINDOW="${DB_SLOW_WINDOW:-300}"
DB_SLOW_THRESHOLD="${DB_SLOW_THRESHOLD:-20}"
DB_EXTREME_THRESHOLD="${DB_EXTREME_THRESHOLD:-3}"
P2P_DELAY_THRESHOLD_MS="${P2P_DELAY_THRESHOLD_MS:-30000}"
P2P_DELAY_COUNT_THRESHOLD="${P2P_DELAY_COUNT_THRESHOLD:-3}"
PERF_ALERT_COOLDOWN="${PERF_ALERT_COOLDOWN:-900}"
PERF_HISTORY_COOLDOWN="${PERF_HISTORY_COOLDOWN:-60}"
REMINDER_SECONDS="${REMINDER_SECONDS:-1800}"
SERVER_NAME="Wayward Winds"

cd "$ROOT"

notify() {
  local title="$1"
  local message="$2"
  local color="${3:-BLUE}"
  "$NOTIFY" -t "$title" -m "$message" -c "$color" -s "$SERVER_NAME" || true
}

container_state() {
  docker inspect windrose --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}no-health{{end}}' 2>/dev/null || true
}

container_running() {
  local status
  status="$(container_state)"
  echo "$status" | grep -q '^running'
}

players_json() {
  if ! container_running; then
    printf '{"count":0,"names":"none"}\n'
    return
  fi

  python3 - <<'PY'
import json
import os
import re
from pathlib import Path

log = Path('/home/windrose/server-files/R5/Saved/Logs/R5.log')
max_bytes = 4 * 1024 * 1024
ready_re = re.compile(r"ServerAccount\. AccountName '([^']+)'\. AccountId ([A-F0-9]+)\.")
account_line_re = re.compile(
    r"Name '([^']+)'\. AccountId '([A-F0-9]+)'\. State '([^']+)'.*?"
    r"TimeInGame ([+0-9:.]+).*?TimeOnServer ([+0-9:.]+).*?FarewellReason\s*(.*)$"
)
disconnect_re = re.compile(r"(?:Account disconnected\. AccountId|Disconnect AccountId) ([A-F0-9]+)")
players = {}

try:
    size = log.stat().st_size
    with log.open('rb') as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
            fh.readline()
        text = fh.read().decode('utf-8', 'replace')
except OSError:
    print(json.dumps({'count': 999, 'names': 'unknown'}))
    raise SystemExit

for line in text.splitlines():
    ready = ready_re.search(line)
    if ready:
        name, account_id = ready.groups()
        players[account_id] = name

    account_line = account_line_re.search(line)
    if account_line:
        name, account_id, state_name, _time_in_game, _time_on_server, farewell = account_line.groups()
        if state_name == 'ReadyToPlay' and not farewell.strip():
            players[account_id] = name
        else:
            players.pop(account_id, None)

    disconnect = disconnect_re.search(line)
    if disconnect:
        players.pop(disconnect.group(1), None)

names = ', '.join(sorted(players.values(), key=str.lower)) or 'none'
print(json.dumps({'count': len(players), 'names': names}))
PY
}

online_count() {
  players_json | python3 -c 'import json,sys; print(json.load(sys.stdin).get("count", 999))'
}

online_names() {
  players_json | python3 -c 'import json,sys; print(json.load(sys.stdin).get("names", "unknown"))'
}

windrose_pid() {
  local line pid stat cmd fallback
  fallback=""
  while read -r line; do
    pid="$(awk '{print $1}' <<<"$line")"
    stat="$(awk '{print $2}' <<<"$line")"
    cmd="$(cut -d' ' -f3- <<<"$line")"
    [ -n "$pid" ] || continue
    [[ "$stat" == Z* ]] && continue
    [[ "$cmd" != *WindroseServer-Win64-Shipping.exe* && "$cmd" != *WindroseServer-Linux* ]] && continue
    if [[ "$cmd" == *xvfb-run* || "$cmd" == start.exe* ]]; then
      [ -z "$fallback" ] && fallback="$pid"
      continue
    fi
    printf '%s\n' "$pid"
    return 0
  done < <(ps -eo pid=,stat=,cmd= 2>/dev/null || true)
  [ -n "$fallback" ] && printf '%s\n' "$fallback" && return 0
  return 1
}

process_snapshot() {
  local pid="$1"
  if [ -z "$pid" ] || ! ps -p "$pid" >/dev/null 2>&1; then
    printf 'Process: not running\n'
    return
  fi

  local ps_line io_line
  ps_line="$(ps -p "$pid" -o pid=,stat=,etimes=,%cpu=,%mem=,rss=,cmd= 2>/dev/null | sed 's/^ *//' || true)"
  io_line="$(pidstat -d -p "$pid" 1 1 2>/dev/null | awk -v pid="$pid" '$0 ~ pid && $0 !~ /UID/ {line=$0} END {print line}' | sed 's/^ *//' || true)"

  printf 'Process: %s\n' "${ps_line:-unknown}"
  if [ -n "$io_line" ]; then
    printf 'Process IO: %s\n' "$io_line"
  fi
}

disk_snapshot() {
  local df_line io_line
  df_line="$(df -h /home/windrose | awk 'END {print $3 " used / " $2 " total (" $5 "), " $4 " free"}')"
  io_line="$(iostat -xz 1 2 2>/dev/null | awk '/^Device/ {capture=1; next} capture && $1 ~ /^sd|^nvme|^vd/ {line=$0} END {print line}' | sed 's/^ *//' || true)"

  printf 'Disk: %s\n' "$df_line"
  if [ -n "$io_line" ]; then
    printf 'Disk IO: %s\n' "$io_line"
  fi
}

log_perf_json() {
  [ -s "$LOG_FILE" ] || {
    printf '{"db_slow":0,"db_extreme":0,"p2p_delays":0,"max_p2p_delay_ms":0,"latest_perf_line":"none"}\n'
    return
  }

  python3 - "$LOG_FILE" "$DB_SLOW_WINDOW" "$P2P_DELAY_THRESHOLD_MS" <<'PY'
import json
import re
import sys
import time
from pathlib import Path

log = Path(sys.argv[1])
window = int(sys.argv[2])
p2p_threshold = int(sys.argv[3])
max_bytes = 6 * 1024 * 1024
now = time.time()
line_re = re.compile(r'^\[(\d{4})\.(\d{2})\.(\d{2})-(\d{2})\.(\d{2})\.(\d{2})')
delay_re = re.compile(r'(?:Send|Read|Receive|Call receive|Pending data check|Pending data receive):? (\d+) msec')

try:
    size = log.stat().st_size
    with log.open('rb') as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
            fh.readline()
        text = fh.read().decode('utf-8', 'replace')
except OSError:
    print(json.dumps({'db_slow': 0, 'db_extreme': 0, 'p2p_delays': 0, 'max_p2p_delay_ms': 0, 'latest_perf_line': 'unreadable log'}))
    raise SystemExit

db_slow = 0
db_extreme = 0
p2p_delays = 0
max_p2p_delay = 0
latest = 'none'

for line in text.splitlines():
    match = line_re.match(line)
    if not match:
        continue
    year, month, day, hour, minute, second = map(int, match.groups())
    # Game log timestamps are UTC.
    ts = time.mktime((year, month, day, hour, minute, second, 0, 0, 0)) - time.timezone
    if now - ts > window:
        continue

    if 'R5BLDalAsyncQueue::DetectProblems' in line and 'commitT' in line:
        if 'Slow task' in line or 'EXTREMELY slow task' in line:
            db_slow += 1
            latest = line
        if 'EXTREMELY slow task' in line:
            db_extreme += 1

    if 'Delay between datagrams' in line:
        delays = [int(value) for value in delay_re.findall(line)]
        line_max = max(delays) if delays else 0
        max_p2p_delay = max(max_p2p_delay, line_max)
        if line_max >= p2p_threshold:
            p2p_delays += 1
            latest = line

print(json.dumps({
    'db_slow': db_slow,
    'db_extreme': db_extreme,
    'p2p_delays': p2p_delays,
    'max_p2p_delay_ms': max_p2p_delay,
    'latest_perf_line': latest[-900:],
}))
PY
}

perf_state_key() {
  local json
  json="$1"
  printf '%s' "$json" | python3 -c '
import json, sys
d=json.load(sys.stdin)
labels=[]
if d.get("db_slow", 0) > 0:
    labels.append("db:{}:{}".format(d.get("db_slow", 0), d.get("db_extreme", 0)))
if d.get("p2p_delays", 0) > 0:
    labels.append("p2p:{}:{}s".format(d.get("p2p_delays", 0), d.get("max_p2p_delay_ms", 0)//1000))
print("|".join(labels) or "clear")
'
}

maybe_notify_performance() {
  container_running || return 0

  local json db_slow db_extreme p2p_delays max_p2p latest now last_ts last_key key pid message
  json="$(log_perf_json)"
  db_slow="$(printf '%s' "$json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("db_slow", 0))')"
  db_extreme="$(printf '%s' "$json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("db_extreme", 0))')"
  p2p_delays="$(printf '%s' "$json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("p2p_delays", 0))')"
  max_p2p="$(printf '%s' "$json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("max_p2p_delay_ms", 0))')"
  latest="$(printf '%s' "$json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("latest_perf_line", "none"))')"
  key="$(perf_state_key "$json")"
  now="$(date +%s)"

  if [ "$db_slow" -gt 0 ] || [ "$db_extreme" -gt 0 ] || [ "$p2p_delays" -gt 0 ]; then
    last_ts=0
    last_key=""
    if [ -f "$PERF_HISTORY_STATE_FILE" ]; then
      read -r last_ts last_key < "$PERF_HISTORY_STATE_FILE" || true
    fi
    if [ "$key" != "$last_key" ] || [ $((now - last_ts)) -ge "$PERF_HISTORY_COOLDOWN" ]; then
      printf '%s %s\n' "$now" "$key" > "$PERF_HISTORY_STATE_FILE"
      jq -cn \
        --arg time "$(date -Is)" \
        --arg key "$key" \
        --arg latest "$latest" \
        --argjson db_slow "$db_slow" \
        --argjson db_extreme "$db_extreme" \
        --argjson p2p_delays "$p2p_delays" \
        --argjson max_p2p_delay_ms "$max_p2p" \
        --argjson players "$(online_count)" \
        '{
          time: $time,
          type: "performance",
          key: $key,
          db_slow: $db_slow,
          db_extreme: $db_extreme,
          p2p_delays: $p2p_delays,
          max_p2p_delay_ms: $max_p2p_delay_ms,
          players: $players,
          latest: $latest
        }' >> "$HICCUP_LOG" 2>/dev/null || true
    fi
  fi

  if [ "$db_slow" -lt "$DB_SLOW_THRESHOLD" ] && \
     [ "$db_extreme" -lt "$DB_EXTREME_THRESHOLD" ] && \
     [ "$p2p_delays" -lt "$P2P_DELAY_COUNT_THRESHOLD" ]; then
    return 0
  fi

  last_ts=0
  last_key=""
  if [ -f "$PERF_STATE_FILE" ]; then
    read -r last_ts last_key < "$PERF_STATE_FILE" || true
  fi

  if [ "$key" = "$last_key" ] && [ $((now - last_ts)) -lt "$PERF_ALERT_COOLDOWN" ]; then
    return 0
  fi

  printf '%s %s\n' "$now" "$key" > "$PERF_STATE_FILE"
  pid="$(windrose_pid || true)"
  message="Performance hiccup detected in the last ${DB_SLOW_WINDOW}s.\n\nDB slow commits: $db_slow\nDB extreme commits: $db_extreme\nP2P delay lines over ${P2P_DELAY_THRESHOLD_MS}ms: $p2p_delays\nMax P2P delay: ${max_p2p}ms\nPlayers online: $(online_count)\nPlayers: $(online_names)\n\n$(process_snapshot "$pid")\n$(disk_snapshot)\n\nLatest matching log line:\n$latest"
  notify "Server Performance Hiccup" "$message" "YELLOW"
}

server_config_summary() {
  jq -r '
    .ServerDescription_Persistent |
    "Server: \(.ServerName)\nInvite code: \(.InviteCode)\nPassword protected: \(.IsPasswordProtected)\nMax players: \(.MaxPlayerCount)\nWorld: \(.WorldIslandId)"
  ' "$SERVER_FILES/R5/ServerDescription.json"
}

ready_session_id() {
  [ -s "$LOG_FILE" ] || return 1
  local recent session
  recent="$(tail -n 5000 "$LOG_FILE" 2>/dev/null || true)"
  grep -q 'Server registration finished successfully' <<<"$recent" || return 1
  grep -qE 'Server\. Change state .*=> (WaitingForFirstAccount|ReadyToPlay)' <<<"$recent" || return 1
  session="$(grep 'Server registration finished successfully' <<<"$recent" | tail -1 | sed -n 's/.*BLSessionId \([A-Za-z0-9]*\).*/\1/p')"
  [ -n "$session" ] || return 1
  printf '%s\n' "$session"
}

maybe_notify_ready() {
  container_running || return 0

  local session last_session
  session="$(ready_session_id || true)"
  [ -n "$session" ] || return 0

  last_session=""
  [ -f "$READY_SESSION_FILE" ] && last_session="$(cat "$READY_SESSION_FILE" 2>/dev/null || true)"
  [ "$session" = "$last_session" ] && return 0

  printf '%s\n' "$session" > "$READY_SESSION_FILE"
  notify "Server Ready" "Wayward Winds is ready for clients.\n\n$(server_config_summary)\nBackend: registered\nSession: $session" "GREEN"
}

maybe_notify_players() {
  container_running || return 0

  local json count names current last
  json="$(players_json)"
  count="$(printf '%s' "$json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("count", 999))')"
  names="$(printf '%s' "$json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("names", "unknown"))')"
  [ "$count" = "999" ] && return 0

  current="${count}|${names}"
  last=""
  [ -f "$PLAYER_STATE_FILE" ] && last="$(cat "$PLAYER_STATE_FILE" 2>/dev/null || true)"
  [ "$current" = "$last" ] && return 0

  printf '%s\n' "$current" > "$PLAYER_STATE_FILE"
  notify "Players Online" "Players online: $count\nPlayers: $names" "BLUE"
}

broken_queue_detected() {
  [ -s "$LOG_FILE" ] || return 1

  local now log_mtime age count last_line
  now="$(date +%s)"
  log_mtime="$(stat -c %Y "$LOG_FILE")"
  age=$((now - log_mtime))
  [ "$age" -le "$BROKEN_QUEUE_MAX_AGE" ] || return 1

  count="$(tail -n 2000 "$LOG_FILE" | grep -c 'Input Queue is closed\. Cannot GetNewMessage after close' || true)"
  last_line="$(tail -n 1 "$LOG_FILE" || true)"

  [ "$count" -ge "$BROKEN_QUEUE_THRESHOLD" ] && echo "$last_line" | grep -q 'Input Queue is closed\. Cannot GetNewMessage after close'
}

maybe_remind_players() {
  local count names now last
  count="$(online_count)"
  names="$(online_names)"
  now="$(date +%s)"
  last=0
  [ -f "$REMINDER_FILE" ] && last="$(cat "$REMINDER_FILE" 2>/dev/null || echo 0)"

  if [ "$count" -gt 0 ] && [ $((now - last)) -ge "$REMINDER_SECONDS" ]; then
    printf '%s\n' "$now" > "$REMINDER_FILE"
    notify "Server Needs Restart" "The Windrose backend queue is stuck. Please log out when convenient. The server will restart automatically when everyone is offline.\n\nPlayers online: $count\nPlayers: $names" "YELLOW"
  fi
}

handle_broken_queue() {
  local count names
  count="$(online_count)"
  names="$(online_names)"

  if [ ! -f "$BROKEN_QUEUE_FILE" ]; then
    date -Is > "$BROKEN_QUEUE_FILE"
    notify "Server Degraded" "Detected sustained Windrose backend queue failure: Input Queue is closed. Inventory and other backend actions may stop responding.\n\nPlease log out when convenient. The server will restart automatically once player count reaches zero.\n\nPlayers online: $count\nPlayers: $names" "RED"
  fi

  if [ "$count" -eq 0 ]; then
    notify "Auto Restarting Server" "Broken backend queue is active, or the affected container has stopped, and no players are online. Restarting Wayward Winds now." "YELLOW"
    "$ROOT/windrose-server.sh" restart
    rm -f "$BROKEN_QUEUE_FILE" "$REMINDER_FILE"
    notify "Auto Restart Complete" "Wayward Winds restarted after broken backend queue recovery." "GREEN"
  else
    maybe_remind_players
  fi
}

main() {
  exec 9>"$LOCK_FILE"
  flock -n 9 || exit 0

  if [ ! -f "$MONITOR_ENABLED_FILE" ]; then
    exit 0
  fi

  local status disk_usage state message color
  status="$(container_state)"
  disk_usage="$(df /home/windrose | awk 'END {gsub(/%/, "", $5); print $5}')"

  if [ -z "$status" ]; then
    state="missing"
    message="Windrose container is missing."
    color="RED"
  elif echo "$status" | grep -q 'running healthy'; then
    state="healthy"
    message="Windrose server is running and healthy."
    color="GREEN"
  elif echo "$status" | grep -q '^running'; then
    state="running-not-healthy"
    message="Windrose container is running but health is not healthy: $status"
    color="YELLOW"
  else
    state="down"
    message="Windrose container is not running: $status"
    color="RED"
  fi

  if [ "$disk_usage" -ge "$DISK_THRESHOLD" ]; then
    state="$state-disk"
    message="$message Disk usage is ${disk_usage}%."
    color="YELLOW"
  fi

  maybe_notify_ready
  maybe_notify_players
  maybe_notify_performance

  if container_running && broken_queue_detected; then
    state="$state-broken-queue"
    message="$message Broken backend queue detected. Waiting for players to log out before restart."
    color="RED"
    handle_broken_queue
  elif [ -f "$BROKEN_QUEUE_FILE" ]; then
    rm -f "$BROKEN_QUEUE_FILE" "$REMINDER_FILE"
    notify "Server Queue Recovered" "Broken backend queue spam stopped before an automatic restart was needed." "GREEN"
  fi

  local last_state
  last_state=""
  [ -f "$STATE_FILE" ] && last_state="$(cat "$STATE_FILE")"

  if [ "$state" != "$last_state" ]; then
    printf '%s\n' "$state" > "$STATE_FILE"
    notify "Windrose Monitor" "$message" "$color"
  fi

  printf '%s\n' "$message"
}

main "$@"
