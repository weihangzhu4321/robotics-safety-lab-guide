#!/usr/bin/env python3
"""Bounded Gazebo-only three-Burger line-formation demo using model-state feedback."""
from __future__ import print_function

import csv
import itertools
import json
import math
import os
import threading
import time
from urllib.parse import urlsplit

import rospy
from gazebo_msgs.msg import ModelStates
from geometry_msgs.msg import Twist


def clamp(value, limit):
    return max(-limit, min(limit, value))


def wrap_angle(value):
    return math.atan2(math.sin(value), math.cos(value))


def yaw_from_quaternion(q):
    return math.atan2(2.0 * (q.w * q.z + q.x * q.y),
                      1.0 - 2.0 * (q.y * q.y + q.z * q.z))


class FormationRun(object):
    def __init__(self):
        self.names = rospy.get_param('~robots', [])
        if len(self.names) != 3 or len(set(r.get('name') for r in self.names)) != 3:
            raise ValueError('~robots must define exactly three uniquely named robots')
        for robot in self.names:
            if not isinstance(robot.get('name'), str) or not robot['name'].startswith('tb3_'):
                raise ValueError('robot names must use tb3_* model names')
            if len(robot.get('start', [])) != 2 or len(robot.get('goal', [])) != 2:
                raise ValueError('each robot requires [x,y] start and goal coordinates')
        if not rospy.get_param('~simulation_only', False):
            raise RuntimeError('refusing motion: ~simulation_only must be explicitly true')
        if not rospy.get_param('/use_sim_time', False):
            raise RuntimeError('refusing motion: /use_sim_time must be true for the Gazebo run')
        master_uri = os.environ.get('ROS_MASTER_URI', 'http://localhost:11311')
        master_host = urlsplit(master_uri).hostname
        if master_host not in ('localhost', '127.0.0.1', '::1'):
            raise RuntimeError('refusing motion: Gazebo-only demo requires a loopback ROS_MASTER_URI')

        self.max_seconds = float(rospy.get_param('~max_seconds', 90.0))
        self.rate_hz = float(rospy.get_param('~rate_hz', 10.0))
        self.max_linear = float(rospy.get_param('~max_linear', 0.12))
        self.max_angular = float(rospy.get_param('~max_angular', 0.35))
        self.min_separation = float(rospy.get_param('~min_separation', 0.42))
        self.goal_tolerance = float(rospy.get_param('~goal_tolerance', 0.10))
        if not 1.0 <= self.max_seconds <= 180.0:
            raise ValueError('~max_seconds must be within 1..180 wall-clock seconds')
        if not 1.0 <= self.rate_hz <= 20.0:
            raise ValueError('~rate_hz must be within 1..20')
        if not 0.0 < self.max_linear <= 0.20 or not 0.0 < self.max_angular <= 0.50:
            raise ValueError('speed limits must be positive and <=0.20 m/s, <=0.50 rad/s')
        if not 0.25 <= self.min_separation <= 2.0 or not 0.03 <= self.goal_tolerance <= 0.30:
            raise ValueError('invalid separation or goal tolerance')

        self.lock = threading.Lock()
        self.states = {}
        self.state_received = 0.0
        self.publishers = {}
        self.subscriber = rospy.Subscriber('/gazebo/model_states', ModelStates,
                                           self._model_states, queue_size=1)
        for robot in self.names:
            topic = '/' + robot['name'] + '/cmd_vel'
            self.publishers[robot['name']] = rospy.Publisher(topic, Twist, queue_size=1)
        self.output_dir = self._new_run_dir(rospy.get_param('~output_dir', '/tmp/lab11_formation_runs'))
        self.csv_file = open(os.path.join(self.output_dir, 'trajectory.csv'), 'w', newline='')
        self.csv_writer = csv.DictWriter(self.csv_file, fieldnames=[
            'elapsed_wall_s', 'robot', 'x', 'y', 'yaw', 'goal_x', 'goal_y',
            'goal_distance', 'min_pair_separation', 'cmd_linear_x', 'cmd_angular_z'])
        self.csv_writer.writeheader()
        self.initial_poses = {}
        self.final_poses = {}
        self.completed = set()
        self.minimum_observed_separation = float('inf')
        self.status = 'not_started'
        self.error = None
        self.started = time.monotonic()

    @staticmethod
    def _new_run_dir(base):
        os.makedirs(base, exist_ok=True)
        stamp = time.strftime('%Y%m%d_%H%M%S', time.gmtime())
        for suffix in range(100):
            path = os.path.join(base, 'run_' + stamp + ('_%02d' % suffix if suffix else ''))
            try:
                os.mkdir(path)
                return path
            except OSError:
                if not os.path.exists(path):
                    raise
        raise IOError('could not allocate a unique run directory')

    def _model_states(self, msg):
        selected = {}
        for robot in self.names:
            name = robot['name']
            try:
                i = msg.name.index(name)
                pose = msg.pose[i]
                selected[name] = (float(pose.position.x), float(pose.position.y),
                                  yaw_from_quaternion(pose.orientation))
            except (ValueError, IndexError, AttributeError):
                continue
        with self.lock:
            self.states = selected
            self.state_received = time.monotonic()

    def state_snapshot(self):
        with self.lock:
            return dict(self.states), self.state_received

    def publish(self, name, linear=0.0, angular=0.0):
        msg = Twist()
        msg.linear.x = clamp(linear, self.max_linear)
        msg.angular.z = clamp(angular, self.max_angular)
        self.publishers[name].publish(msg)
        return msg.linear.x, msg.angular.z

    def stop_all(self, count=5):
        for _ in range(count):
            for robot in self.names:
                self.publish(robot['name'], 0.0, 0.0)
            time.sleep(0.05)

    def wait_ready(self):
        deadline = time.monotonic() + 25.0
        while not rospy.is_shutdown() and time.monotonic() < deadline:
            states, received = self.state_snapshot()
            missing_states = [r['name'] for r in self.names if r['name'] not in states]
            missing_links = [name for name, pub in self.publishers.items()
                             if pub.get_num_connections() < 1]
            if not missing_states and received and time.monotonic() - received < 1.0 and not missing_links:
                return states
            self.stop_all(count=1)
            time.sleep(0.1)
        raise RuntimeError('Gazebo readiness timeout; missing model states or namespaced cmd_vel subscribers')

    def separate(self, states):
        distances = []
        for a, b in itertools.combinations([r['name'] for r in self.names], 2):
            if a in states and b in states:
                distances.append(math.hypot(states[a][0] - states[b][0], states[a][1] - states[b][1]))
        return min(distances) if len(distances) == 3 else float('inf')

    def control(self, robot, pose):
        gx, gy = [float(v) for v in robot['goal']]
        x, y, yaw = pose
        dx, dy = gx - x, gy - y
        distance = math.hypot(dx, dy)
        if distance <= self.goal_tolerance:
            self.completed.add(robot['name'])
            return distance, 0.0, 0.0
        error = wrap_angle(math.atan2(dy, dx) - yaw)
        angular = clamp(1.4 * error, self.max_angular)
        linear = 0.0 if abs(error) > 0.55 else min(self.max_linear, 0.45 * distance) * max(0.0, math.cos(error))
        return distance, linear, angular

    def write_row(self, elapsed, robot, pose, distance, min_sep, linear, angular):
        gx, gy = robot['goal']
        self.csv_writer.writerow({'elapsed_wall_s': '%.3f' % elapsed, 'robot': robot['name'],
                                  'x': '%.5f' % pose[0], 'y': '%.5f' % pose[1],
                                  'yaw': '%.5f' % pose[2], 'goal_x': gx, 'goal_y': gy,
                                  'goal_distance': '%.5f' % distance,
                                  'min_pair_separation': '%.5f' % min_sep,
                                  'cmd_linear_x': '%.4f' % linear,
                                  'cmd_angular_z': '%.4f' % angular})
        self.csv_file.flush()

    def run(self):
        try:
            states = self.wait_ready()
            self.initial_poses = dict((name, list(pose)) for name, pose in states.items())
            for robot in self.names:
                sx, sy = robot['start']
                x, y, _ = states[robot['name']]
                if math.hypot(x - float(sx), y - float(sy)) > 0.25:
                    raise RuntimeError('spawn pose mismatch for ' + robot['name'])
            sep = self.separate(states)
            if sep <= self.min_separation:
                raise RuntimeError('initial robot separation is below configured safety threshold')

            self.status = 'running'
            self.started = time.monotonic()
            deadline = self.started + self.max_seconds
            while not rospy.is_shutdown() and time.monotonic() < deadline:
                states, received = self.state_snapshot()
                if not received or time.monotonic() - received > 1.0:
                    raise RuntimeError('Gazebo model state stream stale for more than 1 second')
                if any(robot['name'] not in states for robot in self.names):
                    raise RuntimeError('Gazebo model state missing a robot')
                sep = self.separate(states)
                self.minimum_observed_separation = min(self.minimum_observed_separation, sep)
                if sep <= self.min_separation:
                    self.status = 'safety_stop_minimum_separation'
                    raise RuntimeError('minimum pair separation crossed safety threshold')

                all_done = True
                elapsed = time.monotonic() - self.started
                for robot in self.names:
                    name = robot['name']
                    pose = states[name]
                    distance, linear, angular = self.control(robot, pose)
                    if name not in self.completed:
                        all_done = False
                    sent_linear, sent_angular = self.publish(name, linear, angular)
                    self.write_row(elapsed, robot, pose, distance, sep, sent_linear, sent_angular)
                if all_done:
                    self.status = 'success'
                    break
                time.sleep(1.0 / self.rate_hz)
            else:
                if rospy.is_shutdown():
                    self.status = 'interrupted'
                else:
                    self.status = 'timeout'
        except Exception as exc:
            if self.status not in ('safety_stop_minimum_separation',):
                self.status = 'error'
            self.error = str(exc)[:500]
            rospy.logerr('%s', self.error)
        finally:
            self.stop_all()
            states, _ = self.state_snapshot()
            self.final_poses = dict((name, list(pose)) for name, pose in states.items())
            self.csv_file.close()
            summary = {
                'scope': 'isolated Gazebo simulation only',
                'status': self.status,
                'error': self.error,
                'model_names': [r['name'] for r in self.names],
                'command_topics': ['/' + r['name'] + '/cmd_vel' for r in self.names],
                'start_poses_xy_yaw': self.initial_poses,
                'goal_poses_xy': dict((r['name'], r['goal']) for r in self.names),
                'final_poses_xy_yaw': self.final_poses,
                'minimum_pair_separation_m': None if math.isinf(self.minimum_observed_separation)
                    else round(self.minimum_observed_separation, 5),
                'minimum_required_separation_m': self.min_separation,
                'max_linear_m_s': self.max_linear,
                'max_angular_rad_s': self.max_angular,
                'duration_wall_s': round(time.monotonic() - self.started, 3),
                'published_zero_velocity_on_exit': True,
                'physical_robot_tested': False,
                'trajectory_csv': os.path.join(self.output_dir, 'trajectory.csv')}
            path = os.path.join(self.output_dir, 'summary.json')
            with open(path, 'w') as stream:
                json.dump(summary, stream, indent=2, sort_keys=True)
                stream.write('\n')
            rospy.loginfo('formation run %s; summary=%s', self.status, path)
        return self.status


def main():
    rospy.init_node('lab11_three_burger_formation', anonymous=False)
    try:
        run = FormationRun()
    except Exception as exc:
        rospy.logfatal('%s', str(exc))
        return 2
    status = run.run()
    return 0 if status == 'success' else 1


if __name__ == '__main__':
    raise SystemExit(main())
