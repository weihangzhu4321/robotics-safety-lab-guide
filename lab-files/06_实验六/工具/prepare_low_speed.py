#!/usr/bin/env python3
"""Create separate low-speed lab-six launch files without changing the kit.

This is an offline text-generation tool. It does not run the source scripts,
connect to ROS, start Docker, or publish velocity commands.
"""

import argparse
import ast
import os
from pathlib import Path
import stat
import sys


class PreparationError(Exception):
    pass


def replace_once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise PreparationError(
            "{}: expected exactly 1 source match, found {}".format(label, count)
        )
    return text.replace(old, new, 1)


def source_file(path):
    if path.is_symlink() or not path.is_file():
        raise PreparationError("source must be a regular file: {}".format(path))
    content = path.read_bytes()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PreparationError("source is not UTF-8: {}".format(path)) from exc
    return text, stat.S_IMODE(path.stat().st_mode)


def prepare(kit_dir):
    kit = Path(kit_dir).expanduser().resolve(strict=True)
    if not kit.is_dir():
        raise PreparationError("kit_dir must be a directory: {}".format(kit))
    lab_source = kit / "lab.sh"
    teleop_source = kit / "scripts" / "teleop_ros1.py"
    lab_text, lab_mode = source_file(lab_source)
    teleop_text, teleop_mode = source_file(teleop_source)

    # All parsing and exact source-shape checks happen before any output opens.
    ast.parse(teleop_text, filename=str(teleop_source))
    new_lab = replace_once(
        lab_text,
        "/lab/scripts/teleop_ros1.py",
        "/lab/scripts/teleop_lab6_ros1.py",
        "lab.sh teleop script path",
    )
    new_teleop = replace_once(
        teleop_text, "0.05 *", "0.01 *", "linear velocity expression"
    )
    new_teleop = replace_once(
        new_teleop, "0.35 *", "0.10 *", "angular velocity expression"
    )
    new_teleop = replace_once(
        new_teleop,
        'self.root.title("实验二/三 低速键盘控制")',
        'self.root.title("实验六低速键盘控制")',
        "keyboard window title",
    )
    ast.parse(new_teleop, filename="teleop_lab6_ros1.py")

    outputs = [
        (kit / "lab6.sh", new_lab.encode("utf-8"), lab_mode),
        (kit / "scripts" / "teleop_lab6_ros1.py",
         new_teleop.encode("utf-8"), teleop_mode),
    ]
    # Treat the outputs as a pair. An incomplete or altered previous pair is
    # never overwritten or completed implicitly.
    if any(path.exists() or path.is_symlink() for path, _, _ in outputs):
        identical = all(
            not path.is_symlink() and path.is_file() and path.read_bytes() == data
            for path, data, _ in outputs
        )
        if identical:
            print("existing: both generated files already match exactly")
            return "existing"
        raise PreparationError(
            "refusing to overwrite: an output already exists and the complete "
            "pair is not identical"
        )

    created = []
    try:
        for path, data, mode in outputs:
            # Exclusive creation also protects against a newly appearing file.
            with path.open("xb") as handle:
                created.append(path)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            path.chmod(mode)
    except Exception:
        for path in reversed(created):
            path.unlink()
        raise
    print("created: {}".format(outputs[0][0]))
    print("created: {}".format(outputs[1][0]))
    print("limits: linear=0.01 m/s, angular=0.10 rad/s; original files unchanged")
    return "created"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kit_dir", help="existing robot_labs kit directory")
    args = parser.parse_args(argv)
    try:
        prepare(args.kit_dir)
    except (PreparationError, OSError, SyntaxError) as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
