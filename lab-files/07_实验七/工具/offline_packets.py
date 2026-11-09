#!/usr/bin/env python3
"""Construct and inspect synthetic PCAPs. No sockets, interfaces, or transmission."""
import argparse
import ipaddress
import json
from pathlib import Path
import struct
import sys

MAC_A = bytes.fromhex("020000000010")
MAC_B = bytes.fromhex("020000000020")
IP_A = "192.0.2.10"
IP_B = "198.51.100.20"


def checksum(data):
    if len(data) % 2:
        data += b"\x00"
    total = sum(struct.unpack("!%dH" % (len(data) // 2), data))
    while total >> 16:
        total = (total & 65535) + (total >> 16)
    return (~total) & 65535


def ip_bytes(address):
    return ipaddress.IPv4Address(address).packed


def ipv4(source, destination, protocol, payload, identification=100):
    header = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(payload),
                         identification, 0, 64, protocol, 0,
                         ip_bytes(source), ip_bytes(destination))
    header = header[:10] + struct.pack("!H", checksum(header)) + header[12:]
    return header + payload


def ethernet(ip_packet, destination_mac=MAC_B, source_mac=MAC_A):
    return destination_mac + source_mac + b"\x08\x00" + ip_packet


def tcp_packet(source=IP_A, destination=IP_B, source_port=40000,
               destination_port=80, sequence=1000, acknowledgement=0,
               flags=2, identification=100, reverse_mac=False):
    tcp = struct.pack("!HHIIBBHHH", source_port, destination_port, sequence,
                      acknowledgement, 0x50, flags, 4096, 0, 0)
    pseudo = ip_bytes(source) + ip_bytes(destination) + struct.pack("!BBH", 0, 6, len(tcp))
    tcp = tcp[:16] + struct.pack("!H", checksum(pseudo + tcp)) + tcp[18:]
    packet = ipv4(source, destination, 6, tcp, identification)
    return ethernet(packet, MAC_A, MAC_B) if reverse_mac else ethernet(packet)


def icmp_packet(source, destination, reply=False, broadcast=False, identification=100):
    payload = b"LAB7_OFFLINE_THEORETICAL_MODEL!!"  # fixed synthetic data
    icmp = struct.pack("!BBHHH", 0 if reply else 8, 0, 0, 0x0707, 1) + payload
    icmp = icmp[:2] + struct.pack("!H", checksum(icmp)) + icmp[4:]
    ip_packet = ipv4(source, destination, 1, icmp, identification)
    return ethernet(ip_packet, bytes.fromhex("ffffffffffff") if broadcast else MAC_A,
                    MAC_A if broadcast else MAC_B)


def dns_messages():
    # RFC 2606 .test name; one TXT question and one deliberately synthetic answer.
    qname = b"\x07example\x04test\x00"
    question = qname + struct.pack("!HH", 16, 1)
    request = struct.pack("!HHHHHH", 0x7A07, 0x0100, 1, 0, 0, 0) + question
    txt = b"".join(bytes([128]) + bytes([65 + i]) * 128 for i in range(4))
    answer = b"\xc0\x0c" + struct.pack("!HHIH", 16, 1, 60, len(txt)) + txt
    response = struct.pack("!HHHHHH", 0x7A07, 0x8180, 1, 1, 0, 0) + question + answer
    return request, response


def udp_packet(source, destination, source_port, destination_port, payload,
               identification=100, reverse_mac=False):
    udp = struct.pack("!HHHH", source_port, destination_port, len(payload) + 8, 0) + payload
    pseudo = ip_bytes(source) + ip_bytes(destination) + struct.pack("!BBH", 0, 17, len(udp))
    value = checksum(pseudo + udp) or 65535
    udp = udp[:6] + struct.pack("!H", value) + udp[8:]
    packet = ipv4(source, destination, 17, udp, identification)
    return ethernet(packet, MAC_A, MAC_B) if reverse_mac else ethernet(packet)


def fixture_frames():
    request, response = dns_messages()
    return {
        "normal_tcp_handshake": [
            tcp_packet(),
            tcp_packet(IP_B, IP_A, 80, 40000, 5000, 1001, 0x12, 101, True),
            tcp_packet(sequence=1001, acknowledgement=5001, flags=0x10, identification=102)],
        "syn_only_20": [tcp_packet(source_port=40000 + i, sequence=1000 + i,
                                    identification=100 + i) for i in range(20)],
        "smurf_theoretical_model": [
            icmp_packet(IP_A, "198.51.100.255", broadcast=True),
            *[icmp_packet("198.51.100." + str(21 + i), IP_A, reply=True,
                          identification=101 + i) for i in range(3)]],
        "dns_theoretical_model": [
            udp_packet(IP_A, IP_B, 40053, 53, request),
            udp_packet(IP_B, IP_A, 53, 40053, response, 101, True)]}


def pcap_bytes(frames):
    result = bytearray(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
    for index, frame in enumerate(frames):
        result.extend(struct.pack("<IIII", 0, index * 1000, len(frame), len(frame)))
        result.extend(frame)
    return bytes(result)


def parse_pcap(data):
    if len(data) < 24 or data[:4] not in (b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4"):
        raise ValueError("expected microsecond classic PCAP")
    endian = "<" if data[:4] == b"\xd4\xc3\xb2\xa1" else ">"
    _, major, minor, _, _, snaplen, linktype = struct.unpack(endian + "IHHIIII", data[:24])
    if (major, minor) != (2, 4) or linktype != 1:
        raise ValueError("only PCAP 2.4 Ethernet DLT1 is supported")
    cursor = 24
    frames = []
    while cursor < len(data):
        if cursor + 16 > len(data):
            raise ValueError("truncated PCAP record header")
        _, usec, captured, original = struct.unpack(endian + "IIII", data[cursor:cursor + 16])
        cursor += 16
        if usec >= 1000000 or captured != original or captured > snaplen or cursor + captured > len(data):
            raise ValueError("invalid or truncated PCAP record")
        frames.append(data[cursor:cursor + captured])
        cursor += captured
    return frames


def inspect_frame(frame):
    if len(frame) < 34 or frame[12:14] != b"\x08\x00":
        raise ValueError("expected Ethernet IPv4 frame")
    ip = frame[14:]
    ihl = (ip[0] & 15) * 4
    total = struct.unpack("!H", ip[2:4])[0]
    if ip[0] >> 4 != 4 or ihl < 20 or len(ip) < total or total < ihl:
        raise ValueError("invalid IPv4 lengths")
    if checksum(ip[:ihl]) != 0:
        raise ValueError("invalid IPv4 checksum")
    if struct.unpack("!H", ip[6:8])[0] & 0x3FFF:
        raise ValueError("fragmented IPv4 is outside this offline analyzer")
    ip = ip[:total]
    payload = ip[ihl:]
    protocol = ip[9]
    info = {"source": str(ipaddress.IPv4Address(ip[12:16])),
            "destination": str(ipaddress.IPv4Address(ip[16:20])),
            "protocol": protocol, "direction": "other", "ethernet_bytes": len(frame),
            "ip_bytes": total, "application_bytes": 0}
    pseudo = ip[12:20] + struct.pack("!BBH", 0, protocol, len(payload))
    if protocol == 6:
        if len(payload) < 20 or checksum(pseudo + payload) != 0:
            raise ValueError("invalid TCP checksum/length")
        offset = (payload[12] >> 4) * 4
        if offset < 20 or offset > len(payload):
            raise ValueError("invalid TCP header length")
        flags = payload[13]
        info["tcp_flags"] = flags
        info["direction"] = "response" if flags & 0x12 == 0x12 else "request" if flags & 2 else "other"
        info["application_bytes"] = len(payload) - offset
    elif protocol == 1:
        if len(payload) < 8 or checksum(payload) != 0:
            raise ValueError("invalid ICMP checksum/length")
        info["icmp_type"] = payload[0]
        info["direction"] = "request" if payload[0] == 8 else "response" if payload[0] == 0 else "other"
        info["application_bytes"] = len(payload) - 8
    elif protocol == 17:
        if len(payload) < 8:
            raise ValueError("invalid UDP length")
        source_port, destination_port, length, udp_checksum = struct.unpack("!HHHH", payload[:8])
        if length != len(payload) or (udp_checksum and checksum(pseudo + payload) != 0):
            raise ValueError("invalid UDP checksum/length")
        info["application_bytes"] = len(payload) - 8
        if 53 in (source_port, destination_port):
            dns = payload[8:]
            if len(dns) < 12:
                raise ValueError("invalid DNS length")
            info["dns_id"] = struct.unpack("!H", dns[:2])[0]
            info["direction"] = "response" if dns[2] & 0x80 else "request"
    return info


def analyze_frames(frames):
    totals = {direction: {"packets": 0, "ethernet_bytes": 0, "ip_bytes": 0,
                          "application_bytes": 0} for direction in ("request", "response", "other")}
    decoded = []
    for frame in frames:
        info = inspect_frame(frame)
        decoded.append(info)
        group = totals[info["direction"]]
        group["packets"] += 1
        for key in ("ethernet_bytes", "ip_bytes", "application_bytes"):
            group[key] += info[key]
    ratios = {}
    for key in ("packets", "ethernet_bytes", "ip_bytes", "application_bytes"):
        denominator = totals["request"][key]
        ratios[key] = round(totals["response"][key] / denominator, 6) if denominator else None
    return {"total_packets": len(frames), "total_ethernet_bytes": sum(map(len, frames)),
            "directions": totals, "response_over_request_ratio": ratios,
            "packets": decoded,
            "ratio_scope": "synthetic theoretical byte/count ratio, not measured network amplification; "
                           "Ethernet excludes preamble/FCS/IFG, application is protocol payload"}


def generate(output):
    output = Path(output).expanduser().absolute()
    output.mkdir(parents=False, exist_ok=False)
    summary = {"scope": "OFFLINE SYNTHETIC DATA ONLY: no transmission or field result",
               "addresses": "RFC5737 documentation networks", "dns_name": "example.test",
               "timestamp": "synthetic Unix epoch zero; not a field timestamp",
               "smurf_assumptions": "one forged-source request and exactly three hypothetical responders; "
                                    "no assertion that any actual network forwards/responds to broadcast",
               "dns_assumptions": "one invented TXT reply; no real resolver, open recursion or service behavior tested",
               "cases": {}}
    for name, frames in fixture_frames().items():
        (output / (name + ".pcap")).write_bytes(pcap_bytes(frames))
        summary["cases"][name] = analyze_frames(frames)
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--output", help="new output directory (parent must exist)")
    choice.add_argument("--analyze", help="read an existing Ethernet PCAP without capturing traffic")
    args = parser.parse_args(argv)
    try:
        summary = generate(args.output) if args.output else analyze_frames(parse_pcap(Path(args.analyze).read_bytes()))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
