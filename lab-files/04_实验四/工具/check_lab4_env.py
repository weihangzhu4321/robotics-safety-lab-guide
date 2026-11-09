#!/usr/bin/env python3
"""实验四 Noetic 容器纯离线软件验收（Python 3.8+）。

必须使用 Docker --network none；不连接 ROS master，不启动 Gazebo/相机/
底盘服务，不安装依赖。通过仅表示依赖和离线测试可用，不能证明仿真或
现场循线已完成。结果写到独立目录，避免覆盖以前的验收证据。
"""
import argparse
from contextlib import ExitStack
from datetime import datetime
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import traceback
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET


class CheckFailure(Exception):
    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details


class OfflineCheck:
    def __init__(self, output_dir):
        self.output_dir = output_dir
        self.checks = []
        self.log_parts = []

    def log(self, message):
        line = str(message)
        print(line, flush=True)
        self.log_parts.append(line)

    def check(self, name, action):
        started = time.monotonic()
        record = {"name": name}
        try:
            record["details"] = action()
            record["status"] = "pass"
        except Exception as exc:
            record["status"] = "fail"
            record["error"] = "{}: {}".format(type(exc).__name__, exc)
            if isinstance(exc, CheckFailure) and exc.details is not None:
                record["details"] = exc.details
            self.log(traceback.format_exc().rstrip())
        record["elapsed_s"] = round(time.monotonic() - started, 4)
        self.checks.append(record)
        self.log("[{}] {}".format(record["status"].upper(), name))
        return record["status"] == "pass"

    def command(self, args, timeout=20):
        """只调用版本/本地包查询命令；不用 shell，也不调用 ROS master API。"""
        result = subprocess.run(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False)
        details = {"argv": args, "returncode": result.returncode,
                   "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}
        self.log("$ {}\n{}".format(" ".join(args),
                 json.dumps(details, ensure_ascii=False)))
        if result.returncode != 0:
            raise CheckFailure("本地查询命令退出非零", details)
        return details


def require_offline_noetic_container():
    """严格前置检查；失败时不导入 ROS，也不执行后续软件查询或测试。"""
    if platform.system() != "Linux":
        raise CheckFailure("仅允许在 Linux Noetic 容器内验收；当前不是 Linux")
    container_markers = [str(path) for path in
                         (Path("/.dockerenv"), Path("/run/.containerenv"))
                         if path.exists()]
    cgroup_file = Path("/proc/1/cgroup")
    cgroup = cgroup_file.read_text(errors="replace") if cgroup_file.exists() else ""
    if not container_markers and not any(
            word in cgroup for word in ("docker", "containerd", "kubepods")):
        raise CheckFailure("没有检测到容器标记；不要在 Ubuntu 宿主机运行验收")
    interfaces = sorted(path.name for path in Path("/sys/class/net").iterdir())
    if interfaces != ["lo"]:
        raise CheckFailure("必须由 docker run --network none 启动，且仅有 lo 接口",
                           {"interfaces": interfaces})
    if os.environ.get("ROS_DISTRO") != "noetic":
        raise CheckFailure("ROS_DISTRO 必须为 noetic；请先 source Noetic 环境")
    if not Path("/opt/ros/noetic/setup.bash").is_file():
        raise CheckFailure("未找到 /opt/ros/noetic/setup.bash")
    return {"container_markers": container_markers, "interfaces": interfaces,
            "ROS_DISTRO": os.environ.get("ROS_DISTRO"),
            "ROS_VERSION": os.environ.get("ROS_VERSION"),
            "ROS_PYTHON_VERSION": os.environ.get("ROS_PYTHON_VERSION")}


def check_python():
    if sys.version_info < (3, 8):
        raise CheckFailure("需要 Python 3.8 或更新的 Python 3")
    return {"version": sys.version, "executable": sys.executable,
            "implementation": platform.python_implementation()}


def import_module(name):
    module = importlib.import_module(name)
    return {"module": name, "version": getattr(module, "__version__", None),
            "file": getattr(module, "__file__", None)}


def check_bridge_roundtrip():
    """内存数组往返检查 cv_bridge 的本机 ABI；不获取相机或发布话题。"""
    import numpy as np
    from cv_bridge import CvBridge
    bridge = CvBridge()
    image = np.zeros((2, 3, 3), dtype=np.uint8)
    image[0, 1] = (0, 255, 255)
    message = bridge.cv2_to_imgmsg(image, encoding="bgr8")
    restored = bridge.imgmsg_to_cv2(message, desired_encoding="bgr8")
    if not np.array_equal(image, restored):
        raise CheckFailure("cv_bridge 内存图像往返结果不一致")
    return {"shape": list(restored.shape), "dtype": str(restored.dtype),
            "encoding": message.encoding, "source": "synthetic_in_memory"}


def command_paths():
    names = ("gazebo", "gzserver", "gzclient", "xacro", "rospack", "rosversion")
    paths = {name: shutil.which(name) for name in names}
    missing = [name for name, path in paths.items() if path is None]
    if missing:
        raise CheckFailure("缺少命令：{}".format(", ".join(missing)), paths)
    return paths


def gazebo_debian_version(checker):
    """查本机 Debian 包版本；不调用可能返回 255 的 gazebo --version。"""
    details = checker.command(["dpkg-query", "-W", "-f=${Version}", "gazebo11"])
    version = details["stdout"]
    details.update({"version_source": "Debian package database (dpkg-query)",
                    "debian_package": "gazebo11", "debian_version": version,
                    "executable_validation": "required_command_paths"})
    # 接受 Debian epoch/revision 与常见上游后缀，但必须确实为 Gazebo 11。
    if re.fullmatch(r"(?:[0-9]+:)?11\.[0-9]+(?:\.[0-9]+)*(?:[+~.-][0-9A-Za-z.+:~_-]+)?",
                    version) is None:
        raise CheckFailure("gazebo11 的 Debian 包版本无效或并非 Gazebo 11", details)
    details["gazebo_major"] = 11
    return details


def find_package(checker, name, expected_root=None):
    query = checker.command(["rospack", "find", name])
    package_path = Path(query["stdout"])
    manifest = package_path / "package.xml"
    if not package_path.is_dir() or not manifest.is_file():
        raise CheckFailure("rospack 返回的包目录或 package.xml 不存在", query)
    if expected_root is not None and package_path.resolve() != expected_root.resolve():
        raise CheckFailure("rospack 找到其他同名包，未对应本次挂载源码",
                           {"resolved": str(package_path.resolve()),
                            "expected": str(expected_root.resolve())})
    xml = ET.parse(str(manifest)).getroot()
    return {"path": str(package_path), "version": xml.findtext("version"),
            "package_name": xml.findtext("name")}


def inspect_xml(path, expected_root):
    root = ET.parse(str(path)).getroot()
    if root.tag != expected_root:
        raise CheckFailure("{} 根节点应为 {}，实际为 {}".format(
            path.name, expected_root, root.tag))
    return {"path": str(path), "root": root.tag,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def run_existing_tests(package_root, checker):
    tests_dir = package_root / "tests"
    if not tests_dir.is_dir():
        raise CheckFailure("没有找到现有 tests 目录")
    # test_watchdog_mock.py 自行替换外部 ROS 接口；所有用例均在内存中运行。
    sys.path.insert(0, str(package_root / "scripts"))
    loader = unittest.TestLoader()
    suite = loader.discover(str(tests_dir), pattern="test_*.py")
    discovered = suite.countTestCases()
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    checker.log(stream.getvalue().rstrip())
    details = {"discovered": discovered, "testsRun": result.testsRun,
               "failures": len(result.failures), "errors": len(result.errors),
               "skipped": len(result.skipped),
               "expectedFailures": len(result.expectedFailures),
               "unexpectedSuccesses": len(result.unexpectedSuccesses),
               "failure_test_ids": [test.id() for test, _ in result.failures],
               "error_test_ids": [test.id() for test, _ in result.errors],
               "skips": [{"test": test.id(), "reason": reason}
                         for test, reason in result.skipped],
               "test_file_sha256": {
                   path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in sorted(tests_dir.glob("test_*.py"))}}
    if (result.testsRun == 0 or result.testsRun != discovered or
            not result.wasSuccessful() or result.skipped or
            result.expectedFailures or result.unexpectedSuccesses):
        raise CheckFailure("要求现有测试实际执行且失败、错误、跳过均为 0", details)
    return details


def deny_network(*args, **kwargs):
    raise RuntimeError("离线验收禁止建立网络连接或绑定网络监听端口")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="结果目录；默认 /lab4/data/check_本机时间")
    args = parser.parse_args()
    started_at = datetime.now().astimezone()
    output_dir = args.output_dir or Path("/lab4/data") / (
        "check_" + started_at.strftime("%Y%m%d_%H%M%S_%f"))
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "result.json"
    log_path = output_dir / "check.log"
    if json_path.exists() or log_path.exists():
        parser.error("结果文件已存在；请换新 --output-dir，避免覆盖验收记录")
    checker = OfflineCheck(output_dir)
    checker.log("实验四纯离线软件验收；不代表 Gazebo 或实车实验完成。")
    checker.log("OUTPUT_DIR={}".format(output_dir))
    ready = checker.check("offline_noetic_container", require_offline_noetic_container)
    exit_code = 2
    package_root = Path(__file__).resolve().parent / "line_follow_lab4"
    if ready:
        # 除 Docker 的网络隔离之外，再阻止本 Python 中的连接及监听调用。
        with ExitStack() as stack:
            for attribute in ("connect", "connect_ex", "bind", "sendto"):
                stack.enter_context(patch.object(socket.socket, attribute, deny_network))
            stack.enter_context(patch.object(socket, "create_connection", deny_network))
            checker.check("python3", check_python)
            for name in ("cv2", "numpy", "rospy", "cv_bridge"):
                checker.check("import_" + name, lambda name=name: import_module(name))
            checker.check("cv_bridge_memory_roundtrip", check_bridge_roundtrip)
            checker.check("required_command_paths", command_paths)

            def noetic_version():
                details = checker.command(["rosversion", "-d"])
                if details["stdout"] != "noetic":
                    raise CheckFailure("rosversion -d 并非 noetic", details)
                return details

            checker.check("ros_distribution", noetic_version)
            # 版本来源为 Debian 包数据库；可执行路径由前一项独立检查。
            checker.check("gazebo_version", lambda: gazebo_debian_version(checker))
            for name in ("line_follow_lab4", "gazebo_ros", "turtlebot3_description",
                         "robot_state_publisher", "xacro", "rospy", "cv_bridge"):
                checker.check("rospack_" + name,
                              lambda name=name: find_package(
                                  checker, name,
                                  package_root if name == "line_follow_lab4" else None))
            for relative, root_tag in (("package.xml", "package"),
                                       ("launch/course.launch", "launch"),
                                       ("launch/follower.launch", "launch"),
                                       ("worlds/prep_ring.world", "sdf")):
                checker.check("xml_" + relative,
                              lambda relative=relative, root_tag=root_tag:
                              inspect_xml(package_root / relative, root_tag))
            checker.check("existing_offline_tests",
                          lambda: run_existing_tests(package_root, checker))

            def no_ros_node():
                rospy = importlib.import_module("rospy")
                if rospy.core.is_initialized():
                    raise CheckFailure("离线验收期间不应初始化 ROS 节点")
                return {"rospy_node_initialized": False}

            checker.check("no_ros_node_initialized", no_ros_node)
        exit_code = 0 if all(item["status"] == "pass" for item in checker.checks) else 1
    passed = sum(item["status"] == "pass" for item in checker.checks)
    report = {"scope": "offline_software_only", "started_at": started_at.isoformat(),
              "finished_at": datetime.now().astimezone().isoformat(),
              "status": "PASS" if exit_code == 0 else "FAIL", "exit_code": exit_code,
              "checks_passed": passed, "checks_failed": len(checker.checks) - passed,
              "checks_total": len(checker.checks), "checks": checker.checks,
              "package_root": str(package_root),
              "limitations": ["没有连接 ROS master、相机或小车",
                              "没有启动 Gazebo，也未验收 GUI、仿真物理或实际图像频率",
                              "合成数组及 mock 测试不能当作现场循线结果"]}
    checker.log("CHECK_RESULT={} pass={} fail={}".format(
        report["status"], report["checks_passed"], report["checks_failed"]))
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    log_path.write_text("\n".join(checker.log_parts) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
