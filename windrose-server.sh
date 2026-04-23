#!/usr/bin/env bash
set -euo pipefail

cd /home/windrose

SERVICE="windrose"
NOTIFY="/home/windrose/server_scripts/notify_discord.sh"
BACKUP="/home/windrose/server_scripts/backup_world.sh"
SERVER_FILES="/home/windrose/server-files"
MONITOR_ENABLED="/home/windrose/server_scripts/.monitor_enabled"

if ! docker ps >/dev/null 2>&1; then
  if [ "${WINDROSE_SG_REEXEC:-0}" != "1" ] && command -v sg >/dev/null 2>&1; then
    quoted_args=""
    for arg in "$@"; do
      quoted_args+=" $(printf '%q' "$arg")"
    done
    exec sg docker -c "cd /home/windrose && WINDROSE_SG_REEXEC=1 ./windrose-server.sh$quoted_args"
  fi

  echo "Cannot access Docker. Log out and back in, or run: newgrp docker" >&2
  exit 1
fi

notify() {
  local title="$1"
  local message="$2"
  local color="${3:-BLUE}"

  if [ -x "$NOTIFY" ]; then
    "$NOTIFY" -t "$title" -m "$message" -c "$color" -s "Wayward Winds" || true
  fi
}

server_config_summary() {
  jq -r '
    .ServerDescription_Persistent |
    "Server: \(.ServerName)\nInvite code: \(.InviteCode)\nPassword protected: \(.IsPasswordProtected)\nMax players: \(.MaxPlayerCount)\nWorld: \(.WorldIslandId)"
  ' "$SERVER_FILES/R5/ServerDescription.json"
}

backend_status_summary() {
  local log_file="$SERVER_FILES/R5/Saved/Logs/R5.log"

  if [ ! -s "$log_file" ]; then
    echo "Backend: no game log yet"
    return
  fi

  local windrose_pid=""
  local pid proc_state
  while read -r pid; do
    [ -n "$pid" ] || continue
    proc_state="$(ps -o stat= -p "$pid" 2>/dev/null | awk '{print $1}' || true)"
    if [[ "$proc_state" != Z* ]]; then
      windrose_pid="$pid"
      break
    fi
  done < <(pgrep -f 'WindroseServer-Win64-Shipping.exe|WindroseServer-Linux|WindroseServer-' || true)

  if [ -z "$windrose_pid" ]; then
    echo "Backend: process not running"
    return
  fi

  local last_event
  last_event="$(
    grep -nE 'Server registration finished successfully|Register server|Fatal error|GsStream is broken|GcStream is broken|Received RST_STREAM|Stream removed|appError called|Server Authorization failed|AuthenticateDedicatedServer.*ResponseCode 503|AuthenticateDedicatedServer.*IsOk false|Error on Ue P2P|Check consent was failed|Failed to connect to remote' "$log_file" |
      tail -1 || true
  )"

  if echo "$last_event" | grep -qE 'Fatal error|GsStream is broken|Received RST_STREAM|Stream removed|appError called'; then
    echo "Backend: crashed or disconnected from Windrose CM"
    echo "Reason: $(echo "$last_event" | sed 's/^[0-9]*://')"
  elif echo "$last_event" | grep -qE 'GcStream is broken|Server Authorization failed|AuthenticateDedicatedServer.*ResponseCode 503'; then
    echo "Backend: Windrose authorization service error"
    echo "Reason: $(echo "$last_event" | sed 's/^[0-9]*://')"
  elif echo "$last_event" | grep -q 'Server registration finished successfully'; then
    local session_id
    session_id="$(echo "$last_event" | sed -n 's/.*BLSessionId \([A-Za-z0-9]*\).*/\1/p')"
    if [ -n "$session_id" ]; then
      echo "Backend: registered"
      echo "Session: $session_id"
    else
      echo "Backend: registered"
    fi
  elif echo "$last_event" | grep -q 'Register server'; then
    echo "Backend: registration pending"
  elif echo "$last_event" | grep -qi 'AuthenticateDedicatedServer.*IsOk false\|Error on Ue P2P\|Check consent was failed\|Failed to connect to remote'; then
    echo "Backend: connection error in log"
  else
    echo "Backend: not registered yet"
  fi
}

status_summary() {
  printf '%s\n%s\n' "$(server_config_summary)" "$(backend_status_summary)"
}

log_line_count() {
  local log_file="$SERVER_FILES/R5/Saved/Logs/R5.log"
  if [ -f "$log_file" ]; then
    wc -l < "$log_file"
  else
    echo 0
  fi
}

wait_for_client_ready() {
  local start_line="${1:-0}"
  local timeout="${2:-300}"
  local log_file="$SERVER_FILES/R5/Saved/Logs/R5.log"
  local deadline=$((SECONDS + timeout))
  local recent=""

  while [ "$SECONDS" -lt "$deadline" ]; do
    if [ -s "$log_file" ]; then
      recent="$(tail -n +$((start_line + 1)) "$log_file" 2>/dev/null || true)"
      if grep -q 'Server registration finished successfully' <<<"$recent" && \
         grep -qE 'Server\. Change state .*=> (WaitingForFirstAccount|ReadyToPlay)' <<<"$recent"; then
        return 0
      fi
    fi
    sleep 5
  done

  return 1
}

notify_booting() {
  local action="$1"
  notify "Server ${action}" "Wayward Winds is booting. I will post again when the server is ready for clients.

$(server_config_summary)" "YELLOW"
}

notify_ready_or_timeout() {
  local start_line="$1"
  local action="$2"
  if wait_for_client_ready "$start_line" 360; then
    notify "Server Ready" "Wayward Winds is ready for clients.

$(status_summary)" "GREEN"
  else
    notify "Server Still Starting" "Wayward Winds was ${action}, but it did not reach the client-ready log state within 6 minutes. Check the web panel/logs before joining.

$(status_summary)" "YELLOW"
  fi
}

steamcmd_update() {
  UPDATE_ON_START=true docker compose up -d --remove-orphans
}

steamcmd_latest_buildid() {
  docker compose pull "$SERVICE" >/dev/null
  docker compose images -q "$SERVICE" 2>/dev/null | head -1
}

local_buildid() {
  awk -F '"' '/"buildid"/ { print $4; exit }' "$SERVER_FILES/steamapps/appmanifest_4129620.acf" 2>/dev/null || true
}

update_check_summary() {
  local local_id latest_id
  local_id="$(local_buildid)"
  latest_id="$(steamcmd_latest_buildid)"

  echo "Local build: ${local_id:-unknown}"
  echo "Latest public build: ${latest_id:-unknown}"

  if [ -n "$local_id" ] && [ -n "$latest_id" ] && [ "$local_id" = "$latest_id" ]; then
    echo "Update: not needed"
  elif [ -n "$latest_id" ]; then
    echo "Update: available"
  else
    echo "Update: unable to determine"
  fi
}

run_backup() {
  if [ -x "$BACKUP" ]; then
    "$BACKUP"
  fi
}

hard_stop() {
  local ids
  ids="$(docker compose ps -aq "$SERVICE" 2>/dev/null || true)"

  if [ -n "$ids" ]; then
    docker update --restart=no $ids >/dev/null 2>&1 || true
  fi

  docker compose stop "$SERVICE" >/dev/null 2>&1 && {
    docker compose rm -f "$SERVICE" >/dev/null 2>&1 || true
    return 0
  }

  pkill -TERM -f 'WindroseServer-Win64-Shipping.exe' 2>/dev/null || true
  sleep 8
  pkill -KILL -f 'WindroseServer-Win64-Shipping.exe|Xvfb :99|wineserver64|winedevice.exe' 2>/dev/null || true

  docker compose rm -f "$SERVICE" >/dev/null 2>&1 || true
}

case "${1:-start}" in
  start)
    touch "$MONITOR_ENABLED"
    start_line="$(log_line_count)"
    docker compose up -d --remove-orphans
    notify_booting "Started"
    notify_ready_or_timeout "$start_line" "started"
    ;;
  stop)
    rm -f "$MONITOR_ENABLED" \
      /home/windrose/server_scripts/.broken_queue_pending_restart \
      /home/windrose/server_scripts/.broken_queue_last_reminder \
      /home/windrose/server_scripts/.last_monitor_state
    hard_stop
    notify "Server Stopped" "Windrose dedicated server stopped." "YELLOW"
    ;;
  restart)
    touch "$MONITOR_ENABLED"
    hard_stop
    start_line="$(log_line_count)"
    docker compose up -d --remove-orphans
    notify_booting "Restarted"
    notify_ready_or_timeout "$start_line" "restarted"
    ;;
  update-check)
    summary="$(update_check_summary)"
    echo "$summary"
    notify "Update Check Complete" "$summary" "BLUE"
    ;;
  update)
    notify "Server Update Started" "Backing up saves, stopping server, checking SteamCMD, and refreshing container image." "YELLOW"
    run_backup
    touch "$MONITOR_ENABLED"
    hard_stop
    steamcmd_update
    docker compose pull "$SERVICE"
    start_line="$(log_line_count)"
    docker compose up -d --remove-orphans
    notify_booting "Updated"
    notify_ready_or_timeout "$start_line" "updated"
    notify "Server Updated" "$(update_check_summary)\n$(status_summary)" "GREEN"
    ;;
  logs)
    docker compose logs -f "$SERVICE"
    ;;
  status)
    docker compose ps
    echo
    summary="$(status_summary)"
    echo "$summary"
    notify "Server Status" "$summary" "BLUE"
    ;;
  monitor)
    docker compose ps
    echo
    docker compose logs --tail=40 "$SERVICE"
    ;;
  notify-test)
    notify "Notification Test" "$(status_summary)" "BLUE"
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|update|update-check|logs|status|monitor|notify-test}" >&2
    exit 2
    ;;
esac
