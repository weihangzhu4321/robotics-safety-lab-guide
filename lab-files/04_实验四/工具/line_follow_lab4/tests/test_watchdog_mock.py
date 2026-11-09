#!/usr/bin/env python3
"""用内存替身检查停车逻辑；没有 ROS master，也不创建网络连接。

这只是节点逻辑测试，不能代替真实 ROS、相机、底盘和停车距离验证。
"""
import importlib.util
import io
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import patch


class FakeTwist:
    def __init__(self):
        self.linear = types.SimpleNamespace(x=0.0)
        self.angular = types.SimpleNamespace(z=0.0)


class FakeFloat32:
    def __init__(self, data=0.0):
        self.data = data


class FakeBridgeError(Exception):
    pass


class Publisher:
    def __init__(self):
        self.messages = []

    def publish(self, message):
        self.messages.append(message)


def load_with_fake_ros():
    # 仅装载本地源代码，所有外部 ROS/OpenCV 接口均替换成内存对象。
    modules = {}
    for package in ("sensor_msgs", "geometry_msgs", "std_msgs"):
        modules[package] = types.ModuleType(package)
        modules[package + ".msg"] = types.ModuleType(package + ".msg")
    modules["sensor_msgs.msg"].Image = object
    modules["geometry_msgs.msg"].Twist = FakeTwist
    modules["std_msgs.msg"].Float32 = FakeFloat32
    ros = types.ModuleType("rospy")
    ros.logwarn_throttle = lambda *args: None
    ros.logerr_throttle = lambda *args: None
    ros.logerr = lambda *args: None
    modules["rospy"] = ros
    bridge = types.ModuleType("cv_bridge")
    bridge.CvBridge = object
    bridge.CvBridgeError = FakeBridgeError
    modules["cv_bridge"] = bridge
    fake_cv = types.ModuleType("cv2")
    fake_cv.error = type("FakeCvError", (Exception,), {})
    modules["cv2"] = fake_cv
    source = Path(__file__).resolve().parents[1] / "scripts" / "follower.py"
    spec = importlib.util.spec_from_file_location("lab4_follower_mock", source)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


follower = load_with_fake_ros()


class TestWatchdogMock(unittest.TestCase):
    def make_node(self, enabled=True):
        node = follower.LineFollower.__new__(follower.LineFollower)
        node._lock = threading.RLock()
        node._shutdown_started = False
        node._fault_latched = False
        node._fault_reason = ""
        node._last_frame_wall = 100.0
        node._last_stamp = 10.0
        node._start_wall = 100.0
        node._last_cx = None
        node._last_error = None
        node._last_pixels = 0
        node._csv_failed = False
        node.enable_motion = enabled
        node.cmd_pub = Publisher() if enabled else None
        node.error_pub = Publisher()
        node.debug_pub = Publisher()
        node.mask_pub = Publisher()
        node.timeout_s = 0.5
        node.startup_timeout_s = 5.0
        node.future_tolerance_s = 0.1
        node._now_ros = lambda: 10.0
        node.rows = []
        node._write_row = lambda *args, **kwargs: node.rows.append((args, kwargs))
        node.vision_config = follower.VisionConfig()
        node.control_config = follower.ControlConfig()
        return node

    def message(self, stamp=10.1):
        return types.SimpleNamespace(header=types.SimpleNamespace(
            stamp=types.SimpleNamespace(to_sec=lambda: stamp)))

    def detection(self):
        return types.SimpleNamespace(found=True, cx_px=140.0, error_px=40.0,
                                     line_pixels=100, debug_bgr=object(), mask=object())

    def successful_bridge(self):
        return types.SimpleNamespace(imgmsg_to_cv2=lambda *a, **k: object(),
                                     cv2_to_imgmsg=lambda *a, **k: types.SimpleNamespace(header=None))

    def assert_last_zero(self, node):
        self.assertTrue(node.cmd_pub.messages)
        self.assertEqual(node.cmd_pub.messages[-1].linear.x, 0.0)
        self.assertEqual(node.cmd_pub.messages[-1].angular.z, 0.0)

    def test_no_images_stops_by_wall_clock_even_if_ros_clock_paused(self):
        node = self.make_node()
        with patch.object(follower.time, "monotonic", return_value=100.6):
            node._watchdog_step()
        self.assertTrue(node._fault_latched)
        self.assertEqual(node._fault_reason, "no_fresh_frame")
        self.assert_last_zero(node)

    def test_capture_stamp_age_stops_despite_fresh_receipt(self):
        node = self.make_node()
        node._now_ros = lambda: 10.6
        with patch.object(follower.time, "monotonic", return_value=100.1):
            node._watchdog_step()
        self.assertEqual(node._fault_reason, "stale_capture_stamp")
        self.assert_last_zero(node)

    def test_startup_timeout_stops(self):
        node = self.make_node()
        node._last_frame_wall = None
        with patch.object(follower.time, "monotonic", return_value=105.1):
            node._watchdog_step()
        self.assertEqual(node._fault_reason, "startup_no_image")
        self.assert_last_zero(node)

    def test_startup_grace_only_publishes_zero_without_latching(self):
        node = self.make_node()
        node._last_frame_wall = None
        with patch.object(follower.time, "monotonic", return_value=103.0):
            node._watchdog_step()
        self.assertFalse(node._fault_latched)
        self.assert_last_zero(node)

    def test_first_frame_within_startup_grace_can_track(self):
        node = self.make_node()
        node._last_frame_wall = None
        node._last_stamp = None
        node._now_ros = lambda: 13.0
        node.bridge = self.successful_bridge()
        with patch.object(follower.time, "monotonic", return_value=103.0), \
                patch.object(follower, "detect_line", return_value=self.detection()):
            node._watchdog_step()
            node.image_callback(self.message(13.0))
        self.assertFalse(node._fault_latched)
        self.assertEqual(node.cmd_pub.messages[-1].linear.x, 0.05)

    def test_invalid_startup_timeout_rejected_before_ros_publishers(self):
        for value in (0.1, float("inf"), True):
            def get_param(name, default):
                return value if name == "~startup_timeout_s" else default
            with self.subTest(value=value), \
                    patch.object(follower.rospy, "get_param", create=True, side_effect=get_param), \
                    self.assertRaises(ValueError):
                follower.LineFollower()

    def test_stale_first_frame_waits_at_zero_then_fresh_frame_can_track(self):
        node = self.make_node()
        node._last_frame_wall = node._last_stamp = None
        node._now_ros = lambda: 13.0
        node.bridge = self.successful_bridge()
        with patch.object(follower.time, "monotonic", return_value=103.0), \
                patch.object(follower, "detect_line", return_value=self.detection()):
            node.image_callback(self.message(1.0))
            self.assertFalse(node._fault_latched)
            self.assertIsNone(node._last_frame_wall)
            self.assert_last_zero(node)
            self.assertEqual(node.rows[-1][0][0], "startup_discard_stale_frame")
            node.image_callback(self.message(13.0))
            self.assertFalse(node._fault_latched)
            self.assertEqual(node.cmd_pub.messages[-1].linear.x, 0.05)
            self.assertEqual(node._last_stamp, 13.0)

    def test_repeated_stale_first_frames_do_not_extend_startup_deadline(self):
        node = self.make_node()
        node._last_frame_wall = node._last_stamp = None
        node._now_ros = lambda: 20.0
        for wall in (101.0, 103.0, 104.9, 105.01):
            with patch.object(follower.time, "monotonic", return_value=wall):
                node.image_callback(self.message(1.0))
        self.assertEqual(node._fault_reason, "startup_no_image")
        self.assertIsNone(node._last_frame_wall)
        self.assertTrue(all(m.linear.x == 0 and m.angular.z == 0 for m in node.cmd_pub.messages))

    def test_stale_frame_after_tracking_still_latches_and_cannot_recover(self):
        node = self.make_node()
        node._now_ros = lambda: 10.1
        node.bridge = self.successful_bridge()
        with patch.object(follower.time, "monotonic", return_value=100.1), \
                patch.object(follower, "detect_line", return_value=self.detection()):
            node.image_callback(self.message(10.1))
            self.assertEqual(node.cmd_pub.messages[-1].linear.x, 0.05)
            node._now_ros = lambda: 11.0
            node.image_callback(self.message(10.1))
            self.assertEqual(node._fault_reason, "stale_capture_stamp")
            self.assert_last_zero(node)
            node.image_callback(self.message(11.0))
            self.assertTrue(node._fault_latched)
            self.assert_last_zero(node)

    def test_startup_deadline_fault_cannot_be_cleared_by_late_fresh_frame(self):
        node = self.make_node()
        node._last_frame_wall = node._last_stamp = None
        node._now_ros = lambda: 20.0
        node.bridge = self.successful_bridge()
        with patch.object(follower.time, "monotonic", return_value=105.01), \
                patch.object(follower, "detect_line", return_value=self.detection()):
            node.image_callback(self.message(1.0))
            self.assertEqual(node._fault_reason, "startup_no_image")
            node.image_callback(self.message(20.0))
            self.assertTrue(node._fault_latched)
            self.assert_last_zero(node)

    def test_stale_first_frame_in_preview_creates_no_velocity_publisher(self):
        node = self.make_node(enabled=False)
        node._last_frame_wall = node._last_stamp = None
        node._now_ros = lambda: 13.0
        with patch.object(follower.time, "monotonic", return_value=103.0):
            node.image_callback(self.message(1.0))
        self.assertIsNone(node.cmd_pub)
        self.assertFalse(node._fault_latched)
        self.assertIsNone(node._last_frame_wall)

    def test_fresh_first_frame_after_deadline_cannot_start_before_watchdog_tick(self):
        node = self.make_node()
        node._last_frame_wall = node._last_stamp = None
        node._now_ros = lambda: 20.0
        node.bridge = self.successful_bridge()
        with patch.object(follower.time, "monotonic", return_value=105.01):
            node.image_callback(self.message(20.0))
        self.assertEqual(node._fault_reason, "startup_no_image")
        self.assert_last_zero(node)
        self.assertIsNone(node._last_frame_wall)

    def test_zero_and_future_headers_at_startup_still_latch(self):
        for stamp,reason in ((0.0,'zero_or_invalid_stamp'),(14.0,'future_stamp_check_clock')):
            with self.subTest(stamp=stamp):
                node = self.make_node()
                node._last_frame_wall = node._last_stamp = None
                node._now_ros = lambda: 13.0
                with patch.object(follower.time, "monotonic", return_value=103.0):
                    node.image_callback(self.message(stamp))
                self.assertEqual(node._fault_reason,reason)
                self.assert_last_zero(node)

    def test_catkin_devel_relay_imports_real_sibling_module(self):
        # 模拟 catkin devel relay：sys.path[0] 在 devel，而 __file__ 指向源脚本。
        # main 入口用哨兵退出，不创建 ROS 节点、线程、日志或网络连接。
        class ReachedMain(Exception):
            pass
        fake_ros = types.ModuleType("rospy")
        def init_node(*args, **kwargs):
            raise ReachedMain()
        fake_ros.init_node = init_node
        bridge = types.ModuleType("cv_bridge")
        bridge.CvBridge = object
        bridge.CvBridgeError = FakeBridgeError
        modules = {"rospy": fake_ros, "cv2": follower.cv2, "cv_bridge": bridge}
        for package in ("sensor_msgs", "geometry_msgs", "std_msgs"):
            modules[package] = types.ModuleType(package)
            modules[package + ".msg"] = types.ModuleType(package + ".msg")
        modules["sensor_msgs.msg"].Image = object
        modules["geometry_msgs.msg"].Twist = FakeTwist
        modules["std_msgs.msg"].Float32 = FakeFloat32
        scripts = Path(__file__).resolve().parents[1] / "scripts"
        source = scripts / "follower.py"
        relay = ("python_script = " + repr(str(source)) + "\n"
                 "with open(python_script, encoding='utf-8') as stream:\n"
                 "    context = {'__file__': python_script, '__name__': '__main__'}\n"
                 "    exec(compile(stream.read(), python_script, 'exec'), context)\n")
        clean_path = [item for item in sys.path if Path(item or ".").resolve() != scripts.resolve()]
        relay_path = str(scripts.parent / "devel" / "lib" / "line_follow_lab4")
        with patch.dict(sys.modules, modules), patch.object(sys, "path", [relay_path] + clean_path):
            # 排除已有模块缓存，确保实际验证同目录导入能找到正确源文件。
            sys.modules.pop("vision_control", None)
            with self.assertRaises(ReachedMain):
                exec(compile(relay, relay_path + "/follower.py", "exec"), {})
            loaded = sys.modules["vision_control"]
            self.assertEqual(Path(loaded.__file__).resolve(), (scripts / "vision_control.py").resolve())

    def test_preview_never_publishes_twist(self):
        node = self.make_node(enabled=False)
        with patch.object(follower.time, "monotonic", return_value=100.6):
            node._watchdog_step()
        self.assertIsNone(node.cmd_pub)
        self.assertFalse(node._fault_latched)

    def test_fresh_callback_can_publish_proportional_command(self):
        node = self.make_node()
        node._now_ros = lambda: 10.1
        node.bridge = self.successful_bridge()
        with patch.object(follower.time, "monotonic", return_value=100.1), \
                patch.object(follower, "detect_line", return_value=self.detection()):
            node.image_callback(self.message())
        self.assertEqual(node.cmd_pub.messages[-1].linear.x, 0.05)
        self.assertAlmostEqual(node.cmd_pub.messages[-1].angular.z, -0.04)

    def test_bridge_error_immediately_latches_zero(self):
        node = self.make_node()
        node._now_ros = lambda: 10.1
        def broken(*args, **kwargs):
            raise FakeBridgeError("synthetic conversion failure")
        node.bridge = types.SimpleNamespace(imgmsg_to_cv2=broken)
        with patch.object(follower.time, "monotonic", return_value=100.1):
            node.image_callback(self.message())
        self.assertEqual(node._fault_reason, "cv_bridge_error")
        self.assert_last_zero(node)

    def test_zero_header_rejected(self):
        node = self.make_node()
        with patch.object(follower.time, "monotonic", return_value=100.1):
            node.image_callback(self.message(stamp=0.0))
        self.assertEqual(node._fault_reason, "zero_or_invalid_stamp")
        self.assert_last_zero(node)

    def test_line_loss_latches_even_after_good_line_returns(self):
        node = self.make_node()
        node._now_ros = lambda: 10.1
        node.bridge = self.successful_bridge()
        lost = self.detection()
        lost.found = False
        lost.cx_px = lost.error_px = None
        with patch.object(follower.time, "monotonic", return_value=100.1), \
                patch.object(follower, "detect_line", return_value=lost):
            node.image_callback(self.message(10.1))
        self.assertEqual(node._fault_reason, "line_lost")
        node._now_ros = lambda: 10.2
        with patch.object(follower.time, "monotonic", return_value=100.2), \
                patch.object(follower, "detect_line", return_value=self.detection()):
            node.image_callback(self.message(10.2))
        self.assert_last_zero(node)

    def test_processing_older_than_timeout_cannot_publish_motion(self):
        node = self.make_node()
        node._now_ros = lambda: 10.1
        node.bridge = self.successful_bridge()
        with patch.object(follower.time, "monotonic", side_effect=[100.1, 100.1, 100.7]), \
                patch.object(follower, "detect_line", return_value=self.detection()):
            node.image_callback(self.message())
        self.assert_last_zero(node)
        self.assertEqual(node._fault_reason, "no_fresh_frame")

    def test_watchdog_stop_during_processing_cannot_be_restarted_by_late_callback(self):
        node = self.make_node()
        node._now_ros = lambda: 10.1
        node.bridge = self.successful_bridge()
        def process_with_watchdog(*args, **kwargs):
            # 图像计算不占命令锁，看门狗先锁存；模拟迟到的好图随后完成。
            with patch.object(follower.time, "monotonic", return_value=100.6):
                node._watchdog_step()
            return self.detection()
        with patch.object(follower.time, "monotonic", return_value=100.1), \
                patch.object(follower, "detect_line", side_effect=process_with_watchdog):
            node.image_callback(self.message())
        self.assertTrue(node._fault_latched)
        self.assertTrue(all(item.linear.x == 0 and item.angular.z == 0 for item in node.cmd_pub.messages))


if __name__ == "__main__":
    unittest.main(verbosity=2)
