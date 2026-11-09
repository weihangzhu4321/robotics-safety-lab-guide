#!/usr/bin/env python3
"""Opt-in, bounded ROS1 move_base action client for an already verified map setup."""
from __future__ import print_function

import json
import math
import os
import time

import actionlib
import rospy
from actionlib_msgs.msg import GoalStatus
from move_base_msgs.msg import MoveBaseAction, MoveBaseGoal


def main():
    rospy.init_node('lab11_course_move_base_goals', anonymous=True)
    if not rospy.get_param('~allow_send_goals', False):
        rospy.logfatal('refusing to send action goals: set ~allow_send_goals:=true only after verifying the target map and ROS master')
        return 2
    frame = rospy.get_param('~frame_id', '')
    robots = rospy.get_param('~robots', ['tb3_0', 'tb3_1', 'tb3_2'])
    goals = rospy.get_param('~goals', [
        {'x': 0.0, 'y': -0.5, 'yaw': 0.0},
        {'x': 0.0, 'y': 0.0, 'yaw': 0.0},
        {'x': 0.0, 'y': 0.5, 'yaw': 0.0}])
    server_timeout = float(rospy.get_param('~server_timeout', 10.0))
    goal_timeout = float(rospy.get_param('~goal_timeout', 90.0))
    total_timeout = float(rospy.get_param('~total_timeout', 120.0))
    if not frame or frame.startswith('/'):
        rospy.logfatal('~frame_id is required and must be a verified non-absolute TF frame name')
        return 2
    if len(robots) != 3 or len(goals) != 3 or len(set(robots)) != 3:
        rospy.logfatal('exactly three unique robot namespaces and three goals are required')
        return 2
    if not (0.5 <= server_timeout <= 60 and 1 <= goal_timeout <= 180 and 1 <= total_timeout <= 300):
        rospy.logfatal('timeout values outside safe bounds')
        return 2
    try:
        points = [(float(g['x']), float(g['y']), float(g.get('yaw', 0.0))) for g in goals]
        if any(not all(math.isfinite(v) for v in point) or
               abs(point[0]) > 20.0 or abs(point[1]) > 20.0 or abs(point[2]) > math.pi
               for point in points):
            raise ValueError('goal coordinates must be finite and within map bound +/-20 m')
        if min(math.hypot(a[0] - b[0], a[1] - b[1])
               for i, a in enumerate(points) for b in points[i + 1:]) < 0.35:
            raise ValueError('goal positions must be at least 0.35 m apart')
    except (KeyError, TypeError, ValueError) as exc:
        rospy.logfatal('invalid goals: %s', str(exc))
        return 2

    clients = []
    for name in robots:
        client = actionlib.SimpleActionClient('/' + name + '/move_base', MoveBaseAction)
        if not client.wait_for_server(rospy.Duration(server_timeout)):
            rospy.logfatal('move_base action server unavailable: /%s/move_base', name)
            return 2
        clients.append(client)

    started = time.monotonic()
    rows = []
    completed_all = False
    try:
        for name, client, spec in zip(robots, clients, goals):
            goal = MoveBaseGoal()
            goal.target_pose.header.frame_id = frame
            goal.target_pose.header.stamp = rospy.Time.now()
            goal.target_pose.pose.position.x = float(spec['x'])
            goal.target_pose.pose.position.y = float(spec['y'])
            goal.target_pose.pose.orientation.z = float(math.sin(float(spec.get('yaw', 0.0)) / 2.0))
            goal.target_pose.pose.orientation.w = float(math.cos(float(spec.get('yaw', 0.0)) / 2.0))
            client.send_goal(goal)
            rows.append({'robot': name, 'goal_xy_yaw': [spec['x'], spec['y'], spec.get('yaw', 0.0)], 'status': 'sent'})

        deadline = min(started + total_timeout, started + goal_timeout)
        while not rospy.is_shutdown() and time.monotonic() < deadline:
            done = True
            for row, client in zip(rows, clients):
                state = client.get_state()
                if state in (GoalStatus.SUCCEEDED, GoalStatus.ABORTED, GoalStatus.REJECTED,
                             GoalStatus.PREEMPTED, GoalStatus.RECALLED, GoalStatus.LOST):
                    row['status'] = {
                        GoalStatus.SUCCEEDED: 'SUCCEEDED',
                        GoalStatus.ABORTED: 'ABORTED',
                        GoalStatus.REJECTED: 'REJECTED',
                        GoalStatus.PREEMPTED: 'PREEMPTED',
                        GoalStatus.RECALLED: 'RECALLED',
                        GoalStatus.LOST: 'LOST'}[state]
                else:
                    done = False
            if done:
                completed_all = True
                break
            time.sleep(0.1)
        else:
            for row in rows:
                if row['status'] == 'sent':
                    row['status'] = 'timeout_or_shutdown_cancel_requested'
    finally:
        if not completed_all:
            for row, client in zip(rows, clients):
                if row['status'] in ('sent', 'timeout_or_shutdown_cancel_requested'):
                    client.cancel_goal()

    report = {'scope': 'move_base action goals; requires an operator-verified matching map and clear area',
              'frame_id': frame, 'robots': robots, 'results': rows,
              'duration_wall_s': round(time.monotonic() - started, 3),
              'direct_cmd_vel_published_by_this_node': False,
              'physical_robot_tested': False}
    output = rospy.get_param('~output_json', '')
    if output:
        parent = os.path.dirname(os.path.abspath(output))
        os.makedirs(parent, exist_ok=True)
        with open(output, 'w') as stream:
            json.dump(report, stream, indent=2, sort_keys=True)
            stream.write('\n')
    rospy.loginfo('%s', json.dumps(report, sort_keys=True))
    return 0 if all(row['status'] == 'SUCCEEDED' for row in rows) else 1


if __name__ == '__main__':
    raise SystemExit(main())
