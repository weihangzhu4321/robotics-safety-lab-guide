#!/usr/bin/env bash
set -euo pipefail
image=robot-lab4:noetic-prep
docker image inspect "$image" >/dev/null || { echo '缺少含 Gazebo 的课程镜像，请先查看资料的实验十一依赖。' >&2; exit 1; }
docker run --rm -it --init --network none --ipc private \
  --user "$(id -u):$(id -g)" --volume "$HOME/catkin_ws:$HOME/catkin_ws" \
  --volume "$HOME/lab11_prep:$HOME/lab11_prep:ro" --workdir "$HOME/catkin_ws" \
  --env "HOME=$HOME" --env ROS_HOME=/tmp/robotics-ros --env ROS_MASTER_URI=http://127.0.0.1:11311 --env ROS_IP=127.0.0.1 \
  --env GAZEBO_MODEL_DATABASE_URI= --env TURTLEBOT3_MODEL=burger "$image" \
  bash -c 'source /opt/ros/noetic/setup.bash && exec bash --noprofile --norc -i'
