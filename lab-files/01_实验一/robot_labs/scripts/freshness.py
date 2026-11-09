"""ROS-independent sensor freshness decisions shared by lab tools."""

import math
import threading
import time


class FreshnessMonitor:
    def __init__(self, names=("scan", "odom"), receive_limit=1.0,
                 stamp_limit=0.5, warning_limit=0.3):
        self.receive_limit = receive_limit
        self.stamp_limit = stamp_limit
        self.warning_limit = warning_limit
        self.lock = threading.Lock()
        self.state = {name: {"count": 0, "received_at": None, "stamps": [], "frames": []}
                      for name in names}

    def receive(self, name, stamps, frames=(), received_at=None):
        if received_at is None:
            received_at = time.monotonic()
        with self.lock:
            row = self.state[name]
            row["count"] += 1
            row["received_at"] = float(received_at)
            row["stamps"] = list(stamps)
            row["frames"] = list(frames)

    def snapshot(self, ros_now, monotonic_now=None):
        if monotonic_now is None:
            monotonic_now = time.monotonic()
        with self.lock:
            rows = {name: dict(row, stamps=list(row["stamps"]), frames=list(row["frames"]))
                    for name, row in self.state.items()}
        failures, warnings, result = [], [], {}
        for name, row in rows.items():
            received_age = (None if row["received_at"] is None
                            else monotonic_now - row["received_at"])
            valid_stamps = (bool(row["stamps"]) and all(
                stamp is not None and math.isfinite(stamp) and stamp > 0
                for stamp in row["stamps"]))
            ages = [ros_now - stamp for stamp in row["stamps"]] if valid_stamps else []
            worst_age = max(ages, key=abs) if ages else None
            reasons = []
            if row["count"] == 0:
                reasons.append("未收到消息")
            elif received_age is None or received_age < 0 or received_age >= self.receive_limit:
                reasons.append("距最近收包 %.3f 秒（要求 < %.1f 秒）" %
                               (received_age or 0.0, self.receive_limit))
            elif received_age > self.warning_limit:
                warnings.append("%s：距最近收包 %.3f 秒" % (name, received_age))
            if row["count"] and not valid_stamps:
                reasons.append("header 时间戳缺失、为零或非有限数值")
            elif worst_age is not None:
                if not math.isfinite(worst_age) or abs(worst_age) >= self.stamp_limit:
                    reasons.append("header age=%+.3f 秒（要求绝对值 < %.1f 秒；负数表示源时间在未来）" %
                                   (worst_age, self.stamp_limit))
                elif abs(worst_age) > self.warning_limit:
                    warnings.append("%s：header age=%+.3f 秒，接近 %.1f 秒限制" %
                                    (name, worst_age, self.stamp_limit))
            failures.extend("%s：%s" % (name, reason) for reason in reasons)
            result[name] = {
                "count": row["count"], "frames": row["frames"],
                "last_received_age_seconds": received_age,
                "stamp_age_seconds": worst_age,
                "all_stamp_ages_seconds": ages,
                "ready": not reasons,
            }
        return {"ready": not failures, "sensors": result,
                "failure_reasons": failures, "warnings": warnings}


def stamp_seconds(header):
    try:
        value = float(header.stamp.to_sec())
        return value if math.isfinite(value) else None
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def record_ros_message(monitor, name, message):
    """Works with ordinary message-shaped mocks; does not import ROS."""
    if name == "tf":
        transforms = getattr(message, "transforms", [])
        stamps = [stamp_seconds(getattr(item, "header", None)) for item in transforms]
        frames = ["%s -> %s" % (getattr(getattr(item, "header", None), "frame_id", ""),
                                  getattr(item, "child_frame_id", ""))
                  for item in transforms]
    else:
        header = getattr(message, "header", None)
        stamps = [stamp_seconds(header)]
        frames = [getattr(header, "frame_id", "")]
    monitor.receive(name, stamps, frames)


def env_topic(env, key, default):
    value = env.get(key, default)
    if not value.startswith("/") or len(value) < 2 or any(c.isspace() for c in value):
        raise ValueError("%s 必须是完整 ROS 话题名，如 %s" % (key, default))
    return value
