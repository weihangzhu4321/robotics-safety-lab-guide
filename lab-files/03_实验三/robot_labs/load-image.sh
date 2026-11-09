#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[[ ${EUID} -ne 0 ]] || { echo '请用普通用户运行本脚本。' >&2; exit 1; }
command -v docker >/dev/null || { echo '先运行 bash install-host.sh。' >&2; exit 1; }
cd -- "$root/images"
sha256sum -c robot-labs-noetic-20261022.tar.gz.sha256
if docker info >/dev/null 2>&1; then engine=(docker); else sudo -v; engine=(sudo docker); fi
"${engine[@]}" load -i robot-labs-noetic-20261022.tar.gz
"${engine[@]}" image inspect robot-labs:noetic-20261022 --format '导入完成：{{.Id}}'
echo '下一步：在套件根目录运行 bash lab.sh health。'
