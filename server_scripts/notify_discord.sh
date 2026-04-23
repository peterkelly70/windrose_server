#!/usr/bin/env bash
set -euo pipefail

WEBHOOK_FILE="/home/windrose/server_scripts/.discord_webhook"

TITLE="Notification"
MESSAGE="Windrose server event"
COLOR="BLUE"
SENDER="Windrose"

while getopts "t:m:c:s:" opt; do
  case "$opt" in
    t) TITLE="$OPTARG" ;;
    m) MESSAGE="$OPTARG" ;;
    c) COLOR="$OPTARG" ;;
    s) SENDER="$OPTARG" ;;
    *) echo "Usage: $0 -t title -m message [-c RED|GREEN|YELLOW|BLUE] [-s sender]" >&2; exit 2 ;;
  esac
done

# Some callers pass "\n" inside quoted shell arguments. Convert those to
# actual newlines before building the Discord embed JSON.
MESSAGE="${MESSAGE//\\n/$'\n'}"
TITLE="${TITLE//\\n/$'\n'}"

if [ ! -s "$WEBHOOK_FILE" ]; then
  echo "Discord webhook file missing: $WEBHOOK_FILE" >&2
  exit 1
fi

case "$COLOR" in
  RED) COLOR_VALUE=15158332 ;;
  GREEN) COLOR_VALUE=3066993 ;;
  YELLOW) COLOR_VALUE=16776960 ;;
  BLUE|*) COLOR_VALUE=3447003 ;;
esac

PAYLOAD="$(jq -n \
  --arg title "$TITLE @ $SENDER" \
  --arg description "$MESSAGE" \
  --arg hostname "$(hostname)" \
  --arg server_time "$(date)" \
  --argjson color "$COLOR_VALUE" \
  '{
    embeds: [{
      title: $title,
      description: $description,
      color: $color,
      fields: [
        {name: "Hostname", value: $hostname, inline: true},
        {name: "Server Time", value: $server_time, inline: true}
      ],
      footer: {text: "Windrose Server"}
    }]
  }')"

curl -fsS -H "Content-Type: application/json" -d "$PAYLOAD" "$(cat "$WEBHOOK_FILE")" >/dev/null
