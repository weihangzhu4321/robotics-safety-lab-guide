"""Conservative offline IPv4/TCP segmentation helpers for relay_twist."""
import struct


class SegmentError(ValueError):
    pass


def checksum(data):
    if len(data) & 1:
        data += b"\0"
    words = struct.unpack("!%dH" % (len(data) // 2), data)
    total = sum(words)
    while total >> 16:
        total = (total & 0xffff) + (total >> 16)
    return (~total) & 0xffff


def _tcp_options(raw):
    i = 0
    timestamp_seen = False
    while i < len(raw):
        kind = raw[i]
        if kind == 0:  # EOL; remaining bytes are alignment padding.
            if any(raw[i + 1:]):
                raise SegmentError("nonzero bytes follow TCP EOL option")
            return
        if kind == 1:  # NOP
            i += 1
            continue
        if i + 2 > len(raw):
            raise SegmentError("truncated TCP option header")
        length = raw[i + 1]
        if length < 2 or i + length > len(raw):
            raise SegmentError("invalid or truncated TCP option length")
        if kind != 8 or length != 10:
            raise SegmentError("TCP options allow only NOP, EOL and Timestamp")
        if timestamp_seen:
            raise SegmentError("duplicate TCP Timestamp option")
        timestamp_seen = True
        i += length


def segment_ipv4_tcp(frame, mtu, payload_limit=512):
    """Return (segments, was_segmented); inputs/outputs are complete Ethernet frames."""
    if not isinstance(mtu, int) or not 576 <= mtu <= 65535:
        raise SegmentError("MTU must be an integer from 576 to 65535")
    if not isinstance(payload_limit, int) or not 64 <= payload_limit <= 1200:
        raise SegmentError("payload limit must be an integer from 64 to 1200")
    if len(frame) < 14 + 20:
        raise SegmentError("truncated Ethernet/IPv4 frame")
    eth = frame[:14]
    if eth[12:14] != b"\x08\x00":
        raise SegmentError("only untagged Ethernet IPv4 is supported")
    ip = frame[14:]
    if ip[0] >> 4 != 4 or (ip[0] & 15) != 5:
        raise SegmentError("IPv4 header must be version 4 with IHL 5")
    ip_len = struct.unpack("!H", ip[2:4])[0]
    if ip_len < 40 or len(ip) < ip_len:
        raise SegmentError("truncated IPv4 packet")
    if ip[9] != 6:
        raise SegmentError("only IPv4 TCP is supported")
    frag = struct.unpack("!H", ip[6:8])[0]
    if frag & 0xbfff:
        raise SegmentError("fragmented IPv4 packet is unsupported")
    tcp = ip[20:ip_len]
    if len(tcp) < 20:
        raise SegmentError("truncated TCP header")
    tcp_len = (tcp[12] >> 4) * 4
    if tcp_len < 20 or tcp_len > len(tcp):
        raise SegmentError("invalid or truncated TCP data offset")
    if tcp[12] & 0x0f:
        raise SegmentError("TCP reserved bits must be zero")
    flags = tcp[13]
    if flags & ~0x18 or not flags & 0x10 or tcp[18:20] != b"\0\0":
        raise SegmentError("only ACK/PSH TCP with zero urgent pointer is supported")
    _tcp_options(tcp[20:tcp_len])
    if ip_len <= mtu:
        return [frame], False
    payload = tcp[tcp_len:]
    chunk_size = min(mtu - 20 - tcp_len, payload_limit)
    if chunk_size <= 0:
        raise SegmentError("MTU leaves no TCP payload capacity")
    count = (len(payload) + chunk_size - 1) // chunk_size
    if count > 128:
        raise SegmentError("segmentation would exceed 128 packets")
    if not payload or count < 2:
        raise SegmentError("oversized TCP packet cannot be segmented safely")
    result = []
    original_seq = struct.unpack("!I", tcp[4:8])[0]
    original_id = struct.unpack("!H", ip[4:6])[0]
    for index, offset in enumerate(range(0, len(payload), chunk_size)):
        part = payload[offset:offset + chunk_size]
        new_ip = bytearray(ip[:20])
        new_tcp = bytearray(tcp[:tcp_len])
        new_tcp[4:8] = struct.pack("!I", (original_seq + offset) & 0xffffffff)
        if index != count - 1:
            new_tcp[13] &= ~0x08  # PSH only on final segment.
        new_ip[2:4] = struct.pack("!H", 20 + tcp_len + len(part))
        new_ip[4:6] = struct.pack("!H", (original_id + index) & 0xffff)
        new_ip[10:12] = b"\0\0"
        new_ip[10:12] = struct.pack("!H", checksum(bytes(new_ip)))
        new_tcp[16:18] = b"\0\0"
        pseudo = bytes(new_ip[12:20]) + b"\0\x06" + struct.pack("!H", tcp_len + len(part))
        new_tcp[16:18] = struct.pack("!H", checksum(pseudo + bytes(new_tcp) + part))
        result.append(eth + bytes(new_ip) + bytes(new_tcp) + part)
    return result, True
