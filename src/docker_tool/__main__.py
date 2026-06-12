from __future__ import annotations

import argparse
import json
from pathlib import Path

from docker_tool.hosting import doctor, start_host, stop_host
from docker_tool.measurement import measure_in_docker
from docker_tool.resources import SlotLease, SlotScheduler
from docker_tool.visual import run_visual_in_docker


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

    measure = sub.add_parser("measure")
    measure.add_argument("--url", required=True)
    measure.add_argument("--device", choices=["mobile", "desktop"], required=True)
    measure.add_argument("--num-runs", type=int, default=5)
    measure.add_argument("--host-container-id", default="")
    measure.add_argument("--slot-json", default="")
    measure.add_argument("--headed", action="store_true")

    visual = sub.add_parser("visual")
    visual.add_argument("--url", required=True)
    visual.add_argument("--screenshot-path", required=True)
    visual.add_argument("--repo-id", required=True)
    visual.add_argument("--commit-id", default="")
    visual.add_argument("--framework", default="Static HTML")
    visual.add_argument("--host-file-path", default="")
    visual.add_argument("--patch-file", default="")
    visual.add_argument("--output-json", required=True)
    visual.add_argument("--host-container-id", default="")
    visual.add_argument("--slot-json", default="")

    slot_cmd = sub.add_parser("slot")
    slot_cmd.add_argument("--slot-index", type=int, required=True)
    slot_cmd.add_argument("--slot-count", type=int, default=0)
    slot_cmd.add_argument("--mode", default="docker")

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
    if args.cmd == "measure":
        slot = SlotLease(**json.loads(args.slot_json)) if args.slot_json else None
        result = measure_in_docker(
            url=args.url,
            device=args.device,
            num_runs=args.num_runs,
            host_container_id=args.host_container_id or None,
            slot=slot,
            headed=args.headed,
        )
        if result.result is not None:
            print(json.dumps(result.result, default=_json_default))
        else:
            print(json.dumps({"status": "error", "error": result.error, "command": result.command}, default=_json_default))
        return 0 if result.status == "success" else 1
    if args.cmd == "visual":
        slot = SlotLease(**json.loads(args.slot_json)) if args.slot_json else None
        result = run_visual_in_docker(
            url=args.url,
            screenshot_path=Path(args.screenshot_path),
            repo_id=args.repo_id,
            commit_id=args.commit_id,
            framework=args.framework,
            host_file_path=args.host_file_path,
            patch_file=Path(args.patch_file) if args.patch_file else None,
            output_json=Path(args.output_json),
            host_container_id=args.host_container_id or None,
            slot=slot,
        )
        if result.result is not None:
            print(json.dumps(result.result, default=_json_default))
        else:
            print(json.dumps({"status": "error", "error": result.error, "command": result.command}, default=_json_default))
        return 0 if result.status == "success" else 1
    if args.cmd == "slot":
        try:
            scheduler = SlotScheduler(slots=args.slot_count or None)
            print(json.dumps(scheduler.lease_for_index(args.slot_index, mode=args.mode).to_dict()))
            return 0
        except Exception as exc:
            print(json.dumps({"status": "error", "tool": "slot", "error": str(exc)}))
            return 1
    if args.cmd == "doctor":
        print(json.dumps(doctor(), indent=2))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
