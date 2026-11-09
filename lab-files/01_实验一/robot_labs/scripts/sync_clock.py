#!/usr/bin/env python3
"""Synchronize robot UTC using authenticated SSH; never store a password.

Run on the Ubuntu host after exporting ROBOT_IP and ROBOT_USER. No ROS or
robot motion command is used. Clock changes are explicit, not part of doctor.
"""
import getpass
import ipaddress
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from decimal import Decimal, InvalidOperation


class SyncError(Exception):
    pass


def destination():
    host = os.environ.get("ROBOT_IP", "").strip()
    user = os.environ.get("ROBOT_USER", "").strip()
    try:
        ipaddress.IPv4Address(host)
    except ipaddress.AddressValueError as exc:
        raise SyncError("请先配置并导出有效的 ROBOT_IP（IPv4）。") from exc
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*[$]?", user):
        raise SyncError("请先配置并导出有效的 ROBOT_USER。")
    return f"{user}@{host}"


def synchronize():
    if not shutil.which("ssh"):
        raise SyncError("Ubuntu 尚未安装 openssh-client。")
    target = destination()
    password = None
    with tempfile.TemporaryDirectory(prefix="rl-ssh-") as temp_dir:
        control = os.path.join(temp_dir, "c")
        common = ["ssh", "-S", control, "-o", "ConnectTimeout=8",
                  "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3"]
        connected = False
        try:
            print(f"先完成 {target} 的 SSH 认证；首次连接请核对小车身份。", flush=True)
            # Inherit the real terminal for host-key and SSH password prompts.
            result = subprocess.run(common + ["-M", "-N", "-f", "-o",
                                    "ControlPersist=yes", target], check=False)
            if result.returncode:
                raise SyncError("SSH 认证失败，尚未修改小车时间。")
            connected = True

            def remote(command, secret=None):
                return subprocess.run(
                    common + ["-o", "ControlMaster=no", "-o", "BatchMode=yes",
                              target, command],
                    input=(secret + "\n") if secret is not None else "",
                    text=True, capture_output=True, timeout=25, check=False)

            # Verify the same sudo executable before taking the clock sample.
            privilege = remote("sudo -n date -u +%s.%N")
            use_password = privilege.returncode != 0
            if use_password:
                if not sys.stdin.isatty():
                    raise SyncError("sudo 需要密码；请在 Ubuntu 本地图形终端运行校时。")
                for attempt in range(3):
                    password = getpass.getpass("小车 sudo 密码（不显示、不保存）：")
                    privilege = remote("sudo -S -p '' date -u +%s.%N", password)
                    if privilege.returncode == 0:
                        break
                    print("未取得 sudo date 权限，请核对密码与小车账户权限。", file=sys.stderr)
                else:
                    raise SyncError("sudo 权限验证失败，尚未修改小车时间；脚本不修改 sudoers。")

            # All interactive authentication is complete. A dead control master
            # now fails BatchMode instead of asking for a new, delaying password.
            stamp = time.time_ns()
            epoch = f"{stamp // 1_000_000_000}.{stamp % 1_000_000_000:09d}"
            sudo = "sudo -S -p ''" if use_password else "sudo -n"
            changed = remote(f"{sudo} date -u -s {shlex.quote('@' + epoch)}",
                             password if use_password else None)
            if changed.returncode:
                raise SyncError("校时命令失败；检查连接、sudo date 权限，或系统是否要求 sudo TTY。")
            start = time.time_ns()
            verify = remote("date -u +%s.%N")
            finish = time.time_ns()
            if verify.returncode:
                raise SyncError("已执行校时，但回读验证失败；重新执行校时并验收传感器时间戳。")
            try:
                robot_time = Decimal(verify.stdout.strip())
            except InvalidOperation as exc:
                raise SyncError("已执行校时，但小车时间回读格式异常。") from exc
            midpoint = Decimal(start + finish) / Decimal(2_000_000_000)
            offset = robot_time - midpoint
            roundtrip = Decimal(finish - start) / Decimal(1_000_000_000)
            print(f"CLOCK_SYNC_DONE：UTC 已校准；回读估计差值 {offset:+.3f} 秒，"
                  f"SSH 往返 {roundtrip:.3f} 秒。")
            print("随后运行 bash lab.sh check；以 scan、odom 的实际时间戳差值验收。")
        finally:
            password = None
            if connected:
                try:
                    subprocess.run(common + ["-O", "exit", target],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=10, check=False)
                except (OSError, subprocess.TimeoutExpired):
                    pass


def main():
    try:
        synchronize()
    except KeyboardInterrupt:
        print("校时已中断。请重新检查时间戳后再继续实验。", file=sys.stderr)
        return 130
    except (SyncError, OSError, subprocess.TimeoutExpired) as exc:
        # Never print a subprocess command/input or an exception carrying stdin.
        message = str(exc) if isinstance(exc, SyncError) else "SSH 调用失败或超时，请检查网络。"
        print("错误：" + message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
