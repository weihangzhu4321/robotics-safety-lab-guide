#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Lab 8 course-compatible RSA helpers. Python 2.7 / 3; no ROS required.

Protocol: RSAES-PKCS1-v1_5 of ASCII 'linear.x angular.z', RSA 2048.
This educational protocol provides neither sender authentication nor replay defence.
"""
from __future__ import print_function
import argparse
import binascii
import hashlib
import json
import math
import os
import socket
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'vendor'))
import Crypto
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_v1_5
from Crypto.Random import get_random_bytes

MAX_LINEAR = 0.08
MAX_ANGULAR = 0.35
WATCHDOG = 0.6
MONOTONIC = getattr(time, 'monotonic', time.time)


def emit(event, **values):
    values.update(event=event, utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
    print(json.dumps(values, sort_keys=True))
    sys.stdout.flush()


def fingerprint(key):
    if not hasattr(key, 'publickey'):
        key = RSA.import_key(key)
    return hashlib.sha256(key.publickey().export_key(format='DER')).hexdigest()


def load_key(path, private=False):
    with open(os.path.expanduser(path), 'rb') as handle:
        key = RSA.import_key(handle.read())
    if key.size_in_bits() != 2048:
        raise ValueError('only RSA 2048 is accepted for this lab preparation')
    if private and not key.has_private():
        raise ValueError('private key required')
    if not private and key.has_private():
        raise ValueError('use public.pem; do not copy private keys to controller')
    return key


def validate_pair(x, z):
    if isinstance(x, bool) or isinstance(z, bool):
        raise ValueError('booleans are not velocities')
    x, z = float(x), float(z)
    if math.isnan(x) or math.isinf(x) or math.isnan(z) or math.isinf(z):
        raise ValueError('non-finite velocity')
    if abs(x) > MAX_LINEAR or abs(z) > MAX_ANGULAR:
        raise ValueError('velocity exceeds low-speed limits')
    return x, z


def encode_command(x, z):
    x, z = validate_pair(x, z)
    return ('%.6f %.6f' % (x, z)).encode('ascii')


def parse_command(payload):
    if not isinstance(payload, bytes):
        payload = bytes(bytearray(payload))
    if len(payload) > 64:
        raise ValueError('command too long')
    parts = payload.decode('ascii').split()
    if len(parts) != 2:
        raise ValueError('expected two velocity fields')
    return validate_pair(parts[0], parts[1])


def wire_bytes(data):
    # Int16MultiArray is the course wire carrier; only 0..255 are valid bytes.
    if isinstance(data, bytes):
        return data
    values = list(data)
    if len(values) != 256:
        raise ValueError('RSA2048 ciphertext must contain 256 bytes')
    for item in values:
        if isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 255:
            raise ValueError('cipher array contains a non-byte')
    return bytearray(values)


class Codec(object):
    def __init__(self, key):
        if key.size_in_bits() != 2048:
            raise ValueError('RSA2048 required')
        self.key = key
        self.cipher = PKCS1_v1_5.new(key)

    def encrypt(self, x, z):
        return self.cipher.encrypt(encode_command(x, z))

    def decrypt(self, data):
        raw = wire_bytes(data)
        if len(raw) != 256:
            raise ValueError('RSA2048 ciphertext must contain 256 bytes')
        sentinel = get_random_bytes(32)
        plain = self.cipher.decrypt(raw, sentinel)
        if plain == sentinel:
            raise ValueError('invalid command')
        return parse_command(plain)


class MotionGuard(object):
    """Pure state machine; publisher invokes output at a bounded 20Hz cadence."""
    def __init__(self, codec, clock=MONOTONIC, timeout=WATCHDOG):
        self.codec, self.clock, self.timeout = codec, clock, timeout
        self.lock = threading.Lock()
        self.command, self.last_valid, self.reason = (0.0, 0.0), None, 'startup'
        self.accepted, self.rejected = 0, 0

    def accept(self, raw):
        with self.lock:
            try:
                command = self.codec.decrypt(raw)
            except Exception:
                self.command, self.last_valid, self.reason = (0.0, 0.0), None, 'invalid'
                self.rejected += 1
                return False
            self.command, self.last_valid, self.reason = command, self.clock(), 'valid'
            self.accepted += 1
            return True

    def stop(self, reason='exit'):
        with self.lock:
            self.command, self.last_valid, self.reason = (0.0, 0.0), None, reason

    def output(self):
        with self.lock:
            if self.last_valid is not None:
                age = self.clock() - self.last_valid
                if age < 0 or age >= self.timeout:
                    self.command, self.last_valid, self.reason = (0.0, 0.0), None, 'watchdog'
            return self.command


def ensure_directory(path, mode=0o700):
    if not os.path.isdir(path):
        os.makedirs(path, mode)


def save_new_key_pair(directory):
    directory = os.path.realpath(os.path.expanduser(directory))
    ensure_directory(directory)
    if os.name != 'nt': os.chmod(directory, 0o700)
    private_path, public_path = [os.path.join(directory, n) for n in ('private.pem', 'public.pem')]
    if os.path.exists(private_path) or os.path.exists(public_path):
        # Fail closed if only half of a pair exists. Never silently replace a key.
        private = load_key(private_path, private=True)
        public = load_key(public_path)
        if fingerprint(private) != fingerprint(public):
            raise ValueError('existing public/private pair mismatch; preserve and investigate')
        if os.name != 'nt': os.chmod(private_path, 0o600)
        return private, False
    private = RSA.generate(2048)
    for path, data, mode in ((private_path, private.export_key(), 0o600),
                              (public_path, private.publickey().export_key(), 0o644)):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(data)
    return private, True


def self_test():
    key = RSA.generate(2048)  # In-memory test key, never saved or distributed.
    enc, dec = Codec(key.publickey()), Codec(key)
    checks = []
    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
    check('RSA2048', key.size_in_bits() == 2048)
    check('DER fingerprint stable', fingerprint(key) == fingerprint(key.publickey()))
    for index, pair in enumerate(((0, 0), (0.08, 0.35), (-0.08, -0.35), (0.04, -0.2))):
        raw = enc.encrypt(*pair)
        check('roundtrip-%d' % index, dec.decrypt(raw) == pair)
        check('cipher256-%d' % index, len(raw) == 256)
    check('probabilistic padding', enc.encrypt(0, 0) != enc.encrypt(0, 0))
    for index, text in enumerate((b'nan 0', b'0 inf', b'-inf 0', b'0.081 0',
                                  b'0 -0.351', b'0', b'0 0 0', b'\xff 0', b'1e999 0', b'0'*65)):
        try:
            dec.decrypt(PKCS1_v1_5.new(key.publickey()).encrypt(text))
        except (ValueError, UnicodeError):
            checks.append('reject-plaintext-%d' % index)
        else:
            raise AssertionError('invalid plaintext accepted')
    for index, raw in enumerate((b'', b'0'*255, b'0'*257, b'\0'*256,
                                 [-1]*256, [256]*256, [0.5]*256, [True]*256)):
        try:
            dec.decrypt(raw)
        except (ValueError, TypeError):
            checks.append('reject-cipher-%d' % index)
        else:
            raise AssertionError('invalid ciphertext accepted')
    clock_value = [100.0]
    guard = MotionGuard(dec, clock=lambda: clock_value[0])
    check('startup-zero', guard.output() == (0.0, 0.0))
    check('valid-command', guard.accept(enc.encrypt(0.04, 0.1)))
    check('command-output', guard.output() == (0.04, 0.1))
    clock_value[0] += 0.59
    check('pre-watchdog', guard.output() == (0.04, 0.1))
    clock_value[0] += 0.02
    check('watchdog-zero', guard.output() == (0.0, 0.0))
    guard.accept(enc.encrypt(0.04, 0.1))
    check('invalid-stops', not guard.accept(b'bad') and guard.output() == (0.0, 0.0))
    guard.accept(enc.encrypt(0.04, 0.1))
    guard.stop()
    check('exit-zero', guard.output() == (0.0, 0.0))
    guard.accept(enc.encrypt(0.04, 0.1))
    clock_value[0] -= 5
    check('backward-clock-zero', guard.output() == (0.0, 0.0))
    emit('SELF_TEST_PASS', scope='memory_mock_no_ROS_no_robot', count=len(checks),
         checks=checks, crypto_version=Crypto.__version__, python=sys.version.split()[0])


def loopback():
    key = RSA.generate(2048)
    enc, guard = Codec(key.publickey()), MotionGuard(Codec(key))
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(('127.0.0.1', 0)); listener.listen(1); listener.settimeout(3)
    received = []
    def server():
        try:
            connection, _ = listener.accept(); connection.settimeout(3)
            try:
                raw = bytearray()
                while len(raw) < 256:
                    part = connection.recv(256-len(raw))
                    if not part:
                        raise RuntimeError('EOF before full ciphertext')
                    raw.extend(part)
                if not guard.accept(raw):
                    raise AssertionError('loopback decrypt failed')
                received.append(guard.output())
            finally:
                connection.close()
        except Exception as exc:
            received.append(str(exc))
    worker = threading.Thread(target=server); worker.daemon=True; worker.start()
    client = socket.create_connection(listener.getsockname(), timeout=3)
    try:
        raw = enc.encrypt(0.04, 0.1)
        client.sendall(raw[:43]); client.sendall(raw[43:])
    finally:
        client.close()
    worker.join(4); listener.close()
    if received != [(0.04, 0.1)] or worker.is_alive():
        raise AssertionError('loopback: %r' % received)
    time.sleep(WATCHDOG + 0.05)
    if guard.output() != (0.0, 0.0):
        raise AssertionError('loopback disconnect watchdog')
    emit('LOOPBACK_PASS', scope='real_127.0.0.1_TCP_no_ROS_no_robot', ciphertext_bytes=256,
         fragmented_writes=True, disconnect_watchdog_zero=True)


def benchmark(samples):
    if not 10 <= samples <= 1000:
        raise ValueError('samples must be 10..1000')
    key = RSA.generate(2048); enc, dec = Codec(key.publickey()), Codec(key)
    raw = enc.encrypt(0, 0); dec.decrypt(raw)
    encrypted, decrypted = [], []
    for _ in range(samples):
        start = MONOTONIC(); raw = enc.encrypt(0.04, 0.1); encrypted.append(MONOTONIC()-start)
        start = MONOTONIC(); dec.decrypt(raw); decrypted.append(MONOTONIC()-start)
    emit('BENCHMARK', scope='local_CPU_not_robot_latency', samples=samples,
         crypto_version=Crypto.__version__, key_bits=2048, ciphertext_bytes=len(raw),
         encrypt_mean_ms=1000*sum(encrypted)/samples, decrypt_mean_ms=1000*sum(decrypted)/samples,
         decrypt_p95_ms=1000*sorted(decrypted)[int(samples*0.95)-1],
         optimization='cache imported key and cipher object; preserve PKCS1_v1_5 protocol')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command')
    key = sub.add_parser('keygen'); key.add_argument('--key-dir', default='~/lab8_keys')
    key.add_argument('--robot-local', action='store_true', help='confirm this is the local robot, not a VM')
    fp = sub.add_parser('fingerprint'); fp.add_argument('--public-key', required=True)
    sub.add_parser('self-test'); sub.add_parser('loopback')
    bench = sub.add_parser('benchmark'); bench.add_argument('--samples', type=int, default=100)
    args = parser.parse_args()
    if args.command == 'keygen':
        if not args.robot_local:
            parser.error('private key generation is robot-local only; add --robot-local on the robot')
        key, created = save_new_key_pair(args.key_dir)
        emit('ROBOT_KEY_READY', created=created, fingerprint=fingerprint(key), key_bits=2048)
    elif args.command == 'fingerprint':
        emit('PUBLIC_KEY_FINGERPRINT', fingerprint=fingerprint(load_key(args.public_key)))
    elif args.command == 'self-test': self_test()
    elif args.command == 'loopback': loopback()
    elif args.command == 'benchmark': benchmark(args.samples)
    else: parser.print_help(); return 2
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        emit('FAIL', error=str(error)); sys.exit(1)
