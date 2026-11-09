#!/usr/bin/env python3
"""Read-only dependency checks plus isolated, synthetic offline self-tests."""
import argparse
import ast
import datetime
import hashlib
import importlib.metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import offline_packets

REQUIRED_FILES = ("syn_probe.py", "tcp_monitor.py", "offline_packets.py", "offline_syn.c",
                  "check_lab7_env.py", "test_tools.py", "README.md")
COMMON_COMMANDS = {"python3": ["--version"], "tcpdump": ["--version"],
                   "ip": ["-V"], "timeout": ["--version"]}
ATTACKER_COMMANDS = {"hping3": ["--version"], "nmap": ["--version"],
                     "tshark": ["--version"], "wireshark": ["--version"], "gcc": ["--version"]}


def check_command(name, arguments):
    executable = shutil.which(name)
    if not executable:
        return {"name": "command:" + name, "status": "FAIL", "detail": "missing command"}
    try:
        completed = subprocess.run([executable] + arguments, capture_output=True, text=True,
                                   timeout=10, check=False)
        text = (completed.stdout + completed.stderr).strip()
        # hping3 3.0.0-alpha-2 can report version on stderr with exit status 1.
        okay = completed.returncode == 0 or (name == "hping3" and "hping" in text.lower() and "version" in text.lower())
        return {"name": "command:" + name, "status": "PASS" if okay else "FAIL",
                "path": executable, "returncode": completed.returncode, "detail": text[:2000],
                "command": [executable] + arguments}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"name": "command:" + name, "status": "FAIL", "detail": str(exc)}


def check_manifest(manifest, package_root):
    if not manifest.is_file():
        return {"name": "package:manifest", "status": "SKIP", "detail": "manifest missing; deployment acceptance remains incomplete"}
    try:
        mapping = json.loads(manifest.read_text(encoding="utf-8-sig"))
        if not isinstance(mapping, dict):
            raise ValueError("manifest must be a JSON object")
        mapping = mapping.get("files", mapping)
        if not isinstance(mapping, dict) or not mapping:
            raise ValueError("manifest must contain a nonempty relative-path-to-SHA256 map")
        verified = []
        for relative, expected in mapping.items():
            if not isinstance(relative, str) or not isinstance(expected, (str, dict)):
                raise ValueError("invalid manifest entry")
            expected = expected.get("sha256") if isinstance(expected, dict) else expected
            path = (package_root / relative.replace("\\", "/")).resolve()
            if package_root.resolve() not in path.parents:
                raise ValueError("manifest path escapes package root")
            if not isinstance(expected, str) or len(expected) != 64:
                raise ValueError("invalid SHA256: " + relative)
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual.lower() != expected.lower():
                raise ValueError("hash mismatch: " + relative)
            verified.append(relative)
        return {"name": "package:manifest", "status": "PASS", "verified_files": len(verified),
                "manifest": str(manifest)}
    except (OSError, ValueError, TypeError) as exc:
        return {"name": "package:manifest", "status": "FAIL", "detail": str(exc)}


def run_checks(role, output, manifest=None):
    output = Path(output).expanduser().absolute()
    output.mkdir(parents=False, exist_ok=False)
    root = Path(__file__).resolve().parent
    checks = []
    files = {}
    manifest = Path(manifest) if manifest else root.parent / "文件校验.json"
    checks.append(check_manifest(manifest, root.parent))
    for name in REQUIRED_FILES:
        path = root / name
        if not path.is_file():
            checks.append({"name": "file:" + name, "status": "FAIL", "detail": "missing file"})
            continue
        data = path.read_bytes()
        files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        try:
            if path.suffix == ".py":
                ast.parse(data.decode("utf-8"), filename=str(path))
            checks.append({"name": "file:" + name, "status": "PASS"})
        except (SyntaxError, UnicodeError) as exc:
            checks.append({"name": "file:" + name, "status": "FAIL", "detail": str(exc)})
    checks.append({"name": "platform:Linux", "status": "PASS" if sys.platform.startswith("linux") else "FAIL",
                   "detail": sys.platform + "; deployment uses Linux"})
    checks.append({"name": "python:3.8+", "status": "PASS" if sys.version_info >= (3, 8) else "FAIL",
                   "detail": sys.version})
    commands = dict(COMMON_COMMANDS)
    if role == "attacker":
        commands.update(ATTACKER_COMMANDS)
    for name, arguments in commands.items():
        checks.append(check_command(name, arguments))
    try:
        scapy_version = importlib.metadata.version("scapy")
        checks.append({"name": "optional:scapy", "status": "PASS", "detail": scapy_version})
    except importlib.metadata.PackageNotFoundError:
        checks.append({"name": "optional:scapy", "status": "OPTIONAL_MISSING",
                       "detail": "not required; all supplied tools use Python stdlib"})
    try:
        with tempfile.TemporaryDirectory(prefix="lab7_offline_") as temporary:
            temporary = Path(temporary)
            summary = offline_packets.generate(temporary / "fixtures")
            valid = [len(offline_packets.parse_pcap((temporary / "fixtures" / (name + ".pcap")).read_bytes()))
                     == case["total_packets"] for name, case in summary["cases"].items()]
            if not all(valid):
                raise ValueError("PCAP round-trip count mismatch")
            (output / "fixture_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
            checks.append({"name": "offline:fixtures", "status": "PASS", "detail": "4 synthetic PCAP cases round-tripped in temporary directory"})
            if role == "attacker" and shutil.which("gcc"):
                compiler_command = [shutil.which("gcc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
                                    str(root / "offline_syn.c"), "-o", str(temporary / "offline_syn")]
                compile_run = subprocess.run(compiler_command, capture_output=True, text=True, timeout=30, check=False)
                if compile_run.returncode:
                    raise ValueError("C11 compilation failed: " + compile_run.stderr[:2000])
                c_output = temporary / "from_c.pcap"
                c_run = subprocess.run([str(temporary / "offline_syn"), str(c_output)], capture_output=True,
                                       text=True, timeout=5, check=False)
                if c_run.returncode or c_output.read_bytes() != offline_packets.pcap_bytes([offline_packets.tcp_packet()]):
                    raise ValueError("C/Python first SYN PCAP differs")
                checks.append({"name": "offline:C11", "status": "PASS", "detail": "-Wall -Wextra -Werror; C and Python PCAP bytes identical"})
            elif role == "attacker":
                checks.append({"name": "offline:C11", "status": "FAIL", "detail": "gcc missing, no compilation performed"})
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        checks.append({"name": "offline:fixtures_or_C11", "status": "FAIL", "detail": str(exc)})
    try:
        command = [sys.executable, "-B", "-m", "unittest", "discover", "-s", str(root), "-p", "test_tools.py", "-v"]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
        (output / "unittest.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
        checks.append({"name": "offline:unittest", "status": "PASS" if completed.returncode == 0 else "FAIL",
                       "returncode": completed.returncode, "detail": "see unittest.log"})
    except (OSError, subprocess.SubprocessError) as exc:
        checks.append({"name": "offline:unittest", "status": "FAIL", "detail": str(exc)})
    report = {"role": role, "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "scope": "OFFLINE ONLY: no hping send, nmap scan, capture, ROS process, or robot action",
              "status": "FAIL" if any(check["status"] == "FAIL" for check in checks) else
                        "INCOMPLETE" if any(check["status"] == "SKIP" for check in checks) else "PASS",
              "checks": checks, "files": files}
    (output / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True, choices=("controller", "attacker"))
    parser.add_argument("--output", required=True, help="fresh directory; parent must exist")
    parser.add_argument("--manifest", help="default: package root / 文件校验.json")
    args = parser.parse_args(argv)
    try:
        return run_checks(args.role, args.output, args.manifest)
    except OSError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
