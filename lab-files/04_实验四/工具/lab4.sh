#!/usr/bin/env bash
# 在 Ubuntu 本地图形桌面终端运行；此脚本不自动连接/改写小车，不自动发速度。
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ACTION=${1:-help}
IMAGE=robot-lab4:noetic-prep
fail() { printf '错误：%s\n' "$*" >&2; exit 1; }
case "$ACTION" in
  help)
    cat <<'EOF'
原机已部署：日常不运行 image/build。到场操作实体车请先打开07实车离线操作单。
离线自检：bash lab4.sh check       无网络检查已部署环境、视觉转换和全部测试
仿真终端A：bash lab4.sh sim        开一个仿真容器 shell，随后按指南启动 course.launch
仿真新终端：bash lab4.sh shell-sim 进入同一仿真容器
实车终端A：LAB4_ROBOT_IP=本车实际IPv4 bash lab4.sh real
                                 仅打开实车容器，不启动/改写小车驱动
实车新终端：bash lab4.sh shell-real 进入同一实车容器
停车后清理：bash lab4.sh stop-sim 或 bash lab4.sh stop-real
sim 与 real 不能同时打开。本包使用独立镜像，不修改已有实验三镜像。
新机器安装/维护才用：bash lab4.sh image / bash lab4.sh build（详见01）。
EOF
    exit 0 ;;
  image|build|check|sim|real|shell-sim|shell-real|stop-sim|stop-real) ;;
  *) fail '未知命令；执行 bash lab4.sh help 查看用法。' ;;
esac
if [[ $ACTION == real ]]; then
  ROBOT_IP=${LAB4_ROBOT_IP:-}
  [[ -n $ROBOT_IP ]] || fail '请先核对本车地址：LAB4_ROBOT_IP=本车实际IPv4 bash lab4.sh real；不再默认使用历史地址。'
  [[ $ROBOT_IP =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || fail 'LAB4_ROBOT_IP 必须是现场 IPv4 地址。'
  IFS=. read -r -a octets <<< "$ROBOT_IP"
  for octet in "${octets[@]}"; do
    (( 10#$octet <= 255 )) || fail 'IPv4 每段必须介于0和255。'
  done
  (( 10#${octets[0]} > 0 && 10#${octets[0]} < 224 && 10#${octets[0]} != 127 )) || fail '请填写本车单播地址，不能用回环/组播地址。'
fi
[[ $EUID -ne 0 ]] || fail '不要 sudo bash lab4.sh；需要时脚本自行请求 sudo。'
command -v docker >/dev/null || fail '先确认实验三的 Docker 已安装。'
if docker info >/dev/null 2>&1; then D=(docker); else sudo -v; D=(sudo docker); fi
running() { "${D[@]}" ps --format '{{.Names}}' | grep -Fxq "lab4-$1"; }
if [[ $ACTION == image ]]; then
  BASE_IMAGE=${LAB4_BASE_IMAGE:-lab3-ros1:noetic}
  "${D[@]}" image inspect "$BASE_IMAGE" >/dev/null 2>&1 || fail "缺少基础镜像 $BASE_IMAGE；参见指南的首次准备。"
  "${D[@]}" build --build-arg "BASE_IMAGE=$BASE_IMAGE" -t "$IMAGE" -f "$ROOT/Dockerfile" "$ROOT"
  exit 0
fi
if [[ $ACTION == stop-* ]]; then
  ROLE=${ACTION#stop-}
  if running "$ROLE"; then
    # 应先在 follower 终端 Ctrl+C 并目视确认停稳。只停止自己启动的容器。
    "${D[@]}" stop -t 12 "lab4-$ROLE"
  fi
  printf '本包容器已停止；请确认小车停稳，小车端 master/驱动可能仍运行。\n'
  exit 0
fi
"${D[@]}" image inspect "$IMAGE" >/dev/null 2>&1 || fail '先在有互联网时运行 bash lab4.sh image。'
mkdir -p "$ROOT/runtime/home" "$ROOT/data" "$ROOT/ws/src"
COMMON=(--rm --init --ipc host --user "$(id -u):$(id -g)"
  -e TZ=Asia/Shanghai
  -e HOME=/lab4/runtime/home -e ROS_LOG_DIR=/lab4/data/roslogs
  -e DISABLE_ROS1_EOL_WARNINGS=1 -e GAZEBO_MODEL_DATABASE_URI=
  -v "$ROOT:/lab4" -w /lab4)
if [[ $ACTION == check ]]; then
  [[ -f "$ROOT/ws/devel/setup.bash" ]] || fail '先运行 bash lab4.sh build。'
  "${D[@]}" run "${COMMON[@]}" --network none "$IMAGE" bash -c '
    source /opt/ros/noetic/setup.bash
    source /lab4/ws/devel/setup.bash
    exec python3 /lab4/check_lab4_env.py
  '
  exit 0
fi
if [[ $ACTION == build ]]; then
  "${D[@]}" run "${COMMON[@]}" --network none "$IMAGE" bash -c '
    set -e
    source /opt/ros/noetic/setup.bash
    test -d /lab4/line_follow_lab4
    if [[ ! -e /lab4/ws/src/line_follow_lab4 ]]; then
      ln -s /lab4/line_follow_lab4 /lab4/ws/src/line_follow_lab4
    fi
    chmod +x /lab4/line_follow_lab4/scripts/follower.py
    cd /lab4/ws
    catkin_make
    source devel/setup.bash
    rospack find line_follow_lab4
    python3 -c "import cv2,numpy,rospy,cv_bridge; print(\"VISION_IMPORT_OK\")"
    python3 -m unittest discover -s /lab4/line_follow_lab4/tests -v
  '
  exit 0
fi
if [[ $ACTION == shell-* ]]; then
  ROLE=${ACTION#shell-}
  running "$ROLE" || fail "先在另一个 Ubuntu 终端运行 bash lab4.sh $ROLE。"
  "${D[@]}" exec -it "lab4-$ROLE" bash --noprofile --rcfile /lab4/container_rc.bash -i
  exit 0
fi
[[ -f "$ROOT/ws/devel/setup.bash" ]] || fail '先运行 bash lab4.sh build。'
if running sim || running real; then fail '已有本包会话；请用 shell-sim / shell-real 进入，或先停车收尾。'; fi
[[ -n ${DISPLAY:-} ]] || fail '请在 Ubuntu 本地图形桌面终端执行。'
AUTH=${XAUTHORITY:-}
[[ -f $AUTH ]] || AUTH="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/gdm/Xauthority"
[[ -f $AUTH ]] || AUTH="$HOME/.Xauthority"
[[ -f $AUTH ]] || fail '找不到桌面 Xauthority；请解锁 Ubuntu 桌面。'
: > "$ROOT/runtime/Xauthority"
chmod 600 "$ROOT/runtime/Xauthority"
xauth -f "$AUTH" nlist "$DISPLAY" | sed 's/^..../ffff/' | xauth -f "$ROOT/runtime/Xauthority" nmerge -
[[ -s "$ROOT/runtime/Xauthority" ]] || fail '桌面显示授权为空。'
COMMON+=(-e DISPLAY="$DISPLAY" -e XAUTHORITY=/tmp/lab4.Xauthority
  -e QT_X11_NO_MITSHM=1 -e LIBGL_ALWAYS_SOFTWARE=1 -e LIBGL_DRI3_DISABLE=1
  -e QT_XCB_GL_INTEGRATION=none
  -v /tmp/.X11-unix:/tmp/.X11-unix:ro -v "$ROOT/runtime/Xauthority:/tmp/lab4.Xauthority:ro")
unset ROS_HOSTNAME
if [[ $ACTION == sim ]]; then
  COMMON+=(-e ROS_MASTER_URI=http://127.0.0.1:11441 -e ROS_IP=127.0.0.1
    -e GAZEBO_MASTER_URI=http://127.0.0.1:11445 -e TURTLEBOT3_MODEL=waffle
    -e GALLIUM_DRIVER=llvmpipe -e LP_NUM_THREADS=2)
else
  PC_IP=$(ip -4 route get "$ROBOT_IP" | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' | head -n1)
  [[ -n $PC_IP && $PC_IP != 127.* && $PC_IP != "$ROBOT_IP" ]] || fail '找不到到小车的有效源地址；检查 VMware 桥接。'
  printf '只连接既有 master：电脑=%s 小车=%s。相机与驱动仍须现场验收。\n' "$PC_IP" "$ROBOT_IP"
  COMMON+=(-e "ROS_MASTER_URI=http://$ROBOT_IP:11311" -e "ROS_IP=$PC_IP" -e TURTLEBOT3_MODEL=burger -e GALLIUM_DRIVER=softpipe)
fi
"${D[@]}" run "${COMMON[@]}" --network host --name "lab4-$ACTION" -it "$IMAGE" \
  bash --noprofile --rcfile /lab4/container_rc.bash -i
