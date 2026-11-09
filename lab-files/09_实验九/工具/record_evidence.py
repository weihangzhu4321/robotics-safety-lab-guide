#!/usr/bin/env python3
"""Bounded ROS1 subscriber evidence recorder. Preview is the default. No Publisher."""
import argparse
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid

from lab9 import append_json, ensure_run, hash_file, now


def image_ppm(msg, max_bytes=12 * 1024 * 1024):
    """Convert exact ROS rgb8/bgr8/mono8 rows including step padding to PPM/PGM."""
    width, height, step = int(msg.width), int(msg.height), int(msg.step)
    encoding = str(msg.encoding).lower()
    channels = {'rgb8': 3, 'bgr8': 3, 'mono8': 1}.get(encoding)
    if channels is None:
        raise ValueError('unsupported encoding: ' + encoding)
    if width <= 0 or height <= 0 or width > 4096 or height > 4096:
        raise ValueError('invalid image dimensions')
    row_size = width * channels
    if step < row_size or step * height > max_bytes:
        raise ValueError('invalid or oversized image stride')
    raw = bytes(msg.data)
    if len(raw) != step * height:
        raise ValueError('image data length differs from step*height')
    pixels = bytearray(row_size * height)
    for y in range(height):
        row = raw[y * step:y * step + row_size]
        if encoding == 'bgr8':
            for x in range(0, row_size, 3):
                pixels[y * row_size + x:y * row_size + x + 3] = row[x:x + 3][::-1]
        else:
            pixels[y * row_size:(y + 1) * row_size] = row
    magic, suffix = ('P5', '.pgm') if channels == 1 else ('P6', '.ppm')
    return ('%s\n%d %d\n255\n' % (magic, width, height)).encode('ascii') + bytes(pixels), suffix


def stamp_info(msg):
    header = getattr(msg, 'header', None)
    if header is None:
        return {}
    stamp = getattr(header, 'stamp', None)
    return {'seq': getattr(header, 'seq', None), 'frame_id': getattr(header, 'frame_id', ''),
            'stamp_secs': getattr(stamp, 'secs', None), 'stamp_nsecs': getattr(stamp, 'nsecs', None)}


def finite_vector(v):
    values = [float(v.x), float(v.y), float(v.z)]
    if not all(math.isfinite(x) for x in values):
        raise ValueError('non-finite vector')
    return values


class Recorder:
    def __init__(self, run, max_images, max_bytes, image_interval):
        self.run = Path(run)
        token = uuid.uuid4().hex[:10]
        self.path = self.run / ('ros_evidence_' + token + '.jsonl')
        self.image_dir = self.run / 'images' / token
        self.image_dir.mkdir(parents=True)
        self.max_images, self.max_bytes = max_images, max_bytes
        self.image_interval = image_interval
        self.images = self.bytes = self.messages = self.rejected = self.dropped = 0
        self.last_image = -1e12
        self.lock = threading.Lock()
        self.stopped = False
        self.per_topic = {}

    def save(self, row):
        row.update(utc=now(), received_monotonic=time.monotonic())
        encoded_size = len(json.dumps(row, ensure_ascii=False, allow_nan=False).encode('utf-8')) + 1
        if self.bytes + encoded_size > self.max_bytes:
            self.dropped += 1
            return False
        append_json(self.path, row)
        self.bytes += encoded_size
        return True

    def image(self, msg):
        with self.lock:
            if self.stopped:
                return
            self.messages += 1
            t = time.monotonic()
            if self.images >= self.max_images or t - self.last_image < self.image_interval:
                self.dropped += 1
                return
            self.last_image = t
            try:
                data, suffix = image_ppm(msg)
                if self.bytes + len(data) + 2048 > self.max_bytes:
                    self.dropped += 1
                    return
                p = self.image_dir / ('image_%04d%s' % (self.images + 1, suffix))
                with p.open('xb') as f:
                    f.write(data)
                    f.flush()
                self.bytes += len(data)
                self.images += 1
                self.save(dict(kind='image', path=str(p.relative_to(self.run)), sha256=hash_file(p),
                               encoding=msg.encoding, width=msg.width, height=msg.height,
                               step=msg.step, **stamp_info(msg)))
            except (ValueError, OSError, TypeError) as e:
                self.rejected += 1
                self.save({'kind': 'image_rejected', 'reason': str(e)[:400], **stamp_info(msg)})

    def motion(self, msg, kind):
        with self.lock:
            if self.stopped:
                return
            self.messages += 1
            t = time.monotonic()
            if t - self.per_topic.get(kind, -1e12) < 0.1:
                self.dropped += 1
                return
            self.per_topic[kind] = t
            try:
                twist = msg.twist.twist if kind == 'odom_feedback' else msg
                row = dict(kind=kind, linear=finite_vector(twist.linear), angular=finite_vector(twist.angular))
                row.update(stamp_info(msg))
                self.save(row)
            except (ValueError, TypeError, AttributeError) as e:
                self.rejected += 1
                self.save({'kind': 'motion_rejected', 'reason': str(e)[:400]})

    def close(self):
        with self.lock:
            self.stopped = True
            result = {'scope': 'subscriber evidence; cmd_vel is a command, odom is reported feedback',
                      'images': self.images, 'received_messages': self.messages, 'rejected': self.rejected,
                      'dropped_or_throttled': self.dropped, 'payload_bytes': self.bytes,
                      'jsonl': str(self.path), 'closed_utc': now(), 'published_motion': False}
            p = self.path.with_suffix('.summary.json')
            with p.open('x', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False, indent=2, allow_nan=False)
            return result


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run', required=True)
    p.add_argument('--image-topic', required=True)
    p.add_argument('--cmd-topic', default='/cmd_vel')
    p.add_argument('--odom-topic', default='/odom')
    p.add_argument('--duration', type=float, default=30)
    p.add_argument('--max-images', type=int, default=20)
    p.add_argument('--max-mib', type=int, default=64)
    p.add_argument('--image-interval', type=float, default=1)
    p.add_argument('--execute', action='store_true')
    p.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    return p


def main(argv=None):
    a = parser().parse_args(argv)
    if not math.isfinite(a.duration) or not 1 <= a.duration <= 120:
        raise ValueError('duration must be 1-120 seconds')
    if not 1 <= a.max_images <= 100 or not 1 <= a.max_mib <= 256:
        raise ValueError('max-images 1-100 and max-mib 1-256 required')
    if not math.isfinite(a.image_interval) or not 0.1 <= a.image_interval <= 120:
        raise ValueError('image-interval must be 0.1-120 seconds')
    for name in [a.image_topic, a.cmd_topic, a.odom_topic]:
        if not name.startswith('/') or any(c.isspace() for c in name):
            raise ValueError('use discovered absolute ROS topic names')
    run = ensure_run(a.run)
    if not a.execute:
        print(json.dumps({'mode': 'preview', 'run': str(run), 'topics': [a.image_topic, a.cmd_topic, a.odom_topic],
                          'duration_wall_seconds': a.duration, 'max_images': a.max_images,
                          'max_mib': a.max_mib, 'ros_imported': False, 'published_motion': False}, ensure_ascii=False, indent=2))
        return 0
    if not a.worker:
        if sys.platform != 'linux':
            raise ValueError('ROS execution requires the prepared Linux ROS1 Python3 environment; use preview on this host')
        # An outer process also bounds master registration and connection setup.
        # A dead master can otherwise keep rospy.init_node retrying indefinitely.
        args = list(sys.argv[1:] if argv is None else argv) + ['--worker']
        child = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve())] + args)
        try:
            return child.wait(timeout=a.duration + 10)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            child.send_signal(signal.SIGINT)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)
            print(json.dumps({'stopped': 'outer wall timeout or interrupt', 'child_rc': child.returncode,
                              'published_motion': False, 'check_partial_evidence': True}), file=sys.stderr)
            return 124
    # These imports happen only after explicit execution, never in preview/self-check.
    import rospy
    from sensor_msgs.msg import Image
    from geometry_msgs.msg import Twist
    from nav_msgs.msg import Odometry
    recorder = Recorder(run, a.max_images, a.max_mib * 1024 * 1024, a.image_interval)
    stop = threading.Event()
    previous = {}
    subscribers = []
    try:
        for sig in [signal.SIGINT, signal.SIGTERM]:
            previous[sig] = signal.signal(sig, lambda *_: stop.set())
        rospy.init_node('lab9_evidence', anonymous=True, disable_signals=True)
        subscribers = [
            rospy.Subscriber(a.image_topic, Image, recorder.image, queue_size=1, buff_size=16 * 1024 * 1024),
            rospy.Subscriber(a.cmd_topic, Twist, lambda m: recorder.motion(m, 'cmd_vel_command'), queue_size=10),
            rospy.Subscriber(a.odom_topic, Odometry, lambda m: recorder.motion(m, 'odom_feedback'), queue_size=10)]
        deadline = time.monotonic() + a.duration
        while not stop.is_set() and not rospy.is_shutdown() and time.monotonic() < deadline:
            stop.wait(min(0.1, max(0, deadline - time.monotonic())))
    finally:
        for sub in subscribers:
            sub.unregister()
        rospy.signal_shutdown('lab9 bounded evidence recorder finished')
        result = recorder.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, ImportError) as e:
        print(json.dumps({'error': type(e).__name__, 'detail': str(e)[:1000], 'published_motion': False}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
