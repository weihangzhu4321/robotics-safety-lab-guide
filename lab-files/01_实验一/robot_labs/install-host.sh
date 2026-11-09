#!/usr/bin/env bash
set -euo pipefail
fail() { printf '错误：%s\n' "$*" >&2; exit 1; }
[[ ${EUID} -ne 0 ]] || fail '请用普通用户运行 bash install-host.sh，不要给整条命令加 sudo。'
source /etc/os-release
[[ ${ID:-} == ubuntu && ${VERSION_ID:-} == 24.04 ]] || fail '本套件宿主安装脚本针对 Ubuntu 24.04；请先核对系统版本。'
[[ $(uname -m) == x86_64 ]] || fail '镜像适用于 amd64/x86_64，不能用于 ARM Ubuntu。'
echo '此步骤需要 Ubuntu 能访问互联网；安装后可切换到小车局域网。'
sudo -v
sudo apt-get update
sudo apt-get install -y ca-certificates curl xauth x11-xserver-utils openssh-client python3 unzip nano
if command -v docker >/dev/null 2>&1; then
  echo '检测到现有 Docker，保留当前安装，不混装 docker.io。'
else
  if dpkg-query -W -f='${Status}\n' docker-ce docker-ce-cli containerd.io 2>/dev/null | grep -q 'install ok installed'; then
    fail '存在未完成的 Docker CE 安装。请先按其官方安装方式修复，不要混装 docker.io。'
  fi
  sudo apt-get install -y docker.io
fi
sudo systemctl start docker
sudo docker info >/dev/null
echo '宿主工具就绪。下一步：bash load-image.sh'
echo '本脚本不安装 ROS2，也不修改现有 ROS 环境或将用户加入 docker 组。'
