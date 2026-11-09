#!/usr/bin/env python3
"""仅生成合成数组；不连接 ROS、不读取相机、不发布速度。"""
import importlib.util
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from vision_control import (Command, ControlConfig, VisionConfig, detect_line,
                            frame_age_problem, proportional_command)


class TestProportionalControl(unittest.TestCase):
    def test_center(self):
        self.assertEqual(proportional_command(0.0, ControlConfig()), Command(0.05, 0.0))

    def test_right_target_turns_right(self):
        self.assertAlmostEqual(proportional_command(100.0, ControlConfig()).angular_rps, -0.1)

    def test_left_target_turns_left(self):
        self.assertAlmostEqual(proportional_command(-100.0, ControlConfig()).angular_rps, 0.1)

    def test_both_limits(self):
        self.assertEqual(proportional_command(1000.0, ControlConfig()).angular_rps, -0.4)
        self.assertEqual(proportional_command(-1000.0, ControlConfig()).angular_rps, 0.4)

    def test_no_line_stops(self):
        self.assertEqual(proportional_command(None, ControlConfig()), Command())

    def test_reject_nonfinite_error(self):
        for value in (float("nan"), float("inf"), True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                proportional_command(value, ControlConfig())


class TestParameters(unittest.TestCase):
    def test_default_configs(self):
        self.assertEqual(VisionConfig().hsv_lower, (20, 43, 46))
        self.assertEqual(VisionConfig().hsv_upper, (90, 255, 255))
        self.assertEqual(ControlConfig().kp, 0.001)

    def test_invalid_vision_parameters(self):
        for kwargs in ({"hsv_upper": (180, 255, 255)}, {"hsv_lower": (-1, 0, 0)},
                       {"hsv_lower": (100, 0, 0)}, {"hsv_upper": (90.5, 255, 255)},
                       {"roi_start_ratio": 1.0}, {"roi_start_ratio": float("nan")},
                       {"roi_height_px": 0}, {"roi_height_px": 1.5}, {"min_line_pixels": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                VisionConfig(**kwargs)

    def test_invalid_control_parameters(self):
        for kwargs in ({"kp": -1}, {"linear_speed": -0.1},
                       {"max_angular_speed": 0}, {"kp": float("inf")}, {"kp": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ControlConfig(**kwargs)


class TestFreshness(unittest.TestCase):
    def test_fresh(self):
        self.assertIsNone(frame_age_problem(10.0, 9.9, 0.1, 0.5, 0.1))

    def test_no_new_image_uses_wall_clock(self):
        self.assertEqual(frame_age_problem(10.0, 10.0, 0.51, 0.5, 0.1), "no_fresh_frame")

    def test_old_capture_is_rejected_even_when_just_received(self):
        self.assertEqual(frame_age_problem(10.0, 9.0, 0.0, 0.5, 0.1), "stale_capture_stamp")

    def test_zero_stamp_cannot_enable_motion(self):
        self.assertEqual(frame_age_problem(10.0, 0.0, 0.0, 0.5, 0.1), "zero_or_invalid_stamp")

    def test_future_clock(self):
        self.assertEqual(frame_age_problem(10.0, 11.0, 0.0, 0.5, 0.1), "future_stamp_check_clock")

    def test_no_ros_clock(self):
        self.assertEqual(frame_age_problem(0.0, 1.0, 0.0, 0.5, 0.1), "ros_clock_not_ready")

    def test_bad_timeout(self):
        for timeout, tolerance in ((0, 0.1), (-1, 0.1), (0.5, -0.1)):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                frame_age_problem(10.0, 10.0, 0.0, timeout, tolerance)


HAS_VISION = importlib.util.find_spec("cv2") is not None and importlib.util.find_spec("numpy") is not None


@unittest.skipUnless(HAS_VISION, "当前 Python 缺 OpenCV/NumPy，视觉数组测试未执行")
class TestVision(unittest.TestCase):
    def test_yellow_roi_centroid_and_sign(self):
        import numpy as np
        image = np.zeros((120, 200, 3), dtype=np.uint8)
        image[100:120, 150:160] = (0, 255, 255)  # BGR 黄色；中心 x=154.5。
        result = detect_line(image, VisionConfig())
        self.assertTrue(result.found)
        self.assertAlmostEqual(result.cx_px, 154.5)
        self.assertAlmostEqual(result.error_px, 54.5)
        self.assertEqual(result.line_pixels, 200)
        self.assertLess(proportional_command(result.error_px, ControlConfig()).angular_rps, 0)

    def test_roi_excludes_upper_line(self):
        import numpy as np
        image = np.zeros((120, 200, 3), dtype=np.uint8)
        image[10:30, 70:100] = (0, 255, 255)
        self.assertFalse(detect_line(image, VisionConfig()).found)

    def test_empty_or_tiny_segment_is_lost(self):
        import numpy as np
        image = np.zeros((120, 200, 3), dtype=np.uint8)
        self.assertFalse(detect_line(image, VisionConfig()).found)
        image[100, 50] = (0, 255, 255)
        self.assertFalse(detect_line(image, VisionConfig()).found)

    def test_roi_clamps_small_image(self):
        import numpy as np
        image = np.zeros((2, 3, 3), dtype=np.uint8)
        result = detect_line(image, VisionConfig())
        self.assertEqual((result.roi_y0, result.roi_y1), (1, 2))

    def test_reject_other_encoding(self):
        import numpy as np
        with self.assertRaises(ValueError):
            detect_line(np.zeros((20, 20), dtype=np.uint8), VisionConfig())
        with self.assertRaises(ValueError):
            detect_line(np.zeros((20, 20, 3), dtype=np.float32), VisionConfig())


if __name__ == "__main__":
    unittest.main(verbosity=2)
