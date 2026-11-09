#!/usr/bin/env python3
"""Cancel on stale sensors; only publishes zero velocity, never motion."""

import os
import sys
import time

from freshness import FreshnessMonitor, env_topic, record_ros_message


def main():
    import rospy
    from sensor_msgs.msg import LaserScan
    from nav_msgs.msg import Odometry
    from actionlib_msgs.msg import GoalID
    from geometry_msgs.msg import Twist

    rospy.init_node("lab_navigation_data_guard")
    monitor = FreshnessMonitor()
    subscribers = [rospy.Subscriber(env_topic(os.environ, key, topic), cls,
                   lambda msg, name=name: record_ros_message(monitor, name, msg), queue_size=1)
                   for name, key, topic, cls in (
                       ("scan", "SCAN_TOPIC", "/scan", LaserScan),
                       ("odom", "ODOM_TOPIC", "/odom", Odometry))]
    cancel = rospy.Publisher(env_topic(os.environ, "CANCEL_TOPIC", "/move_base/cancel"),
                             GoalID, queue_size=1)
    stop = rospy.Publisher(env_topic(os.environ, "CMD_VEL_TOPIC", "/cmd_vel"),
                           Twist, queue_size=1, tcp_nodelay=True)

    def cancel_and_stop():
        try:
            # Empty GoalID cancels all active goals. No nonzero Twist is created.
            cancel.publish(GoalID())
            stop.publish(Twist())
        except (rospy.ROSException, RuntimeError):
            pass

    rospy.on_shutdown(cancel_and_stop)
    had_fault = True
    last_warning = float("-inf")
    try:
        while not rospy.is_shutdown():
            now = time.monotonic()
            state = monitor.snapshot(rospy.Time.now().to_sec(), now)
            if not state["ready"]:
                cancel_and_stop()
                had_fault = True
                if now - last_warning >= 2.0:
                    rospy.logwarn("导航保护已取消目标并发送零速度：%s", "；".join(state["failure_reasons"]))
                    last_warning = now
            elif had_fault:
                # Repeat once on recovery so the old action is explicitly
                # cancelled even if publishers connected late during startup.
                cancel_and_stop()
                rospy.logwarn("传感器数据已恢复。原目标不会自动重发；确认定位与周围环境后，由操作者重新设置目标。")
                had_fault = False
            elif state["warnings"] and now - last_warning >= 2.0:
                rospy.logwarn("导航数据接近时延限制：%s", "；".join(state["warnings"]))
                last_warning = now
            # Steady wall-clock wait ensures checks continue if ROS time jumps
            # or /clock pauses. Header ages are still computed in ROS time.
            time.sleep(0.2)
    finally:
        cancel_and_stop()
        for subscriber in subscribers:
            subscriber.unregister()
    return 0


if __name__ == "__main__":
    sys.exit(main())
