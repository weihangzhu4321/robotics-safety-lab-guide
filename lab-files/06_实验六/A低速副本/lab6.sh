#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ACTION=${1:-help}; shift || true
IMAGE=robot-labs:noetic-20261022
fail() { printf '错误：%s\n' "$*" >&2; exit 1; }
usage() {
cat <<'EOF'
机器人与安全实验一至三（在 Ubuntu 图形桌面终端运行）
  bash install-host.sh       首次联网安装 Docker / SSH / 显示工具
  bash load-image.sh         校验并导入本包的离线 ROS1 镜像
  bash lab.sh health         离线软件检查；不会连接小车
  bash lab.sh sim            实验一 turtlesim（保持该终端运行）
  bash lab.sh sim-shell      实验一新命令终端，已 source Noetic
  bash lab.sh circle         实验一 1Hz 圆周发布程序，TURTLE_NAME 默认 turtle1
  bash lab.sh plot           实验一 rqt_plot
  bash lab.sh graph          实验一 rqt_graph
  bash lab.sh net            检查到小车的路由及 SSH/ROS 端口
  bash lab.sh doctor         只读检查小车 ROS、节点与电源状态
  bash lab.sh prepare        按需启动小车 master/驱动（不发布速度）
  bash lab.sh sync-clock     认证后校准小车时间（不发布速度）
  bash lab.sh check          20秒传感器/时间戳检查（不发布速度）
  bash lab.sh view           实验二 RViz，固定坐标系 odom
  bash lab.sh teleop         低速按住行驶、松开停车，实验二/三共用
  bash lab.sh slam           实验三 gmapping 建图，创建本次数据目录
  bash lab.sh save           停车并关闭遥控后保存地图，SLAM 保持运行
  bash lab.sh nav [data/目录/map.yaml]  关闭SLAM/遥控/view后启动导航
  bash lab.sh cancel         显式取消导航并发送零速度
  bash lab.sh stop           停止本套件电脑端容器；小车驱动保留
  bash lab.sh status         查看本套件容器状态
  bash lab.sh files          列出地图/CSV/日志
  bash lab.sh shell          真实小车 ROS1 命令终端
  bash lab.sh robot-shell    SSH 进入小车（自行输入密码）
所有 GUI 命令在各自终端保持运行。不要 sudo bash lab.sh。
EOF
}
[[ $ACTION == help ]] && { usage; exit 0; }
[[ $EUID -ne 0 ]] || fail '请以普通用户运行，不要 sudo bash lab.sh。脚本会在需要时请求 Ubuntu sudo 密码。'
ROBOT_IP=CHANGE_ME; ROBOT_USER=pi; MODEL=burger; ROBOT_ROS_DISTRO=auto; ROBOT_WS=auto; VM_IP=; STUDENT_ID=U_REPLACE_ME
if [[ -f "$ROOT/settings.env" ]]; then
  # settings.env is your own shell-format configuration. Never put passwords in it.
  source "$ROOT/settings.env"
fi
export ROBOT_IP ROBOT_USER MODEL ROBOT_ROS_DISTRO ROBOT_WS STUDENT_ID
mkdir -p "$ROOT/data" "$ROOT/runtime/home/.ros" "$ROOT/runtime/logs"
PREFIX="robotlabs-$(printf '%s' "$ROOT" | sha256sum | cut -c1-8)"
if [[ $ACTION == files ]]; then
  find "$ROOT/data" "$ROOT/runtime/logs" -type f | sort
  exit 0
fi
command -v docker >/dev/null || fail '没有 Docker，先运行 bash install-host.sh。'
if docker info >/dev/null 2>&1; then D=(docker); else sudo -v; D=(sudo docker); fi
running() { "${D[@]}" ps --format '{{.Names}}' | grep -Fxq "$PREFIX-$1"; }
[[ $ACTION == status ]] && { "${D[@]}" ps -a --filter "name=$PREFIX-"; exit 0; }
"${D[@]}" image inspect "$IMAGE" >/dev/null 2>&1 || fail '没有实验镜像，请先运行 bash load-image.sh。'
NET_MODE=host
[[ $ACTION != health ]] || NET_MODE=none
ARGS=(--rm --init --network "$NET_MODE" --ipc host --user "$(id -u):$(id -g)"
  -e HOME=/home/labuser -e TURTLEBOT3_MODEL="$MODEL"
  -e STUDENT_ID="$STUDENT_ID" -e DATA_DIR=/lab/data -e DISABLE_ROS1_EOL_WARNINGS=1
  -v "$ROOT:/lab" -v "$ROOT/runtime/home:/home/labuser")
gui() {
  [[ -n ${DISPLAY:-} ]] || fail '没有 DISPLAY，请在 Ubuntu 图形桌面的终端运行。'
  local auth=${XAUTHORITY:-}
  [[ -f $auth ]] || auth="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/gdm/Xauthority"
  [[ -f $auth ]] || auth="$HOME/.Xauthority"
  [[ -f $auth ]] || fail '找不到 Xauthority。请解锁 Ubuntu 桌面并在本地终端重试。'
  local output="$ROOT/runtime/Xauthority"
  : > "$output"; chmod 600 "$output"
  xauth -f "$auth" nlist "$DISPLAY" | sed 's/^..../ffff/' | xauth -f "$output" nmerge -
  [[ -s $output ]] || fail '显示授权为空，请在 Ubuntu 本地图形终端重试。'
  ARGS+=(-e DISPLAY="$DISPLAY" -e XAUTHORITY=/tmp/robotlabs.Xauthority
    -e QT_X11_NO_MITSHM=1 -e LIBGL_ALWAYS_SOFTWARE=1 -e LIBGL_DRI3_DISABLE=1
    -e GALLIUM_DRIVER=softpipe -e QT_XCB_GL_INTEGRATION=none
    -v /tmp/.X11-unix:/tmp/.X11-unix:ro -v "$output:/tmp/robotlabs.Xauthority:ro")
}
run_ros() {
  local role=$1; shift
  "${D[@]}" run "${ARGS[@]}" --name "$PREFIX-$role" "$IMAGE" \
    bash -c 'unset ROS_HOSTNAME; source /opt/ros/noetic/setup.bash; exec "$@"' _ "$@"
}
robot_env() {
  [[ $ROBOT_IP =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail '先将 settings.env 中 ROBOT_IP 改成现场小车 IPv4 地址。'
  [[ $ROBOT_USER =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ ]] || fail 'ROBOT_USER 格式不正确。'
  [[ $MODEL == burger || $MODEL == waffle || $MODEL == waffle_pi ]] || fail 'MODEL 应为 burger、waffle 或 waffle_pi。'
  PC_IP=${VM_IP:-$(ip -4 route get "$ROBOT_IP" | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' | head -n1)}
  python3 - "$ROBOT_IP" "$PC_IP" <<'PY'
import ipaddress,sys
robot,pc=map(ipaddress.IPv4Address,sys.argv[1:])
if pc.is_loopback or pc.is_unspecified or pc == robot:
    raise SystemExit('Ubuntu VM 地址无效；检查 VMware 桥接和 VM_IP。')
PY
  ip -4 -o addr show | awk '{split($4,a,"/");print a[1]}' | grep -Fxq "$PC_IP" || fail "VM_IP=$PC_IP 不属于当前 Ubuntu，不能填写 Windows 地址。"
  export ROS_MASTER_URI="http://$ROBOT_IP:11311" ROS_IP="$PC_IP"
  ARGS+=(-e ROS_MASTER_URI="$ROS_MASTER_URI" -e ROS_IP="$ROS_IP")
  echo "Ubuntu VM=$PC_IP 小车=$ROBOT_IP ROS master=$ROS_MASTER_URI"
}
sim_exec() {
  running sim || fail '先在另一个终端运行 bash lab.sh sim，并保持窗口。'
  "${D[@]}" exec "$@" "$PREFIX-sim" bash -c 'unset ROS_HOSTNAME; source /opt/ros/noetic/setup.bash; exec "$@"' _ "${SIM_CMD[@]}"
}
check_data() { run_ros check timeout -k 3 40 python3 /lab/scripts/check_sensors.py; }
controller_check() { run_ros publishers timeout -k 2 10 python3 /lab/scripts/check_publishers.py; }
close_role() { running "$1" && fail "$1 仍在运行，请在其终端 Ctrl+C 或运行 bash lab.sh stop。"; return 0; }
read_run() {
  [[ -f "$ROOT/runtime/current_run.txt" ]] || fail '尚无建图目录；先 slam，或给 nav 指定 data/目录/map.yaml。'
  RUN=$(cat "$ROOT/runtime/current_run.txt")
  [[ $RUN =~ ^[0-9]{8}_[0-9]{6}(_[0-9]+)?$ ]] || fail 'current_run 内容无效。'
}
case "$ACTION" in
  health)
    # No robot and no external network are used here.
    ARGS+=(-e ROS_MASTER_URI=http://127.0.0.1:11431 -e ROS_IP=127.0.0.1)
    run_ros health bash -c 'set -e; for p in turtlesim rqt_plot rqt_graph catkin turtlebot3_slam turtlebot3_navigation gmapping amcl map_server move_base rviz; do rospack find "$p"; done; command -v catkin_make catkin_create_pkg; python3 -m compileall -q /lab/scripts /lab/lab1_addon; echo SOFTWARE_CHECK_PASS'
    ;;
  sim)
    close_role sim; gui
    ARGS+=(-e ROS_MASTER_URI=http://127.0.0.1:11431 -e ROS_IP=127.0.0.1)
    run_ros sim bash -c 'roscore -p 11431 > /lab/runtime/logs/sim-master.log 2>&1 & master=$!; trap "kill $master 2>/dev/null || true" EXIT; for i in {1..50}; do rosparam list >/dev/null 2>&1 && break; sleep .2; done; rosrun turtlesim turtlesim_node'
    ;;
  sim-shell) SIM_CMD=(bash --norc -i); sim_exec -it ;;
  circle) SIM_CMD=(python3 /lab/lab1_addon/circle_publisher.py); sim_exec -e "TURTLE_NAME=${TURTLE_NAME:-turtle1}" ;;
  plot) SIM_CMD=(rosrun rqt_plot rqt_plot); sim_exec ;;
  graph) SIM_CMD=(rosrun rqt_graph rqt_graph); sim_exec ;;
  stop)
    if running navigation; then
      (robot_env; run_ros cancel timeout -k 2 6 python3 /lab/scripts/cancel.py) || echo '取消请求未确认，继续停止本套件电脑端；请现场确认小车停稳。'
    fi
    for role in teleop navigation slam view sim; do
      if running "$role"; then "${D[@]}" stop -t 12 "$PREFIX-$role"; fi
    done
    echo '本套件电脑端已停止。请观察小车停稳；小车 master/驱动仍保留。'
    ;;
  *)
    robot_env
    case "$ACTION" in
      net)
        ip -4 route get "$ROBOT_IP"
        python3 - "$ROBOT_IP" <<'PY'
import socket,sys
failed=False
for port,label in [(22,'SSH'),(11311,'ROS master')]:
    try:
        with socket.create_connection((sys.argv[1],port),5): pass
        print(f'{label} :{port} 可达')
    except OSError as exc:
        failed=True; print(f'{label} :{port} 不可达：{exc}')
if failed:
    print('22不可达先处理桥接/供电/IP；仅11311不通时继续doctor/prepare。')
    sys.exit(1)
PY
        ;;
      doctor|prepare)
        [[ $ROBOT_ROS_DISTRO =~ ^(auto|kinetic|melodic|noetic)$ ]] || fail 'ROBOT_ROS_DISTRO 无效。'
        [[ $ROBOT_WS == auto || $ROBOT_WS =~ ^/[a-zA-Z0-9_./-]+$ ]] || fail 'ROBOT_WS 应为 auto 或不含空格的 Linux 绝对路径。'
        ssh -o ConnectTimeout=8 -o ServerAliveInterval=5 -o ServerAliveCountMax=3 "$ROBOT_USER@$ROBOT_IP" \
          bash -s -- "$ACTION" "$ROBOT_IP" "$MODEL" "$ROBOT_ROS_DISTRO" "$ROBOT_WS" < "$ROOT/scripts/robot.sh"
        ;;
      sync-clock) python3 "$ROOT/scripts/sync_clock.py" ;;
      check) check_data ;;
      robot-shell) ssh -o ConnectTimeout=8 "$ROBOT_USER@$ROBOT_IP" ;;
      shell) ARGS+=(-it); run_ros shell bash --norc -i ;;
      view)
        close_role slam; close_role navigation; close_role view
        check_data; gui
        run_ros view roslaunch /lab/config/view.launch 2>&1 | tee -a "$ROOT/runtime/logs/view.log"
        ;;
      teleop)
        close_role navigation; close_role teleop
        if ! running view && ! running slam; then fail '先启动 view（实验二）或 slam（实验三），查看雷达与实车。'; fi
        check_data; controller_check; gui
        run_ros teleop python3 /lab/scripts/teleop_lab6_ros1.py 2>&1 | tee -a "$ROOT/runtime/logs/teleop.log"
        ;;
      slam)
        close_role navigation; close_role view; close_role slam
        check_data; gui
        RUN=$(date +%Y%m%d_%H%M%S)_$$
        mkdir -p "$ROOT/data/$RUN"
        printf '%s\n' "$RUN" > "$ROOT/runtime/current_run.txt"
        echo "本次地图目录：$ROOT/data/$RUN"
        run_ros slam roslaunch turtlebot3_slam turtlebot3_slam.launch slam_methods:=gmapping 2>&1 | tee "$ROOT/data/$RUN/slam-start.log"
        ;;
      save)
        running slam || fail '保存地图时请保持 slam 运行。'
        close_role teleop; read_run
        run_ros save timeout 30 rosrun map_server map_saver -f "/lab/data/$RUN/map"
        [[ -s "$ROOT/data/$RUN/map.yaml" && -s "$ROOT/data/$RUN/map.pgm" ]] || fail '地图保存不完整。'
        sed -i 's|^image:.*|image: map.pgm|' "$ROOT/data/$RUN/map.yaml"
        echo "已保存：data/$RUN/map.yaml 和 map.pgm（两者一起保留）。"
        ;;
      nav)
        close_role slam; close_role teleop; close_role view; close_role navigation
        if [[ $# -gt 0 ]]; then
          MAP=$(realpath -e -- "$ROOT/$1") || fail '地图路径不存在。'
        else
          read_run; MAP="$ROOT/data/$RUN/map.yaml"
        fi
        [[ $MAP == "$ROOT/data/"*/map.yaml && -s $MAP && -s "${MAP%/*}/map.pgm" ]] || fail '请指定本包 data/ 下成对的 map.yaml 和 map.pgm。'
        check_data; controller_check; gui
        NAV_RUN="${MAP%/*}/nav_$(date +%Y%m%d_%H%M%S)_$$"
        mkdir -p "$NAV_RUN"
        echo "本次导航记录：$NAV_RUN。先2D Pose Estimate，雷达对齐后再2D Nav Goal。"
        run_ros navigation bash /lab/scripts/navigation_session.sh "/lab/${NAV_RUN#"$ROOT/"}" "/lab/${MAP#"$ROOT/"}" 2>&1 | tee "$NAV_RUN/navigation-start.log"
        ;;
      cancel) run_ros cancel timeout -k 2 6 python3 /lab/scripts/cancel.py ;;
      *) usage; fail "不认识的命令：$ACTION" ;;
    esac
    ;;
esac
