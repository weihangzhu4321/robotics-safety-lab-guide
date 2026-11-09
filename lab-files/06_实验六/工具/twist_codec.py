"""Strict, network-free codec for one ROS1 geometry_msgs/Twist TCPROS frame."""
import math
import struct

FRAME_BYTES = 52
MAX_COMPLETE_FRAMES = 32


class UnsupportedFrame(ValueError):
    """The bytes are not the single supported planar Twist frame."""


def encode_twist(values):
    values = tuple(values)
    if len(values) != 6 or not all(math.isfinite(x) for x in values):
        raise UnsupportedFrame("six finite doubles are required")
    return struct.pack("<I6d", 48, *values)


def decode_twist(payload, max_linear=0.02, max_angular=0.2):
    if len(payload) != 52:
        raise UnsupportedFrame("TCP payload must be exactly 52 bytes; no reassembly is implemented")
    length, *values = struct.unpack("<I6d", payload)
    if length != 48:
        raise UnsupportedFrame("little-endian TCPROS length prefix must be 48")
    if not all(math.isfinite(x) for x in values):
        raise UnsupportedFrame("NaN or infinity is not supported")
    if any(values[i] != 0.0 for i in (1, 2, 3, 4)):
        raise UnsupportedFrame("linear.y/z and angular.x/y must be zero")
    if abs(values[0]) > max_linear or abs(values[5]) > max_angular:
        raise UnsupportedFrame("input exceeds the configured linear/angular amplitude limit")
    return tuple(values)


def transform_twist(payload, mode, factor=0.5, max_linear=0.02, max_angular=0.2):
    if mode not in ("invert", "scale"):
        raise ValueError("transform mode must be invert or scale")
    if not math.isfinite(factor) or not 0 < factor <= 1:
        raise ValueError("scale factor must be finite and in (0, 1]")
    before = decode_twist(payload, max_linear, max_angular)
    # Preserve every bit of an all-zero stop command, including IEEE signed zero.
    if before[0] == 0.0 and before[5] == 0.0:
        return payload, before, before
    after = list(before)
    coefficient = -1.0 if mode == "invert" else factor
    # Do not flip signed zero on an inactive component of a nonzero command.
    for index in (0, 5):
        if before[index] != 0.0:
            after[index] = before[index] * coefficient
    changed = encode_twist(after)
    checked = decode_twist(changed, max_linear, max_angular)
    if len(changed) != len(payload):
        raise AssertionError("a TCP payload length change is forbidden")
    return changed, before, checked


def transform_twist_frames(payload, mode, factor=0.5, max_linear=0.02, max_angular=0.2):
    """Transform 1..32 complete, already-aligned frames in one TCP payload.

    Never searches for a magic prefix or buffers/reassembles a TCP stream.
    A bad frame rejects the whole payload before any bytes are returned.
    """
    if not payload or len(payload) % FRAME_BYTES:
        raise UnsupportedFrame("payload is not a whole number of aligned 52-byte frames; no TCP reassembly")
    count = len(payload) // FRAME_BYTES
    if count > MAX_COMPLETE_FRAMES:
        raise UnsupportedFrame("payload exceeds the 32-complete-frame bound")
    pieces, before_frames, after_frames = [], [], []
    for index in range(count):
        start = index * FRAME_BYTES
        frame = payload[start:start + FRAME_BYTES]
        try:
            changed, before, after = transform_twist(frame, mode, factor, max_linear, max_angular)
        except UnsupportedFrame as exc:
            raise UnsupportedFrame("frame %d at payload offset %d: %s" % (index, start, exc)) from exc
        pieces.append(changed)
        before_frames.append(before)
        after_frames.append(after)
    result = b"".join(pieces)
    if len(result) != len(payload):
        raise AssertionError("a TCP payload length change is forbidden")
    return result, tuple(before_frames), tuple(after_frames)
