#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
ACTION=${1:-help}; shift || true
IMAGE=robot-lab4:noetic-prep
fail() { printf '错误：%s\n' "$*" >&2; exit 1; }
case "$ACTION" in
help|-h|--help)
  cat <<'EOF'
控制机A普通用户入口（不要sudo bash）
 self-check      network none中的ROS/CV导入和工具测试
 ros-check       network none中的合成图像/速度/里程计订阅验收
 gui-check       network none中的2秒GUI测试，不启动ROS
 offline-shell   断网ROS终端，不自动启动节点
 shell           当天LAN参数核对后现场ROS终端，不自动启动节点
 status          只查看本目录、本用户实验九容器
 stop            先实体停车，再只停止精确属于本套件的容器
现场先填settings.env四项；M展示标牌无需Docker/ROS。
原get_num.py及模型未提供，不自动装库或运行未知识别程序。
EOF
  exit 0;;
self-check|ros-check|gui-check|offline-shell|shell|status|stop) ;;
*) fail '未知动作，运行help查看。';;
esac
[[ $# -eq 0 ]] || fail '不接受额外命令参数。'
[[ $EUID -ne 0 ]] || fail '以普通Ubuntu用户运行，不要sudo bash。'
command -v docker >/dev/null || fail '只有A已有Docker；M不要运行此入口。'
if docker info >/dev/null 2>&1;then D=(docker);else sudo -v;D=(sudo docker);fi
SCOPE=$(printf '%s:%s' "$ROOT" "$(id -u)" | sha256sum | cut -c1-16)
PREFIX="lab9-$SCOPE"
owned() {
  local id name scope uid
  while IFS= read -r id;do
    [[ -n $id ]] || continue
    scope=$("${D[@]}" inspect -f '{{index .Config.Labels "org.robotlabs.lab9.scope"}}' "$id") || continue
    uid=$("${D[@]}" inspect -f '{{index .Config.Labels "org.robotlabs.lab9.uid"}}' "$id") || continue
    name=$("${D[@]}" inspect -f '{{.Name}}' "$id") || continue
    if [[ $scope == "$SCOPE" && $uid == "$(id -u)" && $name =~ ^/$PREFIX-(self-check|ros-check|gui-check|offline-shell|shell)-[0-9]+$ ]];then printf '%s\n' "$id";fi
  done < <("${D[@]}" ps -aq --filter "label=org.robotlabs.lab9.scope=$SCOPE" --filter "label=org.robotlabs.lab9.uid=$(id -u)")
}
if [[ $ACTION == status || $ACTION == stop ]];then
  mapfile -t IDS < <(owned)
  for id in "${IDS[@]}";do
    if [[ $ACTION == status ]];then "${D[@]}" inspect -f '{{.Name}} {{.State.Status}} {{.HostConfig.NetworkMode}}' "$id";else "${D[@]}" stop -t 8 "$id";fi
  done
  printf '匹配容器=%s；停止容器不代表实车停车。\n' "${#IDS[@]}"
  exit 0
fi
"${D[@]}" image inspect "$IMAGE" >/dev/null 2>&1 || fail "缺少既有镜像 $IMAGE，不自动拉取升级。"
mkdir -p "$ROOT/runtime/home/.ros" "$ROOT/runtime/logs" "$ROOT/teacher" "$ROOT/runs"
chmod 700 "$ROOT/runtime/home"
NET=none;MASTER=http://127.0.0.1:11991;ROS_ADDR=127.0.0.1
if [[ $ACTION == shell ]];then
  read -r VM_IP ROBOT_IP MASTER_IP < <(python3 "$ROOT/工具/check_network.py" "$ROOT/settings.env")
  [[ -n ${VM_IP:-} && -n ${MASTER_IP:-} ]] || fail '当天网络检查未通过。'
  # Refuse if another course motion session is running; do not stop it.
  if "${D[@]}" ps --format '{{.Names}}' | grep -Eq '^(lab4-|robotlabs-|robot-labs-|lab23-|lab3-|lab8-)';then fail '发现其他课程会话；由原操作者正常结束后再使用。';fi
  NET=host;MASTER="http://$MASTER_IP:11311";ROS_ADDR=$VM_IP
fi
NAME="$PREFIX-$ACTION-$$"
ARGS=(--rm --init --network "$NET" --user "$(id -u):$(id -g)" --name "$NAME"
 --label "org.robotlabs.lab9.scope=$SCOPE" --label "org.robotlabs.lab9.uid=$(id -u)"
 --mount "type=bind,source=$ROOT,target=/lab9"
 --mount "type=bind,source=$ROOT/teacher,target=/lab9/teacher,readonly"
 --workdir /lab9 -e HOME=/lab9/runtime/home -e TZ=Asia/Shanghai
 -e ROS_LOG_DIR=/lab9/runtime/logs -e ROS_MASTER_URI="$MASTER" -e ROS_IP="$ROS_ADDR"
 -e PYTHONDONTWRITEBYTECODE=1 -e DISABLE_ROS1_EOL_WARNINGS=1)
if [[ $ACTION == shell || $ACTION == offline-shell ]];then
 [[ -t 0 && -t 1 ]] || fail 'shell需要Ubuntu交互终端。'
 ARGS+=(-it)
else ARGS+=(-i);fi
if [[ $ACTION == shell || $ACTION == gui-check ]];then
 [[ -n ${DISPLAY:-} && $DISPLAY =~ ^:[0-9]+(\.[0-9]+)?$ ]] || fail '需在Ubuntu本地图形终端运行，Wayland须有XWayland。'
 AUTH=${XAUTHORITY:-}
 [[ -f $AUTH ]] || AUTH="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/gdm/Xauthority"
 [[ -f $AUTH ]] || AUTH="$HOME/.Xauthority"
 [[ -f $AUTH ]] || fail '没有Xauthority，先登录解锁桌面。'
 AUTH_COPY="$ROOT/runtime/Xauthority.$$.tmp"
 trap 'rm -f -- "$AUTH_COPY"' EXIT
 touch "$AUTH_COPY";chmod 600 "$AUTH_COPY"
 xauth -f "$AUTH" nlist "$DISPLAY" | sed 's/^..../ffff/' | xauth -f "$AUTH_COPY" nmerge -
 [[ -s $AUTH_COPY ]] || fail '图形授权为空。'
 ARGS+=(-e DISPLAY="$DISPLAY" -e XAUTHORITY=/tmp/lab9.Xauthority -e QT_X11_NO_MITSHM=1
  -e LIBGL_ALWAYS_SOFTWARE=1 -e QT_XCB_GL_INTEGRATION=none
  --mount type=bind,source=/tmp/.X11-unix,target=/tmp/.X11-unix,readonly
  --mount "type=bind,source=$AUTH_COPY,target=/tmp/lab9.Xauthority,readonly")
fi
LOG="$ROOT/runtime/logs/${ACTION}_$(date +%Y%m%d_%H%M%S)_$$.log"
printf 'action=%s network=%s image=%s container=%s master=%s ros_ip=%s\n' "$ACTION" "$NET" "$IMAGE" "$NAME" "$MASTER" "$ROS_ADDR" | tee "$LOG"
if [[ $ACTION == shell || $ACTION == offline-shell ]];then
 "${D[@]}" run "${ARGS[@]}" "$IMAGE" bash -c 'unset ROS_HOSTNAME; source /opt/ros/noetic/setup.bash; echo "实验九终端就绪；未启动任何节点。工作目录/lab9；teacher只读，工作副本放runtime。"; exec bash --noprofile --norc -i'
else
 "${D[@]}" run "${ARGS[@]}" "$IMAGE" bash -s -- "$ACTION" <<'CONTAINER' 2>&1 | tee -a "$LOG"
set -eo pipefail
unset ROS_HOSTNAME
source /opt/ros/noetic/setup.bash
set -u
python3 - <<'PY'
import os
assert sorted(os.listdir('/sys/class/net')) == ['lo'], 'Nonloopback interface exists'
assert not any(line.split()[1]=='00000000' for line in open('/proc/net/route').read().splitlines()[1:] if line.split()), 'Default route exists'
print('NETWORK_NONE_VERIFIED')
PY
case "$1" in
self-check)
 python3 -c 'import cv2,numpy,rospy,cv_bridge,sensor_msgs.msg,geometry_msgs.msg,nav_msgs.msg; print("ROS_CV_IMPORT_PASS",cv2.__version__,numpy.__version__)'
 python3 -m unittest discover -s 测试 -v
 ;;
ros-check) python3 测试/ros_integration.py ;;
gui-check)
 python3 - <<'PY'
import cv2,numpy as np
cv2.imshow('Lab9 offline GUI check',np.full((180,420,3),230,np.uint8));cv2.waitKey(2000);cv2.destroyAllWindows();print('GUI_CHECK_PASS')
PY
 ;;
esac
CONTAINER
fi
