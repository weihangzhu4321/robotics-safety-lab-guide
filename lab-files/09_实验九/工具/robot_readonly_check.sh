#!/usr/bin/env bash
# B robot only: inspect existing driver without installation or motion commands.
set -u
printf '\nDATE\n'; date -Is
printf '\nSYSTEM\n'; uname -a; test ! -f /etc/os-release || cat /etc/os-release
printf '\nROS ENV\n'; printf 'ROS_DISTRO=%s ROS_VERSION=%s ROS_MASTER_URI=%s ROS_IP=%s ROS_HOSTNAME=%s\n' "${ROS_DISTRO:-}" "${ROS_VERSION:-}" "${ROS_MASTER_URI:-}" "${ROS_IP:-}" "${ROS_HOSTNAME:-}"
printf '\nPYTHON\n'; python --version 2>&1; python3 --version 2>&1
printf '\nUSB\n'; command -v lsusb >/dev/null && lsusb
printf '\nLAN\n'; ip -4 addr; ip route
printf '\nEXISTING CAMERA PACKAGE\n'
if command -v rospack >/dev/null;then
 CAMERA=$(timeout 8 rospack find orbbec_camera 2>/dev/null) || CAMERA=''
 if [[ -n $CAMERA && -d $CAMERA ]];then
  printf 'package=%s\n' "$CAMERA"
  test ! -f "$CAMERA/package.xml" || cat "$CAMERA/package.xml"
  test ! -f "$CAMERA/launch/gemini.launch" || { sha256sum "$CAMERA/launch/gemini.launch"; cat "$CAMERA/launch/gemini.launch"; }
 else printf 'orbbec_camera NOT FOUND in this terminal environment; check workspace source; do not replace driver blindly.\n';fi
fi
printf '\nMASTER TOPICS read only 8s bound\n'
command -v rostopic >/dev/null && timeout 8 rostopic list
printf '\nNo installation, driver launch, or velocity publishing was performed.\n'
