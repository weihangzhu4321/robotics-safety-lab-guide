# 仅供本包 Docker 容器内的交互式 bash 使用，不改 Ubuntu 的 bashrc。
source /opt/ros/noetic/setup.bash
if [[ -f /lab4/ws/devel/setup.bash ]]; then
  source /lab4/ws/devel/setup.bash
fi
case "${ROS_MASTER_URI:-}" in
  http://127.0.0.1:11441)
    export PS1='[实验四 仿真 SIM] \w\$ '
    printf '\n【仿真 SIM】当前连接 Gazebo 虚拟车，实体车不会响应。\n'
    ;;
  *)
    export PS1='[实验四 实车待核验 REAL] \w\$ '
    printf '\n【实车待核验 REAL】先按07检查真实相机与底盘；进入shell不等于连接成功。\n'
    ;;
esac
printf 'ROS=%s master=%s ROS_IP=%s model=%s\n' "$ROS_DISTRO" "$ROS_MASTER_URI" "${ROS_IP:-未设置}" "$TURTLEBOT3_MODEL"
