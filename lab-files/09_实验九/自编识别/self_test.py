#!/usr/bin/env python3
"""Synthetic checks for the self-written recognizer; no course image labels are used."""
import cv2
import numpy as np

from sign_reader import recognize


def make_sign(text, seed=0, noise=False):
    canvas = np.full((240, 320, 3), 245, np.uint8)
    cv2.circle(canvas, (160, 120), 82, (0, 0, 220), 8)
    font = cv2.FONT_HERSHEY_SIMPLEX
    (w, h), baseline = cv2.getTextSize(text, font, 1.65, 4)
    cv2.putText(canvas, text, (160 - w // 2, 120 + h // 2), font, 1.65, (10, 10, 10), 4, cv2.LINE_AA)
    if noise:
        rng = np.random.RandomState(seed)
        n = rng.normal(0, 18, canvas.shape).astype(np.int16)
        canvas = np.clip(canvas.astype(np.int16) + n, 0, 255).astype(np.uint8)
    return canvas


def main():
    for digit in '0123456789':
        result, _ = recognize(make_sign(digit), min_confidence=0.25)
        assert result['number'] == digit, (digit, result)
        print('PASS independent_digit_%s number=%s confidence=%s' %
              (digit, result['number'], result['confidence']))
    clean, _ = recognize(make_sign('37'), min_confidence=0.25)
    assert clean['number'] == '37', clean
    print('PASS independent_pair_37 number=%s confidence=%s' % (clean['number'], clean['confidence']))
    noise, _ = recognize(make_sign('42', seed=73, noise=True), min_confidence=0.25)
    assert noise['number'] == '42', noise
    print('PASS noisy_pair_42 number=%s confidence=%s uncertain=%s' %
          (noise['number'], noise['confidence'], noise['uncertain']))
    blank = np.full((240, 320, 3), 127, np.uint8)
    result, _ = recognize(blank)
    assert result['number'] is None and result['uncertain'], result
    print('PASS blank_frame number=None reason=%s' % result['reason'])
    print('PASS all synthetic checks')


if __name__ == '__main__':
    main()
