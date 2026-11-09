#!/usr/bin/env bash
# 在 lab4-real 容器内运行；只读检查，不发布速度、不启动驱动、不改参数。
set -uo pipefail

if [[ ${ROS_DISTRO:-} != noetic ]]; then
  echo '请先在 Ubuntu 桌面终端进入 bash lab4.sh real，再在容器内运行本脚本。' >&2
  exit 2
fi
if [[ ! ${ROS_MASTER_URI:-} =~ ^http://([0-9]+\.){3}[0-9]+:11311/?$ ]] ||
   [[ ${ROS_MASTER_URI:-} == http://127.* ]]; then
  echo '当前不是实车 IPv4:11311 master；请退出仿真，核对本车地址后进入 real。' >&2
  exit 2
fi
for tool in timeout rostopic rosnode rosparam; do
  command -v "$tool" >/dev/null || { echo "缺少 $tool；请使用已部署的 Noetic 容器。" >&2; exit 2; }
done
mkdir -p /lab4/data
RUN_DIR=$(mktemp -d "/lab4/data/real_camera_$(date +%Y%m%d_%H%M%S)_XXXXXX") || exit 2
exec > >(tee "$RUN_DIR/real_camera_check.log") 2>&1
printf '记录目录：%s\n时间：%s\nROS_MASTER_URI=%s\nROS_IP=%s\n' \
  "$RUN_DIR" "$(date --iso-8601=seconds)" "$ROS_MASTER_URI" "${ROS_IP:-未设置}"
echo '本脚本只读检查。超时返回124表示本项到达采样时限；查看实际接收内容。'

record() {
  local seconds=$1
  shift
  printf '\n执行：'; printf '%q ' "$@"; printf '\n'
  timeout --signal=INT --kill-after=2s "${seconds}s" "$@"
  local status=$?
  printf '本项退出码：%s\n' "$status"
  return "$status"
}

record 6 rosnode list || { echo '无法读取实车 master。先查 Wi-Fi、桥接、本车地址和 master；不继续运动。'; exit 3; }
record 4 rosparam get /use_sim_time
echo '实车 use_sim_time 应为 false；参数不存在也表示默认 false。若为 true，停在这里检查连接。'
record 6 rostopic list -v
record 5 rostopic info /cmd_vel
echo '运动前 /cmd_vel 不应有其他速度发布者；本脚本不会停止任何节点。'

if [[ -n ${1:-} ]]; then
  IMAGE_TOPIC=$1
else
  if ! timeout --signal=INT --kill-after=2s 5s rostopic find sensor_msgs/Image > "$RUN_DIR/raw_image_topics.txt"; then
    echo '未能读取 raw 图像话题；请保留本日志。'; exit 4
  fi
  mapfile -t topics < <(sed '/^[[:space:]]*$/d' "$RUN_DIR/raw_image_topics.txt")
  if [[ ${#topics[@]} != 1 ]]; then
    echo 'raw 图像候选如下（空白表示没有）：'; cat "$RUN_DIR/raw_image_topics.txt"
    echo '多个候选：按07操作单指定实际话题重跑；没有raw：查看上方是否只有CompressedImage，或驱动未启动。'
    exit 4
  fi
  IMAGE_TOPIC=${topics[0]}
fi
TYPE=$(timeout --signal=INT --kill-after=2s 5s rostopic type "$IMAGE_TOPIC")
if [[ $TYPE != sensor_msgs/Image ]]; then
  printf '话题 %s 类型为 %s，当前 follower 需要 sensor_msgs/Image。\n' "$IMAGE_TOPIC" "$TYPE"
  exit 4
fi
printf '\n本次图像话题：%s\n' "$IMAGE_TOPIC"
record 5 rostopic info "$IMAGE_TOPIC"
record 6 rostopic echo -n 1 "$IMAGE_TOPIC/header"
record 8 rostopic hz "$IMAGE_TOPIC"
record 8 rostopic delay "$IMAGE_TOPIC"
echo
echo '检查结束，尚未开启循线。请读日志并在rqt看真实画面、遮挡确认画面响应。'
echo '频率有输出且header持续新鲜才继续；delay异常/零时间戳/断图时先处理相机或校时。'
echo '本程序帧保护为0.5秒，允许时钟超前最多0.1秒；不能只凭平均频率判定通过。'
printf '记录已保存：%s/real_camera_check.log\n' "$RUN_DIR"
