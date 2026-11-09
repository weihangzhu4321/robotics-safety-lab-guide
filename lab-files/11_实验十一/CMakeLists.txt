cmake_minimum_required(VERSION 3.0.2)
project(lab11_formation)

find_package(catkin REQUIRED COMPONENTS
  actionlib
  actionlib_msgs
  gazebo_msgs
  gazebo_ros
  geometry_msgs
  move_base_msgs
  robot_state_publisher
  rospy
  std_msgs
  turtlebot3_description
  turtlebot3_gazebo
  xacro
)

catkin_package(CATKIN_DEPENDS
  actionlib actionlib_msgs gazebo_msgs gazebo_ros geometry_msgs move_base_msgs
  robot_state_publisher rospy std_msgs turtlebot3_description turtlebot3_gazebo xacro
)

catkin_install_python(PROGRAMS
  scripts/formation_sim.py
  scripts/course_move_base.py
  DESTINATION ${CATKIN_PACKAGE_BIN_DESTINATION}
)

install(DIRECTORY launch worlds
  DESTINATION ${CATKIN_PACKAGE_SHARE_DESTINATION}
)
