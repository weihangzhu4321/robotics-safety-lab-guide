#!/usr/bin/env bash
set -euo pipefail
run_dir=$1
map_file=$2
rostopic echo /move_base/result >> "$run_dir/navigation_result.yaml" 2>&1 & p1=$!
rostopic echo /initialpose >> "$run_dir/initial_pose.yaml" 2>&1 & p2=$!
rostopic echo /move_base_simple/goal >> "$run_dir/navigation_goals.yaml" 2>&1 & p3=$!
python3 /lab/scripts/navigation_guard.py >> "$run_dir/navigation_guard.log" 2>&1 & p4=$!
cleanup() {
  timeout -k 2 6 python3 /lab/scripts/cancel.py || true
  kill "$p1" "$p2" "$p3" "$p4" 2>/dev/null || true
}
trap cleanup EXIT
roslaunch /lab/config/navigation.launch "map_file:=$map_file"
