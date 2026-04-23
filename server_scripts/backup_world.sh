#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/windrose"
SERVER_FILES="$ROOT/server-files"
SAVE_ROOT="$SERVER_FILES/R5/Saved"
BACKUP_DIR="$ROOT/backups"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
LOCK_FILE="/tmp/windrose-world-backup.lock"
NOTIFY="$ROOT/server_scripts/notify_discord.sh"

notify() {
  local title="$1"
  local message="$2"
  local color="${3:-BLUE}"

  if [ -x "$NOTIFY" ]; then
    "$NOTIFY" -t "$title" -m "$message" -c "$color" -s "Wayward Winds" || true
  fi
}

mkdir -p "$BACKUP_DIR"

(
  flock -n 9 || {
    echo "Another Windrose backup is already running."
    exit 0
  }

  if [ ! -d "$SAVE_ROOT/SaveProfiles" ]; then
    notify "Backup Failed" "SaveProfiles directory was not found at $SAVE_ROOT/SaveProfiles" "RED"
    exit 1
  fi

  timestamp="$(date +%Y%m%d-%H%M%S)"
  tmp_file="$BACKUP_DIR/.wayward-winds-$timestamp.tar.gz.tmp"
  backup_file="$BACKUP_DIR/wayward-winds-$timestamp.tar.gz"
  log_file="$BACKUP_DIR/wayward-winds-$timestamp.log"
  status=0

  tar \
    --warning=no-file-changed \
    --ignore-failed-read \
    -C "$SERVER_FILES/R5" \
    -czf "$tmp_file" \
    ServerDescription.json \
    Saved/SaveProfiles \
    Saved/Config \
    >"$log_file" 2>&1 || status=$?

  if [ "$status" -gt 1 ]; then
    rm -f "$tmp_file"
    notify "Backup Failed" "tar exited with status $status. See $log_file" "RED"
    exit "$status"
  fi

  mv "$tmp_file" "$backup_file"
  find "$BACKUP_DIR" -maxdepth 1 -name 'wayward-winds-*.tar.gz' -type f -mtime +"$RETENTION_DAYS" -delete
  find "$BACKUP_DIR" -maxdepth 1 -name 'wayward-winds-*.log' -type f -mtime +"$RETENTION_DAYS" -delete

  size="$(du -h "$backup_file" | awk '{print $1}')"
  count="$(find "$BACKUP_DIR" -maxdepth 1 -name 'wayward-winds-*.tar.gz' -type f | wc -l)"
  message="Created $(basename "$backup_file") ($size). Retention: $RETENTION_DAYS days. Stored backups: $count."

  echo "$message"
  notify "Backup Complete" "$message" "GREEN"
) 9>"$LOCK_FILE"
