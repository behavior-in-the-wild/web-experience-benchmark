from __future__ import annotations

import argparse
import json
from pathlib import Path

from docker_tool.hosting import doctor, start_host, stop_host
from docker_tool.resources import SlotLease


def _json_default(obj):
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    raise TypeError(type(obj).__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="Shared Docker/local hosting tool for benchmark jobs")
    sub = parser.add_subparsers(dest="cmd", required=True)

    host = sub.add_parser("host")
    host.add_argument("--repo-dir", required=True)
    host.add_argument("--framework", default="Static HTML")
    host.add_argument("--host-file-path", default="")
    host.add_argument("--port", type=int, required=True)
    host.add_argument("--log", required=True)
    host.add_argument("--mode", choices=["auto", "docker", "local"], default="auto")
    host.add_argument("--slot-json", default="")

    stop = sub.add_parser("stop")
    stop.add_argument("--container-id", default="")
    stop.add_argument("--pid", type=int, default=0)

    sub.add_parser("doctor")

    args = parser.parse_args()
    if args.cmd == "host":
        slot = SlotLease(**json.loads(args.slot_json)) if args.slot_json else None
        result = start_host(
            repo_dir=Path(args.repo_dir),
            framework=args.framework,
            host_file_path=args.host_file_path,
            port=args.port,
            log=Path(args.log),
            mode=args.mode,
            slot=slot,
        )
        print(json.dumps(result.to_dict(), default=_json_default))
        return 0 if result.status == "success" else 1
    if args.cmd == "stop":
        stop_host(container_id=args.container_id or None, pid=args.pid or None)
        print(json.dumps({"status": "success"}))
        return 0
    if args.cmd == "doctor":
        print(json.dumps(doctor(), indent=2))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
