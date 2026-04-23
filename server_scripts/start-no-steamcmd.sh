#!/usr/bin/env bash
set -euo pipefail

SERVERDIR=${SERVERDIR:-/data}
WINEPREFIX=${WINEPREFIX:-/home/steam/.wine}
PORT=${PORT:-7777}
QUERYPORT=${QUERYPORT:-7778}
MULTIHOME=${MULTIHOME:-0.0.0.0}
SERVER_DESC="$SERVERDIR/R5/ServerDescription.json"

patch_server_config() {
  if [ ! -f "$SERVER_DESC" ]; then
    return
  fi

  tr -d '\r' < "$SERVER_DESC" | jq \
    --arg invite "${INVITE_CODE:-}" \
    --arg name "${SERVER_NAME:-}" \
    --arg note "${SERVER_NOTE:-}" \
    --arg password "${SERVER_PASSWORD:-}" \
    --arg proxy "${P2P_PROXY_ADDRESS:-127.0.0.1}" \
    --argjson maxplayers "${MAX_PLAYERS:-4}" \
    '
    .ServerDescription_Persistent.P2pProxyAddress = $proxy |
    if $invite != "" then .ServerDescription_Persistent.InviteCode = $invite else . end |
    if $name != "" then .ServerDescription_Persistent.ServerName = $name else . end |
    if $note != "" then .ServerDescription_Persistent.Note = $note else . end |
    if $password != "" then
      .ServerDescription_Persistent.IsPasswordProtected = true |
      .ServerDescription_Persistent.Password = $password
    else
      .ServerDescription_Persistent.IsPasswordProtected = false |
      .ServerDescription_Persistent.Password = ""
    end |
    .ServerDescription_Persistent.MaxPlayerCount = $maxplayers
    ' > "$SERVER_DESC.tmp"

  mv "$SERVER_DESC.tmp" "$SERVER_DESC"
}

rm -f /tmp/.X99-lock || true
Xvfb :99 -screen 0 1024x768x16 -nolisten tcp >/dev/null 2>&1 &
XVFB_PID=$!
trap 'kill "$XVFB_PID" 2>/dev/null || true' EXIT

if [ ! -d "$WINEPREFIX" ]; then
  wineboot -i || true
fi

SERVER_EXE=$(find "$SERVERDIR" -iname "WindroseServer-Win64-Shipping.exe" | head -n 1 || true)
if [ -z "$SERVER_EXE" ]; then
  echo "ERROR: Windrose server executable not found"
  find "$SERVERDIR" -maxdepth 4
  exit 1
fi

echo "Starting Windrose dedicated server without SteamCMD validation"
echo "Executable: $SERVER_EXE"

exec wine "$SERVER_EXE" \
  -log \
  -MULTIHOME="$MULTIHOME" \
  -PORT="$PORT" \
  -QUERYPORT="$QUERYPORT"
