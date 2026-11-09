ARG BASE_IMAGE=lab3-ros1:noetic
FROM ${BASE_IMAGE}
USER root
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    ros-noetic-cv-bridge ros-noetic-image-transport \
    ros-noetic-compressed-image-transport ros-noetic-rqt-image-view \
    ros-noetic-gazebo-ros-pkgs ros-noetic-turtlebot3-gazebo \
    ros-noetic-turtlebot3-description ros-noetic-robot-state-publisher \
    python3-opencv python3-numpy xauth \
 && apt-get clean
# 供无桌面登录时的独立显示仿真验收；普通使用仍显示在Ubuntu桌面。
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential xvfb x11-utils scrot \
 && apt-get clean
WORKDIR /lab4
CMD ["/bin/bash"]
