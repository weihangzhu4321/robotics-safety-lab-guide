#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实验五隔离前置验收：默认只打印计划；--execute 才创建私有测试网络。

只在本轮新建的 namespace/veth/bridge 内发送人工 UDP 和 ARP，不使用 ROS、
小车或现场控制数据。要求专用 Linux VM 的所有既有非 lo 网卡 carrier=0，
并且主 namespace 没有 IPv4/IPv6 默认路由。标准库负责编排和 pcap 核对；
Scapy 仅由同目录的 arpCheat.py 使用。必须在断开 VMware 虚拟网卡后运行。
"""

import argparse
import collections
import datetime as dt
import json
import os
from pathlib import Path
import secrets
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time


ROLES = ("A", "B", "C")
IPS = {"A": "10.251.245.10", "B": "10.251.245.20", "C": "10.251.245.30"}
MACS = {"A": "02:00:00:05:00:10", "B": "02:00:00:05:00:20", "C": "02:00:00:05:00:30"}
PORTS = {"A": 45210, "B": 45220}
IFACE = "lab0"
MARKER = "LAB5_ISOLATED_SYNTHETIC_UDP_NON_FIELD"
REDIRECT_KEYS = (
    "net.ipv4.conf.all.send_redirects",
    "net.ipv4.conf.default.send_redirects",
    "net.ipv4.conf.lab0.send_redirects",
)


class PreflightError(Exception):
    pass


class PreflightInterrupted(PreflightError):
    pass


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def write_json_atomic(path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def udp_worker(args):
    """只由父进程在新建 A/B namespace 内调用；不用原始套接字。"""
    if not args.execute or args.worker not in ("A", "B") or not args.worker_token:
        raise PreflightError("内部 UDP worker 参数不完整")
    # 父进程已创建随机命名 netns；进一步要求自身仅有 lo 和固定 lab0。
    interfaces = {name for _, name in socket.if_nameindex()}
    if interfaces != {"lo", IFACE}:
        raise PreflightError("worker 只允许 lo/lab0 两个接口：%r" % interfaces)
    role = args.worker
    peer = "B" if role == "A" else "A"
    directory = Path(args.out)
    identity = json.loads((directory / "isolation_identity.json").read_text(encoding="utf-8"))
    actual_inode = os.stat("/proc/self/ns/net").st_ino
    if identity.get("marker") != MARKER or identity.get("token") != args.worker_token or identity["namespace_inodes"].get(role) != actual_inode:
        raise PreflightError("worker 不是父进程新建的对应隔离 namespace")
    if actual_inode == os.stat("/proc/1/ns/net").st_ino:
        raise PreflightError("worker 不允许在主 namespace 运行")
    phase_file = directory / "phase.json"
    active = True

    def stop(signum, frame):
        nonlocal active
        active = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, 64)
    sock.bind((IPS[role], PORTS[role]))
    sock.setblocking(False)
    sequence = 0
    next_send = time.monotonic()
    with (directory / ("udp_%s.jsonl" % role)).open("x", encoding="utf-8") as stream:
        def log(row):
            stream.write(json.dumps({"time_utc": utc_now(), **row}, ensure_ascii=False) + "\n")
            stream.flush()
        log({"event": "worker_started", "role": role, "ip": IPS[role], "marker": MARKER})
        try:
            while active:
                now = time.monotonic()
                if now >= next_send:
                    phase = json.loads(phase_file.read_text(encoding="utf-8"))["phase"]
                    message = {"marker": MARKER, "token": args.worker_token, "phase": phase,
                               "direction": role + "_to_" + peer, "sequence": sequence,
                               "sent_epoch_ns": time.time_ns()}
                    raw = json.dumps(message, separators=(",", ":")).encode("ascii")
                    try:
                        size = sock.sendto(raw, (IPS[peer], PORTS[peer]))
                        log({"event": "sent", **message, "bytes": size})
                    except OSError as exc:
                        # 转发关闭时可能收到 ICMP 错误；保留它而非退出持续观测。
                        log({"event": "send_error", **message, "error": str(exc)})
                    sequence += 1
                    next_send = time.monotonic() + 0.125  # 每个方向最多 8 包/秒。
                ready, _, _ = select.select([sock], [], [], min(0.05, max(0, next_send - time.monotonic())))
                if ready:
                    try:
                        raw, sender = sock.recvfrom(4096)
                        message = json.loads(raw.decode("ascii"))
                        if sender == (IPS[peer], PORTS[peer]) and message.get("marker") == MARKER and message.get("token") == args.worker_token:
                            log({"event": "received", **message, "received_epoch_ns": time.time_ns(), "peer": list(sender)})
                        else:
                            log({"event": "unexpected_datagram", "peer": list(sender)})
                    except (OSError, ValueError, UnicodeError) as exc:
                        log({"event": "receive_error", "error": str(exc)})
        finally:
            log({"event": "worker_stopped", "sequences_attempted": sequence})
            sock.close()
    return 0


def read_pcap(path, token):
    """只解码本轮 tcpdump 的 Ethernet/IPv4/UDP 与 ARP，不解释 ROS。"""
    packets, arp = [], collections.Counter()
    formats = {b"\xd4\xc3\xb2\xa1": ("<", 1e6), b"\xa1\xb2\xc3\xd4": (">", 1e6),
               b"\x4d\x3c\xb2\xa1": ("<", 1e9), b"\xa1\xb2\x3c\x4d": (">", 1e9)}
    total = 0
    with path.open("rb") as stream:
        header = stream.read(24)
        if len(header) != 24 or header[:4] not in formats:
            raise PreflightError("非完整 classic pcap：" + str(path))
        endian, divisor = formats[header[:4]]
        if struct.unpack_from(endian + "I", header, 20)[0] & 0xffff != 1:
            raise PreflightError("隔离抓包必须保留 EN10MB Ethernet 头：" + str(path))
        while True:
            record = stream.read(16)
            if not record:
                break
            if len(record) != 16:
                raise PreflightError("pcap 记录头截断：" + str(path))
            sec, subsec, caplen, original = struct.unpack(endian + "IIII", record)
            if caplen > 65536 or caplen != original:
                raise PreflightError("pcap 捕获长度异常：" + str(path))
            raw = stream.read(caplen)
            if len(raw) != caplen or len(raw) < 14:
                raise PreflightError("pcap 帧截断：" + str(path))
            total += 1
            dstmac = ":".join("%02x" % b for b in raw[:6])
            srcmac = ":".join("%02x" % b for b in raw[6:12])
            ethertype = struct.unpack_from("!H", raw, 12)[0]
            if ethertype == 0x0806 and len(raw) >= 42:
                op = struct.unpack_from("!H", raw, 20)[0]
                sender_mac = ":".join("%02x" % b for b in raw[22:28])
                sender_ip = socket.inet_ntoa(raw[28:32])
                target_ip = socket.inet_ntoa(raw[38:42])
                arp["op=%d %s=%s to=%s ether=%s>%s" % (op, sender_ip, sender_mac, target_ip, srcmac, dstmac)] += 1
                continue
            if ethertype != 0x0800 or len(raw) < 34:
                continue
            ip = raw[14:]
            ihl = (ip[0] & 15) * 4
            length = struct.unpack_from("!H", ip, 2)[0]
            if ip[0] >> 4 != 4 or ihl < 20 or length < ihl + 8 or len(ip) < length or ip[9] != 17:
                continue
            if struct.unpack_from("!H", ip, 6)[0] & 0x3fff:
                continue
            udp = ip[ihl:length]
            sport, dport, ulen = struct.unpack_from("!HHH", udp)
            if ulen < 8 or ulen > len(udp):
                continue
            try:
                message = json.loads(udp[8:ulen].decode("ascii"))
            except (ValueError, UnicodeError):
                continue
            if message.get("marker") != MARKER or message.get("token") != token:
                continue
            src, dst = socket.inet_ntoa(ip[12:16]), socket.inet_ntoa(ip[16:20])
            direction = message.get("direction", "")
            if direction not in ("A_to_B", "B_to_A"):
                continue
            source_role, target_role = direction.split("_to_")
            if (src, dst, sport, dport) != (IPS[source_role], IPS[target_role], PORTS[source_role], PORTS[target_role]):
                raise PreflightError("带本轮 token 的 UDP 地址/方向不符")
            packets.append({"phase": message["phase"], "direction": direction, "sequence": message["sequence"],
                            "eth_src": srcmac, "eth_dst": dstmac, "ttl": ip[8],
                            "ip_id": struct.unpack_from("!H", ip, 4)[0], "epoch": sec + subsec / divisor})
    return packets, {"captured_frames": total, "marked_udp_frames": len(packets), "arp_mapping_counts": dict(arp)}


class Preflight:
    def __init__(self, args):
        self.args = args
        self.prefix = "l5" + secrets.token_hex(3)
        self.token = secrets.token_hex(16)
        self.namespaces = {r: self.prefix + "_" + r for r in ROLES}
        self.bridge = self.prefix + "b"
        self.ports = {r: self.prefix + "p" + r.lower() for r in ROLES}
        self.peers = {r: self.prefix + "q" + r.lower() for r in ROLES}
        default = Path.home() / "lab5_environment" / ("ISOLATED_NON_FIELD_" + dt.datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + self.prefix)
        self.out = Path(args.out).expanduser().resolve() if args.out else default
        self.arp_script = Path(__file__).resolve().with_name("arpCheat.py")
        self.children = []
        self.created_namespaces = []
        self.created_links = []
        self.root_before = None
        self.c_sysctl_before = None
        self.external = {}
        self.events = None
        self.completed_phases = []
        self.report = {"kind": "lab5_isolated_network_preflight", "started_at_utc": utc_now(),
                       "scope": "私有 namespace/veth 内实际发送人工 UDP 与 ARP；非 ROS、非小车、非现场实验。",
                       "marker": MARKER, "prefix": self.prefix, "output_directory": str(self.out),
                       "namespaces": self.namespaces, "bridge": self.bridge,
                       "addresses": IPS, "mac_addresses": MACS, "checks": [], "neighbor_observations": [],
                       "phases": [], "children": [], "errors": [], "status": "not_started"}

    def preview(self):
        return {"mode": "preview_no_changes", "requires": ["Linux root", "主 namespace 既有非 lo 接口全部 carrier=0",
                "主 namespace 无 IPv4/IPv6 默认路由", "ip/sysctl/ping/tcpdump/arpspoof 与同目录 arpCheat.py"],
                "network": {"private_bridge_no_ipv4_or_ipv6": self.bridge, "endpoint_namespaces": self.namespaces,
                            "endpoint_ipv4": IPS, "endpoint_macs": MACS, "default_routes_in_endpoints": False},
                "planned_checks": ["双向人工 UDP 基线", "arpspoof 双向映射及 TTL/MAC 转发配对",
                    "仅 C 内 ip_forward 开/关/再开", "不清邻居表，先观察 arpspoof 自动恢复",
                    "Scapy duration=3/interval=1 及自动恢复", "C 转发关闭后的两端直通", "进程与自建网络清理/主 namespace 未变"],
                "actual_operations_performed": False, "output_directory_if_executed": str(self.out),
                "expected_seconds": "约 35–55 秒；失败与清理最坏约 90 秒", "scope": self.report["scope"]}

    def note(self, event, **fields):
        row = {"time_utc": utc_now(), "monotonic": time.monotonic(), "event": event, **fields}
        if self.events:
            self.events.write(json.dumps(row, ensure_ascii=False) + "\n")
            self.events.flush()
        print(json.dumps(row, ensure_ascii=False), flush=True)

    def check(self, name, passed, detail, stop=True):
        row = {"name": name, "passed": bool(passed), "detail": detail}
        self.report["checks"].append(row)
        self.note("check", **row)
        if not passed and stop:
            raise PreflightError(name + ": " + str(detail))
        return bool(passed)

    def command(self, arguments, check=True, timeout=8):
        result = subprocess.run([str(a) for a in arguments], text=True, capture_output=True, timeout=timeout, check=False)
        self.note("command", argv=[str(a) for a in arguments], returncode=result.returncode,
                  stdout=result.stdout, stderr=result.stderr)
        if check and result.returncode:
            raise PreflightError("命令失败(%s)：%r；%s" % (result.returncode, arguments, result.stderr.strip()))
        return result

    def ip_json(self, arguments, role=None):
        argv = ["ip"] + (["-n", self.namespaces[role]] if role else []) + ["-j"] + arguments
        return json.loads(self.command(argv).stdout or "[]")

    def in_ns(self, role, arguments, **kwargs):
        return self.command(["ip", "netns", "exec", self.namespaces[role], *arguments], **kwargs)

    def snapshot_root(self):
        addresses = self.ip_json(["address", "show"])
        normalized = sorted((x["ifname"], tuple(sorted((a["family"], a.get("local"), a.get("prefixlen"), a.get("scope"))
                            for a in x.get("addr_info", [])))) for x in addresses)
        return {"interfaces_and_addresses": normalized,
                "routes_v4": self.ip_json(["-4", "route", "show", "table", "all"]),
                "routes_v6": self.ip_json(["-6", "route", "show", "table", "all"]),
                "ip_forward": Path("/proc/sys/net/ipv4/ip_forward").read_text().strip()}

    def guard(self, initial=False):
        if initial:
            if sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid() != 0:
                raise PreflightError("--execute 只允许 Linux root；请先断开 VMware 网卡再使用 sudo")
            for tool in ("ip", "sysctl", "ping", "tcpdump", "arpspoof"):
                if not shutil.which(tool):
                    raise PreflightError("缺少预装命令：" + tool)
            if not self.arp_script.is_file():
                raise PreflightError("缺少同目录 arpCheat.py")
            self.external = {p.name: {} for p in Path("/sys/class/net").iterdir() if p.name != "lo"}
            if not self.external:
                raise PreflightError("未发现需断开的 VM 网卡，拒绝推断网络隔离")
            self.root_before = self.snapshot_root()
            for route in self.root_before["routes_v4"] + self.root_before["routes_v6"]:
                if route.get("dst") in ("default", "0.0.0.0/0", "::/0"):
                    raise PreflightError("主 namespace 尚有默认路由；请在 VMware 中断开网卡并等待网络管理器更新")
            existing_ns = {line.split()[0] for line in self.command(["ip", "netns", "list"]).stdout.splitlines() if line.strip()}
            existing_links = {x["ifname"] for x in self.ip_json(["link", "show"])}
            wanted = set(self.namespaces.values()) | {self.bridge} | set(self.ports.values()) | set(self.peers.values())
            if wanted & (existing_ns | existing_links):
                raise PreflightError("随机命名发生冲突；本程序不复用或清理既有对象，请另开新轮次")
        known = {"lo", *self.external.keys(), self.bridge, *self.ports.values(), *self.peers.values()}
        current = {p.name for p in Path("/sys/class/net").iterdir()}
        if current - known:
            raise PreflightError("发现未归本轮拥有的新接口：%r" % sorted(current - known))
        for name in self.external:
            base = Path("/sys/class/net") / name
            try:
                carrier = (base / "carrier").read_text().strip()
                operstate = (base / "operstate").read_text().strip()
            except OSError as exc:
                raise PreflightError("无法确认既有接口断链：%s：%s" % (name, exc)) from exc
            if carrier != "0":
                raise PreflightError("既有接口未断链，立即停止隔离验收：%s carrier=%s" % (name, carrier))
            self.external[name] = {"carrier": carrier, "operstate": operstate}
        if initial:
            self.report["external_interfaces_before"] = dict(self.external)
            self.report["root_before"] = self.root_before
            self.check("启动前物理网卡断链且无默认路由", True, dict(self.external))

    def phase(self, name):
        self.guard()
        write_json_atomic(self.out / "phase.json", {"phase": name, "time_utc": utc_now()})
        self.report["phases"].append({"name": name, "time_utc": utc_now(), "monotonic": time.monotonic()})
        self.note("phase", name=name)

    def wait(self, seconds, monitor=True):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            self.guard()
            if monitor:
                for child in self.children:
                    if child["kind"] in ("capture", "udp") and child["process"].poll() is not None:
                        raise PreflightError("持续观测进程提前退出：" + child["name"])
            time.sleep(min(0.1, max(0, until - time.monotonic())))

    def observe(self, name, seconds=1.5):
        self.phase(name)
        self.wait(seconds)
        self.completed_phases.append(name)

    def setup(self):
        for role in ROLES:
            # 名称已在 guard 中证明不存在；先登记意图，覆盖命令返回与登记间的信号窗口。
            self.created_namespaces.append(self.namespaces[role])
            self.command(["ip", "netns", "add", self.namespaces[role]])
        self.created_links.append(self.bridge)
        self.command(["ip", "link", "add", self.bridge, "type", "bridge"])
        self.command(["ip", "link", "set", "dev", self.bridge, "addrgenmode", "none"])
        for role in ROLES:
            port, peer = self.ports[role], self.peers[role]
            self.created_links.append(port)
            self.command(["ip", "link", "add", port, "type", "veth", "peer", "name", peer])
            self.command(["ip", "link", "set", "dev", port, "addrgenmode", "none"])
            self.command(["ip", "link", "set", "dev", port, "master", self.bridge])
            self.command(["ip", "link", "set", "dev", peer, "netns", self.namespaces[role]])
            self.command(["ip", "-n", self.namespaces[role], "link", "set", peer, "name", IFACE])
            self.command(["ip", "-n", self.namespaces[role], "link", "set", "dev", IFACE, "addrgenmode", "none"])
            self.command(["ip", "-n", self.namespaces[role], "link", "set", "dev", IFACE, "address", MACS[role]])
            self.command(["ip", "-n", self.namespaces[role], "address", "add", IPS[role] + "/24", "dev", IFACE])
            self.command(["ip", "-n", self.namespaces[role], "link", "set", "dev", "lo", "up"])
            self.command(["ip", "-n", self.namespaces[role], "link", "set", "dev", IFACE, "up"])
            self.command(["ip", "link", "set", "dev", port, "up"])
        self.command(["ip", "link", "set", "dev", self.bridge, "up"])
        bridge_info = self.ip_json(["address", "show", "dev", self.bridge])
        ports = self.ip_json(["link", "show", "master", self.bridge])
        self.check("私有 bridge 无 IPv4/IPv6 且端口仅本轮 veth", bool(bridge_info) and not bridge_info[0].get("addr_info") and
                   {p["ifname"] for p in ports} == set(self.ports.values()), {"bridge": bridge_info, "ports": ports})
        for role in ROLES:
            links = self.ip_json(["link", "show"], role)
            routes = self.ip_json(["-4", "route", "show", "table", "all"], role)
            self.check("%s 只有独立 lo/lab0 且无默认路由" % role, {p["ifname"] for p in links} == {"lo", IFACE} and
                       not any(p.get("dst") == "default" for p in routes), {"links": links, "routes": routes})
        write_json_atomic(self.out / "isolation_identity.json", {
            "marker": MARKER, "token": self.token, "namespaces": self.namespaces,
            "namespace_inodes": {role: os.stat("/var/run/netns/" + self.namespaces[role]).st_ino for role in ROLES},
        })
        self.c_sysctl_before = {key: self.in_ns("C", ["sysctl", "-n", key]).stdout.strip()
                               for key in ("net.ipv4.ip_forward", *REDIRECT_KEYS)}
        self.report["C_sysctl_before"] = self.c_sysctl_before
        self.forward(0)
        for role in ("A", "B"):
            self.in_ns("C", ["ping", "-n", "-c", "1", "-W", "1", IPS[role]])

    def forward(self, value):
        # 所有 sysctl 写入均在本轮 C namespace，主 namespace 的值不写入。
        self.guard()
        self.in_ns("C", ["sysctl", "-w", "net.ipv4.ip_forward=" + str(value)])
        self.in_ns("C", ["sysctl", "-w", *[key + "=0" for key in REDIRECT_KEYS]])
        actual = self.in_ns("C", ["sysctl", "-n", "net.ipv4.ip_forward"]).stdout.strip()
        self.check("C 内转发值=%s" % value, actual == str(value), {"actual": actual})

    def start_child(self, name, kind, role, argv):
        stdout = (self.out / (name + ".stdout.txt")).open("xb")
        stderr = (self.out / (name + ".stderr.txt")).open("xb")
        arguments = ["ip", "netns", "exec", self.namespaces[role], *[str(x) for x in argv]]
        pending = []
        handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        # 短暂延后父进程取消，保证 Popen 一旦返回就先登记 PID；不屏蔽子进程信号。
        for sig in handlers:
            signal.signal(sig, lambda signum, frame: pending.append(signum))
        try:
            process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                       start_new_session=True)
            child = {"name": name, "kind": kind, "role": role, "process": process,
                     "stdout_handle": stdout, "stderr_handle": stderr, "argv": arguments}
            self.children.append(child)
        except BaseException:
            stdout.close()
            stderr.close()
            raise
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
            if pending:
                handler = handlers[pending[-1]]
                if callable(handler):
                    handler(pending[-1], None)
        self.note("child_started", name=name, kind=kind, role=role, pid=process.pid, argv=arguments)
        return child

    def start_observers(self):
        for role in ROLES:
            name = "capture_" + role
            child = self.start_child(name, "capture", role,
                ["tcpdump", "-i", IFACE, "-y", "EN10MB", "-nn", "-s", "0", "-U", "-w", self.out / (name + ".pcap"),
                 "arp or (ip and udp and host %s and host %s)" % (IPS["A"], IPS["B"])])
            until = time.monotonic() + 4
            ready = False
            while time.monotonic() < until:
                if child["process"].poll() is not None:
                    break
                if "listening on" in (self.out / (name + ".stderr.txt")).read_text(encoding="utf-8", errors="replace"):
                    ready = True
                    break
                self.wait(0.1)
            self.check("%s 抓包已就绪" % role, ready, {"stderr": name + ".stderr.txt"})
        self.phase("warmup")
        for role in ("A", "B"):
            self.start_child("udp_" + role, "udp", role,
                [sys.executable, Path(__file__).resolve(), "--execute", "--worker", role,
                 "--worker-token", self.token, "--out", self.out])
        self.wait(1)

    def neighbors(self, label):
        result = {role: self.ip_json(["neigh", "show", "dev", IFACE], role) for role in ROLES}
        self.report["neighbor_observations"].append({"label": label, "time_utc": utc_now(), "neighbors": result})
        self.note("neighbor_observation", label=label, neighbors=result)
        return result

    @staticmethod
    def mapping_matches(observation, poisoned):
        for role, peer in (("A", "B"), ("B", "A")):
            expected = MACS["C"] if poisoned else MACS[peer]
            entries = [p for p in observation[role] if p.get("dst") == IPS[peer]]
            if len(entries) != 1 or entries[0].get("lladdr", "").lower() != expected:
                return False
        return True

    def wait_poisoned(self, label, limit=3):
        deadline = time.monotonic() + limit
        while True:
            result = self.neighbors(label)
            if self.mapping_matches(result, True):
                self.check(label + " 双端映射=C", True, result)
                return
            if time.monotonic() >= deadline:
                self.check(label + " 双端映射=C", False, result)
            self.wait(0.15)

    def stop_children(self, selected, first_signal, grace):
        selected = [p for p in selected if p["process"].poll() is None]
        for child in selected:
            self.note("child_stopping", name=child["name"], pid=child["process"].pid,
                      signal=first_signal, grace_seconds=grace)
            try:
                os.killpg(child["process"].pid, first_signal)
            except ProcessLookupError:
                pass
        until = time.monotonic() + grace
        while any(c["process"].poll() is None for c in selected) and time.monotonic() < until:
            time.sleep(0.05)
        for child in selected:
            process = child["process"]
            if process.poll() is None:
                self.report["errors"].append("清理等待超时，精确强制结束本轮进程组：" + child["name"])
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=3)
            self.note("child_stopped", name=child["name"], returncode=process.returncode)

    def check_recovered(self, label):
        # 在此之前绝不 ip neigh del/flush；保留工具自动恢复是否被接受的事实。
        observed = self.neighbors(label + "_immediate_without_flush")
        self.check(label + " 自动恢复双端真实映射（未清缓存）", self.mapping_matches(observed, False), observed)
        self.phase("transition")
        self.wait(0.25)
        self.forward(0)
        self.observe(label + "_direct_restored", 1.5)

    def dsniff_test(self):
        self.phase("transition")
        self.forward(1)
        pair = [self.start_child("arpspoof_to_" + role, "attack", "C",
                ["arpspoof", "-i", IFACE, "-t", IPS[role], IPS[peer]])
                for role, peer in (("A", "B"), ("B", "A"))]
        self.wait_poisoned("arpspoof", 3)
        self.observe("arpspoof_forward_on", 1.5)
        self.phase("transition")
        self.wait(0.25)
        self.forward(0)
        self.observe("arpspoof_forward_off", 1.5)
        off_neighbors = self.neighbors("arpspoof_forward_off")
        self.check("转发关闭阶段双端仍映射 C", self.mapping_matches(off_neighbors, True), off_neighbors)
        self.phase("transition")
        self.wait(0.25)  # 清除阶段切换边界，避免旧 phase 包混入重新开启窗口。
        self.forward(1)
        self.observe("arpspoof_forward_on_again", 1.5)
        self.phase("arpspoof_stopping_auto_restore")
        # 两个进程同时发送 INT，再共同等待，>=10 秒恢复裕量。
        self.stop_children(pair, signal.SIGINT, 12)
        self.check("arpspoof 两个进程均正常退出", all(p["process"].returncode == 0 for p in pair),
                   {p["name"]: p["process"].returncode for p in pair})
        self.check_recovered("arpspoof")

    def scapy_test(self):
        self.phase("transition")
        self.forward(1)
        script_log = self.out / "scapy_execute.jsonl"
        child = self.start_child("scapy_short", "attack", "C",
            [sys.executable, self.arp_script, "--iface", IFACE,
             "--controller", IPS["A"], "--controller-mac", MACS["A"],
             "--robot", IPS["B"], "--robot-mac", MACS["B"], "--attacker-mac", MACS["C"],
             "--duration", "3", "--interval", "1", "--execute", "--log", script_log])
        self.wait_poisoned("scapy", 4)
        self.observe("scapy_forward_on", 1.0)
        self.phase("scapy_waiting_auto_restore")
        deadline = time.monotonic() + 10
        while child["process"].poll() is None and time.monotonic() < deadline:
            self.wait(0.1)
        self.check("Scapy 短时运行正常退出", child["process"].poll() == 0,
                   {"returncode": child["process"].poll(), "log": script_log.name})
        entries = [json.loads(line) for line in script_log.read_text(encoding="utf-8").splitlines() if line]
        events = [row.get("event") for row in entries]
        self.check("Scapy 记录发送与恢复调用", "poison_pair_sent" in events and "restore_frames_sent_verify_both_endpoints" in events and
                   not any(e in events for e in ("error", "poison_error", "restore_error")), {"events": events})
        self.check_recovered("scapy")

    def cleanup(self):
        self.note("cleanup_started")
        def attempt(label, action):
            try:
                return action()
            except BaseException as exc:
                text = "%s：%s：%s" % (label, type(exc).__name__, exc)
                self.report["errors"].append(text)
                self.note("cleanup_action_failed", error=text)
                return None
        for kind, sig, seconds in (("attack", signal.SIGINT, 12), ("udp", signal.SIGTERM, 3), ("capture", signal.SIGINT, 4)):
            attempt("停止本轮 " + kind, lambda kind=kind, sig=sig, seconds=seconds:
                    self.stop_children([p for p in self.children if p["kind"] == kind], sig, seconds))
        for child in self.children:
            attempt("关闭 stdout " + child["name"], child["stdout_handle"].close)
            attempt("关闭 stderr " + child["name"], child["stderr_handle"].close)
            self.report["children"].append({"name": child["name"], "kind": child["kind"], "pid": child["process"].pid,
                                            "returncode": child["process"].poll(), "argv": child["argv"]})
        if self.c_sysctl_before:
            # 原值仅属于新建 C；即使将删 namespace，仍记录开关恢复过程。
            attempt("恢复 C 转发值", lambda: self.in_ns("C", ["sysctl", "-w", "net.ipv4.ip_forward=" + self.c_sysctl_before["net.ipv4.ip_forward"]]))
            attempt("恢复 C redirect", lambda: self.in_ns("C", ["sysctl", "-w", *[key + "=" + self.c_sysctl_before[key] for key in REDIRECT_KEYS]]))
        existing_namespaces_result = attempt("读取本轮 namespace 是否存在", lambda: self.command(["ip", "netns", "list"])) if self.created_namespaces else None
        existing_namespaces = ({line.split()[0] for line in existing_namespaces_result.stdout.splitlines() if line.strip()}
                               if existing_namespaces_result is not None else None)
        for namespace in self.created_namespaces:
            if existing_namespaces is not None and namespace not in existing_namespaces:
                continue
            result = attempt("核查 namespace 进程 " + namespace, lambda namespace=namespace:
                             self.command(["ip", "netns", "pids", namespace], check=False))
            self.check("自建 namespace 无遗留进程：" + namespace, result is not None and result.returncode == 0 and not result.stdout.split(),
                       {"pids": result.stdout.split() if result is not None else None}, stop=False)
        # 仅查/删本轮已登记且启动前不存在的精确名称，覆盖创建命令被打断的情况。
        existing_links_result = attempt("读取本轮接口是否存在", lambda: self.ip_json(["link", "show"])) if self.created_links else None
        existing_links = {item["ifname"] for item in existing_links_result} if existing_links_result is not None else None
        for link in reversed(self.created_links):
            if existing_links is not None and link not in existing_links:
                continue
            result = attempt("删除本轮接口 " + link, lambda link=link:
                             self.command(["ip", "link", "del", "dev", link], check=False))
            if result is None or result.returncode:
                self.report["errors"].append("删除本轮接口失败：" + link)
        for namespace in reversed(self.created_namespaces):
            if existing_namespaces is not None and namespace not in existing_namespaces:
                continue
            result = attempt("删除本轮 namespace " + namespace, lambda namespace=namespace:
                             self.command(["ip", "netns", "del", namespace], check=False))
            if result is None or result.returncode:
                self.report["errors"].append("删除本轮 namespace 失败：" + namespace)
        if self.root_before:
            after = attempt("主 namespace 收尾快照", self.snapshot_root)
            self.report["root_after"] = after
            self.check("主 namespace 原有地址/路由/转发值均未改变", after == self.root_before,
                       {"compare": "interfaces_and_addresses/routes_v4/routes_v6/ip_forward"}, stop=False)
            result = attempt("核查 namespace 清理", lambda: self.command(["ip", "netns", "list"]))
            links_after = attempt("核查接口清理", lambda: self.ip_json(["link", "show"]))
            if result is not None and links_after is not None:
                namespaces = {line.split()[0] for line in result.stdout.splitlines() if line.strip()}
                links = {x["ifname"] for x in links_after}
                remain = (set(self.created_namespaces) & namespaces) | (set(self.created_links) & links)
                self.check("本轮网络对象全部清理", not remain, sorted(remain), stop=False)
            else:
                self.check("本轮网络对象全部清理", False, "未能取得完整核查结果", stop=False)
            try:
                self.guard()
                self.check("收尾时物理网卡仍断链", True, dict(self.external), stop=False)
            except PreflightError as exc:
                self.check("收尾时物理网卡仍断链", False, str(exc), stop=False)
        self.note("cleanup_finished")

    def verify_traffic(self):
        captures = {}
        for role in ROLES:
            path = self.out / ("capture_" + role + ".pcap")
            if path.is_file():
                captures[role], info = read_pcap(path, self.token)
                self.report.setdefault("capture_summary", {})[role] = info
                stderr = (self.out / ("capture_" + role + ".stderr.txt")).read_text(encoding="utf-8", errors="replace")
                self.report["capture_summary"][role]["tcpdump_stderr"] = stderr
        if self.completed_phases:
            self.check("三端隔离抓包均存在并可解析", len(captures) == 3, sorted(captures), stop=False)
        if len(captures) != 3:
            return
        receipts = set()
        for role in ("A", "B"):
            path = self.out / ("udp_" + role + ".jsonl")
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    row = json.loads(line)
                    if row.get("event") == "received" and row.get("token") == self.token:
                        receipts.add((row["phase"], row["direction"], row["sequence"]))
        observations = []
        for phase in self.completed_phases:
            for direction in ("A_to_B", "B_to_A"):
                source, target = direction.split("_to_")
                selected = [p for p in captures["C"] if p["phase"] == phase and p["direction"] == direction]
                incoming = {(p["sequence"], p["ip_id"]) for p in selected
                            if p["eth_src"] == MACS[source] and p["eth_dst"] == MACS["C"] and p["ttl"] == 64}
                outgoing = {(p["sequence"], p["ip_id"]) for p in selected
                            if p["eth_src"] == MACS["C"] and p["eth_dst"] == MACS[target] and p["ttl"] == 63}
                matched = {seq for seq, ident in incoming & outgoing if (phase, direction, seq) in receipts}
                received = {seq for p, d, seq in receipts if p == phase and d == direction}
                direct = {p["sequence"] for p in captures[source] if p["phase"] == phase and p["direction"] == direction
                          and p["eth_src"] == MACS[source] and p["eth_dst"] == MACS[target] and p["ttl"] == 64}
                row = {"phase": phase, "direction": direction, "C_ingress_sequences": len(incoming),
                       "C_egress_sequences": len(outgoing), "TTL_MAC_pairs_and_peer_receipt": len(matched),
                       "peer_received_sequences": len(received), "direct_tx_and_peer_receipt": len(direct & received)}
                observations.append(row)
                if phase == "arpspoof_forward_off":
                    passed = len(incoming) >= 3 and not outgoing and not received
                elif phase.endswith("direct_restored") or phase == "baseline":
                    passed = len(direct & received) >= 3 and not outgoing
                else:
                    passed = len(matched) >= 3
                self.check("数据面：%s %s" % (phase, direction), passed, row, stop=False)
        self.report["udp_phase_observations"] = observations

    def execute(self):
        # 独占新建目录；不覆盖以往的证据和报告。
        self.out.mkdir(parents=True, exist_ok=False)
        self.events = (self.out / "events.jsonl").open("x", encoding="utf-8")
        self.report["status"] = "running"
        old_handlers = {}
        interrupted = False

        def stop(signum, frame):
            raise PreflightInterrupted("收到信号 %s；进入本轮精确清理" % signum)

        for sig in (signal.SIGINT, signal.SIGTERM):
            old_handlers[sig] = signal.signal(sig, stop)
        try:
            self.guard(initial=True)
            self.setup()
            self.start_observers()
            self.observe("baseline", 1.5)
            baseline = self.neighbors("baseline")
            self.check("基线双端真实映射", self.mapping_matches(baseline, False), baseline)
            self.dsniff_test()
            self.scapy_test()
            self.phase("finished")
        except BaseException as exc:
            interrupted = isinstance(exc, (KeyboardInterrupt, PreflightInterrupted))
            self.report["errors"].append(type(exc).__name__ + ": " + str(exc))
            self.note("execution_failed", error=self.report["errors"][-1])
        finally:
            # 清理期间忽略后续 INT/TERM，防止第二次按键打断删除。
            for sig in old_handlers:
                signal.signal(sig, signal.SIG_IGN)
            try:
                self.cleanup()
            except BaseException as exc:
                self.report["errors"].append("清理异常：%s：%s" % (type(exc).__name__, exc))
                self.note("cleanup_failed", error=self.report["errors"][-1])
            try:
                self.verify_traffic()
            except BaseException as exc:
                self.report["errors"].append("证据解析异常：%s：%s" % (type(exc).__name__, exc))
                self.note("evidence_verification_failed", error=self.report["errors"][-1])
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)
            passed = not self.report["errors"] and bool(self.report["checks"]) and all(p["passed"] for p in self.report["checks"])
            self.report["status"] = "passed" if passed else "failed"
            self.report["finished_at_utc"] = utc_now()
            self.report["completed_observation_phases"] = self.completed_phases
            self.report["manual_neighbor_flush_performed"] = False
            self.report["limitations"] = [
                "所有 UDP 为本程序构造，实际报文只在新建私有 network namespace/veth 中流动。",
                "不证明 VMware WLAN 桥接、AP 隔离策略、小车邻居表、ROS/TCPROS 或实体控制效果。",
                "未执行 TCP 流重组、ROS 载荷修改或抓取外部接口。",
                "自动恢复结果先于任何人工缓存操作记录；本程序从不删除/清空邻居缓存。",
                "SIGKILL、掉电和内核崩溃无法通过 finally 清理；此时请按报告中精确名称人工核查本轮对象。",
            ]
            write_json_atomic(self.out / "report.json", self.report)
            self.note("finished", status=self.report["status"], report=str(self.out / "report.json"))
            self.events.close()
        return 0 if self.report["status"] == "passed" else (130 if interrupted else 1)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true", help="明确创建隔离网络并实际发送本轮人工报文；默认只预览")
    parser.add_argument("--out", help="本轮全新输出目录，不允许已存在；建议 sudo 前用普通用户的 $HOME 展开")
    parser.add_argument("--worker", choices=("A", "B"), help=argparse.SUPPRESS)
    parser.add_argument("--worker-token", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        return udp_worker(args)
    if args.worker_token:
        parser.error("worker-token 只能用于内部 worker")
    run = Preflight(args)
    if not args.execute:
        print(json.dumps(run.preview(), ensure_ascii=False, indent=2))
        return 0
    try:
        return run.execute()
    except FileExistsError:
        print("拒绝覆盖已有输出目录；请使用新的 --out。", file=sys.stderr)
        return 2
    except OSError as exc:
        print("无法创建或保存本轮记录：%s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
