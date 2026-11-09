#!/usr/bin/env python3
"""Finite, opt-in SYN observation for an explicitly authorized private lab."""
import sys
sys.dont_write_bytecode = True
import argparse
import datetime
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess

PRIVATE_NETWORKS = tuple(ipaddress.IPv4Network(x) for x in
                         ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def validate_target(value):
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as exc:
        raise ValueError("target must be a literal IPv4 address") from exc
    if not any(address in network for network in PRIVATE_NETWORKS):
        raise ValueError("only RFC1918 lab IPv4 targets are permitted")
    # Common /24 edge addresses are conservatively refused even before route lookup.
    if int(address) & 255 in (0, 255):
        raise ValueError(".0 and .255 addresses are conservatively refused")
    return str(address)


def validate_iface(value):
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,14}", value or ""):
        raise ValueError("iface must be a Linux interface name, maximum 15 characters")
    return value


def validate_config(args):
    validate_target(args.target)
    validate_iface(args.iface)
    if not 1 <= args.port <= 65535:
        raise ValueError("port must be 1..65535")
    if not 1 <= args.count <= 100:
        raise ValueError("count must be 1..100")
    if not 1 <= args.pps <= 10:
        raise ValueError("pps must be 1..10")
    if not 1 <= args.seconds <= 15:
        raise ValueError("seconds must be 1..15")
    if args.count > args.pps * args.seconds:
        raise ValueError("count must not exceed pps * seconds")
    if args.confirm_target is not None and args.confirm_target != args.target:
        raise ValueError("confirm-target must exactly match target")
    if args.execute and (not args.lab_confirmed or args.confirm_target != args.target):
        raise ValueError("execution requires --lab-confirmed and --confirm-target TARGET")
    if args.execute and not args.output:
        raise ValueError("execution requires --output NEW_DIRECTORY")


def build_command(args, executable="hping3"):
    # ceil ensures the configured packet rate is never exceeded.
    interval = int(math.ceil(1000000.0 / args.pps))
    return [executable, "-n", "-I", args.iface, "-S", "-p", str(args.port),
            "-c", str(args.count), "-i", "u" + str(interval), args.target]


def new_output_directory(value):
    output = Path(value).expanduser().absolute()
    # Atomic exclusive creation; never overwrite previous evidence.
    output.mkdir(parents=False, exist_ok=False)
    return output


def read_json_command(command):
    completed = subprocess.run(command, capture_output=True, text=True,
                               timeout=5, check=False)
    if completed.returncode != 0:
        raise ValueError("read-only network check failed: " + " ".join(command) +
                         "; " + completed.stderr.strip()[:500])
    result = json.loads(completed.stdout)
    if not isinstance(result, list):
        raise ValueError("unexpected ip JSON output")
    return result


def verify_network(target, iface):
    """Refuse routed, gateway, local, multicast, broadcast, and subnet targets."""
    validate_target(target)
    validate_iface(iface)
    if not sys.platform.startswith("linux"):
        raise ValueError("execution is supported only on Linux")
    if not (Path("/sys/class/net") / iface).is_dir():
        raise ValueError("requested interface does not exist")
    ip_command = shutil.which("ip")
    if not ip_command:
        raise ValueError("ip command is missing")
    addresses = read_json_command([ip_command, "-j", "-4", "addr", "show"])
    defaults = read_json_command([ip_command, "-j", "-4", "route", "show", "default"])
    routes = read_json_command([ip_command, "-j", "-4", "route", "get", target])
    target_ip = ipaddress.IPv4Address(target)
    local_addresses = []
    selected = []
    for interface in addresses:
        for info in interface.get("addr_info", []):
            if info.get("family") != "inet" or "local" not in info:
                continue
            local = ipaddress.IPv4Address(info["local"])
            local_addresses.append(str(local))
            if interface.get("ifname") == iface:
                prefix = int(info["prefixlen"])
                network = ipaddress.IPv4Network(str(local) + "/" + str(prefix), strict=False)
                if target_ip in (network.network_address, network.broadcast_address):
                    raise ValueError("target is interface subnet/broadcast address")
                if target_ip in network:
                    selected.append((str(local), str(network)))
    if target in local_addresses:
        raise ValueError("target is an address of this computer")
    gateways = [entry.get("gateway") for entry in defaults if entry.get("gateway")]
    if target in gateways:
        raise ValueError("target is a default gateway")
    if not selected:
        raise ValueError("target must be in a directly connected IPv4 subnet of iface")
    if len(routes) != 1:
        raise ValueError("ambiguous or absent route")
    route = routes[0]
    if (route.get("dev") != iface or route.get("gateway") or
            route.get("type", "unicast") != "unicast"):
        raise ValueError("target must use iface directly, without a gateway/local route")
    source = route.get("prefsrc", route.get("src"))
    if source not in [entry[0] for entry in selected]:
        raise ValueError("route source must belong to the selected interface")
    return {"source": source, "direct_networks": [entry[1] for entry in selected],
            "default_gateways": gateways, "route": route}


def find_conflicting_processes(proc_root=Path("/proc")):
    conflicts = []
    excluded = {os.getpid(), os.getppid()}
    for entry in proc_root.iterdir():
        if not entry.name.isdigit() or int(entry.name) in excluded:
            continue
        try:
            parts = (entry / "cmdline").read_bytes().split(b"\x00")
            names = [Path(x.decode("utf-8", "replace")).name for x in parts if x]
        except (OSError, ValueError):
            continue
        suspicious = [name for name in names if name in ("hping", "hping2", "hping3", "arpspoof", "syn_probe.py")
                      or ("relay" in name.lower() and name.endswith(".py"))]
        if suspicious:
            conflicts.append({"pid": int(entry.name), "programs": suspicious})
    return conflicts


def stop_process_group(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGINT)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


class InterruptSignalHandlers:
    """Turn termination signals into cleanup, then restore the caller's handlers."""
    def __init__(self, receipt):
        self.receipt = receipt
        self.previous = {}
        self.cleaning = False

    def handle(self, signum, _frame):
        received = self.receipt.setdefault("received_signals", [])
        if len(received) < 16:
            received.append(signum)
        if self.receipt.get("actual_signal") is None:
            self.receipt["actual_signal"] = signum
            self.receipt["actual_signal_name"] = next(
                (name for name in ("SIGINT", "SIGTERM", "SIGHUP")
                 if getattr(signal, name, None) == signum), str(signum))
        # Repeated terminal/timeout signals must not interrupt child cleanup or receipt writing.
        already_interrupted = self.receipt.get("interrupted", False)
        self.receipt["interrupted"] = True
        if not already_interrupted and not self.cleaning:
            raise KeyboardInterrupt

    def install(self):
        for name in ("SIGINT", "SIGTERM", "SIGHUP"):
            signum = getattr(signal, name, None)
            if signum is not None and signum not in self.previous:
                previous = signal.getsignal(signum)
                signal.signal(signum, self.handle)
                self.previous[signum] = previous

    def restore(self):
        for signum, previous in self.previous.items():
            signal.signal(signum, previous)
        self.previous.clear()


def execute_probe(args):
    output = new_output_directory(args.output)
    receipt = {"schema": 1, "kind": "finite_syn_observation", "started_utc": utc_now(),
               "target": args.target, "iface": args.iface, "port": args.port,
               "limits": {"count": args.count, "pps": args.pps, "seconds": args.seconds},
               "command": build_command(args), "returncode": None, "timeout": False,
               "interrupted": False, "started_process": False, "cleanup_complete": True,
               "actual_signal": None, "actual_signal_name": None, "received_signals": [],
               "scope": "bounded observation; no claim of DoS or physical robot effects"}
    process = None
    handlers = InterruptSignalHandlers(receipt)
    exit_status = 1
    try:
        handlers.install()
        if not sys.platform.startswith("linux") or not hasattr(os, "geteuid") or os.geteuid() != 0:
            raise ValueError("SYN execution requires Linux root")
        receipt["network"] = verify_network(args.target, args.iface)
        conflicts = find_conflicting_processes()
        if conflicts:
            receipt["conflicts"] = conflicts
            raise ValueError("another hping/arpspoof/relay process is running; inspect it manually")
        executable = shutil.which("hping3")
        if not executable:
            raise ValueError("hping3 is missing")
        command = build_command(args, executable)
        receipt["command"] = command
        write_json(output / "receipt.json", receipt)
        with (output / "hping.stdout.txt").open("w", encoding="utf-8") as stdout, \
                (output / "hping.stderr.txt").open("w", encoding="utf-8") as stderr:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout,
                                       stderr=stderr, start_new_session=True)
            receipt["started_process"] = True
            receipt["cleanup_complete"] = False
            try:
                receipt["returncode"] = process.wait(timeout=args.seconds)
                exit_status = 0 if receipt["returncode"] == 0 else 1
            except subprocess.TimeoutExpired:
                receipt["timeout"] = True
                stop_process_group(process)
                exit_status = 124
            except KeyboardInterrupt:
                receipt["interrupted"] = True
                stop_process_group(process)
                exit_status = 130
    except KeyboardInterrupt:
        receipt["interrupted"] = True
        exit_status = 130
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        receipt["error"] = str(exc)
    finally:
        handlers.cleaning = True
        try:
            if process is not None:
                stop_process_group(process)
                receipt["returncode"] = process.returncode
                receipt["cleanup_complete"] = process.poll() is not None
            if receipt["interrupted"]:
                exit_status = 130
            receipt["ended_utc"] = utc_now()
            receipt["exit_status"] = exit_status
            write_json(output / "receipt.json", receipt)
        finally:
            handlers.restore()
    print(json.dumps({"output": str(output), "exit_status": exit_status,
                      "receipt": receipt}, indent=2, ensure_ascii=False))
    return exit_status


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--target", required=True)
    result.add_argument("--iface", required=True)
    result.add_argument("--port", required=True, type=int)
    result.add_argument("--count", type=int, default=20)
    result.add_argument("--pps", type=int, default=2)
    result.add_argument("--seconds", type=int, default=10)
    result.add_argument("--execute", action="store_true")
    result.add_argument("--lab-confirmed", action="store_true")
    result.add_argument("--confirm-target")
    result.add_argument("--output")
    return result


def main(argv=None):
    arguments = parser()
    args = arguments.parse_args(argv)
    try:
        validate_config(args)
    except ValueError as exc:
        arguments.error(str(exc))
    if not args.execute:
        print(json.dumps({"mode": "preview_only", "command": build_command(args),
                          "limits": {"count": args.count, "pps": args.pps,
                                     "seconds": args.seconds},
                          "network_checks": "not run; required at execution",
                          "writes": False, "network_activity": False,
                          "note": "private lab authorization is required; this is not a DoS success test"},
                         indent=2, ensure_ascii=False))
        return 0
    try:
        return execute_probe(args)
    except OSError as exc:
        print("Refused: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
