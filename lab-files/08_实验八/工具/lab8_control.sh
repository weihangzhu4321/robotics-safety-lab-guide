#!/usr/bin/env bash
# Experiment 8 controller entry point. Run as the ordinary Ubuntu user.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
ACTION=${1:-help}
shift || true
IMAGE=robot-labs:noetic-20261022
fail() { printf '错误：%s\n' "$*" >&2; exit 1; }
usage() {
  cat <<'EOF'
实验八控制机 A（Ubuntu 普通用户，不要 sudo bash）
  bash 工具/lab8_control.sh help          本说明，不需 Docker
  bash 工具/lab8_control.sh build         断网构建独立的实验八服务类型
  bash 工具/lab8_control.sh self-check    严格 network=none 的软件与密码学自检
  bash 工具/lab8_control.sh offline-shell 断网 ROS 命令终端；不自动启动节点
  bash 工具/lab8_control.sh shell         当天参数与直连检查后进入现场 ROS 终端
  bash 工具/lab8_control.sh status        仅列本目录、本用户的实验八容器
  bash 工具/lab8_control.sh stop          仅停止上述精确身份匹配的容器

现场先填写本包根目录 settings.env：CHECK_DATE=当天日期、VM_IP、ROBOT_IP。
build、自检与离线终端均为 --network none；不会连接小车。
现场 shell 使用 --network host；本入口不启动 roscore、节点或运动。
密钥接收、加密遥控等命令须按到场操作单手工运行。
容器退出即删除；构建、日志与公钥资料保存在本包 runtime/。
stop 不代表小车已停车；先在发送端/接收端按操作单停止并观察实车。
EOF
}
case "$ACTION" in
  help|-h|--help) usage; exit 0 ;;
  build|self-check|offline-shell|shell|status|stop) ;;
  *) usage; fail "未知动作：$ACTION" ;;
esac
[[ $# -eq 0 ]] || fail '本入口不接收附加命令；进入 shell 后按操作单执行。'
[[ $(uname -s) == Linux ]] || fail '请在控制机 A 的 Ubuntu 终端运行。'
[[ $EUID -ne 0 ]] || fail '请以普通用户运行，不要 sudo bash；需要时仅 Docker 命令请求 sudo。'
command -v docker >/dev/null || fail 'A 没有 Docker；本入口不自动安装或改动旧环境。'
if docker info >/dev/null 2>&1; then
  D=(docker)
else
  sudo -v
  D=(sudo docker)
fi
SCOPE="$(printf '%s:%s' "$ROOT" "$(id -u)" | sha256sum | cut -c1-16)"
PREFIX="lab8-$SCOPE"
LABEL_SCOPE=org.robotlabs.lab8.scope
LABEL_UID=org.robotlabs.lab8.uid
ROLE_PATTERN='(build|self-check|offline-shell|shell)'
owned_ids() {
  local cid scope uid name
  while IFS= read -r cid; do
    [[ -n $cid ]] || continue
    scope=$("${D[@]}" inspect --format "{{index .Config.Labels \"$LABEL_SCOPE\"}}" "$cid" 2>/dev/null) || continue
    uid=$("${D[@]}" inspect --format "{{index .Config.Labels \"$LABEL_UID\"}}" "$cid" 2>/dev/null) || continue
    name=$("${D[@]}" inspect --format '{{.Name}}' "$cid" 2>/dev/null) || continue
    if [[ $scope == "$SCOPE" && $uid == "$(id -u)" && $name =~ ^/$PREFIX-$ROLE_PATTERN-[0-9]+$ ]]; then
      printf '%s\n' "$cid"
    fi
  done < <("${D[@]}" ps -aq --no-trunc --filter "label=$LABEL_SCOPE=$SCOPE" --filter "label=$LABEL_UID=$(id -u)")
}
if [[ $ACTION == status || $ACTION == stop ]]; then
  mapfile -t IDS < <(owned_ids)
  if [[ ${#IDS[@]} -eq 0 ]]; then
    printf '本目录、本用户没有实验八容器。\n'
    exit 0
  fi
  if [[ $ACTION == status ]]; then
    for cid in "${IDS[@]}"; do
      "${D[@]}" inspect --format '{{.Name}} status={{.State.Status}} network={{.HostConfig.NetworkMode}} id={{.Id}}' "$cid"
    done
  else
    for cid in "${IDS[@]}"; do
      "${D[@]}" stop -t 8 "$cid"
    done
    printf '本目录、本用户的实验八容器已停止；请现场另外确认小车停稳。\n'
  fi
  exit 0
fi
"${D[@]}" image inspect "$IMAGE" >/dev/null 2>&1 || fail "缺少既有镜像 $IMAGE；不自动联网拉取或升级。"
mkdir -p "$ROOT/runtime/home/.ros" "$ROOT/runtime/logs" "$ROOT/runtime/catkin_ws/src"
chmod 700 "$ROOT/runtime/home"
NET=none
ROS_MASTER_URI=http://127.0.0.1:11581
ROS_IP=127.0.0.1
if [[ $ACTION == shell ]]; then
  command -v python3 >/dev/null || fail '缺少本机 Python3，无法核对现场参数。'
  command -v ip >/dev/null || fail '缺少 ip 命令，无法核对直连路由。'
  [[ -f "$ROOT/settings.env" ]] || fail '先填写本包根目录 settings.env；不沿用旧实验地址。'
  CHECK_DATE=CHANGE_ME
  VM_IP=CHANGE_ME
  ROBOT_IP=CHANGE_ME
  ROBOT_USER=pi
  MODEL=burger
  # Parse only simple key=value records; never evaluate shell substitutions.
  while IFS= read -r line || [[ -n $line ]]; do
    line=${line%$'\r'}
    [[ $line =~ ^[[:space:]]*(#.*)?$ ]] && continue
    [[ $line =~ ^(CHECK_DATE|VM_IP|ROBOT_IP|ROBOT_USER|MODEL)=([A-Za-z0-9._:/-]+)$ ]] || fail 'settings.env 仅允许列出的 KEY=value 简单赋值；不要引号、空格或密码。'
    key=${BASH_REMATCH[1]}
    value=${BASH_REMATCH[2]}
    printf -v "$key" '%s' "$value"
  done < "$ROOT/settings.env"
  [[ $CHECK_DATE == "$(TZ=Asia/Shanghai date +%F)" ]] || fail 'CHECK_DATE 不是中国时区今天；到场重新核实地址后填写当天 YYYY-MM-DD。'
  [[ $ROBOT_USER =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ ]] || fail 'ROBOT_USER 格式不正确。'
  [[ $MODEL == burger || $MODEL == waffle || $MODEL == waffle_pi ]] || fail 'MODEL 应为 burger、waffle 或 waffle_pi。'
  python3 - "$VM_IP" "$ROBOT_IP" <<'PY'
import ipaddress, json, subprocess, sys
vm, robot = [ipaddress.IPv4Address(x) for x in sys.argv[1:]]
rfc1918 = [ipaddress.ip_network(x) for x in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
if vm == robot or not all(any(ip in net for net in rfc1918) for ip in (vm, robot)):
    raise SystemExit("VM_IP / ROBOT_IP 应为不同的实验局域网 RFC1918 地址。")
interfaces = json.loads(subprocess.check_output(["ip", "-j", "-4", "addr", "show"], text=True))
local = [(iface, a) for iface in interfaces for a in iface.get("addr_info", []) if a.get("local") == str(vm)]
if not local:
    raise SystemExit("VM_IP 不属于当前 Ubuntu；不要填 Windows 地址。")
local_ips = {a.get("local") for iface in interfaces for a in iface.get("addr_info", [])}
if str(robot) in local_ips:
    raise SystemExit("ROBOT_IP 是本机地址，不能当作小车。")
routes = json.loads(subprocess.check_output(["ip", "-j", "-4", "route", "get", str(robot)], text=True))
if len(routes) != 1:
    raise SystemExit("到小车的路由不唯一；先检查桥接。")
route = routes[0]
if route.get("gateway") or route.get("type") in ("local", "broadcast", "multicast", "unreachable", "blackhole", "prohibit"):
    raise SystemExit("小车必须在直连实验网段，禁止经网关或使用本机/广播路由。")
if route.get("prefsrc", route.get("src")) != str(vm):
    raise SystemExit("到小车的实际出口源地址与 VM_IP 不一致。")
matching = [(iface, a) for iface, a in local if iface.get("ifname") == route.get("dev")]
if len(matching) != 1:
    raise SystemExit("VM_IP 不在到小车的实际出接口上。")
iface, addr = matching[0]
network = ipaddress.ip_network(str(vm) + "/" + str(addr["prefixlen"]), strict=False)
if robot not in network or robot in (network.network_address, network.broadcast_address):
    raise SystemExit("ROBOT_IP 不在该出接口的可用直连地址范围。")
if iface.get("operstate") != "UP" or "UP" not in iface.get("flags", []):
    raise SystemExit("到小车的接口链路未 UP；检查 VMnet0 / WLAN / 桥接。")
print("NETWORK_PARAMETER_CHECK_PASS VM=" + str(vm) + " ROBOT=" + str(robot) + " interface=" + route["dev"])
PY
  NET=host
  ROS_MASTER_URI="http://$ROBOT_IP:11311"
  ROS_IP=$VM_IP
fi
NAME="$PREFIX-$ACTION-$$"
ARGS=(--rm --init --network "$NET" --user "$(id -u):$(id -g)"
  --label "$LABEL_SCOPE=$SCOPE" --label "$LABEL_UID=$(id -u)"
  --name "$NAME" --workdir /lab8
  -e HOME=/lab8/runtime/home -e PYTHONPATH=/lab8/vendor:/lab8/工具
  -e PYTHONDONTWRITEBYTECODE=1 -e DISABLE_ROS1_EOL_WARNINGS=1
  -e ROS_MASTER_URI="$ROS_MASTER_URI" -e ROS_IP="$ROS_IP"
  --mount "type=bind,source=$ROOT,target=/lab8")
if [[ $ACTION == shell || $ACTION == offline-shell ]]; then
  [[ -t 0 && -t 1 ]] || fail 'shell 需要交互终端；离线自动验收请用 self-check。'
  [[ -s "$ROOT/runtime/catkin_ws/devel/setup.bash" ]] || fail '先运行 build 生成实验八服务类型。'
  ARGS+=(-it)
else
  ARGS+=(-i)
fi
LOG="$ROOT/runtime/logs/${ACTION}_$(TZ=Asia/Shanghai date +%Y%m%d_%H%M%S)_$$.log"
printf '动作=%s network=%s 镜像=%s\n' "$ACTION" "$NET" "$IMAGE"
printf '精确容器名=%s 日志=%s\n' "$NAME" "$LOG"
if [[ $ACTION == shell || $ACTION == offline-shell ]]; then
  printf 'action=%s network=%s image=%s container=%s master=%s ros_ip=%s\n' "$ACTION" "$NET" "$IMAGE" "$NAME" "$ROS_MASTER_URI" "$ROS_IP" > "$LOG"
  printf '此日志只记入口参数；节点输出请按操作单另行保存。\n'
  "${D[@]}" run "${ARGS[@]}" "$IMAGE" bash -c '
    set -eo pipefail
    unset ROS_HOSTNAME
    source /opt/ros/noetic/setup.bash
    source /lab8/runtime/catkin_ws/devel/setup.bash
    printf "实验八终端就绪：master=%s ROS_IP=%s\n" "$ROS_MASTER_URI" "$ROS_IP"
    printf "未启动任何节点。根据操作单手工运行 receive-key / teleop；退出输入 exit。\n"
    exec bash --noprofile --norc -i
  '
else
  "${D[@]}" run "${ARGS[@]}" "$IMAGE" bash -s -- "$ACTION" <<'CONTAINER' 2>&1 | tee "$LOG"
set -eo pipefail
unset ROS_HOSTNAME
source /opt/ros/noetic/setup.bash
python3 - <<'PY'
import os
names = sorted(os.listdir("/sys/class/net"))
if names != ["lo"]:
    raise SystemExit("离线容器存在非 loopback 网卡：" + repr(names))
with open("/proc/net/route", encoding="ascii") as f:
    routes = f.read().splitlines()[1:]
if any(line.split()[1] == "00000000" for line in routes if line.split()):
    raise SystemExit("离线容器有默认路由，停止。")
print("NETWORK_NONE_VERIFIED interfaces=lo no_default_route=true")
PY
if [[ $1 == build ]]; then
  src=/lab8/源码/revised_catkin/lab8_secure
  dest=/lab8/runtime/catkin_ws/src/lab8_secure
  [[ -s $src/package.xml && -s $src/CMakeLists.txt ]] || { echo '缺少实验八服务源码。' >&2; exit 1; }
  if [[ -e $dest || -L $dest ]]; then
    [[ -L $dest && $(readlink -- "$dest") == "$src" ]] || { echo '实验八 workspace 存在不同包；请人工检查，不覆盖。' >&2; exit 1; }
  else
    ln -s -- "$src" "$dest"
  fi
  cd /lab8/runtime/catkin_ws
  catkin_make -j2 -l2 -DPYTHON_EXECUTABLE=/usr/bin/python3
  source devel/setup.bash
  python3 -c 'from lab8_secure.srv import key, keyRequest, keyResponse; print("LAB8_CATKIN_BUILD_PASS")'
else
  [[ -s /lab8/runtime/catkin_ws/devel/setup.bash ]] || { echo '先运行 build。' >&2; exit 1; }
  source /lab8/runtime/catkin_ws/devel/setup.bash
  python3 - <<'PY'
import ast, pathlib, sys
import rospy, std_msgs.msg, geometry_msgs.msg
from lab8_secure.srv import key, keyRequest, keyResponse
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_v1_5
for name in ("lab8.py", "lab8_ros.py"):
    path = pathlib.Path("/lab8/工具") / name
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
print("ROS_CRYPTO_IMPORT_PASS python=" + sys.version.split()[0])
PY
  python3 /lab8/工具/lab8.py self-test
  python3 /lab8/工具/lab8_ros.py --help >/dev/null
  echo LAB8_CONTROL_SELF_CHECK_PASS
fi
CONTAINER
fi
