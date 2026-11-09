#!/usr/bin/env python3
"""只为本包仿真降低官方Waffle相机渲染分辨率，不改已安装URDF或实体车。"""
import argparse
import math
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

p = argparse.ArgumentParser()
p.add_argument('--model', choices=['waffle', 'waffle_pi', 'burger'], default='waffle')
p.add_argument('--width', type=int, default=320)
p.add_argument('--height', type=int, default=240)
p.add_argument('--rate', type=float, default=10.0)
a = p.parse_args()
if not (16 <= a.width <= 1920 and 16 <= a.height <= 1080 and math.isfinite(a.rate) and 0 < a.rate <= 30):
    p.error('相机尺寸或频率超出本预备场地支持范围')
package = subprocess.check_output(['rospack', 'find', 'turtlebot3_description'], text=True).strip()
source = Path(package) / 'urdf' / ('turtlebot3_' + a.model + '.urdf.xacro')
robot = ET.fromstring(subprocess.check_output(['xacro', str(source)]))
changed = 0
for sensor in robot.findall('.//sensor'):
    images = sensor.findall('.//camera/image')
    if not images:
        continue
    for image in images:
        image.find('width').text = str(a.width)
        image.find('height').text = str(a.height)
    rate = sensor.find('update_rate')
    if rate is None:
        rate = ET.SubElement(sensor, 'update_rate')
    rate.text = str(a.rate)
    changed += len(images)
if a.model.startswith('waffle') and changed == 0:
    raise SystemExit('官方模型中未找到相机，拒绝静默继续')
print('本包仿真相机 {}x{} {}Hz，修改{}处；原始模型文件保留。'.format(a.width, a.height, a.rate, changed), file=sys.stderr)
print(ET.tostring(robot, encoding='unicode'))
