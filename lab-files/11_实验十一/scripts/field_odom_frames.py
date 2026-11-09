#!/usr/bin/env python3
"""Normalize three real Burger odometry streams on a separate ROS1 master."""

import math

import rospy
import tf2_ros
from geometry_msgs.msg import TransformStamped
from nav_msgs.msg import Odometry


class RobotFrames:
    def __init__(self, name, broadcaster):
        self.name = name
        self.broadcaster = broadcaster
        self.last_stamp = rospy.Time(0)
        self.output = rospy.Publisher(f'/{name}/odom', Odometry, queue_size=5)
        self.input = rospy.Subscriber(f'/{name}/odom_raw', Odometry, self.forward, queue_size=5)

    def forward(self, odom):
        now = rospy.Time.now()
        stamp = odom.header.stamp
        if stamp.is_zero() or (now - stamp).to_sec() > 1.0 or (stamp - now).to_sec() > 1.0:
            rospy.logwarn_throttle(5, '%s odometry outside the accepted time window', self.name)
            return
        if stamp > now:
            rospy.logwarn_throttle(5, '%s odometry ahead of receive time', self.name)
            stamp = now
        if stamp <= self.last_stamp:
            return
        pose = odom.pose.pose
        position, orientation = pose.position, pose.orientation
        values = (position.x, position.y, position.z, orientation.x, orientation.y,
                  orientation.z, orientation.w)
        if not all(map(math.isfinite, values)) or abs(sum(x*x for x in values[3:]) - 1) > 0.1:
            rospy.logwarn_throttle(5, '%s invalid odometry pose', self.name)
            return
        odom.header.stamp = stamp
        odom.header.frame_id = f'{self.name}/odom'
        odom.child_frame_id = f'{self.name}/base_footprint'
        transform = TransformStamped(header=odom.header, child_frame_id=odom.child_frame_id)
        transform.transform.translation.x = position.x
        transform.transform.translation.y = position.y
        transform.transform.translation.z = position.z
        transform.transform.rotation = orientation
        self.last_stamp = stamp
        self.broadcaster.sendTransform(transform)
        self.output.publish(odom)


def main():
    rospy.init_node('lab11_field_odom_frames')
    if rospy.get_param('/use_sim_time', False):
        raise RuntimeError('field odometry requires wall time')
    broadcaster = tf2_ros.TransformBroadcaster()
    static_broadcaster = tf2_ros.StaticTransformBroadcaster()
    robots = [RobotFrames(f'tb3_{i}', broadcaster) for i in range(3)]
    laser_transforms = []
    for robot in robots:
        laser = TransformStamped(child_frame_id=f'{robot.name}/base_scan')
        laser.header.stamp = rospy.Time.now()
        laser.header.frame_id = f'{robot.name}/base_footprint'
        laser.transform.translation.x = -0.032
        laser.transform.translation.z = 0.182
        laser.transform.rotation.w = 1.0
        laser_transforms.append(laser)
    static_broadcaster.sendTransform(laser_transforms)
    rospy.spin()


if __name__ == '__main__':
    main()
