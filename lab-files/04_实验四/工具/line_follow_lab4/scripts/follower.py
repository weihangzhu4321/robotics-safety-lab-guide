#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ROS1 Noetic / Python3 教学预备节点；默认仅预览，尚未现场验证。

订阅原始 sensor_msgs/Image，不直接订阅 CompressedImage。
首个有效帧前只发零速并在固定启动期限内丢弃过旧首帧。
收到有效帧后任一失线/数据故障都会锁存停车；排除问题后 Ctrl+C 并重新启动。
相机镜像、实际 topic、底盘方向、图像时间戳均须到实验室核对。
"""

import csv
from datetime import datetime
import math
import os
import sys
import threading
import time

import cv2
import rospy
from cv_bridge import CvBridge, CvBridgeError
from geometry_msgs.msg import Twist
from sensor_msgs.msg import Image
from std_msgs.msg import Float32

# catkin 的 devel 启动器仍把 __file__ 指向源脚本；优先从真实同目录导入。
# install 空间中两个 .py 也会安装到同目录，不需要额外 pip 安装。
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from vision_control import (Command, ControlConfig, VisionConfig, detect_line,
                            frame_age_problem, proportional_command)


class LineFollower:
    def __init__(self):
        # 所有状态和速度发布共用一把锁；图像耗时计算放在锁外，防止阻塞停车。
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._shutdown_started = False
        self._fault_latched = False
        self._fault_reason = ""
        self._last_frame_wall = None
        self._last_stamp = None
        self._last_cx = None
        self._last_error = None
        self._last_pixels = 0
        self._start_wall = time.monotonic()
        self._csv_failed = False

        # ROS 参数只在启动时读取，防止运行中改动开关导致意外恢复。
        self.enable_motion = rospy.get_param("~enable_motion", False)
        if not isinstance(self.enable_motion, bool):
            raise ValueError("enable_motion 必须是 bool（true/false）")
        self.image_topic = rospy.get_param("~image_topic", "/camera/rgb/image_raw")
        self.cmd_topic = rospy.get_param("~cmd_topic", "/cmd_vel")
        if not isinstance(self.image_topic, str) or not self.image_topic.strip():
            raise ValueError("image_topic 不得为空")
        if not isinstance(self.cmd_topic, str) or not self.cmd_topic.strip():
            raise ValueError("cmd_topic 不得为空")
        self.vision_config = VisionConfig(
            hsv_lower=tuple(rospy.get_param("~hsv_lower", [20, 43, 46])),
            hsv_upper=tuple(rospy.get_param("~hsv_upper", [90, 255, 255])),
            roi_start_ratio=rospy.get_param("~roi_start_ratio", 5.0 / 6.0),
            roi_height_px=rospy.get_param("~roi_height_px", 20),
            min_line_pixels=rospy.get_param("~min_line_pixels", 20))
        self.control_config = ControlConfig(
            kp=rospy.get_param("~kp", 0.001),
            linear_speed=rospy.get_param("~linear_speed", 0.05),
            max_angular_speed=rospy.get_param("~max_angular_speed", 0.4))
        self.timeout_s = rospy.get_param("~frame_timeout_s", 0.5)
        # 首帧预留握手/相机初始化时间；收到首个有效帧后改用独立的短超时。
        self.startup_timeout_s = rospy.get_param("~startup_timeout_s", 5.0)
        self.future_tolerance_s = rospy.get_param("~future_stamp_tolerance_s", 0.1)
        # 复用纯函数做超时参数检查，此处只关心它是否抛异常。
        frame_age_problem(1.0, 1.0, 0.0, self.timeout_s, self.future_tolerance_s)
        frame_age_problem(1.0, 1.0, 0.0, self.startup_timeout_s, self.future_tolerance_s)
        if self.startup_timeout_s < self.timeout_s:
            raise ValueError("startup_timeout_s 不得小于 frame_timeout_s")

        self.bridge = CvBridge()
        self.debug_pub = rospy.Publisher("~debug_image", Image, queue_size=1)
        self.mask_pub = rospy.Publisher("~mask", Image, queue_size=1)
        self.error_pub = rospy.Publisher("~error_px", Float32, queue_size=1)
        # 预览模式根本不建立速度发布器；即使回调/退出也不会发送 cmd_vel。
        self.cmd_pub = (rospy.Publisher(self.cmd_topic, Twist, queue_size=1)
                        if self.enable_motion else None)

        csv_path = rospy.get_param("~csv_path", "")
        if not isinstance(csv_path, str):
            raise ValueError("csv_path 必须为字符串")
        if not csv_path:
            filename = "line_follow_{}_{}.csv".format(
                datetime.now().strftime("%Y%m%d_%H%M%S"), os.getpid())
            csv_path = os.path.join("~", "lab4_records", filename)
        self.csv_path = os.path.abspath(os.path.expanduser(csv_path))
        os.makedirs(os.path.dirname(self.csv_path), exist_ok=True)
        # x 模式只创建新文件；路径已存在时启动失败，保留原现场记录。
        self._csv_file = open(self.csv_path, "x", newline="", encoding="utf-8-sig")
        self._csv = csv.writer(self._csv_file)
        self._csv.writerow([
            "wall_local_iso", "image_stamp_ros_s", "status", "motion_enabled",
            "fault_latched", "cx_px", "error_px_not_m", "line_pixels",
            "candidate_linear_mps_not_measured", "candidate_angular_rps_not_measured",
            "published_linear_mps_not_measured", "published_angular_rps_not_measured",
            "command_published"])
        self._csv_file.flush()
        rospy.on_shutdown(self.shutdown)
        self._write_row("startup_preview" if not self.enable_motion else "startup_waiting_image")
        # queue_size=1 丢弃排队图像；大 buff_size 留出原始图像接收空间。
        # 队列设置不能保证图像新鲜，因此后面还检查采集 header 和墙钟年龄。
        self.subscriber = rospy.Subscriber(self.image_topic, Image, self.image_callback,
                                           queue_size=1, buff_size=2 ** 24)
        # 采用 Python 墙钟线程，不依赖 ROS /clock；仿真时钟停住也能停车。
        self.watchdog = threading.Thread(target=self._watchdog_loop, name="line_follow_watchdog", daemon=True)
        self.watchdog.start()
        rospy.loginfo("line_follower: motion=%s image=%s cmd=%s CSV=%s",
                      self.enable_motion, self.image_topic, self.cmd_topic, self.csv_path)
        if self.enable_motion:
            rospy.logwarn("运动已启用：任何失线/图像故障锁存停车，必须重启节点恢复；默认超时 %.2fs", self.timeout_s)
        else:
            rospy.loginfo("预览模式：不创建速度发布器。debug_image / mask / error_px 为节点私有 topic。")

    def _now_ros(self):
        return rospy.Time.now().to_sec()

    def _publish(self, command):
        # 调用处均持有 _lock，因此看门狗停车和非零速度发送不会交错。
        if self.cmd_pub is None:
            return False
        twist = Twist()
        twist.linear.x = command.linear_mps
        twist.angular.z = command.angular_rps
        self.cmd_pub.publish(twist)
        return True

    def _write_row(self, status, stamp=None, candidate=None, published=None, was_published=False):
        # 空白代表无该数值；预览候选指令也绝不能标为实测速度。
        if self._csv_failed:
            return
        candidate = candidate if candidate is not None else Command()
        try:
            self._csv.writerow([
                datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "" if stamp is None else "{:.9f}".format(stamp), status,
                int(self.enable_motion), int(self._fault_latched),
                "" if self._last_cx is None else self._last_cx,
                "" if self._last_error is None else self._last_error, self._last_pixels,
                candidate.linear_mps, candidate.angular_rps,
                "" if published is None else published.linear_mps,
                "" if published is None else published.angular_rps, int(was_published)])
            self._csv_file.flush()
        except (OSError, ValueError) as exc:
            # 记录故障也锁存停车；先发零速再报错，避免磁盘写满导致继续行驶。
            self._csv_failed = True
            self._fault_latched = True
            self._fault_reason = "csv_write_error"
            self._publish(Command())
            rospy.logerr("CSV 写入失败，已锁存停车：%s", exc)

    def _fault_locked(self, reason, stamp=None):
        # 预览时也保留状态证据，但停车锁存只影响启用运动的运行。
        first_fault = not self._fault_latched
        if self.enable_motion:
            self._fault_latched = True
            if first_fault:
                self._fault_reason = reason
        self._last_cx = None
        self._last_error = None
        self._last_pixels = 0
        sent = self._publish(Command())
        self._write_row(reason, stamp=stamp, published=Command() if sent else None,
                        was_published=sent)
        self.error_pub.publish(Float32(data=float("nan")))
        if first_fault or not self.enable_motion:
            rospy.logwarn_throttle(2.0, "图像/循线状态：%s；motion=%s", reason, self.enable_motion)

    def _age_reason(self, stamp, received_wall):
        return frame_age_problem(self._now_ros(), stamp,
                                 max(0.0, time.monotonic() - received_wall),
                                 self.timeout_s, self.future_tolerance_s)

    def image_callback(self, message):
        received_wall = time.monotonic()
        stamp = message.header.stamp.to_sec()
        with self._lock:
            if self._shutdown_started:
                return
            if (self._last_frame_wall is None and not self._fault_latched
                    and received_wall - self._start_wall >= self.startup_timeout_s):
                self._fault_locked("startup_no_image", stamp)
                return
            problem = self._age_reason(stamp, received_wall)
            if problem:
                # 新订阅者可能先收到很旧的首帧。尚未接受任何有效帧时保持
                # 零速等待，不把它当作已经开始跟踪后的故障。旧帧不刷新
                # 启动期限；超过固定5秒仍锁存。零/未来时间戳等其他错误
                # 以及有效跟踪开始后的所有旧帧继续按原规则立即锁存。
                if (problem == "stale_capture_stamp" and self._last_frame_wall is None
                        and not self._fault_latched):
                    sent = self._publish(Command())
                    self._write_row("startup_discard_stale_frame", stamp=stamp,
                                    published=Command() if sent else None,
                                    was_published=sent)
                    rospy.logwarn_throttle(2.0, "启动阶段丢弃过旧首帧，保持零速等待新鲜图像；启动期限 %.2fs",
                                           self.startup_timeout_s)
                    return
                self._fault_locked(problem, stamp)
                return
            if self._last_stamp is not None and stamp <= self._last_stamp:
                self._fault_locked("non_increasing_capture_stamp", stamp)
                return
        try:
            # 显式 bgr8 避免把 RGB 当 BGR，导致 HSV 分割颜色错位。
            bgr = self.bridge.imgmsg_to_cv2(message, desired_encoding="bgr8")
            detection = detect_line(bgr, self.vision_config)
            debug_message = self.bridge.cv2_to_imgmsg(detection.debug_bgr, encoding="bgr8")
            mask_message = self.bridge.cv2_to_imgmsg(detection.mask, encoding="mono8")
            debug_message.header = message.header
            mask_message.header = message.header
        except CvBridgeError as exc:
            with self._lock:
                if not self._shutdown_started:
                    self._fault_locked("cv_bridge_error", stamp)
            rospy.logerr_throttle(2.0, "CvBridge 转换失败：%s", exc)
            return
        except (ValueError, cv2.error) as exc:
            with self._lock:
                if not self._shutdown_started:
                    self._fault_locked("vision_processing_error", stamp)
            rospy.logerr_throttle(2.0, "视觉处理失败：%s", exc)
            return
        with self._lock:
            if self._shutdown_started:
                return
            # 计算期间看门狗仍可停车；结果返回后再次检验年龄，拒绝迟到计算。
            problem = self._age_reason(stamp, received_wall)
            if problem:
                self._fault_locked(problem, stamp)
                return
            if self._last_stamp is not None and stamp <= self._last_stamp:
                self._fault_locked("out_of_order_processed_frame", stamp)
                return
            self._last_frame_wall = received_wall
            self._last_stamp = stamp
            self._last_cx = detection.cx_px
            self._last_error = detection.error_px
            self._last_pixels = detection.line_pixels
            self.debug_pub.publish(debug_message)
            self.mask_pub.publish(mask_message)
            self.error_pub.publish(Float32(data=(detection.error_px if detection.found else float("nan"))))
            if not detection.found:
                self._fault_locked("line_lost", stamp)
                return
            candidate = proportional_command(detection.error_px, self.control_config)
            # 失线后即使重新识别成功也维持零速，避免突然重新起步。
            actual = Command() if self._fault_latched else candidate
            sent = self._publish(actual)
            status = ("latched_stop:" + self._fault_reason if self._fault_latched
                      else ("tracking" if self.enable_motion else "preview_line_found"))
            self._write_row(status, stamp=stamp, candidate=candidate,
                            published=actual if sent else None, was_published=sent)

    def _watchdog_step(self):
        # 单步方法也可被离线 mock 测试调用，不必启动 ROS 或等待真实超时。
        with self._lock:
            if self._shutdown_started:
                return
            if self._last_frame_wall is None:
                problem = ("startup_no_image" if time.monotonic() - self._start_wall > self.startup_timeout_s else None)
                if problem is None:
                    # 等待首帧期间只允许零速，避免握手等待期间沿用旧速度。
                    self._publish(Command())
            else:
                problem = self._age_reason(self._last_stamp, self._last_frame_wall)
            if problem:
                self._fault_locked(problem, self._last_stamp)
            elif self._fault_latched:
                # 连续发零速，避免底盘保留上次非零指令；不尝试主动搜索线。
                sent = self._publish(Command())
                self._write_row("latched_stop:" + self._fault_reason, stamp=self._last_stamp,
                                published=Command() if sent else None, was_published=sent)

    def _watchdog_loop(self):
        while not self._stop_event.wait(0.05):  # 20 Hz 墙钟，不受仿真 /clock 影响。
            try:
                self._watchdog_step()
            except Exception as exc:
                # 看门狗自身异常时也以停车结束，不允许线程悄然死亡后继续运动。
                with self._lock:
                    if not self._shutdown_started:
                        self._fault_latched = True
                        self._fault_reason = "watchdog_exception"
                        self._publish(Command())
                rospy.logerr("watchdog 异常，已锁存停车：%s", exc)

    def shutdown(self):
        with self._lock:
            if self._shutdown_started:
                return
            self._shutdown_started = True
            self._stop_event.set()
            self._publish(Command())
        if hasattr(self, "subscriber"):
            self.subscriber.unregister()
        if hasattr(self, "watchdog") and threading.current_thread() is not self.watchdog:
            self.watchdog.join(timeout=0.3)
        # 连续发 5 帧零速；使用墙钟等待，避免 ROS 时钟停住导致退出挂起。
        for _ in range(5):
            with self._lock:
                self._publish(Command())
            time.sleep(0.05)
        with self._lock:
            self._write_row("shutdown_zero", stamp=self._last_stamp,
                            published=Command() if self.cmd_pub is not None else None,
                            was_published=self.cmd_pub is not None)
            self._csv_file.close()


if __name__ == "__main__":
    rospy.init_node("line_follower")
    try:
        node = LineFollower()
        rospy.spin()
    except (ValueError, TypeError, OSError) as exc:
        rospy.logfatal("节点未完成启动，请检查参数、依赖和 CSV 路径：%s", exc)
        raise SystemExit(1)
