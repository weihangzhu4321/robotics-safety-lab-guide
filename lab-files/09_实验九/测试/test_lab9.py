#!/usr/bin/env python3
"""Synthetic, memory and mock tests; these never establish field recognition."""
import ast
import contextlib
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '工具'))
import lab9
import record_evidence as rec


def image(encoding='rgb8', width=2, height=1, step=6, data=b'\x01\x02\x03\x04\x05\x06'):
    return types.SimpleNamespace(encoding=encoding, width=width, height=height, step=step, data=data)


def vec(x=0., y=0., z=0.):
    return types.SimpleNamespace(x=x, y=y, z=z)


class Lab9Tests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get('LAB9_TEST_TMP', str(ROOT / '_工作记录' / 'tests_tmp')))
        scratch.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=str(scratch))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run = lab9.create_run(self.root, 'offline_test', scope='synthetic_offline')

    def test_01_new_run_metadata(self):
        self.assertFalse(json.loads((self.run / 'metadata.json').read_text())['field_completed'])

    def test_02_unique_runs(self):
        self.assertNotEqual(self.run, lab9.create_run(self.root, 'offline_test'))

    def test_03_label_traversal_rejected(self):
        with self.assertRaises(ValueError): lab9.create_run(self.root, '../x')

    def test_04_missing_run_rejected(self):
        with self.assertRaises(ValueError): lab9.ensure_run(self.root / 'not_run')

    def test_05_event_append_keeps_previous(self):
        lab9.add_event(self.run, 'base', '80', '80')
        first = (self.run / 'events.jsonl').read_bytes()
        lab9.add_event(self.run, 'dash', '80', '8')
        self.assertTrue((self.run / 'events.jsonl').read_bytes().startswith(first))
        self.assertEqual(len((self.run / 'events.jsonl').read_text().splitlines()), 2)

    def test_06_summary_no_fabricated_result(self):
        self.assertEqual(lab9.summarize(self.run)['stimuli'], {})

    def test_07_summary_rates(self):
        for value in ['80', '8', 'no_detection']:
            lab9.add_event(self.run, 'dash', '80', value)
        g = lab9.summarize(self.run)['stimuli']['dash']
        self.assertEqual(g['count'], 3)
        self.assertAlmostEqual(g['recognition_rate'], 1 / 3)

    def test_08_summary_recovery_excluded(self):
        lab9.add_event(self.run, 'restore', kind='recovery', note='synthetic')
        self.assertEqual(lab9.summarize(self.run)['stimuli'], {})

    def test_09_summary_malformed_line_reported(self):
        (self.run / 'events.jsonl').write_text('{broken\n')
        self.assertEqual(lab9.summarize(self.run)['malformed_lines'], [1])

    def test_10_nonfinite_distance_rejected(self):
        for n in [math.nan, math.inf, -math.inf]:
            with self.assertRaises(ValueError): lab9.add_event(self.run, 'base', '80', '80', n)

    def test_11_negative_distance_rejected(self):
        with self.assertRaises(ValueError): lab9.add_event(self.run, 'base', '80', '80', -1)

    def test_12_invalid_angle_rejected(self):
        with self.assertRaises(ValueError): lab9.add_event(self.run, 'base', '80', '80', angle_deg=181)

    def test_13_missing_observation_rejected(self):
        with self.assertRaises(ValueError): lab9.add_event(self.run, 'base')

    def test_14_audit_never_executes_source(self):
        source = self.root / 'get_num.py'
        marker = self.root / 'SHOULD_NOT_EXIST'
        source.write_text('import rospy\nfrom geometry_msgs.msg import Twist\n'
                          'raise RuntimeError("must not execute")\n'
                          'rospy.Publisher("/cmd_vel", Twist, queue_size=1)\n'
                          'model = load_model("model.h5")\n'
                          'twist.linear.x = 0.05\n', encoding='utf-8')
        result = lab9.audit_model(source)
        self.assertFalse(result['executed'])
        self.assertFalse(marker.exists())
        self.assertEqual(result['topic_and_loader_calls'][0]['literal_args'][0], '/cmd_vel')
        self.assertTrue(result['speed_mapping_review_lines'])

    def test_15_audit_models_presence(self):
        (self.root / 'model.h5').write_bytes(b'offline-test-not-a-model')
        source = self.root / 'get_num.py'; source.write_text('x="model.h5"')
        self.assertTrue(lab9.audit_model(source)['model_hints'][0]['exists'])

    def test_16_syntax_error_reported(self):
        source = self.root / 'broken.py'; source.write_text('def nope(\n')
        with self.assertRaises(SyntaxError): lab9.audit_model(source)

    def test_17_verify_correct(self):
        p = self.root / 'x'; p.write_bytes(b'hello')
        (self.root / '文件校验.json').write_text(json.dumps({'files': {'x': lab9.hash_file(p)}}), encoding='utf-8')
        self.assertTrue(lab9.verify(self.root)['passed'])

    def test_18_verify_mismatch_missing(self):
        (self.root / 'x').write_bytes(b'changed')
        (self.root / '文件校验.json').write_text(json.dumps({'files': {'x': 'wrong', 'missing': 'wrong'}}), encoding='utf-8')
        result = lab9.verify(self.root)
        self.assertEqual(result['mismatch'], ['x'])
        self.assertEqual(result['missing'], ['missing'])

    def test_19_manifest_traversal_rejected(self):
        (self.root / '文件校验.json').write_text(json.dumps({'files': {'../outside': 'wrong'}}), encoding='utf-8')
        with self.assertRaises(ValueError): lab9.verify(self.root)

    def test_20_rgb_exact(self):
        data, suffix = rec.image_ppm(image())
        self.assertEqual(data, b'P6\n2 1\n255\n\x01\x02\x03\x04\x05\x06')
        self.assertEqual(suffix, '.ppm')

    def test_21_bgr_conversion(self):
        data, _ = rec.image_ppm(image(encoding='bgr8'))
        self.assertTrue(data.endswith(b'\x03\x02\x01\x06\x05\x04'))

    def test_22_padded_rows(self):
        data, _ = rec.image_ppm(image(width=1, height=2, step=4, data=b'abcXdefY'))
        self.assertTrue(data.endswith(b'abcdef'))

    def test_23_mono(self):
        data, suffix = rec.image_ppm(image(encoding='mono8', step=2, data=b'AB'))
        self.assertEqual(data, b'P5\n2 1\n255\nAB')
        self.assertEqual(suffix, '.pgm')

    def test_24_unsupported_encoding(self):
        with self.assertRaises(ValueError): rec.image_ppm(image(encoding='16UC1'))

    def test_25_short_stride(self):
        with self.assertRaises(ValueError): rec.image_ppm(image(step=5))

    def test_26_bad_image_length(self):
        with self.assertRaises(ValueError): rec.image_ppm(image(data=b'bad'))

    def test_27_oversized_image(self):
        with self.assertRaises(ValueError): rec.image_ppm(image(width=10000))

    def test_28_image_limits(self):
        r = rec.Recorder(self.run, 1, 10000, 0)
        r.image(image()); r.image(image())
        result = r.close()
        self.assertEqual(result['images'], 1)
        self.assertEqual(result['dropped_or_throttled'], 1)
        self.assertEqual(len(list(self.run.rglob('*.ppm'))), 1)

    def test_29_byte_limit(self):
        r = rec.Recorder(self.run, 10, 100, 0)
        r.image(image())
        result = r.close()
        self.assertEqual(result['images'], 0)
        self.assertLessEqual(result['payload_bytes'], 100)

    def test_30_rejected_image_logged(self):
        r = rec.Recorder(self.run, 2, 10000, 0)
        r.image(image(encoding='32FC1'))
        result = r.close()
        self.assertEqual(result['rejected'], 1)
        self.assertIn('unsupported encoding', r.path.read_text())

    def test_31_motion_command_feedback_distinct(self):
        r = rec.Recorder(self.run, 2, 10000, 0)
        twist = types.SimpleNamespace(linear=vec(0.01), angular=vec())
        r.motion(twist, 'cmd_vel_command')
        r.motion(types.SimpleNamespace(twist=types.SimpleNamespace(twist=twist)), 'odom_feedback')
        r.close()
        rows = [json.loads(s) for s in r.path.read_text().splitlines()]
        self.assertEqual({x['kind'] for x in rows}, {'cmd_vel_command', 'odom_feedback'})

    def test_32_nonfinite_motion_rejected(self):
        r = rec.Recorder(self.run, 2, 10000, 0)
        r.motion(types.SimpleNamespace(linear=vec(math.nan), angular=vec()), 'cmd_vel_command')
        self.assertEqual(r.close()['rejected'], 1)

    def test_33_preview_never_imports_ros(self):
        self.assertNotIn('rospy', sys.modules)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(rec.main(['--run', str(self.run), '--image-topic', '/actual/image']), 0)
        self.assertFalse(json.loads(out.getvalue())['ros_imported'])
        self.assertNotIn('rospy', sys.modules)

    def test_34_unbounded_duration_rejected(self):
        for n in ['0', '121', 'nan', 'inf']:
            with self.assertRaises(ValueError): rec.main(['--run', str(self.run), '--image-topic', '/image', '--duration', n])

    def test_35_source_has_no_publisher(self):
        tree = ast.parse((ROOT / '工具' / 'record_evidence.py').read_text(encoding='utf-8'))
        calls = [lab9.callable_name(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)]
        self.assertFalse(any(n.endswith('.Publisher') for n in calls))

    def test_36_postclose_callbacks_are_ignored(self):
        r = rec.Recorder(self.run, 2, 10000, 0); r.close(); r.image(image())
        self.assertEqual(r.images, 0)

    def test_37_probe_failure_is_bounded(self):
        p = lab9.probe_module('this_lab9_module_does_not_exist', timeout=3)
        self.assertFalse(p['available'])

    def test_38_topic_requires_absolute(self):
        with self.assertRaises(ValueError): rec.main(['--run', str(self.run), '--image-topic', 'image'])

    def test_39_mock_ros_subscription_lifecycle(self):
        calls, unregistered = [], []
        ros = types.ModuleType('rospy')
        ros.init_node = lambda *a, **kw: calls.append(('init', kw))
        ros.is_shutdown = lambda: True
        ros.signal_shutdown = lambda reason: calls.append(('shutdown', reason))
        def subscriber(topic, dtype, callback, **kwargs):
            calls.append(('sub', topic, kwargs))
            if topic == '/image': callback(image())
            return types.SimpleNamespace(unregister=lambda: unregistered.append(topic))
        ros.Subscriber = subscriber
        modules = {'rospy': ros}
        for name, key in [('sensor_msgs.msg', 'Image'), ('geometry_msgs.msg', 'Twist'), ('nav_msgs.msg', 'Odometry')]:
            module = types.ModuleType(name); setattr(module, key, object); modules[name] = module
        with mock.patch.dict(sys.modules, modules), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(rec.main(['--run', str(self.run), '--image-topic', '/image', '--execute', '--worker']), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result['images'], 1)
        self.assertFalse(result['published_motion'])
        self.assertEqual(set(unregistered), {'/image', '/cmd_vel', '/odom'})
        self.assertEqual(calls[1][2]['buff_size'], 16 * 1024 * 1024)

    def test_40_summary_preserves_leading_zeros(self):
        lab9.add_event(self.run, 'base', '80', '080')
        self.assertEqual(lab9.summarize(self.run)['stimuli']['base']['correct'], 0)

    def test_41_image_budget_argument_rejected(self):
        with self.assertRaises(ValueError): rec.main(['--run', str(self.run), '--image-topic', '/image', '--max-mib', '257'])

    def test_42_image_throttle(self):
        r = rec.Recorder(self.run, 10, 10000, 120)
        r.image(image()); r.image(image())
        self.assertEqual(r.close()['images'], 1)

    def test_43_no_detection_retained_in_total_denominator(self):
        lab9.add_event(self.run, 'dash', '80', '80')
        lab9.add_event(self.run, 'dash', '80', kind='no_detection')
        g = lab9.summarize(self.run)['stimuli']['dash']
        self.assertEqual(g['total_records'], 2)
        self.assertEqual(g['detected'], 1)
        self.assertEqual(g['count'], 1)
        self.assertEqual(g['no_detection'], 1)
        self.assertEqual(g['recognition_rate'], 1)
        self.assertEqual(g['recognition_rate_detected_only'], 1)
        self.assertEqual(g['recognition_rate_including_no_detection'], 0.5)

    def test_44_no_detection_only_and_validation(self):
        with self.assertRaises(ValueError): lab9.add_event(self.run, 'dash', kind='no_detection')
        with self.assertRaises(ValueError): lab9.add_event(self.run, 'dash', '80', '8', kind='no_detection')
        lab9.add_event(self.run, 'dash', '80', kind='no_detection')
        g = lab9.summarize(self.run)['stimuli']['dash']
        self.assertEqual(g['total_records'], 1)
        self.assertEqual(g['count'], 0)
        self.assertIsNone(g['recognition_rate_detected_only'])
        self.assertEqual(g['recognition_rate_including_no_detection'], 0)


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Lab9Tests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(json.dumps({'scope': 'synthetic memory/mock offline only', 'tests': result.testsRun,
                      'failures': len(result.failures), 'errors': len(result.errors),
                      'passed': result.wasSuccessful(), 'field_experiment_completed': False}, ensure_ascii=False))
    raise SystemExit(0 if result.wasSuccessful() else 1)
