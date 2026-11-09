#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""无 ROS 依赖的视觉和 P 控制函数；离线测试不会连接机器人。

这里的 error_px 是图像像素误差，不是机器人横向距离（米）。
Command 是期望 Twist 指令，不是编码器或里程计实测速度。
OpenCV/NumPy 仅在 detect_line 内导入，控制和参数检查可单独测试。
"""

from dataclasses import dataclass
import math
from numbers import Integral, Real
from typing import Any, Optional, Tuple


def _finite_number(name: str, value: float) -> None:
    # bool 在 Python 中也是 int，因此单独排除，避免把 true 当作速度。
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        raise ValueError(name + " 必须是有限数值")


@dataclass(frozen=True)
class VisionConfig:
    # 教材给出的初值较宽，可同时选中部分黄色、绿色；不能当成现场标定值。
    hsv_lower: Tuple[int, int, int] = (20, 43, 46)
    hsv_upper: Tuple[int, int, int] = (90, 255, 255)
    # 只取图像靠近底部的窄条；5/6 指起点，20 指高度（像素）。
    roi_start_ratio: float = 5.0 / 6.0
    roi_height_px: int = 20
    # 小面积阈值抑制孤立噪点；20 是预备代码初值，仍需现场检查。
    min_line_pixels: int = 20

    def __post_init__(self) -> None:
        for name, values in (("hsv_lower", self.hsv_lower), ("hsv_upper", self.hsv_upper)):
            if not isinstance(values, (tuple, list)) or len(values) != 3:
                raise ValueError(name + " 必须包含 H、S、V 三个整数")
            for channel, (value, maximum) in enumerate(zip(values, (179, 255, 255))):
                if isinstance(value, bool) or not isinstance(value, Integral) or not 0 <= value <= maximum:
                    raise ValueError("{} 第 {} 项越界；H:0..179，S/V:0..255".format(name, channel))
        if any(lo > hi for lo, hi in zip(self.hsv_lower, self.hsv_upper)):
            raise ValueError("本初学版本要求 HSV 下界不大于上界，不实现跨 H=0 的红色区间")
        _finite_number("roi_start_ratio", self.roi_start_ratio)
        if not 0 <= self.roi_start_ratio < 1:
            raise ValueError("roi_start_ratio 必须在 [0,1) 内")
        for name, value in (("roi_height_px", self.roi_height_px), ("min_line_pixels", self.min_line_pixels)):
            if isinstance(value, bool) or not isinstance(value, Integral) or value < 1:
                raise ValueError(name + " 必须是正整数")


@dataclass(frozen=True)
class ControlConfig:
    kp: float = 0.001
    linear_speed: float = 0.05
    max_angular_speed: float = 0.4

    def __post_init__(self) -> None:
        for name, value in (("kp", self.kp), ("linear_speed", self.linear_speed),
                            ("max_angular_speed", self.max_angular_speed)):
            _finite_number(name, value)
            if value < 0:
                raise ValueError(name + " 不得为负数")
        if self.max_angular_speed <= 0:
            raise ValueError("max_angular_speed 必须大于零")


@dataclass(frozen=True)
class Command:
    linear_mps: float = 0.0
    angular_rps: float = 0.0


@dataclass
class Detection:
    found: bool
    cx_px: Optional[float]
    error_px: Optional[float]
    line_pixels: int
    roi_y0: int
    roi_y1: int
    mask: Any
    debug_bgr: Any


def proportional_command(error_px: Optional[float], config: ControlConfig) -> Command:
    # 找不到线，直接返回零速；不会自动搜索或倒车。
    if error_px is None:
        return Command()
    _finite_number("error_px", error_px)
    # 常见 ROS 底盘中 angular.z>0 向左；图像目标在右时 e>0，需负角速度。
    # 该符号仍须先架空/低速现场确认，相机镜像会改变判断。
    angular = -config.kp * error_px
    angular = max(-config.max_angular_speed, min(config.max_angular_speed, angular))
    return Command(config.linear_speed, angular)


def frame_age_problem(now_ros_s: float, stamp_s: float, receipt_age_s: float,
                      timeout_s: float, future_tolerance_s: float) -> Optional[str]:
    """无 ROS 依赖的新鲜度检查；返回 None 表示可接受，否则是停机原因。"""
    for name, value in (("now_ros_s", now_ros_s), ("stamp_s", stamp_s),
                        ("receipt_age_s", receipt_age_s), ("timeout_s", timeout_s),
                        ("future_tolerance_s", future_tolerance_s)):
        _finite_number(name, value)
    if timeout_s <= 0 or future_tolerance_s < 0 or receipt_age_s < 0:
        raise ValueError("超时必须>0，未来容差和接收年龄必须>=0")
    if receipt_age_s > timeout_s:
        return "no_fresh_frame"
    if stamp_s <= 0:
        return "zero_or_invalid_stamp"
    if now_ros_s <= 0:
        return "ros_clock_not_ready"
    if stamp_s - now_ros_s > future_tolerance_s:
        return "future_stamp_check_clock"
    if now_ros_s - stamp_s > timeout_s:
        return "stale_capture_stamp"
    return None


def detect_line(bgr: Any, config: VisionConfig) -> Detection:
    import cv2
    import numpy as np

    # cv_bridge 必须先把输入统一成 bgr8；这里再次拒绝其他布局和深度。
    if not isinstance(bgr, np.ndarray) or bgr.dtype != np.uint8:
        raise ValueError("输入必须是 uint8 NumPy BGR 图像")
    if bgr.ndim != 3 or bgr.shape[2] != 3 or bgr.shape[0] < 1 or bgr.shape[1] < 1:
        raise ValueError("输入必须是非空 H×W×3 BGR 图像")
    height, width = bgr.shape[:2]
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    # inRange 输出二值图：满足所有通道上下界的像素为 255，其余为 0。
    full_mask = cv2.inRange(hsv, np.asarray(config.hsv_lower, dtype=np.uint8),
                            np.asarray(config.hsv_upper, dtype=np.uint8))
    y0 = min(height - 1, int(height * config.roi_start_ratio))
    y1 = min(height, y0 + config.roi_height_px)
    # 保留全尺寸 mask，方便 rqt_image_view 对照；ROI 以外直接置零。
    mask = np.zeros((height, width), dtype=np.uint8)
    mask[y0:y1, :] = full_mask[y0:y1, :]
    # binaryImage=True 后 m00 是非零像素数量，m10/m00 是 x 质心。
    moments = cv2.moments(mask, binaryImage=True)
    pixels = int(moments["m00"])
    found = pixels >= config.min_line_pixels
    cx = float(moments["m10"] / moments["m00"]) if found else None
    error = cx - width / 2.0 if found else None
    debug = bgr.copy()
    # 蓝色框标出实际参与计算的区域，白线标出图像横向中心。
    cv2.rectangle(debug, (0, y0), (width - 1, y1 - 1), (255, 0, 0), 1)
    cv2.line(debug, (width // 2, y0), (width // 2, y1 - 1), (255, 255, 255), 1)
    if found:
        cy = float(moments["m01"] / moments["m00"])
        cv2.circle(debug, (int(round(cx)), int(round(cy))), 5, (0, 0, 255), -1)
        label = "e={:.1f}px, pixels={}".format(error, pixels)
    else:
        label = "NO LINE, pixels={}".format(pixels)
    cv2.putText(debug, label, (8, min(height - 1, 24)), cv2.FONT_HERSHEY_SIMPLEX,
                0.55, (0, 0, 255), 1, cv2.LINE_AA)
    return Detection(found, cx, error, pixels, y0, y1, mask, debug)
