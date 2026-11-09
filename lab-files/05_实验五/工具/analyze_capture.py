#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实验五离线抓包清点工具：只读取本地文件，不发送任何网络报文。

用法：python3 analyze_capture.py capture.pcapng --controller 192.0.2.10
      --robot 192.0.2.20 --out analysis [--payload-length 52]
依赖：Python 3、dpkt。不会进行 TCP 重组或把载荷直接解释为机器人速度。
"""

import argparse
import csv
import hashlib
import ipaddress
import json
import struct
import sys
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

try:
    import dpkt
except ImportError:
    dpkt = None


DLT_NAMES = {1: "Ethernet", 113: "Linux cooked v1", 276: "Linux cooked v2"}
MAX_RECORD_BYTES = 64 * 1024 * 1024  # 避免损坏的长度字段导致无界内存分配。
FIELDS = [
    "frame", "time_epoch", "time_iso_utc", "datalink", "direction",
    "eth_src", "eth_dst", "src", "dst", "src_port", "dst_port", "protocol",
    "flags", "flags_hex", "seq", "ack", "payload_len", "payload_hex",
    "payload_hex_truncated",
]
NOTE_52 = (
    "若 ROS1 TCPROS Twist 且单帧恰含完整序列化消息，则 4+6*8=52 字节；"
    "TCP 分段、合并、重传均可破坏该假设。52 字节仅是筛选线索，不能据此认定 Twist。"
)


class CaptureError(Exception):
    """文件类型、链路类型等无法安全继续的错误。"""


class BadPacket(Exception):
    def __init__(self, category):
        self.category = category


def require_datalink(dlt):
    if dlt not in DLT_NAMES:
        raise CaptureError(
            "不支持 datalink=%s；仅支持 Ethernet(1)、Linux cooked v1(113)、v2(276)。" % dlt
        )


def detect_format(stream):
    """按 dpkt 的两种 Reader 检查格式；pcap 失败后必须回到文件起点。"""
    class HeaderView:
        # dpkt 的格式识别会寻找首个 IDB；损坏的块长不能触发超大或负数 read。
        name = getattr(stream, "name", "<capture>")

        @staticmethod
        def read(size):
            if size < 0 or size > MAX_RECORD_BYTES:
                raise CaptureError("文件头中的块长度无效或超过安全上限。")
            return stream.read(size)

    view = HeaderView()
    try:
        reader = dpkt.pcap.Reader(view)
        fmt = "pcap"
    except (ValueError, dpkt.UnpackError, struct.error, OverflowError):
        stream.seek(0)  # 不可省略：第一次尝试已经读取了文件头。
        try:
            reader = dpkt.pcapng.Reader(view)
            fmt = "pcapng"
        except (ValueError, dpkt.UnpackError, struct.error, OverflowError) as exc:
            raise CaptureError("无法识别有效的 pcap/pcapng 文件头：%s" % exc) from exc
    require_datalink(reader.datalink() & 0xffff if fmt == "pcap" else reader.datalink())
    stream.seek(0)
    return fmt


def container_issue(stats, warnings, text, truncated=False):
    stats["container_truncated" if truncated else "container_malformed"] += 1
    if len(warnings) < 30:
        warnings.append(text)


def pcap_records(stream, stats, warnings, datalinks):
    """保留 caplen/origlen，补充 dpkt Reader 没有暴露的截断检查。"""
    header = stream.read(24)
    magic = header[:4]
    formats = {
        b"\xd4\xc3\xb2\xa1": ("<", 10**6), b"\xa1\xb2\xc3\xd4": (">", 10**6),
        b"\x4d\x3c\xb2\xa1": ("<", 10**9), b"\xa1\xb2\x3c\x4d": (">", 10**9),
    }
    if len(header) != 24 or magic not in formats:
        raise CaptureError("不支持的 pcap 文件头。")
    endian, divisor = formats[magic]
    major, minor, _, _, _, raw_dlt = struct.unpack(endian + "HHiIII", header[4:])
    if major != 2 or minor != 4:
        raise CaptureError("仅支持 pcap 2.4；实际为 %s.%s。" % (major, minor))
    # pcap LinkType 字段高位可以携带 FCS 元数据，低 16 位才是 LinkType。
    dlt = raw_dlt & 0xffff
    require_datalink(dlt)
    datalinks.add(dlt)
    while True:
        raw = stream.read(16)
        if not raw:
            return
        if len(raw) != 16:
            container_issue(stats, warnings, "末尾 pcap 记录头不完整，已停止读取。", True)
            return
        sec, subsec, caplen, origlen = struct.unpack(endian + "IIII", raw)
        stats["frames_seen"] += 1
        frame = stats["frames_seen"]
        if caplen > MAX_RECORD_BYTES:
            container_issue(stats, warnings, "帧 %s 的 caplen 超过安全上限，已停止读取。" % frame)
            return
        data = stream.read(caplen)
        if len(data) != caplen:
            stats["discarded_truncated"] += 1
            container_issue(stats, warnings, "帧 %s 的记录数据被截断，已停止读取。" % frame, True)
            return
        if subsec >= divisor:
            stats["discarded_malformed"] += 1
            continue
        yield frame, Decimal(sec) + Decimal(subsec) / divisor, data, dlt, caplen, origlen


def read_options(data, endian):
    """读取 pcapng TLV；拒绝越界选项，不把损坏选项猜作时间精度。"""
    pos = 0
    while pos < len(data):
        if len(data) - pos < 4:
            raise ValueError("pcapng 选项头不完整")
        code, size = struct.unpack_from(endian + "HH", data, pos)
        pos += 4
        if code == 0:
            if size:
                raise ValueError("pcapng 结束选项长度不为零")
            return
        if pos + size > len(data):
            raise ValueError("pcapng 选项越界")
        yield code, data[pos:pos + size]
        pos += (size + 3) & ~3
        if pos > len(data):
            raise ValueError("pcapng 选项填充被截断")


def pcapng_records(stream, stats, warnings, datalinks):
    """逐块检查 pcapng；按每个接口保存链路和时间精度，支持多 section。"""
    endian = None
    interfaces = []
    while True:
        head = stream.read(8)
        if not head:
            return
        if len(head) != 8:
            container_issue(stats, warnings, "末尾 pcapng 块头被截断，已停止读取。", True)
            return
        is_section = head[:4] == b"\x0a\x0d\x0d\x0a"
        extra = b""
        if is_section:
            extra = stream.read(4)
            if extra == b"\x4d\x3c\x2b\x1a":
                endian = "<"
            elif extra == b"\x1a\x2b\x3c\x4d":
                endian = ">"
            else:
                container_issue(stats, warnings, "pcapng 字节序标识无效，已停止读取。")
                return
        if endian is None:
            raise CaptureError("pcapng 未以 Section Header Block 开始。")
        blocktype, size = struct.unpack(endian + "II", head)
        minimum = 28 if is_section else 12
        if size < minimum or size % 4 or size > MAX_RECORD_BYTES:
            container_issue(stats, warnings, "pcapng 块长度无效，已停止读取。")
            return
        rest = extra + stream.read(size - 8 - len(extra))
        if len(rest) != size - 8:
            container_issue(stats, warnings, "pcapng 块数据被截断，已停止读取。", True)
            return
        if struct.unpack(endian + "I", rest[-4:])[0] != size:
            container_issue(stats, warnings, "pcapng 块首尾长度不一致，已停止读取。")
            return
        body = rest[:-4]
        if is_section:
            if struct.unpack_from(endian + "H", body, 4)[0] != 1:
                raise CaptureError("仅支持 pcapng 主版本 1。")
            interfaces = []
            continue
        try:
            if blocktype == 1:  # Interface Description Block
                if len(body) < 8:
                    raise ValueError("IDB 过短")
                dlt, _, snaplen = struct.unpack_from(endian + "HHI", body)
                require_datalink(dlt)  # 任何接口类型不受支持都明确拒绝，避免误解释。
                datalinks.add(dlt)
                divisor, offset = Decimal(10**6), 0
                for code, value in read_options(body[8:], endian):
                    if code == 9:
                        if len(value) != 1:
                            raise ValueError("if_tsresol 长度无效")
                        resolution = value[0]
                        divisor = Decimal((2 if resolution & 0x80 else 10) ** (resolution & 0x7f))
                    elif code == 14:
                        if len(value) != 8:
                            raise ValueError("if_tsoffset 长度无效")
                        offset = struct.unpack(endian + "q", value)[0]
                interfaces.append((dlt, snaplen, divisor, offset))
                continue
            if blocktype not in (2, 3, 6):
                continue  # 统计、名称解析等非报文块无需导出。
            stats["frames_seen"] += 1
            frame = stats["frames_seen"]
            if blocktype == 3:  # Simple Packet Block 没有时间戳，仍可清点报文。
                if len(body) < 4 or not interfaces:
                    raise ValueError("SPB 缺少接口或原始长度")
                iface = 0
                origlen = struct.unpack_from(endian + "I", body)[0]
                snaplen = interfaces[0][1]
                caplen = min(origlen, snaplen) if snaplen else origlen
                data_start, ts = 4, None
            else:
                if len(body) < 20:
                    raise ValueError("报文块固定头过短")
                if blocktype == 6:
                    iface, high, low, caplen, origlen = struct.unpack_from(endian + "IIIII", body)
                else:  # 旧式 Packet Block 的接口编号是 16 位。
                    iface, _, high, low, caplen, origlen = struct.unpack_from(endian + "HHIIII", body)
                if iface >= len(interfaces):
                    raise ValueError("报文引用不存在的接口")
                _, _, divisor, offset = interfaces[iface]
                ts = Decimal(offset) + Decimal((high << 32) | low) / divisor
                data_start = 20
            padded = (caplen + 3) & ~3
            if data_start + padded > len(body):
                stats["discarded_truncated"] += 1
                continue
            if blocktype != 3:
                list(read_options(body[data_start + padded:], endian))
            dlt = interfaces[iface][0]
            yield frame, ts, body[data_start:data_start + caplen], dlt, caplen, origlen
        except (ValueError, struct.error) as exc:
            container_issue(stats, warnings, "pcapng 块无效：%s；已停止读取。" % exc)
            return


def mac_text(raw):
    return ":".join("%02x" % item for item in raw)


def decode_ipv4(data, dlt):
    """只解析已声明支持的三种链路头；SLL 无法恢复完整 Ethernet 源/目的地址。"""
    eth_src = eth_dst = ""
    if dlt == 1:
        if len(data) < 14:
            raise BadPacket("discarded_truncated")
        eth_dst, eth_src = mac_text(data[:6]), mac_text(data[6:12])
        proto, pos = struct.unpack_from("!H", data, 12)[0], 14
        while proto in (0x8100, 0x88a8, 0x9100):
            if len(data) < pos + 4:
                raise BadPacket("discarded_truncated")
            proto = struct.unpack_from("!H", data, pos + 2)[0]
            pos += 4
    elif dlt == 113:
        if len(data) < 16:
            raise BadPacket("discarded_truncated")
        proto, pos = struct.unpack_from("!H", data, 14)[0], 16
    else:  # Linux cooked v2
        if len(data) < 20:
            raise BadPacket("discarded_truncated")
        proto, pos = struct.unpack_from("!H", data, 0)[0], 20
    if proto != 0x0800:
        raise BadPacket("skipped_non_ipv4")
    raw = data[pos:]
    if len(raw) < 20:
        raise BadPacket("discarded_truncated")
    ihl = (raw[0] & 15) * 4
    total = struct.unpack_from("!H", raw, 2)[0]
    if raw[0] >> 4 != 4 or ihl < 20 or total < ihl:
        raise BadPacket("discarded_malformed")
    if len(raw) < ihl or len(raw) < total:
        raise BadPacket("discarded_truncated")
    if struct.unpack_from("!H", raw, 6)[0] & 0x3fff:
        raise BadPacket("discarded_ipv4_fragment")
    try:
        ip = dpkt.ip.IP(raw[:total])
    except (dpkt.UnpackError, ValueError, struct.error) as exc:
        raise BadPacket("discarded_malformed") from exc
    return ip, raw[ihl:total], eth_src, eth_dst


def transport_fields(ip, raw):
    if ip.p == dpkt.ip.IP_PROTO_TCP:
        if len(raw) < 20:
            raise BadPacket("discarded_truncated")
        offset = (raw[12] >> 4) * 4
        if offset < 20:
            raise BadPacket("discarded_malformed")
        if offset > len(raw):
            raise BadPacket("discarded_truncated")
        try:
            tcp = dpkt.tcp.TCP(raw)
        except (dpkt.UnpackError, ValueError, struct.error) as exc:
            raise BadPacket("discarded_malformed") from exc
        flags = tcp.flags
        names = [(1, "FIN"), (2, "SYN"), (4, "RST"), (8, "PSH"), (16, "ACK"),
                 (32, "URG"), (64, "ECE"), (128, "CWR"), (256, "NS")]
        return "TCP", tcp.sport, tcp.dport, "|".join(n for bit, n in names if flags & bit), \
            "0x%03x" % flags, tcp.seq, tcp.ack, raw[offset:]
    if ip.p == dpkt.ip.IP_PROTO_UDP:
        if len(raw) < 8:
            raise BadPacket("discarded_truncated")
        udp = dpkt.udp.UDP(raw)
        if udp.ulen < 8:
            raise BadPacket("discarded_malformed")
        if udp.ulen > len(raw):
            raise BadPacket("discarded_truncated")
        return "UDP", udp.sport, udp.dport, "", "", "", "", raw[8:udp.ulen]
    raise BadPacket("skipped_other_ipv4_protocol")


def analyze(args):
    source = Path(args.input).resolve()
    if not source.is_file():
        raise CaptureError("找不到输入文件：%s" % source)
    out = Path(args.out).resolve()
    if out == source or source in [out / name for name in ("packets.csv", "candidates.csv", "summary.json")]:
        raise CaptureError("输出路径会覆盖输入文件，请另选目录。")
    stats = Counter({key: 0 for key in (
        "frames_seen", "exported_tcp", "exported_udp", "candidate_rows", "discarded_malformed",
        "discarded_truncated", "discarded_ipv4_fragment", "skipped_non_ipv4",
        "skipped_other_endpoints", "skipped_other_ipv4_protocol", "container_malformed",
        "container_truncated", "exported_without_timestamp", "timestamp_out_of_range",
    )})
    warnings, datalinks, histogram = [], set(), Counter()
    out.mkdir(parents=True, exist_ok=True)
    temps = []
    try:
        with source.open("rb") as stream:
            fmt = detect_format(stream)
            records = pcap_records(stream, stats, warnings, datalinks) if fmt == "pcap" \
                else pcapng_records(stream, stats, warnings, datalinks)
            paths = [out / "packets.csv"]
            if args.payload_length is not None:
                paths.append(out / "candidates.csv")
            handles = []
            try:
                for path in paths:
                    tmp = path.with_name(path.name + ".tmp")
                    temps.append(tmp)
                    handles.append(tmp.open("w", encoding="utf-8-sig", newline=""))
                writers = [csv.DictWriter(handle, FIELDS) for handle in handles]
                for writer in writers:
                    writer.writeheader()
                endpoints = {args.controller, args.robot}
                for frame, ts, data, dlt, caplen, origlen in records:
                    if caplen < origlen:
                        stats["discarded_truncated"] += 1
                        continue
                    if caplen > origlen:
                        stats["discarded_malformed"] += 1
                        continue
                    try:
                        ip, raw, eth_src, eth_dst = decode_ipv4(data, dlt)
                        src, dst = str(ipaddress.IPv4Address(ip.src)), str(ipaddress.IPv4Address(ip.dst))
                        if {src, dst} != endpoints:
                            raise BadPacket("skipped_other_endpoints")
                        protocol, sport, dport, flags, flaghex, seq, ack, payload = transport_fields(ip, raw)
                    except BadPacket as exc:
                        stats[exc.category] += 1
                        continue
                    time_epoch, time_iso = "", ""
                    if ts is None:
                        stats["exported_without_timestamp"] += 1
                    else:
                        time_epoch = format(ts, "f")
                        try:
                            # CSV 保留完整 epoch 小数；ISO 便利显示到微秒，并明确使用 UTC。
                            time_iso = datetime.fromtimestamp(float(ts), timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
                        except (OverflowError, OSError, ValueError):
                            stats["timestamp_out_of_range"] += 1
                    row = dict(zip(FIELDS, [
                        frame, time_epoch, time_iso, dlt,
                        "controller_to_robot" if src == args.controller else "robot_to_controller",
                        eth_src, eth_dst, src, dst, sport, dport, protocol, flags, flaghex,
                        seq, ack, len(payload), payload[:128].hex(), int(len(payload) > 128),
                    ]))
                    writers[0].writerow(row)
                    stats["exported_" + protocol.lower()] += 1
                    if protocol == "TCP":
                        histogram[len(payload)] += 1
                        if args.payload_length is not None and len(payload) == args.payload_length:
                            writers[1].writerow(row)
                            stats["candidate_rows"] += 1
            finally:
                for handle in handles:
                    handle.close()
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        notes = [
            "本工具仅离线清点指定 IPv4 两端的 TCP/UDP；TCP 空载包也导出，便于查看握手。",
            "不会重组 TCP 流、去重重传或解码速度。PSH 不保证应用消息边界。",
            "ROS2 通常采用 DDS/RTPS，不能由该工具假定应使用 TCP；UDP 也单独导出供核对。",
            "损坏、截断和 IPv4 碎片计数面向整个输入；端点过滤发生在完整 IPv4 解码后。",
            "SLL/SLL2 不含完整 Ethernet 源/目的地址，eth_src/eth_dst 留空。",
            "不校验 IP/TCP/UDP 校验和；抓包中的硬件校验和卸载可能影响校验和值。",
            "TCP 载荷长度统计包含重传和空载包，每行是一个抓到的包，不是一个 ROS 消息。",
            "pcapng SPB 没有时间戳，导出时两个时间字段留空；ISO 时间显示到微秒，epoch 保留原精度。",
        ]
        if args.payload_length == 52 or 52 in histogram:
            notes.append(NOTE_52)
        summary = {
            "input_file": str(source), "input_sha256": digest.hexdigest(), "capture_format": fmt,
            "controller_ipv4": args.controller, "robot_ipv4": args.robot,
            "datalinks": [{"id": item, "name": DLT_NAMES[item]} for item in sorted(datalinks)],
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "scan_complete": not (stats["container_malformed"] or stats["container_truncated"]),
            "counts": dict(stats),
            "tcp_payload_length_histogram": {str(k): v for k, v in sorted(histogram.items())},
            "candidate_tcp_payload_length": args.payload_length,
            "payload_hex_limit_bytes": 128, "warnings": warnings, "notes": notes,
        }
        tmp_summary = out / "summary.json.tmp"
        temps.append(tmp_summary)
        tmp_summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # 所有解析完成后再替换最终结果；遇到不支持的链路类型不会留下貌似成功的半份 CSV。
        for tmp in temps:
            tmp.replace(tmp.with_name(tmp.name[:-4]))
        print("已导出 TCP %s 包、UDP %s 包；结果：%s" % (stats["exported_tcp"], stats["exported_udp"], out))
        if not summary["scan_complete"]:
            print("警告：文件容器损坏或被截断；有效前缀已导出，详情见 summary.json。", file=sys.stderr)
        return 0 if summary["scan_complete"] else 3
    finally:
        for tmp in temps:
            if tmp.exists():
                tmp.unlink()


def ipv4_arg(value):
    try:
        return str(ipaddress.IPv4Address(value))
    except ipaddress.AddressValueError as exc:
        raise argparse.ArgumentTypeError("需要有效的 IPv4 地址") from exc


def nonnegative_arg(value):
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("载荷长度必须是非负整数") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("载荷长度不能小于零")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", help="本地 pcap 或 pcapng 文件")
    parser.add_argument("--controller", required=True, type=ipv4_arg, help="控制端实际 IPv4")
    parser.add_argument("--robot", required=True, type=ipv4_arg, help="小车实际 IPv4")
    parser.add_argument("--out", required=True, help="离线分析输出目录")
    parser.add_argument("--payload-length", type=nonnegative_arg, help="可选：额外导出此 TCP 载荷长度的 candidates.csv")
    args = parser.parse_args()
    if args.controller == args.robot:
        parser.error("控制端和小车 IPv4 必须不同")
    if dpkt is None:
        print("缺少 dpkt。请在实验环境预先安装 dpkt 后重试；本工具不会联网安装依赖。", file=sys.stderr)
        return 2
    try:
        return analyze(args)
    except (CaptureError, OSError) as exc:
        print("分析失败：%s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
