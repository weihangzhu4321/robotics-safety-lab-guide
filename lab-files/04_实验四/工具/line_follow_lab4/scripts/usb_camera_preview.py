#!/usr/bin/env python2
# -*- coding: utf-8 -*-
"""Standalone Python 2 ROS1 USB camera preview publisher.

Preview-only: publishes sensor_msgs/Image directly to
/lab4_usb_preview/image_raw. It does not use CvBridge and has no velocity
publisher. header.stamp is a software timestamp taken immediately before
VideoCapture.read; it includes time spent in read() but is not a hardware
exposure timestamp.
"""

import csv
import ctypes
import datetime
import os
import tempfile
import threading
import time

import cv2
import rospy
from sensor_msgs.msg import Image


class _Timespec(ctypes.Structure):
    _fields_ = [('tv_sec', ctypes.c_long), ('tv_nsec', ctypes.c_long)]


def monotonic_seconds():
    """Monotonic wall duration clock, with Python 2.7 Linux fallback."""
    if hasattr(time, 'monotonic'):
        return time.monotonic()
    if hasattr(time, 'clock_gettime') and hasattr(time, 'CLOCK_MONOTONIC'):
        return time.clock_gettime(time.CLOCK_MONOTONIC)
    ts = _Timespec()
    for library in (None, 'librt.so.1'):
        try:
            lib = ctypes.CDLL(library) if library else ctypes.CDLL(None)
            clock_gettime = lib.clock_gettime
            clock_gettime.argtypes = [ctypes.c_int, ctypes.POINTER(_Timespec)]
            clock_gettime.restype = ctypes.c_int
            if clock_gettime(1, ctypes.byref(ts)) == 0:  # CLOCK_MONOTONIC
                return float(ts.tv_sec) + float(ts.tv_nsec) / 1000000000.0
        except Exception:
            pass
    # Last resort for unusual non-POSIX test hosts. Wall-clock adjustments can
    # affect these duration values, which will be called out in the log.
    return time.time()


def wall_iso():
    return datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]


def frame_is_fresh(now_mono, read_end_mono, timeout_s):
    return read_end_mono is not None and (now_mono - read_end_mono) <= timeout_s


class PublishRateLimiter(object):
    """Monotonic limiter that cannot emit faster than the configured rate."""
    def __init__(self, hz):
        if hz <= 0:
            raise ValueError('publish rate must be positive')
        self.period_s = 1.0 / float(hz)
        self.next_allowed = None

    def allow(self, now_mono):
        if self.next_allowed is not None and now_mono < self.next_allowed:
            return False
        self.next_allowed = now_mono + self.period_s
        return True


def image_message(frame, stamp, frame_id='usb_camera_preview'):
    """Build bgr8 Image without cv_bridge (works with ROS Kinetic Python 2)."""
    shape = getattr(frame, 'shape', None)
    if shape is None or len(shape) != 3 or shape[2] != 3:
        raise ValueError('expected HxWx3 BGR frame')
    height, width = int(shape[0]), int(shape[1])
    data = frame.tostring()
    step = width * 3
    if height < 1 or width < 1 or len(data) != height * step:
        raise ValueError('frame data length does not match HxWx3 uint8 layout')
    msg = Image()
    msg.header.stamp = stamp
    msg.header.frame_id = frame_id
    msg.height = height
    msg.width = width
    msg.encoding = 'bgr8'
    msg.is_bigendian = 0
    msg.step = step
    msg.data = data
    return msg


class UsbCameraPreview(object):
    def __init__(self):
        self.camera_index = int(rospy.get_param('~camera_index', 0))
        self.width = int(rospy.get_param('~width', 640))
        self.height = int(rospy.get_param('~height', 480))
        self.capture_fps = float(rospy.get_param('~capture_fps', 10.0))
        self.publish_hz = min(10.0, float(rospy.get_param('~publish_hz', 10.0)))
        self.frame_timeout_s = float(rospy.get_param('~frame_timeout_s', 0.5))
        self.max_read_s = float(rospy.get_param('~max_read_s', 0.5))
        self.topic = rospy.get_param('~output_topic', '/lab4_usb_preview/image_raw')
        if (self.width < 1 or self.height < 1 or self.capture_fps <= 0 or
                self.publish_hz <= 0 or self.frame_timeout_s <= 0 or self.max_read_s <= 0):
            raise ValueError('camera dimensions, rates and timeouts must be positive')
        if not self.topic.startswith('/'):
            raise ValueError('output_topic must be an absolute ROS topic')

        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._metrics_lock = threading.Lock()
        self._latest = None
        self._sequence = 0
        self._last_read_problem = 'waiting_for_first_frame'
        self._last_unavailable_reason = None
        self._camera_open = False
        self._cap = None
        self._reader = None
        self._shutdown_done = False
        self._publisher = rospy.Publisher(self.topic, Image, queue_size=1, latch=False)
        self._csv_file, self.csv_path = self._open_metrics_file()
        self._csv = csv.writer(self._csv_file)
        self._csv.writerow([
            'wall_local_iso', 'outcome', 'sequence', 'reason',
            'read_start_local_iso', 'read_end_local_iso',
            'read_start_ros_s', 'read_end_ros_s', 'read_duration_ms',
            'read_to_publish_ms', 'publish_start_local_iso',
            'publish_end_local_iso', 'publish_duration_ms',
            'software_stamp_note'])
        self._csv_file.flush()
        rospy.on_shutdown(self.shutdown)

    def _open_metrics_file(self):
        log_dir = os.path.expanduser(rospy.get_param(
            '~log_dir', os.path.join('~', 'lab4_camera_data')))
        if not os.path.isdir(log_dir):
            os.makedirs(log_dir)
        fd, path = tempfile.mkstemp(prefix='usb_camera_preview_', suffix='.csv', dir=log_dir)
        return os.fdopen(fd, 'wb'), path

    def _metric_row(self, outcome, reason='', frame=None,
                    publish_start=None, publish_end=None, read_to_publish=None,
                    publish_start_wall='', publish_end_wall=''):
        frame = frame or {}
        duration = ''
        if publish_start is not None and publish_end is not None:
            duration = '{:.3f}'.format((publish_end - publish_start) * 1000.0)
        read_duration = frame.get('read_duration_s')
        if read_duration is not None:
            read_duration = '{:.3f}'.format(read_duration * 1000.0)
        else:
            read_duration = ''
        if read_to_publish is not None:
            read_to_publish = '{:.3f}'.format(read_to_publish * 1000.0)
        else:
            read_to_publish = ''
        values = [
            wall_iso(), outcome, frame.get('sequence', ''), reason,
            frame.get('read_start_wall', ''), frame.get('read_end_wall', ''),
            frame.get('read_start_ros_s', ''), frame.get('read_end_ros_s', ''),
            read_duration, read_to_publish,
            publish_start_wall, publish_end_wall, duration,
            'software read-start timestamp; hardware exposure age unavailable']
        with self._metrics_lock:
            self._csv.writerow(values)
            self._csv_file.flush()

    def _camera_reader(self):
        try:
            while not self._stop.is_set():
                read_start_mono = monotonic_seconds()
                read_start_wall = wall_iso()
                read_start_ros_obj = rospy.Time.now()
                read_start_ros = read_start_ros_obj.to_sec()
                ok, frame = self._cap.read()
                read_end_mono = monotonic_seconds()
                read_end_wall = wall_iso()
                read_end_ros_obj = rospy.Time.now()
                read_end_ros = read_end_ros_obj.to_sec()
                read_s = max(0.0, read_end_mono - read_start_mono)
                meta = {
                    'read_start_mono': read_start_mono,
                    'read_start_wall': read_start_wall,
                    'read_end_wall': read_end_wall,
                    'read_start_ros_s': '{:.9f}'.format(read_start_ros),
                    'read_end_ros_s': '{:.9f}'.format(read_end_ros),
                    'read_duration_s': read_s,
                }
                if self._stop.is_set():
                    break
                if not ok or frame is None:
                    with self._lock:
                        self._last_read_problem = 'camera_read_failed'
                    rospy.logwarn_throttle(2.0, 'USB camera read failed')
                    self._stop.wait(0.05)
                    continue
                if read_s > self.max_read_s:
                    with self._lock:
                        self._last_read_problem = 'slow_read_dropped'
                    meta['outcome'] = 'slow_read_dropped'
                    self._metric_row('slow_read_dropped', 'read_over_limit', meta)
                    rospy.logwarn_throttle(2.0, 'USB camera slow read discarded: %.3fs > %.3fs',
                                           read_s, self.max_read_s)
                    continue
                try:
                    # Detach from backend-owned memory. The next read cannot mutate
                    # the frame while the publisher is serializing it.
                    detached = frame.copy()
                    shape = getattr(detached, 'shape', None)
                    if shape is None or len(shape) != 3 or shape[2] != 3:
                        raise ValueError('expected HxWx3 BGR frame')
                    if shape[1] != self.width or shape[0] != self.height:
                        rospy.logwarn_throttle(5.0, 'USB camera returned %sx%s (requested %sx%s)',
                                               shape[1], shape[0], self.width, self.height)
                    meta.update({
                        'frame': detached,
                        'read_end_mono': read_end_mono,
                        'stamp': read_start_ros_obj,
                    })
                except Exception as exc:
                    with self._lock:
                        self._last_read_problem = 'invalid_frame'
                    rospy.logwarn_throttle(2.0, 'USB camera frame rejected: %s', exc)
                    self._stop.wait(0.05)
                    continue
                with self._lock:
                    self._sequence += 1
                    meta['sequence'] = self._sequence
                    self._latest = meta
                    self._last_read_problem = ''
        except Exception as exc:
            with self._lock:
                self._last_read_problem = 'camera_reader_exception'
            rospy.logerr('USB camera reader stopped: %s', exc)
        finally:
            try:
                self._cap.release()
            except Exception:
                pass

    def _publish_image(self, frame, stamp, frame_id, outcome, reason='', meta=None,
                       read_to_publish=None):
        publish_start = monotonic_seconds()
        publish_start_wall = wall_iso()
        try:
            # Preserve the field-tested wire size: raw 640x480 would quadruple
            # TCPROS traffic compared with the temporary 320x240 publisher.
            if frame.shape[1] > 320 or frame.shape[0] > 240:
                frame = cv2.resize(frame, (320, 240))
            message = image_message(frame, stamp, frame_id)
            self._publisher.publish(message)
            outcome_final = outcome
        except Exception as exc:
            outcome_final = 'publish_error'
            reason = '{}: {}'.format(reason, exc)
            rospy.logerr_throttle(2.0, 'USB camera image publish failed: %s', exc)
        publish_end = monotonic_seconds()
        publish_end_wall = wall_iso()
        self._metric_row(outcome_final, reason, meta,
                         publish_start, publish_end, read_to_publish,
                         publish_start_wall, publish_end_wall)
        return publish_end, publish_start_wall, publish_end_wall

    def _publish_loop(self):
        limiter = PublishRateLimiter(self.publish_hz)
        last_published_sequence = 0
        while not rospy.is_shutdown() and not self._stop.is_set():
            now = monotonic_seconds()
            if limiter.allow(now):
                with self._lock:
                    latest = self._latest
                    read_problem = self._last_read_problem
                if latest is not None and latest['sequence'] != last_published_sequence:
                    age = max(0.0, now - latest['read_end_mono'])
                    if frame_is_fresh(now, latest.get('read_start_mono', latest['read_end_mono']), self.frame_timeout_s):
                        self._publish_image(latest['frame'], latest['stamp'],
                                            'usb_camera_preview', 'published', meta=latest,
                                            read_to_publish=age)
                        last_published_sequence = latest['sequence']
                        self._last_unavailable_reason = None
                    else:
                        self._report_unavailable('no_fresh_frame', latest)
                elif latest is None or not frame_is_fresh(
                        now, latest['read_end_mono'], self.frame_timeout_s):
                    reason = read_problem or ('camera_not_open' if not self._camera_open
                                              else 'no_fresh_frame')
                    self._report_unavailable(reason, latest)
            self._stop.wait(0.01)

    def _report_unavailable(self, reason, meta):
        """Log the outage without publishing synthetic data to the raw topic."""
        if reason == self._last_unavailable_reason:
            return
        self._metric_row('no_raw_publish', reason, meta)
        rospy.logwarn('USB camera preview has no fresh raw frame: %s', reason)
        self._last_unavailable_reason = reason

    def run(self):
        try:
            self._cap = cv2.VideoCapture(self.camera_index)
            self._camera_open = bool(self._cap is not None and self._cap.isOpened())
            if self._camera_open:
                width_set = self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
                height_set = self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
                fps_set = self._cap.set(cv2.CAP_PROP_FPS, self.capture_fps)
                actual_width = self._cap.get(cv2.CAP_PROP_FRAME_WIDTH)
                actual_height = self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
                actual_fps = self._cap.get(cv2.CAP_PROP_FPS)
                rospy.loginfo('USB camera index=%d opened; request=%dx%d@%.2f set_returns=%s/%s/%s reported=%.0fx%.0f@%.2f',
                              self.camera_index, self.width, self.height, self.capture_fps,
                              width_set, height_set, fps_set,
                              actual_width, actual_height, actual_fps)
                self._reader = threading.Thread(target=self._camera_reader, name='usb_camera_reader')
                self._reader.daemon = True
                self._reader.start()
            else:
                self._last_read_problem = 'camera_not_open'
                rospy.logerr('USB camera index=%d could not be opened', self.camera_index)
            rospy.loginfo('Preview only: topic=%s queue_size=1 rate<=%.2fHz CSV=%s',
                          self.topic, self.publish_hz, self.csv_path)
            rospy.loginfo('header.stamp is a software read-start timestamp, not sensor exposure time')
            self._publish_loop()
        finally:
            self.shutdown()

    def shutdown(self):
        with self._lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True
        self._stop.set()
        if self._reader is not None and self._reader.is_alive():
            self._reader.join(2.0)
            if self._reader.is_alive() and self._cap is not None:
                # Some V4L2 reads can block. Request release and wait once more;
                # whether this interrupts read() depends on the OpenCV backend.
                rospy.logwarn('USB camera read has not returned; requesting backend release')
                try:
                    self._cap.release()
                except Exception:
                    pass
                self._reader.join(1.0)
        elif self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
        self._metric_row('shutdown')
        with self._metrics_lock:
            try:
                self._csv_file.close()
            except Exception:
                pass
        rospy.loginfo('USB camera preview stopped; metrics=%s', self.csv_path)


def main():
    rospy.init_node('usb_camera_preview', anonymous=True)
    UsbCameraPreview().run()


if __name__ == '__main__':
    main()
