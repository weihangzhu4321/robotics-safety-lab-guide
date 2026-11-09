#!/usr/bin/env python3
"""Offline speed-sign recognizer and bounded, read-only ROS1 image subscriber."""
from __future__ import print_function

import argparse
import json
import os
import sys
import threading
import time

import cv2
import numpy as np


def _digit_templates():
    """Procedural glyph templates; no supplied sample labels or weights are used."""
    templates = {str(d): [] for d in range(10)}
    fonts = [cv2.FONT_HERSHEY_SIMPLEX, cv2.FONT_HERSHEY_DUPLEX,
             cv2.FONT_HERSHEY_COMPLEX, cv2.FONT_HERSHEY_TRIPLEX]
    for digit in range(10):
        for font in fonts:
            for scale in (1.0, 1.25, 1.5):
                canvas = np.full((64, 48), 255, np.uint8)
                (w, h), baseline = cv2.getTextSize(str(digit), font, scale, 2)
                x = max(0, (48 - w) // 2)
                y = min(58, (64 + h - baseline) // 2 + h)
                cv2.putText(canvas, str(digit), (x, y), font, scale, 0, 2, cv2.LINE_AA)
                templates[str(digit)].append(_normalize_glyph(255 - canvas))
    return templates


def _normalize_glyph(mask):
    ys, xs = np.where(mask > 0)
    if len(xs) == 0:
        return np.zeros((48, 36), np.uint8)
    crop = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    h, w = crop.shape
    scale = min(32.0 / max(w, 1), 44.0 / max(h, 1))
    resized = cv2.resize(crop, (max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
                         interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
    out = np.zeros((48, 36), np.uint8)
    y = (48 - resized.shape[0]) // 2
    x = (36 - resized.shape[1]) // 2
    out[y:y + resized.shape[0], x:x + resized.shape[1]] = resized
    return out


TEMPLATES = _digit_templates()


def locate_sign(image):
    """Return the most plausible red circular sign ROI and a confidence in [0,1]."""
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    red1 = cv2.inRange(hsv, np.array([0, 65, 55], np.uint8), np.array([14, 255, 255], np.uint8))
    red2 = cv2.inRange(hsv, np.array([165, 65, 55], np.uint8), np.array([180, 255, 255], np.uint8))
    mask = cv2.bitwise_or(red1, red2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    h, w = image.shape[:2]
    candidates = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < max(120.0, (h * w) * 0.00015):
            continue
        (cx, cy), radius = cv2.minEnclosingCircle(contour)
        if radius < 8:
            continue
        box = cv2.boundingRect(contour)
        x, y, bw, bh = box
        if min(bw, bh) <= 0:
            continue
        circularity = min(bw, bh) / float(max(bw, bh))
        # RETR_EXTERNAL traces the outside edge of the red rim, so the contour
        # area is nearly the whole disk, not just the thin red pixels.
        fill = area / float(np.pi * radius * radius)
        score = min(1.0, circularity) * max(0.0, 1.0 - abs(fill - 0.90))
        if circularity >= 0.58 and 0.50 <= fill <= 1.10:
            pad = int(round(radius * 0.86))
            x0, y0 = max(0, int(cx) - pad), max(0, int(cy) - pad)
            x1, y1 = min(w, int(cx) + pad), min(h, int(cy) + pad)
            candidates.append((score, image[y0:y1, x0:x1], (x0, y0, x1 - x0, y1 - y0)))
    if not candidates:
        return None, 0.0, None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1], float(candidates[0][0]), candidates[0][2]


def _extract_glyphs(roi):
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY) if len(roi.shape) == 3 else roi.copy()
    # Restrict to the central disk to exclude the red outline and background.
    h, w = gray.shape
    yy, xx = np.ogrid[:h, :w]
    disk = ((xx - (w - 1) / 2.0) ** 2 / (max(w * 0.43, 1) ** 2) +
            (yy - (h - 1) / 2.0) ** 2 / (max(h * 0.43, 1) ** 2)) <= 1.0
    # Adaptive threshold tolerates display brightness gradients; try both polarities.
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    binary = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY_INV, 21, 7)
    binary[np.logical_not(disk)] = 0
    # Bridge tiny print/display breaks inside glyphs without joining adjacent digits.
    close_kernel = cv2.getStructuringElement(cv2.MORPH_RECT,
        (max(1, int(round(w * 0.025))), max(1, int(round(h * 0.06)))))
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, close_kernel)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    min_area = max(5, int(h * w * 0.001))
    boxes = []
    for i in range(1, n):
        x, y, bw, bh, area = [int(v) for v in stats[i]]
        if area >= min_area and bh >= h * 0.18 and bw >= w * 0.035:
            boxes.append((x, y, bw, bh))
    boxes.sort(key=lambda b: b[0])
    return gray, binary, boxes


def _classify(mask):
    glyph = _normalize_glyph(mask)
    scores = {}
    for digit, variants in TEMPLATES.items():
        scores[digit] = max(float(cv2.matchTemplate(glyph, v, cv2.TM_CCOEFF_NORMED)[0][0])
                            for v in variants)
    ranked = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
    return ranked[0][0], ranked[0][1], ranked[0][1] - ranked[1][1]


def recognize(image, min_confidence=0.42, min_margin=0.025):
    roi, ring_conf, box = locate_sign(image)
    result = {'number': None, 'confidence': 0.0, 'uncertain': True,
              'reason': 'no_red_circular_sign', 'sign_box': box}
    if roi is None:
        return result, image.copy()
    gray, binary, boxes = _extract_glyphs(roi)
    recognized, confs, margins = [], [], []
    for x, y, w, h in boxes:
        # use the thresholded component with a little white/black border
        x0, y0, x1, y1 = max(0, x - 2), max(0, y - 2), min(binary.shape[1], x + w + 2), min(binary.shape[0], y + h + 2)
        crop = binary[y0:y1, x0:x1]
        digit, conf, margin = _classify(crop)
        recognized.append(digit)
        confs.append(conf)
        margins.append(margin)
    if not recognized:
        result.update(reason='no_digit_components', confidence=ring_conf)
    else:
        overall = min(confs) * (0.65 + 0.35 * ring_conf)
        margin = min(margins)
        uncertain = overall < min_confidence or margin < min_margin or len(recognized) > 3
        result.update(number=''.join(recognized), confidence=round(float(overall), 4),
                      digit_confidences=[round(float(c), 4) for c in confs],
                      template_margins=[round(float(m), 4) for m in margins],
                      uncertain=bool(uncertain), reason='low_confidence_or_ambiguous' if uncertain else 'recognized')
    annotated = image.copy()
    if box:
        x, y, w, h = box
        cv2.rectangle(annotated, (x, y), (x + w, y + h), (0, 200, 0), 2)
        label = result['number'] if result['number'] is not None else 'uncertain'
        cv2.putText(annotated, '%s %.2f' % (label, result['confidence']), (x, max(18, y - 5)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 150, 0), 2)
    return result, annotated


def process_file(path, out_dir, stage, min_confidence):
    image = cv2.imread(path, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError('cannot decode image: ' + path)
    result, annotated = recognize(image, min_confidence=min_confidence)
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(path))[0]
    annotated_path = os.path.join(out_dir, stem + '_' + stage + '_annotated.jpg')
    if not cv2.imwrite(annotated_path, annotated):
        raise IOError('failed to write ' + annotated_path)
    row = {'stage': stage, 'source': os.path.abspath(path), 'annotated': os.path.abspath(annotated_path),
           'image_width': int(image.shape[1]), 'image_height': int(image.shape[0]), **result}
    return row


def compare(args):
    os.makedirs(args.output)
    rows = []
    for stage, path in [('original', args.original), ('strong', args.strong), ('weak', args.weak), ('recovery', args.recovery)]:
        if path:
            rows.append(process_file(path, args.output, stage, args.min_confidence))
    summary = {'recognizer': 'self-written red-circle ROI + procedural glyph templates',
               'model_weights': None, 'label_source': 'no sample labels embedded', 'results': rows,
               'velocity_mapping': 'none; recognition output does not drive any controller'}
    dest = os.path.join(args.output, 'comparison.json')
    with open(dest, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
        f.write('\n')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def ros_subscribe(args):
    # These imports occur only for explicit ROS mode. This node never creates a Publisher.
    import rospy
    from sensor_msgs.msg import Image
    from cv_bridge import CvBridge
    os.makedirs(args.output)
    rospy.init_node('lab9_self_written_sign_reader', anonymous=True)
    bridge = CvBridge()
    log_path = os.path.join(args.output, 'ros_recognition.jsonl')
    state = {'frames': 0}
    callback_lock = threading.Lock()

    def record_frame(msg):
        if state['frames'] >= args.max_frames:
            return
        state['frames'] += 1
        row = {'frame': state['frames'], 'topic': args.image_topic,
               'stamp_secs': int(msg.header.stamp.secs), 'stamp_nsecs': int(msg.header.stamp.nsecs)}
        try:
            image = bridge.imgmsg_to_cv2(msg, desired_encoding='bgr8')
            result, annotated = recognize(image, min_confidence=args.min_confidence)
            row.update(result)
            path = os.path.join(args.output, 'ros_frame_%04d.jpg' % state['frames'])
            if not cv2.imwrite(path, annotated):
                raise IOError('failed to write annotated frame')
            row['annotated'] = os.path.abspath(path)
        except Exception as exc:
            row.update(number=None, confidence=0.0, uncertain=True,
                       reason='image_conversion_or_processing_error', error=str(exc)[:300])
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
        print(json.dumps(row, ensure_ascii=False))
        if state['frames'] >= args.max_frames:
            rospy.signal_shutdown('frame limit reached')

    def callback(msg):
        with callback_lock:
            record_frame(msg)

    subscriber = rospy.Subscriber(args.image_topic, Image, callback, queue_size=1, buff_size=16 * 1024 * 1024)
    rospy.loginfo('read-only subscriber; topic=%s max_frames=%d max_seconds=%d', args.image_topic, args.max_frames, args.seconds)
    deadline = time.monotonic() + args.seconds
    try:
        while not rospy.is_shutdown() and time.monotonic() < deadline:
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    finally:
        subscriber.unregister()
        if not rospy.is_shutdown():
            rospy.signal_shutdown('time limit reached')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command')
    c = sub.add_parser('compare', help='compare original/strong/weak/recovery image files')
    for flag in ('original', 'strong', 'weak', 'recovery'):
        c.add_argument('--' + flag, required=(flag in ('original', 'strong', 'weak')))
    c.add_argument('--output', required=True)
    c.add_argument('--min-confidence', type=float, default=0.42)
    r = sub.add_parser('ros', help='bounded read-only ROS1 image subscriber')
    r.add_argument('--image-topic', required=True)
    r.add_argument('--output', required=True)
    r.add_argument('--seconds', type=int, default=30)
    r.add_argument('--max-frames', type=int, default=10)
    r.add_argument('--min-confidence', type=float, default=0.42)
    args = p.parse_args(argv)
    if args.command == 'compare':
        if not 0.0 <= args.min_confidence <= 1.0:
            p.error('--min-confidence must be 0..1')
        compare(args)
    elif args.command == 'ros':
        if not args.image_topic.startswith('/') or any(ch.isspace() for ch in args.image_topic):
            p.error('--image-topic must be an absolute ROS topic')
        if not 1 <= args.seconds <= 120 or not 1 <= args.max_frames <= 100:
            p.error('--seconds must be 1..120 and --max-frames 1..100')
        if not 0.0 <= args.min_confidence <= 1.0:
            p.error('--min-confidence must be 0..1')
        ros_subscribe(args)
    else:
        p.print_help()
        return 2
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, IOError, OSError, ImportError) as exc:
        print(json.dumps({'error': type(exc).__name__, 'detail': str(exc)[:800]}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
