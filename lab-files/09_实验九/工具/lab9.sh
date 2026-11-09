#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
case "${1:-help}" in
  self-check) exec python3 -B 工具/lab9.py self-check ;;
  env) exec python3 -B 工具/lab9.py env ;;
  terminal) exec "${SHELL:-/bin/bash}" ;;
  *) printf '%s\n' '使用 bash 工具/lab9.sh self-check 或 env；现场命令见06到场快速操作单。' ;;
esac
