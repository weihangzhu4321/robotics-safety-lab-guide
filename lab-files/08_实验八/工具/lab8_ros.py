#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ROS1 transport for lab8.py; default robot endpoint is a preview topic."""
from __future__ import print_function
import argparse
import os
import select
import sys
import threading
import time
import lab8


def receive_key(args):
    from lab8_secure.srv import key, keyResponse
    from Crypto.PublicKey import RSA
    directory = os.path.realpath(os.path.expanduser(args.key_dir))
    lab8.ensure_directory(directory)
    target = os.path.join(directory, 'public.pem')
    def receive(request):
        try:
            raw = bytearray(request.key)
            if len(raw) > 4096: raise ValueError('public key too long')
            public = RSA.import_key(raw)
            if public.has_private() or public.size_in_bits() != 2048:
                raise ValueError('expected RSA2048 public key')
            if os.path.exists(target):
                existing = lab8.load_key(target)
                if lab8.fingerprint(existing) != lab8.fingerprint(public):
                    raise ValueError('key change refused; preserve old key and verify robot before manual rename')
            else:
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                with os.fdopen(descriptor, 'wb') as handle: handle.write(public.export_key())
            lab8.emit('PUBLIC_KEY_RECEIVED', fingerprint=lab8.fingerprint(public),
                      checkpoint='compare this DER-SHA256 with robot console before teleop')
            return keyResponse(True)
        except Exception:
            lab8.emit('KEY_REJECTED'); return keyResponse(False)
    rospy.init_node('lab8_receive_key', anonymous=True)
    service = rospy.Service(args.service, key, receive)
    lab8.emit('KEY_SERVICE_READY', service=args.service)
    rospy.spin()


def send_key(args):
    from lab8_secure.srv import key, keyRequest
    public = lab8.load_key(os.path.join(os.path.expanduser(args.key_dir), 'public.pem'))
    rospy.init_node('lab8_send_key', anonymous=True)
    rospy.wait_for_service(args.service, timeout=20)
    request = keyRequest(); request.key = list(bytearray(public.export_key()))
    response = rospy.ServiceProxy(args.service, key)(request)
    if not response.success: raise ValueError('controller refused key')
    lab8.emit('PUBLIC_KEY_SENT', fingerprint=lab8.fingerprint(public))


def robot(args):
    from geometry_msgs.msg import Twist
    from std_msgs.msg import Int16MultiArray
    private = lab8.load_key(os.path.join(os.path.expanduser(args.key_dir), 'private.pem'), private=True)
    if args.execute:
        if args.confirm_motion != 'I_HAVE_STOP_CONTROL' or args.output_topic != '/cmd_vel':
            raise ValueError('motion requires --execute --confirm-motion I_HAVE_STOP_CONTROL --output-topic /cmd_vel')
    elif args.output_topic != '/lab8/preview_cmd_vel':
        raise ValueError('preview output topic is fixed /lab8/preview_cmd_vel')
    rospy.init_node('lab8_robot', anonymous=True)
    if args.execute:
        import rosgraph
        publishers, _, _ = rosgraph.Master(rospy.get_name()).getSystemState()
        competing = [nodes for topic, nodes in publishers if topic == '/cmd_vel' and nodes]
        if competing:
            raise ValueError('/cmd_vel has existing publishers; stop them and verify before retry')
    publisher = rospy.Publisher(args.output_topic, Twist, queue_size=1, latch=False, tcp_nodelay=True)
    guard = lab8.MotionGuard(lab8.Codec(private))
    stopped = threading.Event()
    publish_lock = threading.Lock()
    def zero():
        stopped.set()
        with publish_lock:
            guard.stop()
            for _ in range(3):
                try: publisher.publish(Twist())
                except Exception: pass
                time.sleep(0.02)
    rospy.on_shutdown(zero)
    def receive(message):
        # Errors are reported as one generic category; no padding details are logged.
        if not guard.accept(message.data): lab8.emit('COMMAND_REJECTED_STOP')
    subscriber = rospy.Subscriber(args.cipher_topic, Int16MultiArray, receive,
                                  queue_size=1, buff_size=65536, tcp_nodelay=True)
    lab8.emit('ROBOT_RECEIVER_READY', mode='execute' if args.execute else 'preview',
              output_topic=args.output_topic, cipher_topic=args.cipher_topic,
              fingerprint=lab8.fingerprint(private), max_linear=lab8.MAX_LINEAR,
              max_angular=lab8.MAX_ANGULAR, watchdog_seconds=lab8.WATCHDOG)
    previous = None
    try:
        while not rospy.is_shutdown() and not stopped.is_set():
            with publish_lock:
                if stopped.is_set(): break
                x, z = guard.output()
                message = Twist(); message.linear.x=x; message.angular.z=z
                publisher.publish(message)
            if (x,z,guard.reason) != previous:
                lab8.emit('OUTPUT', x=x, z=z, reason=guard.reason,
                          accepted=guard.accepted, rejected=guard.rejected)
                previous=(x,z,guard.reason)
            time.sleep(0.05)  # wall clock; watchdog remains active when ROS /clock pauses
    finally:
        zero()


def teleop(args):
    from std_msgs.msg import Int16MultiArray
    import tty
    import termios
    if not sys.stdin.isatty(): raise ValueError('teleop needs an interactive terminal')
    public = lab8.load_key(args.public_key)
    expected = args.fingerprint.lower().replace(':','')
    if expected != lab8.fingerprint(public): raise ValueError('fingerprint mismatch')
    encoder = lab8.Codec(public)
    rospy.init_node('lab8_teleop', anonymous=True)
    publisher=rospy.Publisher(args.cipher_topic, Int16MultiArray, queue_size=1, latch=False, tcp_nodelay=True)
    deadline=lab8.MONOTONIC()+5
    while publisher.get_num_connections() == 0 and not rospy.is_shutdown():
        if lab8.MONOTONIC() >= deadline: raise ValueError('no cipher subscriber; start robot preview first')
        time.sleep(0.05)
    original=termios.tcgetattr(sys.stdin)
    x,z,last_key=0.0,0.0,None
    def publish(x,z):
        message=Int16MultiArray(); message.data=list(bytearray(encoder.encrypt(x,z))); publisher.publish(message)
    print('w=0.04m/s, s=-0.04m/s, a=+0.20rad/s, d=-0.20rad/s, space=stop, q/Ctrl-C=exit')
    print('Each key is a short pulse; idle keyboard stops after 0.25s. Check receiver output before motion.')
    lab8.emit('TELEOP_READY', fingerprint=expected, cipher_topic=args.cipher_topic)
    try:
        tty.setcbreak(sys.stdin.fileno())
        while not rospy.is_shutdown():
            ready,_,_=select.select([sys.stdin],[],[],0.05)
            if ready:
                value=sys.stdin.read(1)
                if value in ('q','\x03',''): break
                x,z={'w':(0.04,0),'s':(-0.04,0),'a':(0,0.2),'d':(0,-0.2)}.get(value,(0,0))
                last_key=lab8.MONOTONIC()
            if last_key is None or lab8.MONOTONIC()-last_key >= 0.25: x,z=0.0,0.0
            publish(x,z)
    finally:
        termios.tcsetattr(sys.stdin,termios.TCSADRAIN,original)
        for _ in range(3):
            try: publish(0,0)
            except Exception: pass
            time.sleep(0.05)
        lab8.emit('TELEOP_STOP_SENT')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service',default='/lab8/exchange')
    parser.add_argument('--cipher-topic',default='/lab8/Cypered_cmd_vel')
    commands=parser.add_subparsers(dest='command')
    recv=commands.add_parser('receive-key'); recv.add_argument('--key-dir',default='~/lab8_remote_key')
    send=commands.add_parser('send-key'); send.add_argument('--key-dir',default='~/lab8_keys')
    rob=commands.add_parser('robot'); rob.add_argument('--key-dir',default='~/lab8_keys')
    rob.add_argument('--execute',action='store_true'); rob.add_argument('--confirm-motion',default='')
    rob.add_argument('--output-topic',default='/lab8/preview_cmd_vel')
    tele=commands.add_parser('teleop'); tele.add_argument('--public-key',required=True)
    tele.add_argument('--fingerprint',required=True)
    args=parser.parse_args()
    if args.command is None: parser.print_help(); return 2
    global rospy
    import rospy
    {'receive-key':receive_key,'send-key':send_key,'robot':robot,'teleop':teleop}[args.command](args)
    return 0


if __name__=='__main__':
    try: sys.exit(main())
    except KeyboardInterrupt: sys.exit(0)
    except Exception as error:
        lab8.emit('FAIL',error=str(error)); sys.exit(1)
