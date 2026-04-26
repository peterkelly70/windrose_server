#!/usr/bin/env python3
import json
import sys
from pathlib import Path

ROOT = Path("/home/windrose")
CONFIG = ROOT / "config" / "instances.json"
EXAMPLE = ROOT / "config" / "instances.example.json"


def load_config():
    path = CONFIG if CONFIG.is_file() else EXAMPLE
    data = json.loads(path.read_text())
    return path, data


def validate_instance(instance):
    required = ["id", "name", "layout", "compose_project", "service_name", "runtime_root", "data_root", "env_file", "ports"]
    missing = [key for key in required if key not in instance or instance[key] in ("", None)]
    if missing:
        raise ValueError(f"Instance {instance.get('id', '<unknown>')} missing fields: {', '.join(missing)}")
    ports = instance["ports"]
    for key in ("game", "query"):
        if key not in ports:
            raise ValueError(f"Instance {instance['id']} missing port {key}")


def cmd_list(data):
    for item in data.get("instances", []):
        print(f"{item['id']}: {item['name']} [{item.get('layout', 'unknown')}]")


def cmd_show(data, instance_id):
    for item in data.get("instances", []):
        if item["id"] == instance_id:
            print(json.dumps(item, indent=2))
            return 0
    print(f"Instance not found: {instance_id}", file=sys.stderr)
    return 1


def cmd_validate(data):
    ids = set()
    port_pairs = set()
    for item in data.get("instances", []):
        validate_instance(item)
        if item["id"] in ids:
            raise ValueError(f"Duplicate instance id: {item['id']}")
        ids.add(item["id"])
        pair = (item["ports"]["game"], item["ports"]["query"])
        if pair in port_pairs:
            raise ValueError(f"Duplicate game/query port pair: {pair}")
        port_pairs.add(pair)
    print("Config valid.")


def main(argv):
    path, data = load_config()
    if len(argv) < 2:
        print(f"Using {path}")
        print("Usage: instance_manager.py {list|show <id>|validate}")
        return 2
    cmd = argv[1]
    if cmd == "list":
        cmd_list(data)
        return 0
    if cmd == "show":
        if len(argv) < 3:
            print("show requires an instance id", file=sys.stderr)
            return 2
        return cmd_show(data, argv[2])
    if cmd == "validate":
        cmd_validate(data)
        return 0
    print(f"Unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
