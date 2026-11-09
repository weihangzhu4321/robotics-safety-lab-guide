"""No ROS required: check trial boundaries and the real-robot isolation gate."""

import unittest
from circle_publisher import command_for_elapsed, validate_config


class CircleChecks(unittest.TestCase):
    def test_only_isolated_master_is_accepted(self):
        self.assertEqual(validate_config("http://127.0.0.1:11431", "student_123"),
                         ("/student_123/cmd_vel", "/student_123/pose"))
        validate_config("http://localhost:11431/", "turtle1")
        for uri in ("http://192.0.2.1:11311", "http://localhost:11311",
                    "http://127.0.0.1:11431/path", "http://user@localhost:11431"):
            with self.assertRaises(ValueError):
                validate_config(uri, "turtle1")

    def test_names_cannot_escape_to_a_root_robot_topic(self):
        for name in ("", "/cmd_vel", "turtle1/..", "123456", "中文"):
            with self.assertRaises(ValueError):
                validate_config("http://localhost:11431", name)

    def test_zero_at_and_after_trial_boundary(self):
        self.assertEqual(command_for_elapsed(0), (4.0, -2.0))
        self.assertEqual(command_for_elapsed(23.999), (4.0, -2.0))
        self.assertEqual(command_for_elapsed(24), (0.0, 0.0))
        self.assertEqual(command_for_elapsed(25), (0.0, 0.0))
        for elapsed in (-0.01, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                command_for_elapsed(elapsed)


if __name__ == "__main__":
    unittest.main()
