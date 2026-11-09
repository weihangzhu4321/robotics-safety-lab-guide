"""Offline checks only; no ROS imports, GUI, network, or motor commands."""

from types import SimpleNamespace
import unittest

from freshness import FreshnessMonitor, env_topic, record_ros_message
from teleop_ros1 import velocity_for_keys


def header(seconds, frame="laser"):
    return SimpleNamespace(stamp=SimpleNamespace(to_sec=lambda: seconds), frame_id=frame)


class DataGateChecks(unittest.TestCase):
    def test_startup_is_stopped_until_both_sensors_arrive(self):
        monitor = FreshnessMonitor()
        self.assertFalse(monitor.snapshot(100.0, 10.0)["ready"])
        monitor.receive("scan", [100.0], ["laser"], received_at=10.0)
        self.assertFalse(monitor.snapshot(100.1, 10.1)["ready"])
        monitor.receive("odom", [100.0], ["odom"], received_at=10.0)
        self.assertTrue(monitor.snapshot(100.1, 10.1)["ready"])

    def test_header_future_and_stale_stamps_block_even_new_receipts(self):
        for stamp in (99.4, 100.6, 0, None):
            monitor = FreshnessMonitor(("scan",))
            monitor.receive("scan", [stamp], received_at=10)
            state = monitor.snapshot(100, 10)
            self.assertFalse(state["ready"])
            self.assertTrue(any("header" in reason for reason in state["failure_reasons"]))

    def test_receipt_clock_is_monotonic_and_separate_from_header_clock(self):
        monitor = FreshnessMonitor(("scan",))
        monitor.receive("scan", [1000.0], received_at=20.0)
        self.assertTrue(monitor.snapshot(1000.1, 20.5)["ready"])
        state = monitor.snapshot(1000.1, 21.0)
        self.assertFalse(state["ready"])
        self.assertTrue(any("收包" in reason for reason in state["failure_reasons"]))

    def test_warning_and_failure_boundaries(self):
        monitor = FreshnessMonitor(("scan",))
        monitor.receive("scan", [100.0], received_at=10.0)
        warned = monitor.snapshot(100.4, 10.1)
        self.assertTrue(warned["ready"])
        self.assertTrue(warned["warnings"])
        self.assertFalse(monitor.snapshot(100.5, 10.1)["ready"])

    def test_tf_uses_all_latest_message_stamps_not_only_first_transform(self):
        monitor = FreshnessMonitor(("tf",))
        monitor.receive("tf", [100.0, 99.0], ["odom -> base", "base -> laser"], 10.0)
        self.assertFalse(monitor.snapshot(100.1, 10.1)["ready"])
        empty = FreshnessMonitor(("tf",))
        empty.receive("tf", [], [], 10.0)
        self.assertFalse(empty.snapshot(100.1, 10.1)["ready"])

    def test_ros_message_adapter_with_plain_mocks(self):
        monitor = FreshnessMonitor(("scan", "tf"))
        record_ros_message(monitor, "scan", SimpleNamespace(header=header(100)))
        record_ros_message(monitor, "tf", SimpleNamespace(transforms=[
            SimpleNamespace(header=header(100, "odom"), child_frame_id="base_footprint")]))
        state = monitor.snapshot(100.1)
        self.assertTrue(state["ready"])
        self.assertEqual(state["sensors"]["tf"]["frames"], ["odom -> base_footprint"])
        self.assertEqual(state["sensors"]["scan"]["count"], 1)

    def test_direction_keys_require_focus_data_and_unblocked_press(self):
        self.assertEqual(velocity_for_keys({"w", "a"}, set(), True, True), (0.05, 0.35))
        self.assertEqual(velocity_for_keys({"s", "d"}, set(), True, True), (-0.05, -0.35))
        self.assertEqual(velocity_for_keys({"w"}, set(), False, True), (0.0, 0.0))
        self.assertEqual(velocity_for_keys({"w"}, set(), True, False), (0.0, 0.0))
        self.assertEqual(velocity_for_keys({"w"}, {"w"}, True, True), (0.0, 0.0))
        self.assertEqual(velocity_for_keys({"w", "s"}, set(), True, True), (0.0, 0.0))

    def test_topics_must_be_absolute(self):
        self.assertEqual(env_topic({}, "SCAN_TOPIC", "/scan"), "/scan")
        for topic in ("scan", "/", "/bad topic"):
            with self.assertRaises(ValueError):
                env_topic({"SCAN_TOPIC": topic}, "SCAN_TOPIC", "/scan")


if __name__ == "__main__":
    unittest.main()
