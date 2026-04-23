import json
import shutil
import struct
import subprocess
import tempfile
import time
from pathlib import Path


ROOT = Path("/home/windrose")
WORLD_ID = "C61AE22221C343255BA6D86805D6A499"
WORLD_DB = ROOT / "data" / "R5" / "Saved" / "SaveProfiles" / "Default" / "RocksDB" / "0.10.0" / "Worlds" / WORLD_ID
MAP_BACKGROUND_DIR = ROOT / "panel" / "static" / "images"
MAP_BACKGROUND_NAMES = (
    "windrose-map.png",
    "windrose-map.jpg",
    "windrose-map.webp",
    "map-background.png",
    "map-background.jpg",
    "map-background.webp",
)

_CACHE = {"timestamp": 0, "data": None}


class BsonReader:
    def __init__(self, data):
        self.data = data

    def cstring(self, offset):
        end = self.data.index(0, offset)
        return self.data[offset:end].decode("utf-8", "replace"), end + 1

    def string(self, offset):
        size = struct.unpack_from("<i", self.data, offset)[0]
        start = offset + 4
        return self.data[start : start + size - 1].decode("utf-8", "replace"), start + size

    def document(self, offset=0):
        size = struct.unpack_from("<i", self.data, offset)[0]
        end = offset + size
        offset += 4
        out = {}
        while offset < end - 1:
            value_type = self.data[offset]
            offset += 1
            key, offset = self.cstring(offset)
            out[key], offset = self.value(value_type, offset)
        return out, end

    def value(self, value_type, offset):
        if value_type == 0x01:
            return struct.unpack_from("<d", self.data, offset)[0], offset + 8
        if value_type == 0x02:
            return self.string(offset)
        if value_type == 0x03:
            return self.document(offset)
        if value_type == 0x04:
            doc, offset = self.document(offset)
            return [doc[key] for key in sorted(doc, key=lambda item: int(item) if item.isdigit() else item)], offset
        if value_type == 0x08:
            return bool(self.data[offset]), offset + 1
        if value_type == 0x10:
            return struct.unpack_from("<i", self.data, offset)[0], offset + 4
        if value_type == 0x12:
            return struct.unpack_from("<q", self.data, offset)[0], offset + 8
        raise ValueError(f"Unsupported BSON value type {value_type:#x}")


def scan_column_family(db_path, family):
    result = subprocess.run(
        [
            "ldb",
            f"--db={db_path}",
            "--try_load_options",
            "--ignore_unknown_options",
            f"--column_family={family}",
            "scan",
            "--hex",
        ],
        text=True,
        capture_output=True,
        timeout=20,
    )
    if result.returncode != 0:
        return []

    rows = []
    for line in result.stdout.splitlines():
        if " ==> " not in line:
            continue
        key_hex, value_hex = line.split(" ==> ", 1)
        try:
            key = bytes.fromhex(key_hex[2:]).decode("utf-8", "replace")
            value, _ = BsonReader(bytes.fromhex(value_hex[2:])).document()
            rows.append((key, value))
        except Exception:
            continue
    return rows


def point(location):
    if not isinstance(location, dict):
        return None
    try:
        return {"x": float(location["X"]), "y": float(location["Y"]), "z": float(location.get("Z", 0))}
    except (KeyError, TypeError, ValueError):
        return None


def add_bounds(bounds, x, y):
    bounds["min_x"] = min(bounds["min_x"], x)
    bounds["max_x"] = max(bounds["max_x"], x)
    bounds["min_y"] = min(bounds["min_y"], y)
    bounds["max_y"] = max(bounds["max_y"], y)


def marker_name(raw):
    if not raw:
        return "Unknown"
    return raw.replace("Quest.Marker.", "").replace("Scenario.Chest.", "").replace("_", " ")


def collect_blackboard_markers(doc):
    markers = []
    blackboards = (
        doc.get("ScenarioSave", {})
        .get("OwnerBlackboard", {})
        .get("PersistantBlackboard", [])
    )
    for blackboard in blackboards:
        if not isinstance(blackboard, dict):
            continue
        for item in blackboard.get("BlackboardValues", []):
            if not isinstance(item, dict):
                continue
            name = item.get("Name", {}).get("TagName")
            values = item.get("BlackboardValues", [])
            state = values[0] if values else None
            if name and name.startswith("Quest.Marker."):
                markers.append({"name": marker_name(name), "active": state == "true"})
    return markers


def map_background_url():
    for name in MAP_BACKGROUND_NAMES:
        if (MAP_BACKGROUND_DIR / name).exists():
            return f"/static/images/{name}"
    return None


def build_map_data():
    with tempfile.TemporaryDirectory(prefix="windrose-map-") as tmp:
        snapshot = Path(tmp) / "worlddb"
        shutil.copytree(WORLD_DB, snapshot)

        island_rows = scan_column_family(snapshot, "R5BLIsland")
        chest_rows = scan_column_family(snapshot, "R5BLIslandChest")
        player_rows = scan_column_family(snapshot, "R5BLPlayerInWorld")

    bounds = {"min_x": float("inf"), "max_x": float("-inf"), "min_y": float("inf"), "max_y": float("-inf")}
    terrain = []
    pins = []
    marker_catalog = {}

    for _, doc in island_rows:
        for terrain_item in doc.get("Terrains", []):
            location = point(terrain_item.get("WorldLocation"))
            if not location:
                continue
            width = float(terrain_item.get("BoundsSizeX", 0))
            height = float(terrain_item.get("BoundsSizeY", 0))
            terrain.append({"x": location["x"], "y": location["y"], "w": width, "h": height})
            add_bounds(bounds, location["x"], location["y"])
            add_bounds(bounds, location["x"] + width, location["y"] + height)

    for key, doc in chest_rows:
        actor = doc.get("Actor", {}).get("ActorData", {})
        location = point(actor.get("WorldLocation"))
        if not location:
            continue
        tag = doc.get("CollectCounterTag", {}).get("TagName") or actor.get("ActorTag", {}).get("TagName")
        pins.append({"id": key, "type": "chest", "name": marker_name(tag), **location})
        add_bounds(bounds, location["x"], location["y"])

    seen_spawns = set()
    for key, doc in player_rows:
        player_id = doc.get("PlayerId", key)
        last_position = point(doc.get("SpawnLocations", {}).get("LastPositionData", {}).get("WorldLocation"))
        if last_position:
            pins.append({"id": f"{key}-last", "type": "player", "name": f"Last position {player_id[:6]}", **last_position})
            add_bounds(bounds, last_position["x"], last_position["y"])

        for spawn in doc.get("SpawnLocations", {}).get("Spawnpoints", {}).get("Spawnpoints", []):
            location = point(spawn.get("WorldLocation"))
            if not location:
                continue
            dedupe = (round(location["x"]), round(location["y"]), spawn.get("CheckPointType", "Spawn"))
            if dedupe in seen_spawns:
                continue
            seen_spawns.add(dedupe)
            pins.append({"id": spawn.get("SpawnRecordId", f"spawn-{len(seen_spawns)}"), "type": "spawn", "name": spawn.get("CheckPointType", "Spawn"), **location})
            add_bounds(bounds, location["x"], location["y"])

        for index, marker in enumerate(doc.get("UserMarkers", [])):
            location = point(marker.get("WorldLocation"))
            if not location:
                continue
            pins.append({"id": f"{key}-user-{index}", "type": "user", "name": "User marker", **location})
            add_bounds(bounds, location["x"], location["y"])

        for marker in collect_blackboard_markers(doc):
            marker_catalog[marker["name"]] = marker

    if not terrain and not pins:
        bounds = {"min_x": -900000, "max_x": 900000, "min_y": -900000, "max_y": 900000}

    return {
        "generated_at": int(time.time()),
        "world_id": WORLD_ID,
        "map_background": map_background_url(),
        "bounds": bounds,
        "terrain": terrain,
        "pins": pins,
        "marker_catalog": sorted(marker_catalog.values(), key=lambda item: item["name"]),
        "counts": {
            "terrain": len(terrain),
            "pins": len(pins),
            "chests": sum(1 for pin in pins if pin["type"] == "chest"),
            "spawns": sum(1 for pin in pins if pin["type"] == "spawn"),
            "user_markers": sum(1 for pin in pins if pin["type"] == "user"),
            "players": sum(1 for pin in pins if pin["type"] == "player"),
            "markers": len(marker_catalog),
        },
    }


def get_map_data(max_age=60):
    now = time.time()
    if _CACHE["data"] and now - _CACHE["timestamp"] < max_age:
        return _CACHE["data"]
    data = build_map_data()
    _CACHE.update({"timestamp": now, "data": data})
    return data
