#!/usr/bin/env bash
# Local checks only: no SSH, ROS master calls, node launches, or movement.
# Run after loading the course ROS 1 setup in an Ubuntu terminal.
set -u
printf 'ROS_VERSION=%s  ROS_DISTRO=%s\n' "${ROS_VERSION:-unset}" "${ROS_DISTRO:-unset}"
if [ "${ROS_VERSION:-}" != "1" ]; then
  printf '%s\n' 'NOT READY: load a validated course ROS 1 environment first.'
  printf '%s\n' 'The current Jazzy environment cannot directly run the course ROS 1 commands.'
  exit 2
fi
lab_missing=0
for lab_cmd in roslaunch rosrun rostopic rosparam rospack rosversion; do
  if ! command -v "$lab_cmd"; then
    printf 'MISSING COMMAND: %s\n' "$lab_cmd"
    lab_missing=1
  fi
done
if [ "$lab_missing" -ne 0 ]; then exit 2; fi
rosversion -d
for lab_pkg in turtlebot3_slam gmapping turtlebot3_navigation map_server amcl move_base turtlebot3_teleop turtlebot3_bringup turtlebot3_description robot_state_publisher tf; do
  if ! rospack find "$lab_pkg"; then
    printf 'MISSING PACKAGE: %s\n' "$lab_pkg"
    lab_missing=1
  fi
done
if [ "$lab_missing" -ne 0 ]; then exit 2; fi
export TURTLEBOT3_MODEL=burger
if ! roslaunch --files turtlebot3_slam turtlebot3_slam.launch slam_methods:=gmapping; then
  printf '%s\n' 'NOT READY: the SLAM launch file could not be resolved.'
  exit 2
fi
printf '%s\n' 'LOCAL PACKAGE CHECK PASSED. Robot network, scan, odometry, TF and navigation remain unverified.'
