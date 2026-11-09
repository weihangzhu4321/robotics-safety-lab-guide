#!/usr/bin/env bash
# 实验五攻击机本机验收：默认不 sudo、不抓包、不探测网络、不发送报文。
# 用法：bash check_attacker_env.sh [--check-sudo]
# --check-sudo 只运行 sudo -n true，绝不索取或记录密码。
set -u
if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' '失败：缺少 python3，无法生成本机环境验收记录。' >&2
  exit 2
fi
LAB5_SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)" || exit 2
exec python3 - "$LAB5_SCRIPT_DIR" "$@" <<'PY'
"""只使用本地文件、内存构包和本机软件包数据库；没有网络连接代码。"""
import csv
import datetime as dt
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import traceback

tool_dir = Path(sys.argv[1]).resolve()
arguments = sys.argv[2:]
if any(arg != "--check-sudo" for arg in arguments) or len(arguments) > 1:
    print("用法：bash check_attacker_env.sh [--check-sudo]", file=sys.stderr)
    sys.exit(2)
check_sudo = arguments == ["--check-sudo"]
started = dt.datetime.now().astimezone()
run_dir = Path.home() / "lab5_environment" / started.strftime("%Y%m%d_%H%M%S_%f")
run_dir.mkdir(parents=True, exist_ok=False)
checks = []
report = {
    "kind": "lab5_local_environment_acceptance",
    "started_at": started.isoformat(),
    "output_directory": str(run_dir),
    "scope": "仅本机软件、内存报文与自建合成文件验收；不是现场实验结果",
    "network_connections_attempted": False,
    "network_packets_sent": False,
    "arp_poisoning_performed": False,
    "capture_validation": {
        "status": "not_performed",
        "reason": "本脚本不抓取任何接口；真实捕获能力须另行验证，不能由软件存在推断。",
    },
    "field_network_validation": {"status": "not_performed"},
    "host": {"platform": platform.platform(), "python": sys.version.split()[0]},
    "checks": checks,
}


def record(name, passed, detail, required=True):
    item = {"name": name, "status": "pass" if passed else "fail", "required": required, "detail": detail}
    checks.append(item)
    print(("通过" if passed else "失败") + "：" + name)
    return passed


def command(args, timeout=30):
    # 参数数组且不启用 shell；只调用下面明确列出的本地命令/本包离线工具。
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False,
                          env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})


def save_process(prefix, result):
    (run_dir / (prefix + ".stdout.txt")).write_text(result.stdout, encoding="utf-8")
    (run_dir / (prefix + ".stderr.txt")).write_text(result.stderr, encoding="utf-8")


def verify_scapy():
    from scapy.layers.l2 import ARP, Ether
    packet = Ether(src="02:00:00:00:00:30", dst="02:00:00:00:00:10") / ARP(
        op=2, psrc="192.168.254.20", pdst="192.168.254.10",
        hwsrc="02:00:00:00:00:30", hwdst="02:00:00:00:00:10")
    raw = bytes(packet)  # 只序列化到内存，不创建发送套接字。
    decoded = Ether(raw)
    assert len(raw) == 42
    assert decoded.type == 0x0806 and decoded[ARP].op == 2
    assert decoded[ARP].psrc == "192.168.254.20"
    assert decoded[ARP].pdst == "192.168.254.10"
    assert decoded.src == decoded[ARP].hwsrc == "02:00:00:00:00:30"
    assert decoded.dst == decoded[ARP].hwdst == "02:00:00:00:00:10"
    return {"version": importlib.metadata.version("scapy"), "serialized_bytes": len(raw),
            "test_data": "人工地址和MAC，纯内存构包，非现场"}


def verify_dry_run(script):
    log_file = run_dir / "arp_default_preview_NON_FIELD.jsonl"
    args = [sys.executable, str(script), "--iface", "lo",
            "--controller", "192.168.254.10", "--controller-mac", "02:00:00:00:00:10",
            "--robot", "192.168.254.20", "--robot-mac", "02:00:00:00:00:20",
            "--attacker-mac", "02:00:00:00:00:30", "--log", str(log_file)]
    # 故意不传 --execute；默认预览在 root/网卡检查及 sendp 之前返回。
    result = command(args)
    save_process("arp_default_preview_NON_FIELD", result)
    assert result.returncode == 0, "预览退出码为 %s；见 stdout/stderr" % result.returncode
    entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    assert entries and entries[0].get("event") == "plan" and entries[0].get("execute") is False
    assert entries[-1].get("event") == "dry_run_no_packets_sent"
    assert all(item.get("event") not in ("poison_pair_sent", "restore_frames_sent_verify_both_endpoints")
               for item in entries)
    return {"returncode": result.returncode, "log": log_file.name,
            "execute_argument_present": False, "test_data": "人工私有地址，非现场"}


def verify_offline_analyzer(script):
    import dpkt
    import socket  # 仅 inet_aton 解析文字地址；不调用 socket.socket/connect/send。
    capture = run_dir / "SYNTHETIC_NON_FIELD.pcap"
    marker = b"SYNTHETIC_NON_FIELD_DATA_"
    tcp_payload = (marker * 3)[:52]
    udp_payload = marker
    packets = []
    for proto, reverse, payload in ((dpkt.ip.IP_PROTO_TCP, False, tcp_payload),
                                    (dpkt.ip.IP_PROTO_UDP, True, udp_payload)):
        src_ip, dst_ip = (("192.168.254.20", "192.168.254.10") if reverse else
                          ("192.168.254.10", "192.168.254.20"))
        if proto == dpkt.ip.IP_PROTO_TCP:
            transport = dpkt.tcp.TCP(sport=40000, dport=30000, seq=1, ack=1,
                                     flags=dpkt.tcp.TH_ACK | dpkt.tcp.TH_PUSH, data=payload)
            transport.off = 5
        else:
            transport = dpkt.udp.UDP(sport=30001, dport=40001, data=payload)
            transport.ulen = len(transport)
        ip = dpkt.ip.IP(src=socket.inet_aton(src_ip), dst=socket.inet_aton(dst_ip),
                        p=proto, ttl=64, data=transport)
        ip.len = len(ip)
        eth = dpkt.ethernet.Ethernet(src=bytes.fromhex("020000000020" if reverse else "020000000010"),
                                   dst=bytes.fromhex("020000000010" if reverse else "020000000020"),
                                   type=dpkt.ethernet.ETH_TYPE_IP, data=ip)
        packets.append(bytes(eth))
    with capture.open("wb") as handle:
        writer = dpkt.pcap.Writer(handle, linktype=dpkt.pcap.DLT_EN10MB)
        for index, raw in enumerate(packets):
            writer.writepkt(raw, ts=float(index))  # 1970合成时间，避免冒充现场时间。
    with capture.open("rb") as handle:
        decoded = list(dpkt.pcap.Reader(handle))
    assert len(decoded) == 2
    assert bytes(dpkt.ethernet.Ethernet(decoded[0][1]).data.data.data) == tcp_payload
    assert bytes(dpkt.ethernet.Ethernet(decoded[1][1]).data.data.data) == udp_payload
    (run_dir / "SYNTHETIC_NON_FIELD_README.txt").write_text(
        "合成软件验收输入，非现场抓包。两包的时间为1970-01-01 UTC起的0秒与1秒。\n"
        "人工IP/MAC及载荷仅供离线分析器验收，不证明ROS、小车、桥接或捕获结果。\n", encoding="utf-8")
    analysis_dir = run_dir / "SYNTHETIC_NON_FIELD_analysis"
    result = command([sys.executable, str(script), str(capture), "--controller", "192.168.254.10",
                      "--robot", "192.168.254.20", "--out", str(analysis_dir), "--payload-length", "52"])
    save_process("offline_analyzer_NON_FIELD", result)
    assert result.returncode == 0, "分析器退出码为 %s；见 stdout/stderr" % result.returncode
    summary = json.loads((analysis_dir / "summary.json").read_text(encoding="utf-8"))
    counts = summary["counts"]
    assert summary["scan_complete"] is True
    assert counts.get("exported_tcp") == counts.get("exported_udp") == counts.get("candidate_rows") == 1
    assert summary["tcp_payload_length_histogram"] == {"52": 1}
    with (analysis_dir / "candidates.csv").open(newline="", encoding="utf-8-sig") as handle:
        candidates = list(csv.DictReader(handle))
    assert len(candidates) == 1
    return {"dpkt_version": importlib.metadata.version("dpkt"), "synthetic_packets": 2,
            "input_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
            "analyzer_returncode": result.returncode, "counts": counts, "test_data": "自建合成pcap，非现场"}


try:
    record("Linux运行环境", sys.platform.startswith("linux"), platform.system())
    os_release = Path("/etc/os-release")
    if os_release.is_file():
        report["host"]["os_release"] = os_release.read_text(encoding="utf-8")
    for name in ("arpspoof", "tcpdump", "tshark", "wireshark", "ip", "startxfce4"):
        path = shutil.which(name)
        record("命令存在：" + name, path is not None, {"path": path})
    packages = ["dsniff", "tcpdump", "tshark", "wireshark", "python3-scapy", "python3-dpkt", "xfce4"]
    dpkg = shutil.which("dpkg-query")
    if dpkg:
        for package in packages:
            result = command([dpkg, "-W", "-f=${Status}\t${Version}\n", package])
            record("APT包版本：" + package,
                   result.returncode == 0 and result.stdout.startswith("install ok installed\t"),
                   {"returncode": result.returncode, "value": result.stdout.strip(),
                    "error": result.stderr.strip()})
    else:
        record("APT包版本数据库", False, "缺少 dpkg-query")
    for name in ("arpCheat.py", "analyze_capture.py"):
        record("本包工具：" + name, (tool_dir / name).is_file(), str(tool_dir / name))
    for name, action in (("Scapy纯内存构包", verify_scapy),
                         ("ARP程序默认预览", lambda: verify_dry_run(tool_dir / "arpCheat.py")),
                         ("dpkt与合成pcap离线分析", lambda: verify_offline_analyzer(tool_dir / "analyze_capture.py"))):
        try:
            detail = action()
            record(name, True, detail)
        except Exception as exc:
            record(name, False, {"error": str(exc), "traceback": traceback.format_exc()})
    if check_sudo:
        sudo = shutil.which("sudo")
        try:
            result = command([sudo, "-n", "true"], timeout=5) if sudo else None
            allowed = result is not None and result.returncode == 0
            report["sudo_noninteractive"] = {
                "status": "available" if allowed else "unavailable",
                "detail": "仅 sudo -n true；没有抓包。" if allowed else
                          "当前无免交互sudo；后续loopback捕获验证需在用户终端输入sudo密码。没有尝试密码。",
            }
        except Exception as exc:
            report["sudo_noninteractive"] = {"status": "unavailable", "detail": str(exc)}
    else:
        report["sudo_noninteractive"] = {"status": "not_checked", "detail": "默认不运行sudo。"}
except Exception as exc:
    record("验收流程", False, {"error": str(exc), "traceback": traceback.format_exc()})
finally:
    success = all(item["status"] == "pass" for item in checks if item["required"])
    report["local_environment_passed"] = success
    report["finished_at"] = dt.datetime.now().astimezone().isoformat()
    report["result_note"] = "本机离线验收通过；真实抓包、桥接、小车与ARP实验未执行。" if success else "本机离线验收未通过；请查看失败项。真实抓包、桥接、小车与ARP实验未执行。"
    json_file = run_dir / "环境验收.json"
    json_file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["实验五攻击机本机环境验收", "时间：" + started.isoformat(), report["scope"], ""]
    for item in checks:
        lines.append(("通过" if item["status"] == "pass" else "失败") + "：" + item["name"])
        lines.append(json.dumps(item["detail"], ensure_ascii=False))
    lines.extend(["", report["result_note"], "sudo：" + json.dumps(report["sudo_noninteractive"], ensure_ascii=False)
                  if "sudo_noninteractive" in report else "sudo未检查。",
                  "真实捕获验收：未执行。现场链路验收：未执行。"])
    (run_dir / "环境验收.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(report["result_note"])
    print("记录目录：" + str(run_dir))
sys.exit(0 if report["local_environment_passed"] else 1)
PY
