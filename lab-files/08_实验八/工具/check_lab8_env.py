#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Bounded ordinary-user offline acceptance. Never requires or starts ROS master."""
from __future__ import print_function
import argparse
import json
import os
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT=os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--loopback',action='store_true',help='also test real local TCP, not ROS')
    parser.add_argument('--benchmark',action='store_true')
    args=parser.parse_args()
    print(json.dumps({'event':'OFFLINE_ENV','scope':'no_ROS_master_no_robot',
                     'python':sys.version,'machine':platform.machine(),'platform':platform.platform()}))
    subprocess.check_call([sys.executable,os.path.join(ROOT,'工具','install_vendor.py'),'--verify-only'])
    for name in ('lab8.py','lab8_ros.py','install_vendor.py','test_lab8_transport.py'):
        with open(os.path.join(ROOT,'工具',name),'rb') as handle: compile(handle.read(),name,'exec')
    ET.parse(os.path.join(ROOT,'源码','revised_catkin','lab8_secure','package.xml'))
    subprocess.check_call([sys.executable,os.path.join(ROOT,'工具','lab8.py'),'self-test'])
    subprocess.check_call([sys.executable,os.path.join(ROOT,'工具','test_lab8_transport.py')])
    if args.loopback: subprocess.check_call([sys.executable,os.path.join(ROOT,'工具','lab8.py'),'loopback'])
    if args.benchmark: subprocess.check_call([sys.executable,os.path.join(ROOT,'工具','lab8.py'),'benchmark','--samples','100'])
    print(json.dumps({'event':'OFFLINE_ACCEPTANCE_PASS','scope':'local_crypto_mock' +
                     ('_and_real_loopback' if args.loopback else ''),
                     'robot_on_site_required':True,'ros_build_separate':True}))


if __name__=='__main__':
    try: main()
    except Exception as error:
        print(json.dumps({'event':'OFFLINE_ACCEPTANCE_FAIL','error':str(error)})); sys.exit(1)
