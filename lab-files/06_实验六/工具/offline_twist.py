#!/usr/bin/env python3
"""Pure offline Twist demonstration: no Scapy, sockets, capture, or sending."""
import argparse
import json
from twist_codec import UnsupportedFrame, encode_twist, transform_twist


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("invert", "scale"), default="invert")
    parser.add_argument("--factor", type=float, default=0.5)
    parser.add_argument("--linear-x", type=float, default=0.01)
    parser.add_argument("--angular-z", type=float, default=0.0)
    parser.add_argument("--hex", dest="hex_data", help="one complete 52-byte frame in hexadecimal")
    args = parser.parse_args(argv)
    try:
        payload = bytes.fromhex(args.hex_data) if args.hex_data is not None else encode_twist(
            (args.linear_x, 0.0, 0.0, 0.0, 0.0, args.angular_z))
        changed, before, after = transform_twist(payload, args.mode, args.factor)
    except (ValueError, UnsupportedFrame) as exc:
        parser.error(str(exc))
    print(json.dumps({"offline_only": True, "synthetic_input": args.hex_data is None,
                      "byte_length": len(payload), "prefix_little_endian": payload[:4].hex(),
                      "serialized_message_bytes": 48, "mode": args.mode,
                      "before": before, "after": after,
                      "before_hex": payload.hex(), "after_hex": changed.hex(),
                      "length_preserved": len(payload) == len(changed)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
