#!/usr/bin/env python3
"""Bounded, explicit-opt-in Ethernet relay for an isolated ROS1 teaching lab.

Default invocation only validates arguments and prints a preview. It neither
imports Scapy nor opens an interface. This script never poisons/restores ARP,
changes sysctl, or implements TCP stream reassembly.
"""
import argparse
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import ipaddress
import json
import math
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
from typing import Optional

from twist_codec import FRAME_BYTES, UnsupportedFrame, transform_twist_frames
from mtu_forward import segment_ipv4_tcp


@dataclass(frozen=True)
class Config:
    iface: str
    a_ip: str
    a_mac: str
    b_ip: str
    b_mac: str
    m_mac: str
    mode: str = "forward"
    duration: float = 30.0
    log: str = "relay.jsonl"
    publisher_src_port: Optional[int] = None
    subscriber_dst_port: Optional[int] = None
    verified_tcpros: bool = False
    factor: float = 0.5
    max_linear: float = 0.02
    max_angular: float = 0.2
    segment_tcp_to_mtu: bool = False
    segment_payload_limit: int = 512


def valid_mac(value):
    value = value.lower()
    if not re.fullmatch(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}", value):
        raise ValueError("MAC addresses must use six colon-separated hexadecimal octets")
    raw = bytes.fromhex(value.replace(":", ""))
    if raw == b"\x00" * 6 or raw[0] & 1:
        raise ValueError("MAC addresses must be nonzero unicast addresses")
    return value


def validate(config, require_verified=False):
    if not config.iface or config.iface.startswith("-") or len(config.iface) > 64:
        raise ValueError("invalid interface name")
    addresses = [ipaddress.IPv4Address(v) for v in (config.a_ip, config.b_ip)]
    for address in addresses:
        if address.is_multicast or address.is_unspecified or address.is_loopback or int(address) == 0xffffffff:
            raise ValueError("A/B must be distinct unicast lab IPv4 addresses")
    if addresses[0] == addresses[1]:
        raise ValueError("A and B IP addresses must differ")
    macs = [valid_mac(v) for v in (config.a_mac, config.b_mac, config.m_mac)]
    if len(set(macs)) != 3:
        raise ValueError("A, B and M MAC addresses must all differ")
    if config.mode not in ("forward", "invert", "scale"):
        raise ValueError("unsupported mode")
    if not math.isfinite(config.duration) or not 1 <= config.duration <= 120:
        raise ValueError("duration must be finite and between 1 and 120 seconds")
    if not math.isfinite(config.factor) or not 0 < config.factor <= 1:
        raise ValueError("factor must be finite and in (0, 1]; speed increases are forbidden")
    if not math.isfinite(config.max_linear) or not 0 < config.max_linear <= 0.05:
        raise ValueError("max-linear must be in (0, 0.05] m/s")
    if not math.isfinite(config.max_angular) or not 0 < config.max_angular <= 0.3:
        raise ValueError("max-angular must be in (0, 0.3] rad/s")
    if config.mode != "forward":
        for port in (config.publisher_src_port, config.subscriber_dst_port):
            if port is None or not 1 <= port <= 65535:
                raise ValueError("invert/scale require the exact verified publisher and subscriber ports")
        if require_verified and not config.verified_tcpros:
            raise ValueError("invert/scale execute requires --verified-tcpros after manual topic/type/tuple/frame-boundary verification")
    if not config.log:
        raise ValueError("a new JSONL log path is required")
    if not isinstance(config.segment_payload_limit, int) or not 64 <= config.segment_payload_limit <= 1200:
        raise ValueError("segment-payload-limit must be an integer from 64 to 1200")
    if config.segment_tcp_to_mtu and config.mode != "forward":
        raise ValueError("--segment-tcp-to-mtu is supported only with --mode forward")


def run_ip_json(arguments):
    result = subprocess.run(["ip", "-j", *arguments], capture_output=True, text=True,
                            timeout=5, check=True)
    return json.loads(result.stdout)


def preflight(config, scapy, *, system=platform.system, geteuid=None,
              ip_json=run_ip_json, read_forward=None):
    validate(config, require_verified=True)
    if system() != "Linux":
        raise ValueError("--execute requires Linux; preview/offline tests work on Windows")
    if geteuid is None:
        geteuid = getattr(os, "geteuid", lambda: -1)
    if geteuid() != 0:
        raise ValueError("--execute requires root")
    if config.iface not in scapy.get_if_list():
        raise ValueError("interface does not exist")
    own_mac = valid_mac(scapy.get_if_hwaddr(config.iface))
    if own_mac != config.m_mac.lower():
        raise ValueError("--m-mac differs from the selected interface's actual MAC")
    links = ip_json(["link", "show", "dev", config.iface])
    if len(links) != 1 or links[0].get("address", "").lower() != own_mac:
        raise ValueError("interface link/MAC cross-check failed")
    mtu = links[0].get("mtu")
    if not isinstance(mtu, int) or not 576 <= mtu <= 65535:
        raise ValueError("selected interface has no valid reported MTU; refusing to guess")
    if "UP" not in links[0].get("flags", []):
        raise ValueError("selected interface is not UP")
    all_addresses = ip_json(["-4", "address", "show"])
    local = {info["local"] for device in all_addresses for info in device.get("addr_info", [])
             if info.get("family") == "inet"}
    if config.a_ip in local or config.b_ip in local:
        raise ValueError("A/B IP is a local address on this machine")
    selected = [info for device in all_addresses if device.get("ifname") == config.iface
                for info in device.get("addr_info", []) if info.get("family") == "inet"]
    if not selected:
        raise ValueError("selected interface has no IPv4 address")
    for peer in (config.a_ip, config.b_ip):
        if not any(ipaddress.IPv4Address(peer) in ipaddress.IPv4Network(
                f"{info['local']}/{info['prefixlen']}", strict=False) for info in selected):
            raise ValueError("both peers must be in an IPv4 subnet configured on the selected interface")
        route = ip_json(["-4", "route", "get", peer])
        if len(route) != 1 or route[0].get("dev") != config.iface or "gateway" in route[0]:
            raise ValueError("peer is not directly reachable through the selected interface")
        if route[0].get("type", "unicast") != "unicast":
            raise ValueError("peer route is not a unicast route")
    if read_forward is None:
        read_forward = lambda: Path("/proc/sys/net/ipv4/ip_forward").read_text().strip()
    if read_forward() != "0":
        raise ValueError("set net.ipv4.ip_forward=0 before running; this script never changes it")
    return {"linux_root": True, "interface_mac": own_mac, "interface_up": True, "interface_mtu": mtu,
            "both_peers_directly_connected": True, "peers_not_local": True, "ip_forward": 0}


class JsonLog:
    def __init__(self, path):
        self.file = Path(path).open("x", encoding="utf-8", newline="\n")

    def write(self, event, **fields):
        record = {"utc": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
        self.file.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        self.file.flush()

    def close(self):
        self.file.close()


class Relay:
    def __init__(self, config, scapy, log, send=None, mtu=1500):
        self.config, self.scapy, self.log = config, scapy, log
        self.send = send if send is not None else scapy.sendp
        self.mtu = mtu
        self.counts = Counter()
        self.modification_disabled = False
        self.send_failed = False
        self.processing_failed = False

    def receive(self, packet):
        """Do not let Scapy swallow a callback exception and report a success."""
        try:
            self.handle(packet)
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, IndexError,
                AttributeError, self.scapy.Scapy_Exception) as exc:
            self.processing_failed = True
            self.counts["processing_error"] += 1
            try:
                self.log.write("processing_error", error_type=type(exc).__name__, error=str(exc),
                               action="stop_without_fallback_send")
            except (OSError, ValueError):
                pass
            print("RELAY ERROR: " + str(exc) + ". Restore ARP immediately.", file=sys.stderr, flush=True)

    def unsupported(self, reason, details):
        self.counts["unsupported"] += 1
        self.modification_disabled = True
        self.log.write("unsupported", reason=reason, mode=self.config.mode,
                       action="forward_unchanged_and_disable_all_further_modification",
                       requires_stop_and_reverify=True, **details)
        print("STOP / REVERIFY: " + reason + ". Modification is disabled; restore ARP and stop the relay.",
              file=sys.stderr, flush=True)

    def handle(self, packet):
        c, s = self.config, self.scapy
        self.counts["seen"] += 1
        if not packet.haslayer(s.Ether) or not packet.haslayer(s.IP):
            self.counts["ignored_non_ipv4_ethernet"] += 1
            return
        eth, ip = packet[s.Ether], packet[s.IP]
        if eth.src.lower() == c.m_mac.lower():
            self.counts["ignored_own_mac"] += 1
            return
        if (ip.src, ip.dst) == (c.a_ip, c.b_ip):
            direction, expected_source, destination = "A_to_B", c.a_mac, c.b_mac
        elif (ip.src, ip.dst) == (c.b_ip, c.a_ip):
            direction, expected_source, destination = "B_to_A", c.b_mac, c.a_mac
        else:
            self.counts["ignored_other_ip_pair"] += 1
            return
        if eth.src.lower() != expected_source.lower() or eth.dst.lower() != c.m_mac.lower():
            self.counts["ignored_other_mac_scope"] += 1
            return
        out = packet.copy()
        out[s.Ether].src, out[s.Ether].dst = c.m_mac.lower(), destination.lower()
        details = {"direction": direction, "ip_src": ip.src, "ip_dst": ip.dst,
                   "ip_protocol": int(ip.proto), "mode": c.mode,
                   "scope_verified": True, "mac_src_out": c.m_mac.lower(),
                   "mac_dst_out": destination.lower()}
        action = "forwarded"
        if packet.haslayer(s.TCP):
            tcp = packet[s.TCP]
            details.update(sport=int(tcp.sport), dport=int(tcp.dport), seq=int(tcp.seq), ack=int(tcp.ack))
            target = (direction == "A_to_B" and int(tcp.sport) == c.publisher_src_port
                      and int(tcp.dport) == c.subscriber_dst_port)
            # Bound bytes by IP length: Ethernet padding is not TCP application data.
            ip_bytes = bytes(ip)
            ip_header = int(ip.ihl or 5) * 4
            tcp_header = int(tcp.dataofs or 5) * 4
            ip_length = int(ip.len or len(ip_bytes))
            data_start = ip_header + tcp_header
            malformed = ip_header < 20 or tcp_header < 20 or ip_length < data_start or ip_length > len(ip_bytes)
            payload = ip_bytes[data_start:ip_length] if not malformed else b""
            details["tcp_payload_bytes"] = len(payload)
            if target and c.mode != "forward" and not self.modification_disabled:
                if malformed or int(ip.frag) != 0 or bool(int(ip.flags) & 1):
                    self.unsupported("malformed or fragmented target IP/TCP packet", {k: v for k, v in details.items() if k != "mode"})
                elif payload:
                    if not c.verified_tcpros:
                        self.unsupported("target TCPROS stream was not explicitly verified", {k: v for k, v in details.items() if k != "mode"})
                    else:
                        try:
                            changed, before_frames, after_frames = transform_twist_frames(
                                payload, c.mode, c.factor, c.max_linear, c.max_angular)
                        except UnsupportedFrame as exc:
                            self.unsupported(str(exc), {k: v for k, v in details.items() if k != "mode"})
                        else:
                            details.update(frame_count=len(before_frames),
                                           frames=[{"index": index, "payload_offset": index * FRAME_BYTES,
                                                    "before": list(before), "after": list(after),
                                                    "changed": payload[index * FRAME_BYTES:(index + 1) * FRAME_BYTES]
                                                    != changed[index * FRAME_BYTES:(index + 1) * FRAME_BYTES]}
                                                   for index, (before, after) in enumerate(zip(before_frames, after_frames))],
                                           target_tuple_verified=True, length_preserved=True,
                                           input_within_limits=True, output_within_limits=True,
                                           max_linear=c.max_linear, max_angular=c.max_angular)
                            if len(before_frames) == 1:
                                details.update(before=list(before_frames[0]), after=list(after_frames[0]))
                            if changed != payload:
                                out[s.TCP].remove_payload()
                                out[s.TCP].add_payload(s.Raw(changed))
                                for field in ("len", "chksum"):
                                    delattr(out[s.IP], field)
                                del out[s.TCP].chksum
                                action = "modified"
                            else:
                                action = "zero_or_unchanged"
        elif c.mode != "forward" and not self.modification_disabled and (int(ip.frag) != 0 or bool(int(ip.flags) & 1)):
            self.unsupported("fragmented pair traffic cannot be checked against the TCP tuple", {k: v for k, v in details.items() if k != "mode"})
        try:
            # Materialize once so checksums/lengths are computed before sending.
            ready = s.Ether(bytes(out))
            ip_length = (int(ready[s.IP].len or len(bytes(ready[s.IP])))
                         if ready.haslayer(s.IP) else 0)
            details.update(ip_length=ip_length, mtu=self.mtu)
            if ip_length > self.mtu:
                if not c.segment_tcp_to_mtu:
                    raise RuntimeError("packet exceeds selected interface MTU (IP length %d > MTU %d); segmentation is disabled; stop and reverify" %
                                       (ip_length, self.mtu))
                if c.mode != "forward":
                    raise RuntimeError("MTU segmentation is forbidden for invert/scale")
                segments, segmented = segment_ipv4_tcp(bytes(ready), self.mtu, c.segment_payload_limit)
                sent = 0
                segment_records = []
                for index, raw_segment in enumerate(segments):
                    try:
                        self.send(s.Ether(raw_segment), iface=c.iface, verbose=False)
                        sent += 1
                        self.counts["wire_packets_sent"] += 1
                        self.counts["segments"] += 1
                        ip_bytes = raw_segment[14:]
                        ip_header_len = (ip_bytes[0] & 15) * 4
                        tcp_seq = int.from_bytes(ip_bytes[ip_header_len + 4:ip_header_len + 8], "big")
                        segment_records.append({"index": index, "seq": tcp_seq,
                                                "ip_length": int.from_bytes(ip_bytes[2:4], "big")})
                    except (OSError, RuntimeError, ValueError, s.Scapy_Exception) as exc:
                        self.send_failed = True
                        self.counts["send_error"] += 1
                        self.log.write("send_error", error_type=type(exc).__name__, error=str(exc),
                                       errno=getattr(exc, "errno", None), successful_segments=sent,
                                       total_segments=len(segments), failed_segment_index=index,
                                       segment_records=segment_records, **details)
                        raise RuntimeError("segmented send failed; no fallback/original frame was sent") from exc
                if segmented:
                    self.counts["segmented_input_packets"] += 1
                details.update(segmented=segmented, segment_count=sent,
                               segment_records=segment_records)
            else:
                self.send(ready, iface=c.iface, verbose=False)
                self.counts["wire_packets_sent"] += 1
        except (OSError, RuntimeError, ValueError, s.Scapy_Exception) as exc:
            if self.send_failed:
                raise
            self.send_failed = True
            self.counts["send_error"] += 1
            self.log.write("send_error", error_type=type(exc).__name__, error=str(exc), **details)
            raise RuntimeError("send failed; no fallback/original frame was sent") from exc
        self.counts[action] += 1
        if "frame_count" in details:
            self.counts["validated_frames_sent"] += details["frame_count"]
            self.counts["modified_frames_sent"] += sum(frame["changed"] for frame in details["frames"])
        self.log.write(action, **details)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("iface", "a-ip", "a-mac", "b-ip", "b-mac", "m-mac"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--mode", choices=("forward", "invert", "scale"), default="forward")
    parser.add_argument("--duration", type=float, default=30)
    parser.add_argument("--log", default="relay.jsonl", help="new JSONL file; its parent directory must already exist")
    parser.add_argument("--publisher-src-port", type=int)
    parser.add_argument("--subscriber-dst-port", type=int)
    parser.add_argument("--verified-tcpros", action="store_true",
                        help="manual topic/type/tuple/frame-boundary verification; not an automatic stream-alignment proof")
    parser.add_argument("--factor", type=float, default=0.5)
    parser.add_argument("--max-linear", type=float, default=0.02)
    parser.add_argument("--max-angular", type=float, default=0.2)
    parser.add_argument("--segment-tcp-to-mtu", action="store_true",
                        help="explicitly segment oversized in-scope forward-mode IPv4/TCP packets to the selected interface MTU")
    parser.add_argument("--segment-payload-limit", type=int, default=512,
                        help="maximum TCP payload bytes per generated segment (64..1200)")
    parser.add_argument("--execute", action="store_true", help="explicitly enable live sniff/sendp after preflight")
    args = parser.parse_args(argv)
    execute = args.execute
    del args.execute
    config = Config(**vars(args))
    try:
        validate(config, require_verified=execute)
    except ValueError as exc:
        parser.error(str(exc))
    return config, execute


def main(argv=None):
    config, execute = parse_args(argv)
    if not execute:
        print(json.dumps({"preview_only": True, "capture": False, "send": False,
                          "configuration": asdict(config),
                          "requires_before_execute": ["Linux root", "verified local interface/MAC and direct peer routes",
                                                      "net.ipv4.ip_forward=0", "new JSONL log path",
                                                      "actual selected-interface MTU from link details (never guessed)",
                                                      "optional segmentation is forward-only and requires explicit --segment-tcp-to-mtu",
                                                      "manually verified TCPROS topic/type/four-tuple/frame boundary for invert/scale"]},
                         ensure_ascii=False, indent=2))
        return 0
    try:
        import scapy.all as scapy
        checks = preflight(config, scapy)
        log = JsonLog(config.log)
    except (ImportError, OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print("PRECHECK FAILED: " + str(exc), file=sys.stderr)
        return 2
    relay = Relay(config, scapy, log, mtu=checks["interface_mtu"])
    status = 0
    started = time.monotonic()
    try:
        log.write("start", configuration=asdict(config), preflight=checks,
                  limitation="1..32 complete aligned TCPROS frames per payload; shape check is not stream-alignment proof; no TCP stream reassembly; ACK/byte-stream sequence space preserved; optional bounded TCP packet resegmentation is forward-only")
        print("LIVE relay started for at most %.1fs. Stop the robot before recovery; follow the guide to exit relay, temporarily restore kernel forwarding, then stop ARP. Ctrl+C exits immediately." % config.duration,
              flush=True)
        # No BPF/libpcap dependency: Python scope check runs before any send.
        scapy.sniff(iface=config.iface, prn=relay.receive, store=False, promisc=False,
                    timeout=config.duration,
                    stop_filter=lambda _packet: relay.send_failed or relay.processing_failed)
    except KeyboardInterrupt:
        log.write("interrupted")
        status = 130
    except (OSError, RuntimeError, ValueError, scapy.Scapy_Exception) as exc:
        log.write("fatal", error_type=type(exc).__name__, error=str(exc))
        print("RELAY FAILED: " + str(exc), file=sys.stderr)
        status = 2
    finally:
        if relay.send_failed or relay.processing_failed:
            status = 2
        elif relay.modification_disabled and status == 0:
            status = 3
        log.write("summary", counts=dict(relay.counts), elapsed_seconds=round(time.monotonic() - started, 6),
                  exit_status=status, requires_stop_and_reverify=relay.modification_disabled)
        log.close()
    print(json.dumps({"counts": dict(relay.counts), "exit_status": status,
                      "arp_restoration_required": True}, ensure_ascii=False))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
