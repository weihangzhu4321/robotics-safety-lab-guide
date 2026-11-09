#!/usr/bin/env bash
# Run on the robot via SSH stdin. This script never sends a velocity command.
# bash -s -- doctor|prepare ROBOT_IP MODEL ROBOT_ROS_DISTRO ROBOT_WS
set -euo pipefail
action=${1:-doctor}
robot_ip=${2:-}
model=${3:-burger}
requested_distro=${4:-auto}
requested_ws=${5:-auto}
fail() { printf '错误：%s\n' "$*" >&2; exit 1; }
case "$action" in doctor|prepare) ;; *) fail '仅支持 doctor 或 prepare。' ;; esac
[[ "$robot_ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || fail 'ROBOT_IP 必须是小车的 IPv4 地址。'
IFS=. read -r -a octets <<< "$robot_ip"
for octet in "${octets[@]}"; do ((10#$octet <= 255)) || fail 'ROBOT_IP 格式无效。'; done
case "$model" in burger|waffle|waffle_pi) ;; *) fail 'MODEL 必须为 burger、waffle 或 waffle_pi。' ;; esac
command -v timeout >/dev/null || fail '小车缺少 timeout 命令。'
command -v ip >/dev/null || fail '小车缺少 ip 命令。'
ip -o -4 addr show | awk '{sub(/\/.*/, "", $4); print $4}' | grep -Fxq "$robot_ip" || fail "本机未持有 $robot_ip；请核对连接的是哪台小车，不继续启动。"

if [ "$requested_distro" = auto ]; then
  available=()
  for candidate in kinetic melodic noetic; do
    [ ! -f "/opt/ros/$candidate/setup.bash" ] || available+=("$candidate")
  done
  [ "${#available[@]}" -eq 1 ] || fail '无法唯一确定小车 ROS1 版本；请将 ROBOT_ROS_DISTRO 改成实际版本。'
  distro=${available[0]}
else
  case "$requested_distro" in kinetic|melodic|noetic) distro=$requested_distro ;; *) fail 'ROBOT_ROS_DISTRO 应为 auto、kinetic、melodic 或 noetic。' ;; esac
fi
[ -f "/opt/ros/$distro/setup.bash" ] || fail "找不到 /opt/ros/$distro/setup.bash；本套件不向小车自动安装 ROS。"
set +u
source "/opt/ros/$distro/setup.bash"
set -u
if [ "$requested_ws" = auto ]; then
  workspace=''
  [ ! -f "$HOME/catkin_ws/devel/setup.bash" ] || workspace="$HOME/catkin_ws"
else
  [[ "$requested_ws" = /* ]] || fail 'ROBOT_WS 必须为 auto 或小车上的绝对路径。'
  workspace=$requested_ws
  [ -f "$workspace/devel/setup.bash" ] || fail "找不到 $workspace/devel/setup.bash。"
fi
if [ -n "$workspace" ]; then
  set +u
  source "$workspace/devel/setup.bash"
  set -u
fi
export ROS_MASTER_URI=http://127.0.0.1:11311 ROS_IP="$robot_ip" TURTLEBOT3_MODEL="$model"
unset ROS_HOSTNAME
printf '小车 ROS1=%s；工作空间=%s；型号=%s；地址=%s\n' "$distro" "${workspace:-使用系统安装包}" "$model" "$robot_ip"
log_dir=''
make_log_dir() {
  [ -n "$log_dir" ] || log_dir=$(mktemp -d "/tmp/robot-labs-${UID}.XXXXXX")
}
if ! timeout 6 rosnode list >/dev/null 2>&1; then
  [ "$action" = prepare ] || fail 'ROS master 未响应；核对小车状态后运行 prepare。'
  if pgrep -f '(^|/)(rosmaster|roscore)([[:space:]]|$)' >/dev/null; then
    fail '存在 ROS master 进程但未响应；保留现场日志检查，不重复启动。'
  fi
  make_log_dir
  nohup roscore >"$log_dir/master.log" 2>&1 </dev/null &
  for attempt in $(seq 1 15); do
    if timeout 2 rosnode list >/dev/null 2>&1; then break; fi
    sleep 1
  done
  timeout 5 rosnode list >/dev/null 2>&1 || fail "master 未就绪，查看 $log_dir/master.log。"
fi
nodes=$(timeout 6 rosnode list) || fail '读取节点失败。'
topics=$(timeout 6 rostopic list) || fail '读取话题失败。'
driver_seen=0
if printf '%s\n' "$nodes" | grep -Eq '^/(turtlebot3_core|turtlebot3_lds|hlds_laser_publisher)$'; then driver_seen=1; fi
if printf '%s\n' "$topics" | grep -Eq '^/(scan|odom)$'; then driver_seen=1; fi
if pgrep -f '[r]oslaunch.*turtlebot3.*(robot|bringup)|[t]urtlebot3_core|[h]lds_laser_publisher|[l]ds_driver' >/dev/null; then driver_seen=1; fi
if [ "$driver_seen" -eq 0 ] && [ "$action" = prepare ]; then
  rospack find turtlebot3_bringup >/dev/null 2>&1 || fail '小车缺少 turtlebot3_bringup；核对该车已有驱动，不自动换包。'
  timeout 10 roslaunch --files turtlebot3_bringup turtlebot3_robot.launch >/dev/null 2>&1 || fail '驱动 launch 或依赖无法解析；请核对该车雷达型号和原驱动包。'
  make_log_dir
  nohup roslaunch turtlebot3_bringup turtlebot3_robot.launch >"$log_dir/bringup.log" 2>&1 </dev/null &
  sleep 8
elif [ "$driver_seen" -eq 1 ]; then
  echo '已有驱动节点、话题或进程，保留现有实例；不重复启动、不终止其他程序。'
fi
echo 'ROBOT_NODES'
timeout 6 rosnode list || fail 'master 再次失去响应。'
echo 'SCAN_HEADER'
scan_ok=0
if timeout 8 rostopic echo -n 1 /scan/header; then scan_ok=1; fi
echo 'ODOM_HEADER'
odom_ok=0
if timeout 8 rostopic echo -n 1 /odom/header; then odom_ok=1; fi
if command -v vcgencmd >/dev/null; then
  echo 'ROBOT_POWER_STATUS'
  vcgencmd get_throttled || true
fi
[ -z "$log_dir" ] || printf '本套件新启动进程的日志：%s\n' "$log_dir"
if [ "$scan_ok" -ne 1 ] || [ "$odom_ok" -ne 1 ]; then
  fail 'scan 或 odom 无新数据；可能只有部分驱动启动。不要重复 prepare，先查看节点、供电、雷达与驱动日志。'
fi
echo 'ROBOT_CHECK_PASS：雷达和里程计有新数据；仍需在电脑执行时间戳与 TF 检查。未发送行驶指令。'
