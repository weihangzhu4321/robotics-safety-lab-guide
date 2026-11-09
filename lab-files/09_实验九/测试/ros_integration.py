#!/usr/bin/env python3
"""Real ROS1 loopback integration in a dedicated --network none container only.

Synthetic Image/Twist/Odometry messages validate recorder transport and cleanup.
This is not camera, recognition-model, robot, or physical-stop validation.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import uuid
import xmlrpc.client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '工具'))
import lab9

PORT = 11991
TOPICS = {'image': '/lab9_synthetic_integration/image',
          'cmd': '/lab9_synthetic_integration/cmd',
          'odom': '/lab9_synthetic_integration/odom'}
RGB = bytes([10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120])
MONO = b'ABCD'


def write_new(path, value):
    with Path(path).open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write('\n')


def wait_for(predicate, timeout, description):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (OSError, ValueError, xmlrpc.client.Error):
            pass
        time.sleep(0.1)
    raise RuntimeError('timeout waiting for ' + description)


class TimeoutTransport(xmlrpc.client.Transport):
    def make_connection(self, host):
        connection = super().make_connection(host)
        connection.timeout = 0.8
        return connection


def master_ready():
    with xmlrpc.client.ServerProxy('http://127.0.0.1:%d' % PORT,
                                  transport=TimeoutTransport()) as master:
        result = master.getPid('/lab9_integration_probe')
    return result[0] == 1 and int(result[2]) > 0


def stop_owned(process, grace=4):
    if process is None:
        return None
    if process.poll() is None:
        # Every process managed here was created with a new POSIX session.
        # Refuse a group mismatch rather than signal an inherited session.
        if os.getpgid(process.pid) != process.pid:
            raise RuntimeError('child process group identity changed')
        os.killpg(process.pid, signal.SIGINT)
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
    return process.returncode


def synthetic_publisher(ready_file):
    import rospy
    from sensor_msgs.msg import Image
    from geometry_msgs.msg import Twist
    from nav_msgs.msg import Odometry
    rospy.init_node('lab9_synthetic_integration_publisher', anonymous=True)
    image_pub = rospy.Publisher(TOPICS['image'], Image, queue_size=1)
    cmd_pub = rospy.Publisher(TOPICS['cmd'], Twist, queue_size=10)
    odom_pub = rospy.Publisher(TOPICS['odom'], Odometry, queue_size=10)
    write_new(ready_file, {'scope': 'synthetic offline publisher', 'pid': os.getpid(),
                           'master': os.environ.get('ROS_MASTER_URI'), 'topics': TOPICS})
    layouts = [
        ('rgb8', 8, bytes([10, 20, 30, 40, 50, 60, 99, 99, 70, 80, 90, 100, 110, 120, 88, 88])),
        ('bgr8', 8, bytes([30, 20, 10, 60, 50, 40, 99, 99, 90, 80, 70, 120, 110, 100, 88, 88])),
        ('mono8', 4, b'ABxxCDyy'),
        ('16UC1', 4, b'\x01\x00\x02\x00\x03\x00\x04\x00')]
    seq = 0
    deadline = time.monotonic() + 50
    try:
        while not rospy.is_shutdown() and time.monotonic() < deadline:
            encoding, step, data = layouts[seq % len(layouts)]
            msg = Image()
            msg.header.seq = seq
            msg.header.stamp = rospy.Time.now()
            msg.header.frame_id = 'synthetic_lab9_no_camera'
            msg.width = msg.height = 2
            msg.encoding, msg.step, msg.data = encoding, step, data
            image_pub.publish(msg)
            command = Twist()
            command.linear.x, command.angular.z = 0.123, 0.456
            cmd_pub.publish(command)
            feedback = Odometry()
            feedback.header = msg.header
            feedback.child_frame_id = 'synthetic_no_robot'
            feedback.twist.twist.linear.x = 0.234
            feedback.twist.twist.angular.z = 0.345
            odom_pub.publish(feedback)
            seq += 1
            time.sleep(0.16)
    finally:
        image_pub.unregister(); cmd_pub.unregister(); odom_pub.unregister()
        rospy.signal_shutdown('synthetic integration publisher finished')
    return 0


def recorder_command(run, duration):
    return [sys.executable, '-B', str(ROOT / '工具' / 'record_evidence.py'),
            '--run', str(run), '--image-topic', TOPICS['image'],
            '--cmd-topic', TOPICS['cmd'], '--odom-topic', TOPICS['odom'],
            '--duration', str(duration), '--image-interval', '0.1',
            '--max-images', '100', '--max-mib', '4', '--execute']


def validate_capture(run, require_all_encodings, check):
    summaries = list(run.glob('ros_evidence_*.summary.json'))
    check('one_clean_summary_' + run.name, len(summaries) == 1)
    if len(summaries) != 1:
        return {'run': str(run), 'summary_files': [str(x) for x in summaries]}
    summary = json.loads(summaries[0].read_text(encoding='utf-8'))
    jsonl = Path(summary['jsonl'])
    check('jsonl_inside_run_' + run.name, jsonl.parent.resolve() == run.resolve())
    rows = [json.loads(line) for line in jsonl.read_text(encoding='utf-8').splitlines()]
    images = [r for r in rows if r.get('kind') == 'image']
    check('positive_image_count_' + run.name, summary['images'] > 0)
    check('summary_image_count_matches_' + run.name, summary['images'] == len(images))
    check('bounded_payload_' + run.name, summary['payload_bytes'] <= 4 * 1024 * 1024)
    check('recorder_has_no_motion_publisher_' + run.name, summary['published_motion'] is False)
    commands = [r for r in rows if r.get('kind') == 'cmd_vel_command']
    feedback = [r for r in rows if r.get('kind') == 'odom_feedback']
    check('cmd_velocity_preserved_' + run.name,
          bool(commands) and all(abs(r['linear'][0] - 0.123) < 1e-9 and abs(r['angular'][2] - 0.456) < 1e-9 for r in commands))
    check('odom_feedback_preserved_' + run.name,
          bool(feedback) and all(abs(r['linear'][0] - 0.234) < 1e-9 and abs(r['angular'][2] - 0.345) < 1e-9 for r in feedback))
    encodings = {r['encoding'] for r in images}
    if require_all_encodings:
        check('three_supported_encodings_received', encodings == {'rgb8', 'bgr8', 'mono8'})
        check('unsupported_encoding_explicitly_rejected', any(
            r.get('kind') == 'image_rejected' and '16uc1' in r.get('reason', '').lower() for r in rows))
    for index, row in enumerate(images):
        image_path = (run / row['path']).resolve()
        check('image_path_inside_run_%s_%d' % (run.name, index), run.resolve() in image_path.parents)
        content = image_path.read_bytes()
        expected = b'P5\n2 2\n255\n' + MONO if row['encoding'] == 'mono8' else b'P6\n2 2\n255\n' + RGB
        check('pixel_and_padding_exact_%s_%d' % (run.name, index), content == expected)
        check('image_sha256_%s_%d' % (run.name, index), hashlib.sha256(content).hexdigest() == row['sha256'])
    return {'run': str(run), 'summary': summary, 'encodings': sorted(encodings),
            'command_records': len(commands), 'odom_records': len(feedback), 'jsonl_rows': len(rows)}


def main():
    if sys.platform != 'linux':
        raise RuntimeError('Run only in the dedicated Linux Noetic --network none container')
    interfaces = sorted(p.name for p in Path('/sys/class/net').iterdir())
    if interfaces != ['lo']:
        raise RuntimeError('Refusing integration outside a network-none namespace: ' + repr(interfaces))
    # Never attach to or reuse a pre-existing master, even on localhost.
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', PORT))
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    output = ROOT / 'runtime' / 'ros_integration' / (stamp + '_' + uuid.uuid4().hex[:8])
    output.mkdir(parents=True)
    env = dict(os.environ)
    env.pop('ROS_HOSTNAME', None)
    env.update(ROS_MASTER_URI='http://127.0.0.1:%d' % PORT, ROS_IP='127.0.0.1',
               ROS_HOME=str(output / 'ros_home'), ROS_LOG_DIR=str(output / 'ros_logs'),
               PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1')
    report = {'scope': 'real ROS1 transport with synthetic loopback messages in network-none container',
              'field_experiment_completed': False, 'camera_validated': False, 'model_validated': False,
              'physical_stop_validated': False, 'interfaces': interfaces,
              'master_uri': env['ROS_MASTER_URI'], 'output': str(output), 'checks': []}
    processes, handles = [], []
    def check(name, passed):
        report['checks'].append({'name': name, 'passed': bool(passed)})
        if not passed:
            raise AssertionError(name)
    def start(command, name):
        log = (output / (name + '.log')).open('x', encoding='utf-8')
        handles.append(log)
        p = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                             env=env, start_new_session=True)
        processes.append((name, p))
        return p
    started = time.monotonic()
    try:
        master = start(['roscore', '-p', str(PORT)], 'master')
        wait_for(master_ready, 12, 'exclusive localhost master')
        check('exclusive_master_running', master.poll() is None)
        ready = output / 'publisher_ready.json'
        publisher = start([sys.executable, '-B', str(Path(__file__).resolve()), '--publisher', str(ready)], 'publisher')
        wait_for(ready.is_file, 10, 'synthetic publisher registration')
        check('publisher_running', publisher.poll() is None)
        mixed = lab9.create_run(output, 'mixed', scope='synthetic_real_ros_loopback')
        recorder = start(recorder_command(mixed, 3), 'mixed_recorder')
        check('mixed_recorder_exit_zero', recorder.wait(timeout=18) == 0)
        report['mixed_capture'] = validate_capture(mixed, True, check)
        interrupted = lab9.create_run(output, 'sigint', scope='synthetic_real_ros_loopback')
        recorder = start(recorder_command(interrupted, 60), 'sigint_recorder')
        wait_for(lambda: any(interrupted.glob('images/*/*.ppm')), 10, 'SIGINT scenario first recorded image')
        # Ensure both motion subscriptions have had at least one publish cycle.
        time.sleep(0.5)
        recorder.send_signal(signal.SIGINT)
        rc = recorder.wait(timeout=8)
        check('sigint_parent_reports_interrupt', rc == 124)
        report['sigint_capture'] = validate_capture(interrupted, False, check)
        check('both_runs_identified_as_synthetic', all(
            json.loads((r / 'metadata.json').read_text())['scope'] == 'synthetic_real_ros_loopback'
            for r in [mixed, interrupted]))
    except Exception as exc:
        report['error'] = {'type': type(exc).__name__, 'detail': str(exc)[:1500]}
    finally:
        cleanup = []
        for name, process in reversed(processes):
            try:
                rc = stop_owned(process)
                cleanup.append({'name': name, 'pid': process.pid, 'returncode': rc, 'stopped': process.poll() is not None})
            except Exception as exc:
                cleanup.append({'name': name, 'stopped': False, 'error': type(exc).__name__ + ': ' + str(exc)[:400]})
        for handle in handles:
            handle.close()
        report['cleanup'] = cleanup
        report['checks'].append({'name': 'all_owned_processes_stopped', 'passed': all(c.get('stopped') for c in cleanup)})
        report['elapsed_wall_seconds'] = round(time.monotonic() - started, 3)
        report['passed'] = 'error' not in report and all(c['passed'] for c in report['checks'])
        report['check_count'] = len(report['checks'])
        write_new(output / 'report.json', report)
        print(json.dumps({'passed': report['passed'], 'report': str(output / 'report.json'),
                          'checks': report['check_count'], 'scope': report['scope']}, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--publisher':
        raise SystemExit(synthetic_publisher(sys.argv[2]))
    raise SystemExit(main())
