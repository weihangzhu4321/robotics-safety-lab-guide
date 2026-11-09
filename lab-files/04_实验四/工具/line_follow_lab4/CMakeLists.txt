cmake_minimum_required(VERSION 3.0.2)
project(line_follow_lab4)
find_package(catkin REQUIRED COMPONENTS rospy sensor_msgs geometry_msgs std_msgs cv_bridge)
catkin_package()
# 仅可执行节点生成catkin relay，辅助模块作为普通文件安装。
# devel空间由follower.py优先加载真实源码同目录；install空间两个文件同目录。
catkin_install_python(PROGRAMS scripts/follower.py
  scripts/prepare_robot_description.py
  DESTINATION ${CATKIN_PACKAGE_BIN_DESTINATION})
install(FILES scripts/vision_control.py DESTINATION ${CATKIN_PACKAGE_BIN_DESTINATION})
install(PROGRAMS scripts/prepare_robot_description.py DESTINATION ${CATKIN_PACKAGE_SHARE_DESTINATION}/scripts)
install(DIRECTORY launch worlds DESTINATION ${CATKIN_PACKAGE_SHARE_DESTINATION})
