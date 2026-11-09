#!/usr/bin/env python3
"""Read-only 20-second sensor check. PASS is not a localization/TF proof."""

import json
import math
import os
from pathlib import Path
import sys
import time

from freshness import FreshnessMonitor, env_topic, record_ros_message


def fmt_age(value):
    return "未收到" if value is None else "%+.3fs" % value


def main():
    import rospy
    from sensor_msgs.msg import LaserScan
    from nav_msgs.msg import Odometry
    from tf2_msgs.msg import TFMessage

    seconds = float(os.environ.get("CHECK_SECONDS", "20"))
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("CHECK_SECONDS 必须是正数，正常现场检查使用 20 秒。")
    rospy.init_node("lab_sensor_check", anonymous=True)
    monitor = FreshnessMonitor(("scan", "odom", "tf"))
    topics = {"scan": env_topic(os.environ, "SCAN_TOPIC", "/scan"),
              "odom": env_topic(os.environ, "ODOM_TOPIC", "/odom"),
              "tf": env_topic(os.environ, "TF_TOPIC", "/tf")}
    subscribers = [rospy.Subscriber(topics[name], msg_type,
                    lambda msg, key=name: record_ros_message(monitor, key, msg), queue_size=20)
                   for name, msg_type in (("scan", LaserScan), ("odom", Odometry), ("tf", TFMessage))]
    started = time.monotonic()
    deadline = started + seconds
    peaks = {name: 0.0 for name in topics}
    while time.monotonic() < deadline and not rospy.is_shutdown():
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        state = monitor.snapshot(rospy.Time.now().to_sec())
        sensors = state["sensors"]
        for name, row in sensors.items():
            if row["stamp_age_seconds"] is not None:
                peaks[name] = max(peaks[name], abs(row["stamp_age_seconds"]))
        print("scan=%d odom=%d tf=%d  header age: scan=%s odom=%s tf=%s" % (
            sensors["scan"]["count"], sensors["odom"]["count"], sensors["tf"]["count"],
            fmt_age(sensors["scan"]["stamp_age_seconds"]),
            fmt_age(sensors["odom"]["stamp_age_seconds"]),
            fmt_age(sensors["tf"]["stamp_age_seconds"])), flush=True)
        for warning in state["warnings"]:
            print("告警：" + warning, flush=True)
    ended = time.monotonic()
    state = monitor.snapshot(rospy.Time.now().to_sec(), ended)
    failures = list(state["failure_reasons"])
    for name, row in state["sensors"].items():
        if row["count"] < 5:
            failures.append("%s：本轮仅 %d 条消息，要求至少 5 条" % (name, row["count"]))
    if rospy.is_shutdown() or ended - started + 0.05 < seconds:
        failures.append("检查被提前中断，未完成设定采样窗口")
    warnings = list(state["warnings"])
    for name, peak in peaks.items():
        if peak >= 0.5 and state["sensors"][name]["ready"]:
            warnings.append("%s：采样期间曾观测到 |header age|=%.3f 秒，结束时已恢复；建议重跑完整检查" %
                            (name, peak))
    result = {
        "passed": not failures,
        "checked_at_local": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "duration_seconds": ended - started,
        "configured_seconds": seconds,
        "topics": topics,
        "counts": {name: row["count"] for name, row in state["sensors"].items()},
        "sensors": state["sensors"], "observed_peak_abs_header_age_seconds": peaks,
        "failure_reasons": failures, "warnings": warnings,
        "limits": {"receive_age_seconds_less_than": 1.0,
                   "absolute_header_age_seconds_less_than": 0.5,
                   "warning_above_seconds": 0.3},
        "scope": "仅证明此窗口消息持续到达及结束时的新鲜性；不验证完整 TF 链、地图质量、定位收敛或导航成功。",
    }
    output = Path(os.environ.get("CHECK_REPORT", str(
        Path(os.environ.get("DATA_DIR", "/lab/data")) / "last_connection_check.json")))
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".%d.tmp" % os.getpid())
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(output)
    print("SENSOR_CHECK=" + ("PASS" if result["passed"] else "FAIL"), flush=True)
    for failure in failures:
        print("失败原因：" + failure, flush=True)
    for warning in warnings:
        print("告警：" + warning, flush=True)
    print(result["scope"], flush=True)
    print("检查记录：" + str(output), flush=True)
    # Keep references alive until the measurement and report have finished.
    for subscriber in subscribers:
        subscriber.unregister()
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError) as exc:
        print("SENSOR_CHECK=FAIL\n检查配置或记录写入失败：%s" % exc, file=sys.stderr)
        sys.exit(1)
