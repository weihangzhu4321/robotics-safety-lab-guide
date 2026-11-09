#!/usr/bin/env python3
"""Hold-to-drive ROS 1 Tk keyboard control with fresh-sensor gating."""

import atexit
import os
import signal
import sys
import time

from freshness import FreshnessMonitor, env_topic, record_ros_message


def velocity_for_keys(pressed, blocked, focused, sensors_ready):
    keys = pressed - blocked if focused and sensors_ready else set()
    return (0.01 * (("w" in keys) - ("s" in keys)),
            0.10 * (("a" in keys) - ("d" in keys)))


class KeyboardControl:
    def __init__(self, tk, rospy, Twist, LaserScan, Odometry):
        self.ros, self.Twist = rospy, Twist
        self.pub = rospy.Publisher(env_topic(os.environ, "CMD_VEL_TOPIC", "/cmd_vel"),
                                   Twist, queue_size=1, tcp_nodelay=True)
        self.monitor = FreshnessMonitor()
        self.subscribers = [rospy.Subscriber(env_topic(os.environ, key, topic), cls,
                            lambda msg, name=name: record_ros_message(self.monitor, name, msg), queue_size=1)
                            for name, key, topic, cls in (
                                ("scan", "SCAN_TOPIC", "/scan", LaserScan),
                                ("odom", "ODOM_TOPIC", "/odom", Odometry))]
        self.root = tk.Tk()
        self.root.title("实验六低速键盘控制")
        self.root.geometry("760x430")
        self.pressed, self.blocked, self.pending_releases = set(), set(), {}
        self.focused, self.closing = False, False
        tk.Label(self.root, text="W/S 前进后退  A/D 左右转\n按住行驶，松开/失焦停车\n空格或 Esc 锁停，松键后重按恢复；Q 退出\n导航前必须关闭此窗口",
                 font=("Sans", 19), justify="center").pack(pady=22)
        self.status = tk.Label(self.root, text="停车：等待新雷达与里程计", font=("Sans", 14),
                               wraplength=720, justify="center", fg="darkorange")
        self.status.pack(pady=8)
        tk.Label(self.root, text="允许行驶条件：最近收包 < 1 秒，|header age| < 0.5 秒\n数据恢复后先松开所有方向键，再重新按下。",
                 font=("Sans", 11)).pack(pady=10)
        self.root.bind("<KeyPress>", self.down)
        self.root.bind("<KeyRelease>", self.up)
        self.root.bind("<FocusIn>", self.focus_in)
        self.root.bind("<FocusOut>", self.focus_out)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        signal.signal(signal.SIGINT, self.close)
        signal.signal(signal.SIGTERM, self.close)
        rospy.on_shutdown(self.send_zero)
        atexit.register(self.send_zero)

    def send(self, x=0.0, z=0.0):
        msg = self.Twist()
        msg.linear.x, msg.angular.z = x, z
        try:
            self.pub.publish(msg)
        except (self.ros.ROSException, RuntimeError):
            pass

    def send_zero(self):
        self.send()

    def stop(self, latch=True):
        if latch:
            self.blocked.update(self.pressed)
        self.send_zero()
        self.status.configure(text="停车：方向键需松开后重新按下", fg="green")

    def down(self, event):
        key = event.keysym.lower()
        if key in self.pending_releases:
            self.root.after_cancel(self.pending_releases.pop(key))
        if key in ("space", "escape"):
            self.stop()
        elif key == "q":
            self.close()
        elif key in ("w", "a", "s", "d"):
            self.pressed.add(key)

    def up(self, event):
        key = event.keysym.lower()
        if key not in ("w", "a", "s", "d"):
            return
        def released():
            self.pending_releases.pop(key, None)
            self.pressed.discard(key)
            self.blocked.discard(key)
            # Send the updated (possibly zero) command without waiting for tick.
            ready = self.monitor.snapshot(self.ros.Time.now().to_sec())["ready"]
            self.send(*velocity_for_keys(self.pressed, self.blocked, self.focused, ready))
        if key in self.pending_releases:
            self.root.after_cancel(self.pending_releases[key])
        # X11 autorepeat can emit release/press pairs. A real release is applied
        # at the next idle event; an immediate repeat press cancels this job.
        self.pending_releases[key] = self.root.after_idle(released)

    def focus_in(self, _event):
        self.focused = True

    def focus_out(self, _event):
        self.focused = False
        for handle in self.pending_releases.values():
            self.root.after_cancel(handle)
        self.pending_releases.clear()
        self.pressed.clear()
        self.blocked.clear()
        self.stop(False)

    def tick(self):
        if self.closing:
            return
        if self.ros.is_shutdown():
            self.close()
            return
        state = self.monitor.snapshot(self.ros.Time.now().to_sec())
        if not state["ready"]:
            self.blocked.update(self.pressed)
        x, z = velocity_for_keys(self.pressed, self.blocked, self.focused, state["ready"])
        self.send(x, z)
        if not state["ready"]:
            text, colour = "停车：" + "；".join(state["failure_reasons"]), "darkorange"
        elif x or z:
            text, colour = "行驶：v=%.2f m/s，ω=%.2f rad/s" % (x, z), "red"
        elif self.pressed & self.blocked:
            text, colour = "停车：数据已恢复或触发锁停，请松键后重按", "green"
        elif not self.focused:
            text, colour = "停车：点击本窗口后才能控制", "green"
        else:
            text, colour = "停车：数据新鲜，可按住 W/S/A/D", "green"
        if state["warnings"]:
            text += "\n告警：" + "；".join(state["warnings"])
        self.status.configure(text=text, fg=colour)
        self.root.after(50, self.tick)  # nominal 20 Hz, subject to GUI scheduling

    def close(self, *_args):
        if self.closing:
            return
        self.closing = True
        for _ in range(3):
            self.send_zero()
            time.sleep(0.02)
        self.ros.signal_shutdown("keyboard closed")
        for subscriber in self.subscribers:
            subscriber.unregister()
        self.root.destroy()

    def run(self):
        self.root.after(50, self.tick)
        self.root.mainloop()


def main():
    import tkinter as tk
    import rospy
    from geometry_msgs.msg import Twist
    from sensor_msgs.msg import LaserScan
    from nav_msgs.msg import Odometry
    rospy.init_node("lab_keyboard", anonymous=False, disable_signals=True)
    KeyboardControl(tk, rospy, Twist, LaserScan, Odometry).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
