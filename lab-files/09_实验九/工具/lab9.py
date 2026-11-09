#!/usr/bin/env python3
"""Offline preparation and append-only field records. Never executes model code."""
import argparse
import ast
import datetime
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

VERSION = '1.0.0'
ROOT = Path(__file__).resolve().parents[1]


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def hash_file(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def append_json(path, record):
    data = (json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        # Each bounded record is appended in one system call.
        if os.write(fd, data) != len(data):
            raise OSError('short append')
        os.fsync(fd)
    finally:
        os.close(fd)


def create_run(root, label, scope='field_pending'):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,48}', label):
        raise ValueError('label must contain 1-48 ASCII letters, digits, underscore or hyphen')
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    run = root / (stamp + '_' + label + '_' + uuid.uuid4().hex[:8])
    run.mkdir()  # exclusive creation; existing evidence is never replaced
    (run / 'images').mkdir()
    (run / 'metadata.json').write_text(json.dumps({
        'schema': 1, 'created_utc': now(), 'tool_version': VERSION,
        'label': label, 'scope': scope, 'field_completed': False,
        'note': 'Fill observed results only from real field evidence. UTC timestamps use Z or +00:00.'
    }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (run / '现场记录待填.md').write_text(
        '# 实验九本轮现场记录\n\n'
        '日期和机器角色：\n\n实际IP与ROS环境：\n\n相机型号与图像话题：\n\n'
        '程序源码和模型SHA256：\n\n标牌编号、距离、角度、光照与顺序：\n\n'
        '原图、处理图和原始识别输出：\n\n/cmd_vel命令与/odom反馈及实车观察：\n\n'
        '异常原文与恢复：\n\n停车与网络恢复核对：\n\n证据路径：\n', encoding='utf-8')
    return run


def ensure_run(path):
    p = Path(path).resolve()
    if not (p / 'metadata.json').is_file():
        raise ValueError('not a lab9 run; use init-run first')
    return p


def add_event(run, stimulus, expected=None, observed=None, distance_m=None,
              angle_deg=None, lighting='', note='', kind='observation'):
    run = ensure_run(run)
    if kind not in ('observation', 'no_detection', 'recovery', 'note'):
        raise ValueError('unknown event kind')
    if not stimulus or len(stimulus) > 100:
        raise ValueError('stimulus length must be 1-100')
    for key, val in [('distance_m', distance_m), ('angle_deg', angle_deg)]:
        if val is not None and not math.isfinite(val):
            raise ValueError(key + ' must be finite')
    if distance_m is not None and distance_m <= 0:
        raise ValueError('distance_m must be positive')
    if angle_deg is not None and abs(angle_deg) > 180:
        raise ValueError('angle_deg must be within [-180,180]')
    if kind == 'observation' and (expected is None or observed is None):
        raise ValueError('observation needs expected and observed; use no_detection for no result')
    if kind == 'no_detection':
        if expected is None:
            raise ValueError('no_detection needs expected original label')
        if observed not in (None, ''):
            raise ValueError('no_detection must not contain an observed recognition value')
        observed = None
    if any(len(str(v)) > 2000 for v in [expected, observed, lighting, note]):
        raise ValueError('record text exceeds limit')
    record = dict(schema=1, utc=now(), kind=kind, stimulus=stimulus,
                  expected=expected, observed=observed, distance_m=distance_m,
                  angle_deg=angle_deg, lighting=lighting, note=note,
                  source='manual_field_entry')
    append_json(run / 'events.jsonl', record)
    return record


def summarize(run):
    run = ensure_run(run)
    groups, malformed = {}, []
    path = run / 'events.jsonl'
    if path.exists():
        for n, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            try:
                r = json.loads(line)
                if r.get('kind') not in ('observation', 'no_detection'):
                    continue
                expected, observed = r['expected'], r['observed']
                if expected is None or (r['kind'] == 'observation' and observed is None):
                    raise ValueError('missing expected/observed')
                g = groups.setdefault(r['stimulus'], {'total_records': 0, 'count': 0,
                                     'detected': 0, 'correct': 0, 'no_detection': 0, 'observed': {}})
                g['total_records'] += 1
                if r['kind'] == 'no_detection':
                    g['no_detection'] += 1
                    continue
                g['count'] += 1
                g['detected'] += 1
                g['correct'] += int(str(expected).strip() == str(observed).strip())
                key = str(observed)
                g['observed'][key] = g['observed'].get(key, 0) + 1
            except (ValueError, KeyError, TypeError):
                malformed.append(n)
    for g in groups.values():
        g['recognition_rate'] = g['correct'] / g['count'] if g['count'] else None
        g['recognition_rate_detected_only'] = g['recognition_rate']
        g['recognition_rate_including_no_detection'] = g['correct'] / g['total_records']
    return {'run': str(run), 'source': 'manual entries, not automatic model output',
            'metric': 'per-record rates; count/detected and recognition_rate exclude no_detection; total_records and recognition_rate_including_no_detection include it; frame repeats are not independent trials',
            'stimuli': groups, 'malformed_lines': malformed,
            'field_success_inferred': False}


def literal(node):
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return None


def callable_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return callable_name(node.value) + '.' + node.attr
    return ''


def audit_model(path):
    path = Path(path).resolve()
    if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('source must be an existing file <= 4MiB')
    # tokenize.open honors the Python source encoding declaration.
    import tokenize
    with tokenize.open(str(path)) as f:
        source = f.read()
    tree = ast.parse(source, filename=path.name)
    imports, calls, hints = [], [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend({'module': a.name, 'alias': a.asname, 'line': node.lineno} for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append({'module': node.module, 'names': [a.name for a in node.names], 'line': node.lineno})
        elif isinstance(node, ast.Call):
            name = callable_name(node.func)
            if name.endswith(('Publisher', 'Subscriber', 'load_model', 'load_weights', 'restore', 'load', 'imread', 'VideoCapture')):
                calls.append({'function': name, 'line': node.lineno,
                              'literal_args': [literal(a) for a in node.args[:4]],
                              'keywords': {k.arg: literal(k.value) for k in node.keywords if k.arg}})
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            s = node.value
            if len(s) < 500 and (s.endswith(('.h5', '.hdf5', '.keras', '.pt', '.pth', '.pb', '.ckpt', '.npy', '.npz', '.json')) or 'checkpoint' in s):
                candidate = (path.parent / s).resolve()
                hints.append({'literal': s, 'line': getattr(node, 'lineno', None),
                              'relative_candidate': str(candidate), 'exists': candidate.is_file(),
                              'sha256': hash_file(candidate) if candidate.is_file() else None})
    speed_lines = [{'line': i, 'text': s[:500]} for i, s in enumerate(source.splitlines(), 1)
                   if re.search(r'cmd_vel|linear\.|angular\.|Twist|speed|velocity', s, re.I)]
    return {'source': str(path), 'sha256': hash_file(path), 'syntax': 'Python3 AST parsed',
            'imports': imports, 'topic_and_loader_calls': calls, 'model_hints': hints,
            'speed_mapping_review_lines': speed_lines,
            'executed': False, 'limitation': 'Static hints only. Dynamic paths, callbacks and speed logic require manual review; no recognition or motion has been validated.'}


def probe_module(name, timeout=8):
    code = ('import importlib,json; m=importlib.import_module(' + repr(name) + '); '
            'print(json.dumps({"version":str(getattr(m,"__version__","unknown")),"file":str(getattr(m,"__file__",""))}))')
    try:
        p = subprocess.run([sys.executable, '-B', '-c', code], capture_output=True,
                           timeout=timeout, cwd=tempfile.gettempdir(), env=dict(os.environ))
        return {'available': p.returncode == 0, 'rc': p.returncode,
                'detail': p.stdout.decode('utf-8', 'replace')[-1000:],
                'error': p.stderr.decode('utf-8', 'replace')[-1000:]}
    except subprocess.TimeoutExpired:
        return {'available': False, 'error': 'import timeout'}


def environment():
    return {'scope': 'offline imports only; no ROS master, sensor or model inference contacted',
            'python': sys.version, 'executable': sys.executable, 'platform': sys.platform,
            'tools': {x: shutil.which(x) for x in ['bash', 'ssh', 'rostopic', 'rosbag', 'roslaunch', 'rqt_image_view', 'docker']},
            'modules': {x: probe_module(x) for x in ['rospy', 'sensor_msgs.msg', 'geometry_msgs.msg', 'nav_msgs.msg', 'cv2', 'numpy', 'tensorflow', 'torch']}}


def verify(root):
    root = Path(root).resolve()
    manifest = json.loads((root / '文件校验.json').read_text(encoding='utf-8'))['files']
    bad, missing = [], []
    for rel, expected in manifest.items():
        p = (root / rel).resolve()
        if p != root and root not in p.parents:
            raise ValueError('manifest path escapes root')
        if not p.is_file():
            missing.append(rel)
        elif hash_file(p) != expected:
            bad.append(rel)
    return {'files': len(manifest), 'missing': missing, 'mismatch': bad,
            'passed': not bad and not missing, 'scope': 'manifest files only; runtime records excluded'}


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    s = p.add_subparsers(dest='command', required=True)
    c = s.add_parser('init-run'); c.add_argument('--root', default=str(ROOT / 'runs')); c.add_argument('--label', required=True)
    c = s.add_parser('event'); c.add_argument('--run', required=True); c.add_argument('--stimulus', required=True)
    c.add_argument('--expected'); c.add_argument('--observed'); c.add_argument('--distance-m', type=float)
    c.add_argument('--angle-deg', type=float); c.add_argument('--lighting', default=''); c.add_argument('--note', default='')
    c.add_argument('--kind', choices=['observation', 'no_detection', 'recovery', 'note'], default='observation')
    c = s.add_parser('summarize'); c.add_argument('--run', required=True)
    c = s.add_parser('audit-model'); c.add_argument('source')
    s.add_parser('env')
    c = s.add_parser('verify'); c.add_argument('--root', default=str(ROOT))
    s.add_parser('self-check')
    return p


def main(argv=None):
    a = parser().parse_args(argv)
    try:
        if a.command == 'init-run':
            emit({'run': str(create_run(a.root, a.label)), 'scope': 'field_pending'})
        elif a.command == 'event':
            emit(add_event(a.run, a.stimulus, a.expected, a.observed, a.distance_m,
                           a.angle_deg, a.lighting, a.note, a.kind))
        elif a.command == 'summarize':
            emit(summarize(a.run))
        elif a.command == 'audit-model':
            emit(audit_model(a.source))
        elif a.command == 'env':
            emit(environment())
        elif a.command == 'verify':
            result = verify(a.root); emit(result)
            return 0 if result['passed'] else 1
        elif a.command == 'self-check':
            test = ROOT / '测试' / 'test_lab9.py'
            return subprocess.call([sys.executable, '-B', str(test)])
        return 0
    except (OSError, ValueError, SyntaxError, KeyError) as e:
        emit({'error': type(e).__name__, 'detail': str(e)[:1000], 'field_success_inferred': False})
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
