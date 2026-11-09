#!/usr/bin/env python3
"""Read-only Linux environment and offline-tool checks; never a robot experiment.

Run as an ordinary user. Each invocation creates a new result directory. Only
standard-library orchestration runs here; subprocesses use preview/offline modes
or the existing fully mocked tests. No sudo, installation, capture, packet send,
ROS connection, VM configuration, or system configuration change is performed.
"""
import argparse
import ast
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys
import time


HEX_FRAME = "300000007b14ae47e17a843f" + "0000000000000000" * 5
SCOPE = "Linux environment and synthetic/mocked offline checks only; no field/robot experiment or network traffic"


def write_json(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def write_text(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(value)


def read_value(path):
    try:
        return {"value": Path(path).read_text(encoding="utf-8").strip()}
    except (OSError, UnicodeError) as exc:
        return {"value": None, "error": str(exc), "error_type": type(exc).__name__}


def make_output(root):
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    for suffix in [""] + ["_%02d" % index for index in range(1, 1000)]:
        candidate = root / (stamp + suffix)
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            continue
    raise FileExistsError("could not create a new timestamped output directory")


def decoded(value):
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else (value or "")


class CheckRun:
    def __init__(self, output, work, timeout):
        self.output, self.work, self.timeout = output, work, timeout
        self.results = []
        self.process_number = 0

    def record(self, name, passed, details):
        self.results.append({"name": name, "passed": bool(passed), "details": details})

    def process(self, name, arguments):
        if "--execute" in arguments:
            raise ValueError("live --execute is forbidden in this checker")
        self.process_number += 1
        prefix = "%02d_%s" % (self.process_number, name)
        command = [sys.executable, "-B", *arguments]
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        started = time.monotonic()
        result = {"command": command, "timeout_seconds": self.timeout,
                  "live_execute_requested": False, "cwd": str(self.work)}
        try:
            child = subprocess.run(command, cwd=str(self.work), env=env, timeout=self.timeout,
                                   capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   check=False)
            stdout, stderr = child.stdout, child.stderr
            result.update(exit_code=child.returncode, timed_out=False)
        except subprocess.TimeoutExpired as exc:
            stdout, stderr = decoded(exc.stdout), decoded(exc.stderr)
            result.update(exit_code=None, timed_out=True, error="subprocess timeout")
        except OSError as exc:
            stdout, stderr = "", str(exc)
            result.update(exit_code=None, timed_out=False, error_type=type(exc).__name__, error=str(exc))
        result["elapsed_seconds"] = round(time.monotonic() - started, 6)
        result["stdout_file"] = prefix + ".stdout.txt"
        result["stderr_file"] = prefix + ".stderr.txt"
        write_text(self.output / result["stdout_file"], stdout)
        write_text(self.output / result["stderr_file"], stderr)
        write_json(self.output / (prefix + ".result.json"), result)
        return result, stdout, stderr

    def checked_process(self, name, arguments, verify):
        result, stdout, stderr = self.process(name, arguments)
        passed = result.get("exit_code") == 0 and not result.get("timed_out")
        try:
            verification = verify(stdout, stderr)
            passed = passed and bool(verification)
            result["expected_output_verified"] = bool(verification)
        except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
            passed = False
            result["verification_error"] = str(exc)
        self.record(name, passed, result)


def verify_twist(stdout, _stderr, expected, synthetic):
    data = json.loads(stdout)
    return (data["offline_only"] is True and data["synthetic_input"] is synthetic
            and data["byte_length"] == 52 and data["serialized_message_bytes"] == 48
            and data["prefix_little_endian"] == "30000000" and data["length_preserved"] is True
            and len(bytes.fromhex(data["before_hex"])) == 52
            and len(bytes.fromhex(data["after_hex"])) == 52
            and len(data["before"]) == 6 and len(data["after"]) == 6
            and math.isclose(data["before"][0], 0.01, rel_tol=0, abs_tol=1e-15)
            and math.isclose(data["after"][0], expected, rel_tol=0, abs_tol=1e-15)
            and all(value == 0 for value in data["before"][1:] + data["after"][1:]))


def verify_arp(stdout, _stderr):
    records = []
    for line in stdout.splitlines():
        if line.startswith("{"):
            records.append(json.loads(line))
    plans = [item for item in records if item.get("event") == "plan"]
    return (len(plans) == 1 and plans[0].get("execute") is False
            and plans[0].get("restore_only") is False
            and any(item.get("event") == "dry_run_no_packets_sent" for item in records)
            and not any(item.get("event") in ("poison_pair_sent", "restore_frames_sent_verify_both_endpoints")
                        for item in records))


def collect_environment():
    data = {"timestamp": datetime.now().astimezone().isoformat(), "scope": SCOPE,
            "sys_version": sys.version, "sys_executable": sys.executable,
            "python_version": platform.python_version(), "platform": platform.platform(),
            "system": platform.system(), "hostname": socket.gethostname(),
            "euid": os.geteuid() if hasattr(os, "geteuid") else None,
            "tools": {name: shutil.which(name) for name in ("arpspoof", "tcpdump", "wireshark", "tshark")},
            "ip_forward": read_value("/proc/sys/net/ipv4/ip_forward"), "interfaces": []}
    try:
        for interface in sorted(Path("/sys/class/net").iterdir()):
            data["interfaces"].append({"name": interface.name,
                                       "carrier": read_value(interface / "carrier"),
                                       "operstate": read_value(interface / "operstate")})
    except OSError as exc:
        data["interfaces_error"] = str(exc)
    return data


def run_checks(output, timeout):
    source = Path(__file__).resolve().parent
    work = output / "execution_copy"
    work.mkdir()
    run = CheckRun(output, work, timeout)
    environment = collect_environment()
    write_json(output / "environment.json", environment)
    run.record("linux", environment["system"] == "Linux", {"system": environment["system"]})
    run.record("ordinary_user", environment["euid"] is not None and environment["euid"] != 0,
               {"euid": environment["euid"], "requirement": "run without sudo"})
    for name, location in environment["tools"].items():
        run.record("binary_" + name, location is not None or name == "arpspoof",
                   {"shutil_which": location, "invoked": False, "required_for_lab6": name != "arpspoof",
                    "note": "arpspoof is retained only for historical/lab5 use; lab6 uses the Scapy helper" if name == "arpspoof" else ""})
    run.record("read_ip_forward", environment["ip_forward"]["value"] in ("0", "1"),
               {**environment["ip_forward"], "changed": False,
                "note": "value 1 is recorded, not changed; a later live relay requires 0"})
    run.record("read_interfaces", bool(environment["interfaces"]) and "interfaces_error" not in environment,
               {"interfaces": environment["interfaces"], "network_connectivity_required": False})

    manifest, ast_failures = [], []
    for script in sorted(source.glob("*.py")):
        raw = script.read_bytes()
        entry = {"name": script.name, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        try:
            ast.parse(raw.decode("utf-8-sig"), filename=str(script))
            entry["ast_passed"] = True
        except (SyntaxError, UnicodeError) as exc:
            entry.update(ast_passed=False, error=str(exc))
            ast_failures.append(script.name)
        manifest.append(entry)
        with (work / script.name).open("xb") as stream:
            stream.write(raw)
    write_json(output / "source_manifest.json", manifest)
    required = {"relay_twist.py", "offline_twist.py", "twist_codec.py", "test_tools.py", "arpCheat.py",
                "mtu_forward.py", "test_mtu_forward.py"}
    missing = sorted(required - {entry["name"] for entry in manifest})
    run.record("python_ast", bool(manifest) and not ast_failures and not missing,
               {"script_count": len(manifest), "ast_failures": ast_failures, "missing_required": missing,
                "note": "AST alone does not verify runtime compatibility; subprocesses below exercise imports"})

    module_code = ("import importlib,json; result={}; "
                   "\nfor name in ('scapy','dpkt'):"
                   "\n m=importlib.import_module(name)"
                   "\n result[name]={'version':str(getattr(m,'__version__','unknown')),'file':str(m.__file__)}"
                   "\nprint(json.dumps(result))")
    def verify_modules(stdout, _stderr):
        data = json.loads(stdout)
        write_json(output / "module_versions.json", data)
        return all(data[name]["version"] not in ("", "unknown", "None") for name in ("scapy", "dpkt"))
    run.checked_process("module_versions", ["-c", module_code], verify_modules)
    run.checked_process("mock_memory_tests", ["test_tools.py"],
                        lambda out, err: bool(re.search(r"Ran\s+54\s+tests?\s+in", out + err)
                                              and re.search(r"^OK\s*$", out + err, re.MULTILINE)))
    run.checked_process("mtu_forward_offline_tests", ["test_mtu_forward.py"],
                        lambda out, err: bool(re.search(r"Ran\s+15\s+tests?\s+in", out + err)
                                              and re.search(r"^OK\s*$", out + err, re.MULTILINE)))
    run.checked_process("offline_invert", ["offline_twist.py", "--mode", "invert", "--linear-x", "0.01", "--angular-z", "0"],
                        lambda out, err: verify_twist(out, err, -0.01, True))
    run.checked_process("offline_scale", ["offline_twist.py", "--mode", "scale", "--factor", "0.5", "--linear-x", "0.01", "--angular-z", "0"],
                        lambda out, err: verify_twist(out, err, 0.005, True))
    run.checked_process("offline_hex", ["offline_twist.py", "--mode", "invert", "--hex", HEX_FRAME],
                        lambda out, err: verify_twist(out, err, -0.01, False))

    preview_log = work / "preview_must_not_create.jsonl"
    common = ["--iface", "lab6_offline0", "--a-ip", "192.168.77.10", "--a-mac", "02:00:00:00:00:0a",
              "--b-ip", "192.168.77.20", "--b-mac", "02:00:00:00:00:0b", "--m-mac", "02:00:00:00:00:0c"]
    def verify_relay(stdout, _stderr):
        data = json.loads(stdout)
        return (data["preview_only"] is True and data["capture"] is False and data["send"] is False
                and data["configuration"]["mode"] == "forward" and not preview_log.exists())
    run.checked_process("relay_default_preview", ["relay_twist.py", *common, "--log", str(preview_log)], verify_relay)
    arp_options = ("--iface", "--controller", "--controller-mac", "--robot", "--robot-mac", "--attacker-mac")
    run.checked_process("arp_help", ["arpCheat.py", "--help"],
                        lambda out, err: all(option in out for option in arp_options))
    run.checked_process("arp_default_preview", ["arpCheat.py", "--iface", "lab6_offline0",
                        "--controller", "192.168.77.10", "--controller-mac", "02:00:00:00:00:0a",
                        "--robot", "192.168.77.20", "--robot-mac", "02:00:00:00:00:0b",
                        "--attacker-mac", "02:00:00:00:00:0c"], verify_arp)
    successes = [entry["name"] for entry in run.results if entry["passed"]]
    failures = [entry["name"] for entry in run.results if not entry["passed"]]
    summary = {"scope": SCOPE, "output_dir": str(output), "passed": not failures,
               "successes": successes, "failures": failures, "checks": run.results,
               "mock_test_expected_count": 54, "mtu_offline_test_expected_count": 15,
               "actual_capture_or_send_requested": False,
               "source_directory_changed": False,
               "field_experiment_completed": False}
    write_json(output / "summary.json", summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=Path.home() / "lab6_environment",
                        help="parent for a new timestamp directory (default: ~/lab6_environment)")
    parser.add_argument("--timeout", type=float, default=30, help="per subprocess timeout, 1 to 30 seconds")
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or not 1 <= args.timeout <= 30:
        parser.error("--timeout must be finite and between 1 and 30 seconds")
    output = None
    try:
        output = make_output(args.output_root)
        summary = run_checks(output, args.timeout)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        failure = {"scope": SCOPE, "output_dir": str(output) if output else None, "passed": False,
                   "successes": [], "failures": ["checker_error"],
                   "error_type": type(exc).__name__, "error": str(exc), "field_experiment_completed": False}
        if output:
            try:
                write_json(output / "checker_error.json", failure)
            except OSError:
                pass
        print(json.dumps(failure, ensure_ascii=False), flush=True)
        return 2
    print(json.dumps({key: summary[key] for key in ("output_dir", "passed", "successes", "failures", "scope",
                                                   "field_experiment_completed")}, ensure_ascii=False), flush=True)
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
