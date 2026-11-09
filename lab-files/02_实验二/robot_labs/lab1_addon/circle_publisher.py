#!/usr/bin/env python3
"""Lab 1 only: publish to an isolated turtlesim and retain real CSV records.

No ROS packages are imported until main(), so the configuration checks can be
tested on a computer without ROS. The launcher must use the simulation master
at localhost:11431. This script never publishes to the robot's /cmd_vel.
"""

import csv
import math
import os
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import urlparse


DURATION_SECONDS = 24.0
PERIOD_SECONDS = 1.0


def validate_config(master_uri, turtle_name):
    """Reject a robot master and malformed/absolute turtle names."""
    uri = urlparse(master_uri)
    if (uri.scheme != "http" or uri.hostname not in ("localhost", "127.0.0.1")
            or uri.port != 11431 or uri.username or uri.password
            or uri.path not in ("", "/") or uri.query or uri.fragment):
        raise ValueError("仅允许实验一仿真 master：http://127.0.0.1:11431")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", turtle_name):
        raise ValueError("TURTLE_NAME 必须以字母开头，后接字母、数字或下划线；不能含 /。")
    return "/%s/cmd_vel" % turtle_name, "/%s/pose" % turtle_name


def command_for_elapsed(elapsed_ros_seconds):
    """The trial's duration and timer are both measured in ROS time."""
    if not math.isfinite(elapsed_ros_seconds) or elapsed_ros_seconds < 0:
        raise ValueError("ROS 时间异常或发生回退；本轮应停止后重新运行。")
    if elapsed_ros_seconds >= DURATION_SECONDS:
        return 0.0, 0.0
    return 4.0, -2.0


class CircleTrial:
    def __init__(self, rospy, Twist, Pose, turtle_name, data_dir):
        self.ros = rospy
        self.Twist = Twist
        self.name = turtle_name
        self.cmd_topic, self.pose_topic = validate_config(
            os.environ.get("ROS_MASTER_URI", ""), turtle_name)
        self.lock = threading.RLock()
        self.start_ros = None
        self.start_monotonic = None
        self.finished = False
        self.timer = None
        stamp = time.strftime("lab1_%Y%m%d_%H%M%S") + "_%s" % os.getpid()
        self.out_dir = Path(data_dir).expanduser() / stamp
        self.out_dir.mkdir(parents=True, exist_ok=False)
        self.pose_file = (self.out_dir / "pose.csv").open("w", newline="", encoding="utf-8")
        self.cmd_file = (self.out_dir / "cmd.csv").open("w", newline="", encoding="utf-8")
        self.pose_csv = csv.writer(self.pose_file)
        self.cmd_csv = csv.writer(self.cmd_file)
        self.pose_csv.writerow(["ros_time_s", "elapsed_ros_s", "elapsed_monotonic_s",
                                "x", "y", "theta", "linear_velocity", "angular_velocity"])
        self.cmd_csv.writerow(["ros_time_s", "elapsed_ros_s", "elapsed_monotonic_s",
                               "linear_x", "angular_z", "reason"])
        self.pub = rospy.Publisher(self.cmd_topic, Twist, queue_size=10)
        self.sub = rospy.Subscriber(self.pose_topic, Pose, self.on_pose, queue_size=100)
        rospy.on_shutdown(self.stop)

    def timestamps(self):
        now = self.ros.Time.now()
        elapsed = (now - self.start_ros).to_sec()
        wall_elapsed = time.monotonic() - self.start_monotonic
        return now.to_sec(), elapsed, wall_elapsed

    def on_pose(self, msg):
        with self.lock:
            if self.start_ros is None or self.finished:
                return
            self.pose_csv.writerow(list(self.timestamps()) + [
                msg.x, msg.y, msg.theta, msg.linear_velocity, msg.angular_velocity])
            self.pose_file.flush()

    def publish(self, linear_x, angular_z, reason):
        msg = self.Twist()
        msg.linear.x = linear_x
        msg.angular.z = angular_z
        self.pub.publish(msg)
        self.cmd_csv.writerow(list(self.timestamps()) + [linear_x, angular_z, reason])
        self.cmd_file.flush()

    def start(self):
        # Wall-clock timeout is only for waiting for a subscriber, not for the
        # trial duration. Timer scheduling and duration below share ROS time.
        deadline = time.monotonic() + 5.0
        while self.pub.get_num_connections() == 0:
            if self.ros.is_shutdown() or time.monotonic() >= deadline:
                raise RuntimeError("未找到速度订阅者；先启动 turtlesim 并创建指定对象。")
            time.sleep(0.05)
        with self.lock:
            self.start_ros = self.ros.Time.now()
            self.start_monotonic = time.monotonic()
            self.publish(4.0, -2.0, "start")
            self.timer = self.ros.Timer(self.ros.Duration(PERIOD_SECONDS), self.on_tick, reset=True)
        self.ros.loginfo("仿真对象 %s：v=4，w=-2，目标 1 Hz，约 24 秒。CSV：%s",
                         self.name, self.out_dir)

    def on_tick(self, event):
        should_shutdown = False
        with self.lock:
            if self.finished:
                return
            elapsed = (event.current_real - self.start_ros).to_sec()
            try:
                linear_x, angular_z = command_for_elapsed(elapsed)
            except ValueError as exc:
                self.ros.logerr(str(exc))
                should_shutdown = True
            else:
                if (linear_x, angular_z) == (0.0, 0.0):
                    should_shutdown = True
                else:
                    self.publish(linear_x, angular_z, "timer")
        if should_shutdown:
            self.stop()
            self.ros.signal_shutdown("24 秒实验结束或时钟异常，已发送零速度")

    def stop(self):
        # Idempotent: both the timer and ROS's Ctrl+C hook may call this.
        with self.lock:
            if self.finished:
                return
            self.finished = True
            if self.timer is not None:
                self.timer.shutdown()
            if self.start_ros is not None:
                # Several zero messages give the local connection time to drain.
                # This short wall sleep is transport cleanup, not trial timing.
                for _ in range(3):
                    try:
                        self.publish(0.0, 0.0, "stop")
                    except Exception as exc:
                        self.ros.logwarn("零速度发送失败：%s", exc)
                        break
                    time.sleep(0.05)
            self.sub.unregister()
            self.pose_file.close()
            self.cmd_file.close()


def main():
    turtle_name = os.environ.get("TURTLE_NAME", "turtle1")
    try:
        validate_config(os.environ.get("ROS_MASTER_URI", ""), turtle_name)
        import rospy
        from geometry_msgs.msg import Twist
        from turtlesim.msg import Pose
        rospy.init_node("lab1_circle_publisher", anonymous=True)
        if rospy.get_param("/use_sim_time", False):
            raise RuntimeError("turtlesim 本轮不使用 /clock。请在仿真 master 执行 rosparam set /use_sim_time false。")
        # A real Pose response establishes that this named simulated turtle exists.
        rospy.wait_for_message("/%s/pose" % turtle_name, Pose, timeout=5.0)
        trial = CircleTrial(rospy, Twist, Pose, turtle_name,
                            os.environ.get("DATA_DIR", "/lab/data"))
        try:
            trial.start()
            rospy.spin()
        finally:
            trial.stop()
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print("实验一未启动/已停止：%s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
