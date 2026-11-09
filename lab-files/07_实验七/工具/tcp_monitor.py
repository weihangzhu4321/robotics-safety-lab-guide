#!/usr/bin/env python3
"""Opt-in 1 Hz TCP connection observation; no application data is sent."""
import sys
sys.dont_write_bytecode = True
import argparse
import errno
import json
import math
import time

import syn_probe


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--target", required=True)
    result.add_argument("--iface", required=True)
    result.add_argument("--port", required=True, type=int)
    result.add_argument("--phase", choices=("baseline", "test", "recovery"), default="baseline")
    result.add_argument("--count", type=int, help="default: one attempt per configured second")
    result.add_argument("--seconds", type=int, default=30)
    result.add_argument("--timeout", type=float, default=0.8)
    result.add_argument("--execute", action="store_true")
    result.add_argument("--lab-confirmed", action="store_true")
    result.add_argument("--confirm-target")
    result.add_argument("--output")
    return result


def validate_config(args):
    syn_probe.validate_target(args.target)
    syn_probe.validate_iface(args.iface)
    if not 1 <= args.port <= 65535:
        raise ValueError("port must be 1..65535")
    if args.count is None:
        args.count = args.seconds
    if not 1 <= args.count <= 30 or not 1 <= args.seconds <= 30 or args.count > args.seconds:
        raise ValueError("1..30 attempts, 1..30 seconds, count <= seconds required")
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 0.8:
        raise ValueError("timeout must be finite, >0 and <=0.8 seconds")
    if args.confirm_target is not None and args.confirm_target != args.target:
        raise ValueError("confirm-target must exactly match target")
    if args.execute and (not args.lab_confirmed or args.confirm_target != args.target or not args.output):
        raise ValueError("execution requires --lab-confirmed --confirm-target TARGET --output NEW_DIRECTORY")


def execute_monitor(args):
    output = syn_probe.new_output_directory(args.output)
    summary = {"kind": "tcp_connection_observation", "phase": args.phase,
               "target": args.target, "port": args.port, "iface": args.iface,
               "started_utc": syn_probe.utc_now(), "limits": {"count": args.count,
               "seconds": args.seconds, "attempt_timeout": args.timeout, "maximum_rate_hz": 1},
               "attempts": 0, "successes": 0, "interrupted": False,
               "scope": "TCP connection setup timing only; not ROS latency or physical robot response"}
    rows = []
    status = 1
    try:
        summary["network"] = syn_probe.verify_network(args.target, args.iface)
        # Lazy import: preview mode neither creates sockets nor performs a network call.
        import socket
        start = time.monotonic()
        last_attempt_start = None
        with (output / "connections.jsonl").open("w", encoding="utf-8") as log:
            for index in range(args.count):
                scheduled = start if last_attempt_start is None else last_attempt_start + 1.0
                remaining = scheduled - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
                if time.monotonic() - start >= args.seconds:
                    break
                row = {"index": index + 1, "utc": syn_probe.utc_now(), "success": False,
                       "returncode": None}
                last_attempt_start = time.monotonic()
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
                        connection.settimeout(min(args.timeout, args.seconds - (last_attempt_start - start)))
                        connection.bind((summary["network"]["source"], 0))
                        row["returncode"] = connection.connect_ex((args.target, args.port))
                        row["success"] = row["returncode"] == 0
                        if not row["success"]:
                            row["error"] = errno.errorcode.get(row["returncode"], str(row["returncode"]))
                except OSError as exc:
                    row["error"] = str(exc)
                row["elapsed_ms"] = round((time.monotonic() - last_attempt_start) * 1000, 3)
                rows.append(row)
                log.write(json.dumps(row) + "\n")
                log.flush()
        status = 0
    except KeyboardInterrupt:
        summary["interrupted"] = True
        status = 130
    except (ValueError, OSError) as exc:
        summary["error"] = str(exc)
    finally:
        summary["attempts"] = len(rows)
        successes = [row for row in rows if row["success"]]
        summary["successes"] = len(successes)
        summary["success_ratio"] = len(successes) / len(rows) if rows else None
        summary["mean_success_elapsed_ms"] = round(sum(row["elapsed_ms"] for row in successes) / len(successes), 3) if successes else None
        summary["ended_utc"] = syn_probe.utc_now()
        summary["exit_status"] = status
        syn_probe.write_json(output / "summary.json", summary)
    print(json.dumps({"output": str(output), "summary": summary}, indent=2))
    return status


def main(argv=None):
    argument_parser = parser()
    args = argument_parser.parse_args(argv)
    try:
        validate_config(args)
    except ValueError as exc:
        argument_parser.error(str(exc))
    if not args.execute:
        print(json.dumps({"mode": "preview_only", "target": args.target, "port": args.port,
                          "iface": args.iface, "phase": args.phase, "limits": {
                          "count": args.count, "seconds": args.seconds, "timeout": args.timeout,
                          "maximum_rate_hz": 1}, "writes": False, "network_activity": False,
                          "scope": "TCP connect only, no application data; not ROS or physical latency"}, indent=2))
        return 0
    try:
        return execute_monitor(args)
    except OSError as exc:
        print("Refused: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
